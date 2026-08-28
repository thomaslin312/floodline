from __future__ import annotations

import math

import numpy as np
import pytest

from floodline.validate.metrics import count_within, extent_metrics, mark_metrics


def _mask(rows: list[str]) -> np.ndarray:
    return np.array([[c == "#" for c in row] for row in rows], dtype=bool)


def test_a_perfect_match_scores_one_everywhere_that_matters() -> None:
    observed = _mask(["##..", "##..", "....", "...."])
    got = extent_metrics(observed, observed)
    assert got.critical_success_index == 1.0
    assert got.hit_rate == 1.0
    assert got.false_alarm_ratio == 0.0
    assert got.bias == 1.0


def test_flooding_everything_scores_a_perfect_hit_rate_and_a_terrible_csi() -> None:
    """The reason hit rate is never reported on its own."""
    observed = _mask(["##..", "....", "....", "...."])
    everything = np.ones_like(observed)
    got = extent_metrics(everything, observed)
    assert got.hit_rate == 1.0
    assert got.critical_success_index == pytest.approx(2 / 16)
    assert got.bias == pytest.approx(8.0)


def test_flooding_nothing_scores_zero_rather_than_dividing_by_zero() -> None:
    observed = _mask(["##..", "....", "....", "...."])
    got = extent_metrics(np.zeros_like(observed), observed)
    assert got.hit_rate == 0.0
    assert got.critical_success_index == 0.0
    assert math.isnan(got.false_alarm_ratio)


def test_bias_says_which_way_the_model_leans() -> None:
    observed = _mask(["##..", "##..", "....", "...."])
    under = _mask(["##..", "....", "....", "...."])
    over = _mask(["###.", "###.", "....", "...."])
    assert extent_metrics(under, observed).bias < 1.0
    assert extent_metrics(over, observed).bias > 1.0


def test_cells_outside_the_valid_mask_are_excluded_not_counted_dry() -> None:
    """A SAR swath edge would otherwise contribute millions of correct negatives."""
    observed = _mask(["#...", "....", "....", "...."])
    modelled = _mask(["#...", "....", "....", "...."])
    valid = _mask(["##..", "##..", "....", "...."])
    got = extent_metrics(modelled, observed, valid=valid)
    assert got.correct_negatives == 3
    assert got.hits == 1


def test_a_model_wet_only_outside_the_valid_area_scores_nothing() -> None:
    observed = _mask(["#...", "....", "....", "...."])
    modelled = _mask(["....", "....", "....", "...#"])
    valid = _mask(["##..", "##..", "....", "...."])
    got = extent_metrics(modelled, observed, valid=valid)
    assert got.false_alarms == 0
    assert got.misses == 1


def test_mismatched_shapes_are_refused() -> None:
    with pytest.raises(ValueError, match="does not match"):
        extent_metrics(np.zeros((2, 2), bool), np.zeros((3, 3), bool))


def test_summary_carries_all_four_numbers() -> None:
    observed = _mask(["##..", "....", "....", "...."])
    text = extent_metrics(observed, observed).summary()
    for word in ("CSI", "hit rate", "false alarm ratio", "bias"):
        assert word in text


# ---- marks -------------------------------------------------------------------


def test_a_perfect_model_has_zero_error() -> None:
    surveyed = np.array([10.0, 12.0, 14.0])
    got = mark_metrics(surveyed, surveyed, surveyed - 2.0)
    assert got.rmse_m == 0.0
    assert got.mean_bias_m == 0.0
    assert got.recall == 1.0


def test_a_dry_mark_is_scored_from_the_ground_not_dropped() -> None:
    """Dropping dry marks flatters the model: those are the ones it gets most wrong."""
    surveyed = np.array([10.0, 12.0])
    modelled = np.array([10.0, np.nan])
    ground = np.array([8.0, 8.0])
    got = mark_metrics(surveyed, modelled, ground)
    assert got.n == 2
    assert got.n_wet == 1
    # Second residual is ground - surveyed = 8 - 12 = -4.
    np.testing.assert_allclose(got.residuals_m, [0.0, -4.0])
    assert got.rmse_m == pytest.approx(math.sqrt((0 + 16) / 2))


def test_dropping_dry_marks_would_have_looked_better() -> None:
    surveyed = np.array([10.0, 12.0])
    ground = np.array([8.0, 8.0])
    scored_all = mark_metrics(surveyed, np.array([10.0, np.nan]), ground)
    scored_wet_only = mark_metrics(surveyed[:1], np.array([10.0]), ground[:1])
    assert scored_wet_only.rmse_m < scored_all.rmse_m


def test_bias_sign_says_which_way_the_surface_sits() -> None:
    surveyed = np.array([10.0, 10.0])
    high = mark_metrics(surveyed, np.array([11.0, 11.0]), np.array([8.0, 8.0]))
    low = mark_metrics(surveyed, np.array([9.0, 9.0]), np.array([8.0, 8.0]))
    assert high.mean_bias_m > 0
    assert low.mean_bias_m < 0


def test_median_absolute_error_resists_one_bad_mark() -> None:
    surveyed = np.array([10.0, 10.0, 10.0, 10.0])
    modelled = np.array([10.1, 10.1, 10.1, 30.0])
    got = mark_metrics(surveyed, modelled, np.full(4, 8.0))
    assert got.median_absolute_m < 1.0
    assert got.rmse_m > 5.0


def test_no_marks_is_an_error_not_a_nan() -> None:
    with pytest.raises(ValueError, match="no marks"):
        mark_metrics(np.array([]), np.array([]), np.array([]))


def test_mark_shape_mismatch_is_refused() -> None:
    with pytest.raises(ValueError, match="must match"):
        mark_metrics(np.array([1.0]), np.array([1.0, 2.0]), np.array([1.0]))


def test_count_within_checks_the_readme_claim() -> None:
    assert count_within(42_556, 19_027, 71_236) is True
    assert count_within(5, 10, 20) is False
    assert count_within(10, 10, 20) is True
