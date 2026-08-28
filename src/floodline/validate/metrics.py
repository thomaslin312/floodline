"""Scoring a modelled flood against something observed.

Two kinds of reference, and they need different arithmetic.

**An observed extent** — a mask of where water was — is scored with the contingency
table every flood-mapping paper reports. Hit rate alone is worthless: a model that
floods the entire watershed scores 1.0. It has to travel with false alarm ratio and
with CSI, which penalises both halves, and with bias, which says which way the model
leans. `extent_metrics` returns all four and refuses to return one.

**Surveyed marks** — a scatter of measured water levels — are scored on elevation
residuals, because a mark is a height, not an area. The subtlety that matters is what
to do with a mark the model leaves dry: dropping it flatters the model, since the marks
it misses are exactly the ones it gets most wrong. `mark_metrics` scores every mark,
counting a dry one as the full distance from ground to surveyed level.

**CSI is not currently reported for floodline**, because it needs an observed extent
polygon and the Sentinel-1 route was dropped for want of credentials. The code is here
so the day a mask exists it is a function call, and so the definitions are written down
rather than improvised later.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np
import numpy.typing as npt

__all__ = ["ExtentMetrics", "MarkMetrics", "count_within", "extent_metrics", "mark_metrics"]


@dataclass(frozen=True, slots=True)
class ExtentMetrics:
    """A modelled mask scored against an observed one."""

    hits: int
    """Wet in both."""

    misses: int
    """Observed wet, modelled dry."""

    false_alarms: int
    """Modelled wet, observed dry."""

    correct_negatives: int

    @property
    def hit_rate(self) -> float:
        """Return the share of observed flooding the model found.

        Alone this means nothing: a model that floods everything scores 1.0.
        """
        observed = self.hits + self.misses
        return self.hits / observed if observed else math.nan

    @property
    def false_alarm_ratio(self) -> float:
        """Share of modelled flooding that was not observed."""
        modelled = self.hits + self.false_alarms
        return self.false_alarms / modelled if modelled else math.nan

    @property
    def critical_success_index(self) -> float:
        """Return hits over everything either side called wet.

        Penalises both errors, which is why it is the headline number rather than
        hit rate.
        """
        denominator = self.hits + self.misses + self.false_alarms
        return self.hits / denominator if denominator else math.nan

    @property
    def bias(self) -> float:
        """Modelled wet area over observed wet area. Above 1 the model over-floods."""
        observed = self.hits + self.misses
        return (self.hits + self.false_alarms) / observed if observed else math.nan

    def summary(self) -> str:
        """One line carrying all four, because reporting one of them is misleading."""
        return (
            f"CSI {self.critical_success_index:.3f}, hit rate {self.hit_rate:.3f}, "
            f"false alarm ratio {self.false_alarm_ratio:.3f}, bias {self.bias:.2f}"
        )


def extent_metrics(
    modelled: npt.NDArray[np.bool_],
    observed: npt.NDArray[np.bool_],
    *,
    valid: npt.NDArray[np.bool_] | None = None,
) -> ExtentMetrics:
    """Score a modelled wet mask against an observed one.

    Parameters
    ----------
    modelled, observed
        Boolean masks on the same grid.
    valid
        Where the comparison is meaningful. Cells outside the watershed, or where the
        observation has no coverage, must be excluded rather than counted as dry: a
        SAR scene that stops at the swath edge would otherwise contribute millions of
        correct negatives and flatter every ratio that has them in the denominator.

    Returns
    -------
    ExtentMetrics
    """
    if modelled.shape != observed.shape:
        raise ValueError(f"modelled {modelled.shape} does not match observed {observed.shape}")
    mask = np.ones(modelled.shape, dtype=bool) if valid is None else np.asarray(valid, dtype=bool)
    if mask.shape != modelled.shape:
        raise ValueError(f"valid {mask.shape} does not match modelled {modelled.shape}")

    wet_model = np.asarray(modelled, dtype=bool) & mask
    wet_obs = np.asarray(observed, dtype=bool) & mask
    return ExtentMetrics(
        hits=int(np.sum(wet_model & wet_obs)),
        misses=int(np.sum(~wet_model & wet_obs)),
        false_alarms=int(np.sum(wet_model & ~wet_obs)),
        correct_negatives=int(np.sum(~wet_model & ~wet_obs & mask)),
    )


@dataclass(frozen=True, slots=True)
class MarkMetrics:
    """Modelled water surface scored against surveyed high-water marks."""

    n: int
    n_wet: int
    """Marks the model put under water. The rest are scored, not dropped."""

    rmse_m: float
    mean_bias_m: float
    """Positive means the model sits above the surveyed level."""

    median_absolute_m: float
    residuals_m: npt.NDArray[np.float64]

    @property
    def recall(self) -> float:
        """Share of marks the model floods at all."""
        return self.n_wet / self.n if self.n else math.nan

    def summary(self) -> str:
        """One line for a caption."""
        return (
            f"RMSE {self.rmse_m:.2f} m over {self.n} marks ({self.n_wet} wet), "
            f"bias {self.mean_bias_m:+.2f} m, median |error| {self.median_absolute_m:.2f} m"
        )


def mark_metrics(
    surveyed_m: npt.ArrayLike,
    modelled_m: npt.ArrayLike,
    ground_m: npt.ArrayLike,
) -> MarkMetrics:
    """Score modelled water surface elevations against surveyed marks.

    Parameters
    ----------
    surveyed_m
        Surveyed water surface elevation at each mark.
    modelled_m
        Modelled water surface elevation, or NaN where the model left the mark dry.
    ground_m
        Ground elevation at the mark, used as the modelled surface where the model is
        dry: a dry mark is not missing data, it is the model saying the water reached
        the ground and no higher, and that is a residual like any other.

    Returns
    -------
    MarkMetrics
    """
    surveyed = np.asarray(surveyed_m, dtype=np.float64)
    modelled = np.asarray(modelled_m, dtype=np.float64)
    ground = np.asarray(ground_m, dtype=np.float64)
    if not (surveyed.shape == modelled.shape == ground.shape):
        raise ValueError(
            f"surveyed {surveyed.shape}, modelled {modelled.shape} and ground "
            f"{ground.shape} must match"
        )
    if surveyed.size == 0:
        raise ValueError("no marks to score")

    wet = np.isfinite(modelled)
    # Dropping dry marks would flatter the model: those are the ones it gets most wrong.
    surface = np.where(wet, modelled, ground)
    residuals = surface - surveyed
    return MarkMetrics(
        n=int(surveyed.size),
        n_wet=int(np.sum(wet)),
        rmse_m=float(np.sqrt(np.mean(residuals**2))),
        mean_bias_m=float(np.mean(residuals)),
        median_absolute_m=float(np.median(np.abs(residuals))),
        residuals_m=residuals,
    )


def count_within(modelled: float, low: float, high: float) -> bool:
    """Report whether an observed count falls inside a modelled interval.

    The claim in the README is that the observed building count lands inside the 90%
    interval. This is that test, as one function, so the claim is checked rather than
    eyeballed.
    """
    return low <= modelled <= high
