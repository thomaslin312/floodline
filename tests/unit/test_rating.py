from __future__ import annotations

import numpy as np
import pytest

from floodline.core.config import Config
from floodline.core.hydro.rating import (
    build_rating_curves,
    discharge_by_area_ratio,
    reach_catchments,
)
from floodline.core.hydro.stage import stage_field_from_discharge


def simple_reach(
    *, rows: int = 9, cols: int = 21, slope: float = 0.001, cellsize: float = 10.0
) -> tuple[np.ndarray, np.ndarray, list[list[int]], np.ndarray]:
    """A straight channel down the middle row with a symmetric V-shaped floodplain.

    HAND rises 1 m per cell away from the channel, so the wetted geometry at a given
    stage is exactly calculable by hand, which is what makes the Manning arithmetic
    checkable rather than merely plausible.
    """
    middle = rows // 2
    hand = np.abs(np.arange(rows) - middle).astype(np.float64)[:, None] * np.ones(cols)
    filled = hand + (np.arange(cols)[::-1] * slope * cellsize)[None, :]
    channel = [middle * cols + c for c in range(cols)]
    reach_of = np.zeros((rows, cols), dtype=np.int64)
    return hand, filled, [channel], reach_of


def test_rating_curve_matches_manning_by_hand() -> None:
    """Check one point of the curve against the arithmetic, not against itself."""
    cfg = Config.model_validate(
        {"hydraulics": {"manning_n": 0.03, "rating_stage_step_m": 1.0, "rating_max_stage_m": 5.0}}
    )
    hand, filled, links, reach_of = simple_reach(rows=9, cols=21, slope=0.001)
    curves = build_rating_curves(hand, filled, links, reach_of, config=cfg, cellsize=(10.0, 10.0))
    curve = curves[0]

    # At stage 2 m: cells with HAND 0 (21 of them, depth 2), HAND 1 (42, depth 1).
    # Volume = (21*2 + 42*1) * 100 m2 = 8400 m3 over a 200 m reach -> A = 42 m2.
    # Wet cells = 63 -> bed area 6300 m2 -> P = 31.5 m -> R = 1.333 m.
    index = int(np.searchsorted(curve.stage_m, 2.0))
    assert curve.stage_m[index] == pytest.approx(2.0)
    assert curve.area_m2[index] == pytest.approx(42.0, rel=1e-6)
    assert curve.hydraulic_radius_m[index] == pytest.approx(42.0 / 31.5, rel=1e-6)

    expected = (1 / 0.03) * 42.0 * (42.0 / 31.5) ** (2 / 3) * np.sqrt(0.001)
    assert curve.discharge_cms[index] == pytest.approx(expected, rel=1e-6)


def test_discharge_increases_with_stage() -> None:
    hand, filled, links, reach_of = simple_reach()
    curve = build_rating_curves(hand, filled, links, reach_of, cellsize=(10.0, 10.0))[0]
    assert np.all(np.diff(curve.discharge_cms) >= 0)
    assert curve.discharge_cms[-1] > curve.discharge_cms[0]


def test_stage_for_discharge_inverts_the_curve() -> None:
    hand, filled, links, reach_of = simple_reach()
    curve = build_rating_curves(hand, filled, links, reach_of, cellsize=(10.0, 10.0))[0]
    for index in (3, 10, 25):
        recovered = curve.stage_for_discharge(float(curve.discharge_cms[index]))
        assert recovered == pytest.approx(curve.stage_m[index], abs=0.3)


def test_discharge_beyond_the_curve_is_capped_and_reported() -> None:
    """Manning on a cross-section the DEM never saw is not a prediction."""
    hand, filled, links, reach_of = simple_reach()
    curve = build_rating_curves(hand, filled, links, reach_of, cellsize=(10.0, 10.0))[0]
    huge = curve.max_discharge_cms * 10
    assert curve.exceeds_curve(huge)
    assert curve.stage_for_discharge(huge) == pytest.approx(curve.stage_m[-1])
    assert not curve.exceeds_curve(curve.max_discharge_cms * 0.5)


def test_negative_discharge_rejected() -> None:
    hand, filled, links, reach_of = simple_reach()
    curve = build_rating_curves(hand, filled, links, reach_of, cellsize=(10.0, 10.0))[0]
    with pytest.raises(ValueError, match="non-negative"):
        curve.stage_for_discharge(-1.0)


def test_a_steeper_reach_carries_more_at_the_same_stage() -> None:
    cfg = Config()
    flat = build_rating_curves(*simple_reach(slope=0.0005), config=cfg, cellsize=(10.0, 10.0))[0]
    steep = build_rating_curves(*simple_reach(slope=0.005), config=cfg, cellsize=(10.0, 10.0))[0]
    assert steep.discharge_cms[5] > flat.discharge_cms[5]
    # Manning: Q scales with sqrt(S), so a 10x slope is a sqrt(10)x discharge
    assert steep.discharge_cms[5] / flat.discharge_cms[5] == pytest.approx(np.sqrt(10), rel=1e-6)


def test_a_rougher_channel_carries_less() -> None:
    smooth = Config.model_validate({"hydraulics": {"manning_n": 0.015}})
    rough = Config.model_validate({"hydraulics": {"manning_n": 0.06}})
    args = simple_reach()
    a = build_rating_curves(*args, config=smooth, cellsize=(10.0, 10.0))[0]
    b = build_rating_curves(*args, config=rough, cellsize=(10.0, 10.0))[0]
    assert a.discharge_cms[5] / b.discharge_cms[5] == pytest.approx(4.0, rel=1e-6)


def test_flat_reaches_get_the_slope_floor() -> None:
    """Manning's Q goes to zero with slope, and a coastal plain has plenty of flat reaches."""
    cfg = Config.model_validate({"hydraulics": {"min_reach_slope": 1e-3}})
    hand, filled, links, reach_of = simple_reach(slope=0.0)
    curve = build_rating_curves(hand, filled, links, reach_of, config=cfg, cellsize=(10.0, 10.0))[0]
    assert curve.geometry.raw_slope == pytest.approx(0.0)
    assert curve.geometry.slope == pytest.approx(1e-3)
    assert curve.discharge_cms[-1] > 0.0


def test_short_reaches_get_no_curve() -> None:
    cfg = Config.model_validate({"hydraulics": {"min_reach_length_m": 100.0}})
    hand, filled, links, reach_of = simple_reach(cols=5)  # 4 steps x 10 m = 40 m
    assert (
        build_rating_curves(hand, filled, links, reach_of, config=cfg, cellsize=(10.0, 10.0)) == {}
    )


def test_shape_mismatch_rejected() -> None:
    hand, filled, links, reach_of = simple_reach()
    with pytest.raises(ValueError, match="same shape"):
        build_rating_curves(hand, filled[:3], links, reach_of, cellsize=(10.0, 10.0))


# --- reach catchments ----------------------------------------------------------------


def test_reach_catchments_composes_drainage_with_links() -> None:
    link_ids = np.array([[-1, 7, -1], [-1, 7, -1], [-1, -1, -1]], dtype=np.int64)
    drainage = np.array([[1, 1, 1], [4, 4, 4], [-1, 4, -1]], dtype=np.int64)
    out = reach_catchments(drainage, link_ids)
    assert out[0, 0] == 7
    assert out[1, 2] == 7
    assert out[2, 0] == -1, "a cell with no drainage belongs to no reach"


def test_reach_catchments_shape_checked() -> None:
    with pytest.raises(ValueError, match="does not match"):
        reach_catchments(np.zeros((2, 2), np.int64), np.zeros((3, 3), np.int64))


# --- area-ratio discharge --------------------------------------------------------------


def test_area_ratio_scales_discharge_with_catchment_size() -> None:
    accumulation = np.array([[10.0, 20.0], [40.0, 80.0]])
    links = [[0], [1], [3]]
    out = discharge_by_area_ratio(100.0, 40.0, links, accumulation)
    assert out[0] == pytest.approx(100.0 * 10 / 40)
    assert out[2] == pytest.approx(100.0 * 80 / 40)


def test_area_ratio_exponent_is_applied() -> None:
    cfg = Config.model_validate({"hydraulics": {"discharge_area_exponent": 0.7}})
    accumulation = np.array([[10.0, 100.0]])
    out = discharge_by_area_ratio(50.0, 100.0, [[0]], accumulation, config=cfg)
    assert out[0] == pytest.approx(50.0 * (10 / 100) ** 0.7)
    assert out[0] > 50.0 * (10 / 100), "a smaller exponent yields more per unit area"


def test_area_ratio_rejects_a_gauge_with_no_catchment() -> None:
    with pytest.raises(ValueError, match="contributing area must be positive"):
        discharge_by_area_ratio(10.0, 0.0, [[0]], np.array([[1.0]]))


def test_area_ratio_rejects_negative_discharge() -> None:
    with pytest.raises(ValueError, match="non-negative"):
        discharge_by_area_ratio(-1.0, 10.0, [[0]], np.array([[1.0]]))


# --- the stage field ---------------------------------------------------------------------


def test_stage_field_assigns_each_cell_its_reach_stage() -> None:
    hand, filled, links, reach_of = simple_reach()
    curves = build_rating_curves(hand, filled, links, reach_of, cellsize=(10.0, 10.0))
    result = stage_field_from_discharge(reach_of, curves, {0: curves[0].discharge_cms[6]})
    assert np.all(result.stage_m == result.stage_m[0, 0])
    assert result.stage_m[0, 0] == pytest.approx(curves[0].stage_m[6], abs=0.3)


def test_cells_of_a_reach_without_a_curve_stay_dry() -> None:
    """Zero floods nothing, which is the honest default when there is nothing to say."""
    *_, reach_of = simple_reach()
    result = stage_field_from_discharge(reach_of, {}, {0: 100.0})
    assert np.all(result.stage_m == 0.0)
    assert result.reaches_without_a_curve == 1


def test_off_curve_reaches_are_counted() -> None:
    hand, filled, links, reach_of = simple_reach()
    curves = build_rating_curves(hand, filled, links, reach_of, cellsize=(10.0, 10.0))
    result = stage_field_from_discharge(reach_of, curves, {0: curves[0].max_discharge_cms * 5})
    assert result.reaches_off_the_curve == 1
    assert result.stage_m.max() == pytest.approx(curves[0].stage_m[-1])


def test_zero_discharge_gives_zero_stage() -> None:
    """A reach carrying no water must not report a quarter-metre of it.

    Without a (Q=0, stage=0) point on the curve, inverting below the first
    tabulated discharge clamps to the first *stage* instead. That surfaced as every
    cell of a watershed showing 0.25 m of water at zero flow.
    """
    hand, filled, links, reach_of = simple_reach()
    curve = build_rating_curves(hand, filled, links, reach_of, cellsize=(10.0, 10.0))[0]
    assert curve.stage_m[0] == 0.0
    assert curve.discharge_cms[0] == 0.0
    assert curve.stage_for_discharge(0.0) == 0.0
