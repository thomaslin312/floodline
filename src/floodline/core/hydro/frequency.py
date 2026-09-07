"""Where a modelled discharge sits in a gauge's own history.

A depth map answers "how deep"; it does not answer "how unusual", and the second
question is the one a reader asks first. This module turns the annual peak series
that `io.sources.peak_discharge` already fetches into that context: the rank of the
event in the record, its empirical return period, a log-Pearson III estimate, and the
handful of historical floods that bracket it.

**Method.** Log-Pearson III fitted by method of moments to log10 of the annual peaks,
with the station skew. This is Bulletin 17B in the form still used for quick work.
Full Bulletin 17C is not implemented: no Expected Moments Algorithm for historical
and censored peaks, no regional skew weighting, no Multiple Grubbs-Beck low-outlier
test. Those matter most for short records and for gauges with historical flood
information, and their absence is why the empirical rank is reported alongside the
fitted return period rather than being replaced by it. Where the two disagree, the
rank is the fact and the fit is the model.

**Three caveats that travel with every number here.**

* A return period longer than about twice the record length is extrapolation. A
  90-year record does not contain the information to distinguish a 200-year flood
  from a 500-year one, and `extrapolated` says when a figure is past that line.
* The series is assumed stationary. Over Houston it is not: the catchment urbanised
  through the record, so early peaks came off different ground than late ones. The
  fit has no way to know that.
* An annual maximum series records the largest peak in each water year. A year with
  two great floods contributes one number.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date
from statistics import NormalDist

__all__ = ["AnnualPeak", "FloodFrequency", "HistoricalContext", "flood_frequency"]

_NORMAL = NormalDist()
# Past this multiple of the record length a fitted quantile is extrapolation.
_EXTRAPOLATION_FACTOR = 2.0
# The bisection ceiling. A log-Pearson III fitted to a century of peaks cannot tell a
# 1000-year flood from a 10,000-year one, so the search stops here and the answer is
# reported as saturated rather than as a number.
_MAX_RETURN_PERIOD = 1000.0


@dataclass(frozen=True, slots=True)
class AnnualPeak:
    """One water year's largest measured discharge."""

    date: str
    discharge_cms: float
    water_year: int


@dataclass(frozen=True, slots=True)
class HistoricalContext:
    """Where one discharge sits against a gauge's record."""

    discharge_cms: float
    rank: int
    """1 means larger than every peak on record."""

    n_years: int
    exceeds_record: bool
    empirical_return_period_years: float
    """Weibull plotting position, (n + 1) / rank. Defined only inside the record."""

    fitted_return_period_years: float | None
    """Log-Pearson III. None when the record is too short to fit."""

    extrapolated: bool
    """True when the fitted return period is beyond twice the record length."""

    fit_saturated: bool
    """True when the fit ran off the top of its range. Harvey does this on Whiteoak
    Bayou: the record peak comes back at the 1000-year ceiling, which says the fitted
    distribution cannot place it, not that the flood is that rare. Urbanisation through
    the record is the likely reason - the stationarity the fit assumes is exactly what a
    century of development in Houston broke."""

    larger_floods: tuple[AnnualPeak, ...]
    """Every peak on record that beat this discharge, largest first."""

    nearest_below: AnnualPeak | None
    """The largest peak this discharge exceeded - the flood it just outdid."""

    def summary(self) -> str:
        """One sentence a reader can put in a caption."""
        if self.exceeds_record:
            beaten = self.nearest_below
            against = (
                f", above the {beaten.water_year} peak of {beaten.discharge_cms:,.0f} m3/s"
                if beaten is not None and beaten.discharge_cms < self.discharge_cms
                else ""
            )
            return (
                f"{self.discharge_cms:,.0f} m3/s is the largest in {self.n_years} years "
                f"of record{against}."
            )
        ordinal = {1: "largest", 2: "2nd largest", 3: "3rd largest"}.get(
            self.rank, f"{self.rank}th largest"
        )
        fitted = ""
        if self.fit_saturated:
            fitted = ", beyond what a log-Pearson III fitted to this record can place"
        elif self.fitted_return_period_years is not None:
            qualifier = " (extrapolated)" if self.extrapolated else ""
            fitted = f", about a 1-in-{self.fitted_return_period_years:,.0f} year flow{qualifier}"
        return (
            f"{self.discharge_cms:,.0f} m3/s is the {ordinal} in {self.n_years} years "
            f"of record{fitted}."
        )


@dataclass(frozen=True, slots=True)
class FloodFrequency:
    """A fitted annual-maximum series for one gauge."""

    site: str
    peaks: tuple[AnnualPeak, ...]
    """Every peak, largest first."""

    log_mean: float
    log_sd: float
    log_skew: float

    @property
    def n_years(self) -> int:
        """Years in the record."""
        return len(self.peaks)

    @property
    def period(self) -> tuple[int, int]:
        """First and last water year in the record."""
        years = [p.water_year for p in self.peaks]
        return min(years), max(years)

    @property
    def can_fit(self) -> bool:
        """False when the record is too short or too flat to fit a distribution.

        Ten years is the shortest the method of moments gives anything worth
        printing, and a zero spread in log space means every peak was identical.
        """
        return self.n_years >= 10 and self.log_sd > 0.0

    def _k_factor(self, exceedance: float) -> float:
        """Return the Pearson III frequency factor by Wilson-Hilferty.

        Reduces to the standard normal deviate when the skew is zero, which is the
        log-normal case.
        """
        z = _NORMAL.inv_cdf(1.0 - exceedance)
        g = self.log_skew
        if abs(g) < 1e-9:
            return z
        k = g / 6.0
        return float((2.0 / g) * (((z - k) * k + 1.0) ** 3 - 1.0))

    def discharge_for(self, return_period_years: float) -> float:
        """Return the log-Pearson III discharge for a return period, in cumecs."""
        if return_period_years <= 1.0:
            raise ValueError("return period must exceed 1 year")
        if not self.can_fit:
            raise ValueError(f"record of {self.n_years} years is too short to fit")
        k = self._k_factor(1.0 / return_period_years)
        return float(10.0 ** (self.log_mean + k * self.log_sd))

    def fitted_return_period(self, discharge_cms: float) -> float | None:
        """Return the log-Pearson III return period for a discharge, or None.

        Solved by bisection on `discharge_for`, which is monotonic in the return
        period, rather than by inverting Wilson-Hilferty analytically.
        """
        if not self.can_fit or discharge_cms <= 0:
            return None
        low, high = 1.0001, _MAX_RETURN_PERIOD
        if discharge_cms <= self.discharge_for(low):
            return low
        if discharge_cms >= self.discharge_for(high):
            return high
        for _ in range(80):
            mid = math.sqrt(low * high)
            if self.discharge_for(mid) < discharge_cms:
                low = mid
            else:
                high = mid
        return math.sqrt(low * high)

    def context_for(self, discharge_cms: float) -> HistoricalContext:
        """Place a discharge in the record."""
        larger = tuple(p for p in self.peaks if p.discharge_cms > discharge_cms)
        rank = len(larger) + 1
        below = next((p for p in self.peaks if p.discharge_cms <= discharge_cms), None)
        fitted = self.fitted_return_period(discharge_cms)
        saturated = fitted is not None and fitted >= _MAX_RETURN_PERIOD
        return HistoricalContext(
            discharge_cms=discharge_cms,
            rank=rank,
            n_years=self.n_years,
            exceeds_record=rank == 1,
            empirical_return_period_years=(self.n_years + 1) / rank,
            fitted_return_period_years=None if saturated else fitted,
            extrapolated=fitted is not None
            and not saturated
            and fitted > _EXTRAPOLATION_FACTOR * self.n_years,
            fit_saturated=saturated,
            larger_floods=larger,
            nearest_below=below,
        )


def _water_year(iso: str) -> int:
    """Return the US water year (October to September) for an ISO date."""
    try:
        parsed = date.fromisoformat(iso[:10])
    except ValueError:
        return 0
    return parsed.year + 1 if parsed.month >= 10 else parsed.year


def flood_frequency(series: list[dict[str, object]], *, site: str = "") -> FloodFrequency:
    """Fit an annual-maximum series, as returned by `sources.peak_discharge`.

    Parameters
    ----------
    series
        Records with `date` and `cms` keys. Non-positive discharges are dropped
        rather than fitted: log-Pearson III is undefined at zero, and a zero annual
        peak means the gauge did not run that year, not that no water passed.

    Returns
    -------
    FloodFrequency
    """
    peaks = [
        AnnualPeak(
            date=str(row.get("date", "")),
            discharge_cms=float(row["cms"]),  # type: ignore[arg-type]
            water_year=_water_year(str(row.get("date", ""))),
        )
        for row in series
        if float(row["cms"]) > 0  # type: ignore[arg-type]
    ]
    peaks.sort(key=lambda p: p.discharge_cms, reverse=True)

    logs = [math.log10(p.discharge_cms) for p in peaks]
    n = len(logs)
    if n == 0:
        return FloodFrequency(site=site, peaks=(), log_mean=0.0, log_sd=0.0, log_skew=0.0)

    mean = sum(logs) / n
    if n < 2:
        return FloodFrequency(
            site=site, peaks=tuple(peaks), log_mean=mean, log_sd=0.0, log_skew=0.0
        )

    variance = sum((x - mean) ** 2 for x in logs) / (n - 1)
    sd = math.sqrt(variance)
    # Unbiased sample skew, as Bulletin 17B specifies for the station value.
    skew = 0.0
    if n > 2 and sd > 0:
        third = sum((x - mean) ** 3 for x in logs)
        skew = (n * third) / ((n - 1) * (n - 2) * sd**3)

    return FloodFrequency(site=site, peaks=tuple(peaks), log_mean=mean, log_sd=sd, log_skew=skew)
