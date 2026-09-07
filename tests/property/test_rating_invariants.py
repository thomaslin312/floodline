"""Hypothesis property tests for the synthetic rating curves.

A rating curve is only useful if it can be inverted, so the invariants that matter
are the ones that make inversion well defined: discharge rises with stage, stage
rises with discharge, and Manning's scaling in slope and roughness is exact.
"""

from __future__ import annotations

import numpy as np
import numpy.typing as npt
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from floodline.core.config import Config
from floodline.core.hydro.rating import build_rating_curves, discharge_by_area_ratio

SETTINGS = settings(
    max_examples=100,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)


def reach(
    rows: int, cols: int, slope: float, cellsize: float = 10.0
) -> tuple[
    npt.NDArray[np.float64], npt.NDArray[np.float64], list[list[int]], npt.NDArray[np.int64]
]:
    """A straight channel with a V-shaped floodplain, so geometry is exact."""
    middle = rows // 2
    hand = np.abs(np.arange(rows) - middle).astype(np.float64)[:, None] * np.ones(cols)
    filled = hand + (np.arange(cols)[::-1] * slope * cellsize)[None, :]
    return (
        hand,
        filled,
        [[middle * cols + c for c in range(cols)]],
        np.zeros((rows, cols), dtype=np.int64),
    )


geometry = st.tuples(
    st.integers(5, 15).map(lambda r: r | 1),  # odd, so there is a middle row
    st.integers(6, 24),
    st.floats(1e-4, 1e-2),
)


@SETTINGS
@given(shape=geometry)
def test_discharge_is_non_decreasing_in_stage(shape: tuple[int, int, float]) -> None:
    """Without this the curve cannot be inverted by interpolation."""
    curve = build_rating_curves(*reach(*shape), cellsize=(10.0, 10.0))[0]
    assert np.all(np.diff(curve.discharge_cms) >= -1e-12)


@SETTINGS
@given(shape=geometry, fraction=st.floats(0.01, 0.99))
def test_stage_is_non_decreasing_in_discharge(
    shape: tuple[int, int, float], fraction: float
) -> None:
    curve = build_rating_curves(*reach(*shape), cellsize=(10.0, 10.0))[0]
    top = curve.max_discharge_cms
    if top <= 0:
        return
    low = curve.stage_for_discharge(top * fraction * 0.5)
    high = curve.stage_for_discharge(top * fraction)
    assert high >= low - 1e-9


@SETTINGS
@given(shape=geometry)
def test_stage_stays_inside_the_tabulated_range(shape: tuple[int, int, float]) -> None:
    curve = build_rating_curves(*reach(*shape), cellsize=(10.0, 10.0))[0]
    for q in (0.0, curve.max_discharge_cms, curve.max_discharge_cms * 100):
        stage = curve.stage_for_discharge(q)
        assert curve.stage_m[0] <= stage <= curve.stage_m[-1]


@SETTINGS
@given(shape=geometry, factor=st.floats(1.5, 50.0))
def test_discharge_scales_as_the_square_root_of_slope(
    shape: tuple[int, int, float], factor: float
) -> None:
    """Manning's exact dependence, checked rather than assumed."""
    rows, cols, slope = shape
    base = build_rating_curves(*reach(rows, cols, slope), cellsize=(10.0, 10.0))[0]
    steep = build_rating_curves(*reach(rows, cols, slope * factor), cellsize=(10.0, 10.0))[0]
    usable = base.discharge_cms > 0
    ratio = steep.discharge_cms[usable] / base.discharge_cms[usable]
    np.testing.assert_allclose(ratio, np.sqrt(factor), rtol=1e-9)


@SETTINGS
@given(shape=geometry, n=st.floats(0.01, 0.1))
def test_discharge_scales_inversely_with_roughness(shape: tuple[int, int, float], n: float) -> None:
    args = reach(*shape)
    reference = build_rating_curves(
        *args,
        config=Config.model_validate({"hydraulics": {"manning_n": 0.03}}),
        cellsize=(10.0, 10.0),
    )[0]
    other = build_rating_curves(
        *args,
        config=Config.model_validate({"hydraulics": {"manning_n": n}}),
        cellsize=(10.0, 10.0),
    )[0]
    usable = reference.discharge_cms > 0
    ratio = other.discharge_cms[usable] / reference.discharge_cms[usable]
    np.testing.assert_allclose(ratio, 0.03 / n, rtol=1e-9)


@SETTINGS
@given(
    q=st.floats(0.0, 5000.0),
    gauge_area=st.floats(1.0, 1e6),
    exponent=st.floats(0.5, 1.0),
)
def test_area_ratio_is_monotone_in_catchment_size(
    q: float, gauge_area: float, exponent: float
) -> None:
    config = Config.model_validate({"hydraulics": {"discharge_area_exponent": exponent}})
    accumulation = np.array([[1.0, 10.0, 100.0, 1000.0]])
    links = [[0], [1], [2], [3]]
    out = discharge_by_area_ratio(q, gauge_area, links, accumulation, config=config)
    values = [out[i] for i in range(4)]
    assert values == sorted(values)


@SETTINGS
@given(q=st.floats(0.0, 5000.0), area=st.floats(1.0, 1e6))
def test_the_gauge_reach_gets_the_gauge_discharge(q: float, area: float) -> None:
    """A reach whose catchment is the gauge's own must reproduce the observation."""
    out = discharge_by_area_ratio(q, area, [[0]], np.array([[area]]))
    assert out[0] == q if q > 0 else out.get(0, 0.0) == 0.0
