from __future__ import annotations

import math

import pytest

from floodline.core.hydro.frequency import flood_frequency


def _series(values: list[float], start: int = 1930) -> list[dict[str, object]]:
    return [{"date": f"{start + i:04d}-05-15", "cms": v} for i, v in enumerate(values)]


# A synthetic 60-year record, log-normal-ish and strictly increasing in rank.
RECORD = _series([100.0 * math.exp(0.35 * math.sin(i) + 0.02 * i) for i in range(60)])


def test_peaks_come_back_largest_first() -> None:
    fit = flood_frequency(RECORD)
    values = [p.discharge_cms for p in fit.peaks]
    assert values == sorted(values, reverse=True)
    assert fit.n_years == 60


def test_water_year_rolls_over_in_october() -> None:
    fit = flood_frequency([{"date": "1994-10-18", "cms": 500.0}])
    assert fit.peaks[0].water_year == 1995
    fit = flood_frequency([{"date": "1994-09-30", "cms": 500.0}])
    assert fit.peaks[0].water_year == 1994


def test_non_positive_peaks_are_dropped_not_logged() -> None:
    fit = flood_frequency(_series([100.0, 0.0, 200.0, -5.0]))
    assert fit.n_years == 2


def test_the_record_flood_ranks_first_and_says_so() -> None:
    fit = flood_frequency(RECORD)
    biggest = fit.peaks[0].discharge_cms
    context = fit.context_for(biggest * 1.5)
    assert context.rank == 1
    assert context.exceeds_record is True
    assert "largest in 60 years" in context.summary()


def test_a_middling_flood_gets_its_rank() -> None:
    fit = flood_frequency(RECORD)
    median = fit.peaks[30].discharge_cms
    context = fit.context_for(median)
    assert context.rank == 31
    assert context.exceeds_record is False
    # Weibull plotting position.
    assert context.empirical_return_period_years == pytest.approx(61 / 31)


def test_larger_floods_are_the_ones_that_beat_it() -> None:
    fit = flood_frequency(RECORD)
    context = fit.context_for(fit.peaks[4].discharge_cms)
    assert len(context.larger_floods) == 4
    assert all(p.discharge_cms > context.discharge_cms for p in context.larger_floods)


def test_return_period_rises_with_discharge() -> None:
    fit = flood_frequency(RECORD)
    small = fit.fitted_return_period(fit.peaks[-1].discharge_cms)
    large = fit.fitted_return_period(fit.peaks[0].discharge_cms)
    assert small is not None and large is not None
    assert large > small


def test_discharge_for_and_return_period_round_trip() -> None:
    fit = flood_frequency(RECORD)
    for years in (2.0, 10.0, 50.0):
        q = fit.discharge_for(years)
        assert fit.fitted_return_period(q) == pytest.approx(years, rel=0.02)


def test_zero_skew_reduces_to_the_log_normal_deviate() -> None:
    # A symmetric log record has no skew, so the factor is the normal deviate.
    logs = [1.0, 2.0, 3.0]
    rows = [{"date": f"{1990 + i}-01-01", "cms": 10.0**x} for i, x in enumerate(logs)]
    fit = flood_frequency(rows)
    assert fit.log_skew == pytest.approx(0.0, abs=1e-9)


def test_extrapolation_beyond_twice_the_record_is_flagged() -> None:
    fit = flood_frequency(RECORD)
    far = fit.discharge_for(500.0)
    assert fit.context_for(far).extrapolated is True
    near = fit.discharge_for(20.0)
    assert fit.context_for(near).extrapolated is False


def test_a_short_record_is_not_fitted() -> None:
    fit = flood_frequency(_series([100.0, 200.0, 150.0]))
    assert fit.can_fit is False
    assert fit.fitted_return_period(180.0) is None
    assert fit.context_for(180.0).fitted_return_period_years is None
    with pytest.raises(ValueError, match="too short"):
        fit.discharge_for(100.0)


def test_an_empty_series_does_not_explode() -> None:
    fit = flood_frequency([])
    assert fit.n_years == 0
    assert fit.can_fit is False


def test_a_return_period_of_one_year_is_refused() -> None:
    with pytest.raises(ValueError, match="exceed 1 year"):
        flood_frequency(RECORD).discharge_for(1.0)


def test_summary_names_the_flood_it_outdid() -> None:
    fit = flood_frequency(RECORD)
    context = fit.context_for(fit.peaks[0].discharge_cms * 2)
    assert context.nearest_below is not None
    assert str(context.nearest_below.water_year) in context.summary()


def test_a_fit_that_runs_off_its_range_says_so_instead_of_giving_a_number() -> None:
    """Harvey does this on Whiteoak Bayou: the record peak hits the ceiling.

    A log-Pearson III fitted to 90 years cannot distinguish a 1000-year flood from a
    10,000-year one, so a saturated search reports no figure rather than a spurious
    one. The rank and the empirical position remain, because those are facts.
    """
    fit = flood_frequency(RECORD)
    enormous = fit.peaks[0].discharge_cms * 50
    context = fit.context_for(enormous)
    assert context.fit_saturated is True
    assert context.fitted_return_period_years is None
    assert context.extrapolated is False
    assert context.rank == 1
    assert context.empirical_return_period_years == pytest.approx(61.0)


def test_an_ordinary_flood_is_not_marked_saturated() -> None:
    fit = flood_frequency(RECORD)
    context = fit.context_for(fit.peaks[20].discharge_cms)
    assert context.fit_saturated is False
    assert context.fitted_return_period_years is not None
