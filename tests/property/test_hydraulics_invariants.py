"""Hypothesis property tests for stage handling and inundation.

The spec's hydraulics invariants:

* inundated area is monotone non-decreasing in stage;
* ``depth = stage - HAND`` on wet cells, 0 elsewhere, and ``max depth <= stage``;
* with the connectivity filter on, every wet cell is 8-connected to a stream cell.
"""

from __future__ import annotations

import numpy as np
import numpy.typing as npt
import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from hypothesis.extra import numpy as hnp

from floodline.config import Config, Connectivity
from floodline.hydraulics.inundate import inundate
from floodline.hydraulics.stage import gauge_reading_to_datum, resolve_gauge
from floodline.terrain._neighbours import neighbour_offsets

SETTINGS = settings(
    max_examples=150,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)

stages = st.floats(0.0, 30.0, allow_nan=False, allow_infinity=False, width=32)


@st.composite
def hand_and_streams(
    draw: st.DrawFn,
) -> tuple[npt.NDArray[np.float64], npt.NDArray[np.bool_]]:
    """Draw a HAND raster with a plausible stream mask (HAND exactly zero)."""
    shape = draw(st.tuples(st.integers(3, 12), st.integers(3, 12)))
    heights = draw(
        hnp.arrays(
            dtype=np.float64,
            shape=shape,
            elements=st.floats(0.0, 25.0, allow_nan=False, allow_infinity=False, width=32),
        )
    )
    streams = draw(hnp.arrays(dtype=np.bool_, shape=shape, elements=st.booleans()))
    heights = np.where(streams, 0.0, heights)
    if draw(st.booleans()):
        holes = draw(hnp.arrays(dtype=np.bool_, shape=shape, elements=st.booleans()))
        heights = np.where(holes & ~streams, np.nan, heights)
    return heights, streams


# --- monotonicity in stage -----------------------------------------------------------


@SETTINGS
@given(data=hand_and_streams(), low=stages, rise=st.floats(0.0, 10.0))
def test_area_is_monotone_in_stage(
    data: tuple[npt.NDArray[np.float64], npt.NDArray[np.bool_]], low: float, rise: float
) -> None:
    heights, streams = data
    shallow = inundate(heights, low, streams=streams, require_connectivity=False)
    deep = inundate(heights, low + rise, streams=streams, require_connectivity=False)
    assert deep.n_wet >= shallow.n_wet
    assert deep.area_m2 >= shallow.area_m2


@SETTINGS
@given(data=hand_and_streams(), low=stages, rise=st.floats(0.0, 10.0))
def test_extents_are_nested(
    data: tuple[npt.NDArray[np.float64], npt.NDArray[np.bool_]], low: float, rise: float
) -> None:
    """Not just larger: a higher stage floods a superset of the same cells."""
    heights, streams = data
    shallow = inundate(heights, low, streams=streams, require_connectivity=False)
    deep = inundate(heights, low + rise, streams=streams, require_connectivity=False)
    assert np.all(deep.wet >= shallow.wet)


@SETTINGS
@given(data=hand_and_streams(), low=stages, rise=st.floats(0.0, 10.0))
def test_area_is_monotone_with_the_connectivity_filter_on(
    data: tuple[npt.NDArray[np.float64], npt.NDArray[np.bool_]], low: float, rise: float
) -> None:
    """Filtering must not break monotonicity: more water cannot disconnect water."""
    heights, streams = data
    shallow = inundate(heights, low, streams=streams, require_connectivity=True)
    deep = inundate(heights, low + rise, streams=streams, require_connectivity=True)
    assert deep.n_wet >= shallow.n_wet
    assert np.all(deep.wet >= shallow.wet)


@SETTINGS
@given(data=hand_and_streams(), stage=stages)
def test_depth_is_monotone_in_stage(
    data: tuple[npt.NDArray[np.float64], npt.NDArray[np.bool_]], stage: float
) -> None:
    heights, streams = data
    shallow = inundate(heights, stage, streams=streams, require_connectivity=False)
    deep = inundate(heights, stage + 1.0, streams=streams, require_connectivity=False)
    both = np.isfinite(shallow.depth) & np.isfinite(deep.depth)
    assert np.all(deep.depth[both] >= shallow.depth[both])


# --- the depth identity ---------------------------------------------------------------


@SETTINGS
@given(data=hand_and_streams(), stage=stages)
def test_depth_identity_and_bound(
    data: tuple[npt.NDArray[np.float64], npt.NDArray[np.bool_]], stage: float
) -> None:
    heights, streams = data
    result = inundate(heights, stage, streams=streams, require_connectivity=False, min_depth=0.0)
    wet = result.wet
    np.testing.assert_allclose(result.depth[wet], stage - heights[wet])
    assert np.all(result.depth[np.isfinite(result.depth) & ~wet] == 0.0)
    if result.n_wet:
        assert result.max_depth_m <= stage + 1e-12


@SETTINGS
@given(data=hand_and_streams(), stage=stages)
def test_undefined_hand_is_never_wet(
    data: tuple[npt.NDArray[np.float64], npt.NDArray[np.bool_]], stage: float
) -> None:
    heights, streams = data
    result = inundate(heights, stage, streams=streams, require_connectivity=False)
    assert not result.wet[~np.isfinite(heights)].any()
    assert np.all(np.isnan(result.depth[~np.isfinite(heights)]))


@SETTINGS
@given(data=hand_and_streams(), stage=stages)
def test_depth_is_never_negative(
    data: tuple[npt.NDArray[np.float64], npt.NDArray[np.bool_]], stage: float
) -> None:
    heights, streams = data
    depth = inundate(heights, stage, streams=streams, require_connectivity=False).depth
    assert np.all(depth[np.isfinite(depth)] >= 0.0)


# --- the connectivity filter ------------------------------------------------------------


@SETTINGS
@given(data=hand_and_streams(), stage=stages)
def test_every_wet_cell_is_connected_to_a_stream(
    data: tuple[npt.NDArray[np.float64], npt.NDArray[np.bool_]], stage: float
) -> None:
    """Walked independently: a flood fill over wet cells starting from wet streams."""
    heights, streams = data
    result = inundate(heights, stage, streams=streams, require_connectivity=True, min_depth=0.0)
    if not result.n_wet:
        return

    reached = result.wet & streams
    offsets = neighbour_offsets(Connectivity.EIGHT)
    rows, cols = heights.shape
    changed = True
    while changed:
        changed = False
        for row, col in np.argwhere(result.wet & ~reached):
            for d_row, d_col in offsets:
                n_row, n_col = row + d_row, col + d_col
                if 0 <= n_row < rows and 0 <= n_col < cols and reached[n_row, n_col]:
                    reached[row, col] = True
                    changed = True
                    break
    np.testing.assert_array_equal(result.wet, reached)


@SETTINGS
@given(data=hand_and_streams(), stage=stages)
def test_filtering_only_removes(
    data: tuple[npt.NDArray[np.float64], npt.NDArray[np.bool_]], stage: float
) -> None:
    heights, streams = data
    loose = inundate(heights, stage, streams=streams, require_connectivity=False)
    strict = inundate(heights, stage, streams=streams, require_connectivity=True)
    assert np.all(strict.wet <= loose.wet)
    assert strict.n_wet + strict.n_removed_by_connectivity == loose.n_wet


@SETTINGS
@given(data=hand_and_streams(), stage=stages)
def test_d4_filtering_is_stricter_than_d8(
    data: tuple[npt.NDArray[np.float64], npt.NDArray[np.bool_]], stage: float
) -> None:
    heights, streams = data
    d8 = inundate(
        heights,
        stage,
        streams=streams,
        require_connectivity=True,
        connectivity=Connectivity.EIGHT,
    )
    d4 = inundate(
        heights,
        stage,
        streams=streams,
        require_connectivity=True,
        connectivity=Connectivity.FOUR,
    )
    assert np.all(d4.wet <= d8.wet)


# --- the datum ----------------------------------------------------------------------------


@SETTINGS
@given(reading=st.floats(-5.0, 30.0), offset=st.floats(-50.0, 50.0))
def test_datum_conversion_is_an_exact_shift(reading: float, offset: float) -> None:
    cfg = Config.model_validate(
        {"hydraulics": {"gauge_datum_offset_m": offset, "gauge_reading_unit": "m"}}
    )
    assert gauge_reading_to_datum(reading, config=cfg) == reading + offset


@SETTINGS
@given(reading=st.floats(0.0, 20.0), bed=st.floats(-10.0, 10.0), offset=st.floats(-5.0, 5.0))
def test_resolved_depth_is_reading_plus_offset_minus_bed(
    reading: float, bed: float, offset: float
) -> None:
    cfg = Config.model_validate(
        {"hydraulics": {"gauge_datum_offset_m": offset, "gauge_reading_unit": "m"}}
    )
    dem = np.full((3, 3), bed)
    if reading + offset - bed < 0:
        return  # refused, covered by a unit test
    gauge = resolve_gauge(reading, dem, (1, 1), config=cfg)
    assert gauge.depth_m == pytest.approx(reading + offset - bed)
