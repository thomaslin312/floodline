"""Hypothesis property tests for flow accumulation.

The invariants:

* ``acc = 1 + sum over upstream neighbours``;
* total accumulation at outlets = valid-cell count *minus* the cells that drain
  into flats;
* accumulation strictly increases along every flow path.
"""

from __future__ import annotations

import numpy as np
import numpy.typing as npt
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from hypothesis.extra import numpy as hnp

from floodline.terrain.fill import fill_depressions
from floodline.terrain.flowacc import flow_accumulation
from floodline.terrain.flowdir import FLOW_FLAT, downstream_index, flow_direction

EPSILON = 1e-3

SETTINGS = settings(
    max_examples=150,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)


@st.composite
def dems(draw: st.DrawFn, *, allow_nodata: bool = True) -> npt.NDArray[np.float64]:
    shape = draw(st.tuples(st.integers(3, 12), st.integers(3, 12)))
    dem = draw(
        hnp.arrays(
            dtype=np.float64,
            shape=shape,
            elements=st.floats(-200.0, 2000.0, allow_nan=False, allow_infinity=False, width=32),
        )
    )
    if allow_nodata and draw(st.booleans()):
        holes = draw(hnp.arrays(dtype=np.bool_, shape=shape, elements=st.booleans()))
        if holes.any() and not holes.all():
            dem = dem.copy()
            dem[holes] = np.nan
    return dem


@st.composite
def epsilon_dems(draw: st.DrawFn) -> npt.NDArray[np.float64]:
    return fill_depressions(draw(dems()), epsilon=EPSILON)


@st.composite
def flat_dems(draw: st.DrawFn) -> npt.NDArray[np.float64]:
    return fill_depressions(draw(dems()), epsilon=0.0)


# --- invariant 1: acc = 1 + upstream sum -------------------------------------------


@SETTINGS
@given(dem=flat_dems())
def test_accumulation_is_one_plus_upstream_sum(dem: npt.NDArray[np.float64]) -> None:
    fdir = flow_direction(dem)
    acc = flow_accumulation(fdir).accumulation
    receiver = downstream_index(fdir)
    cols = dem.shape[1]

    upstream = np.zeros_like(acc)
    for row, col in np.argwhere(np.isfinite(dem)):
        target = int(receiver[row, col])
        if target >= 0:
            upstream[divmod(target, cols)] += acc[row, col]

    valid = np.isfinite(dem)
    np.testing.assert_allclose(acc[valid], 1.0 + upstream[valid])
    assert np.all(acc[~valid] == 0.0)


@SETTINGS
@given(dem=flat_dems())
def test_every_cell_carries_at_least_itself(dem: npt.NDArray[np.float64]) -> None:
    acc = flow_accumulation(flow_direction(dem)).accumulation
    valid = np.isfinite(dem)
    assert np.all(acc[valid] >= 1.0)
    assert np.all(acc[valid] <= valid.sum())


# --- invariant 2: the outlet total, net of flat drainage ---------------------------


@SETTINGS
@given(dem=flat_dems())
def test_outlet_total_is_valid_count_minus_flat_drainage(
    dem: npt.NDArray[np.float64],
) -> None:
    result = flow_accumulation(flow_direction(dem))
    assert result.cells_draining_to_outlets == result.n_valid - result.cells_draining_to_flats
    assert result.cells_draining_to_flats >= 0


@SETTINGS
@given(dem=epsilon_dems())
def test_without_flats_every_cell_reaches_an_outlet(dem: npt.NDArray[np.float64]) -> None:
    """The spec's plain form of the invariant, on a DEM that has no flats."""
    fdir = flow_direction(dem)
    result = flow_accumulation(fdir)
    assert not (fdir == FLOW_FLAT).any()
    assert result.cells_draining_to_flats == 0
    assert result.cells_draining_to_outlets == result.n_valid


@SETTINGS
@given(dem=flat_dems())
def test_flat_drainage_matches_an_explicit_walk(dem: npt.NDArray[np.float64]) -> None:
    """Counted independently by walking every cell to where its path stops."""
    fdir = flow_direction(dem)
    receiver = downstream_index(fdir).ravel()
    cols = dem.shape[1]

    to_flats = 0
    for row, col in np.argwhere(np.isfinite(dem)):
        cell = row * cols + col
        while receiver[cell] >= 0:
            cell = int(receiver[cell])
        if fdir[divmod(cell, cols)] == FLOW_FLAT:
            to_flats += 1

    assert flow_accumulation(fdir).cells_draining_to_flats == to_flats


# --- invariant 3: strictly increasing downstream -----------------------------------


@SETTINGS
@given(dem=flat_dems())
def test_accumulation_strictly_increases_downstream(dem: npt.NDArray[np.float64]) -> None:
    fdir = flow_direction(dem)
    acc = flow_accumulation(fdir).accumulation
    receiver = downstream_index(fdir)
    cols = dem.shape[1]
    for row, col in np.argwhere(fdir > 0):
        target = int(receiver[row, col])
        assert acc[divmod(target, cols)] > acc[row, col]


@SETTINGS
@given(dem=flat_dems())
def test_weighted_accumulation_scales(dem: npt.NDArray[np.float64]) -> None:
    """Uniform weights just rescale: a linear operator on the weight field."""
    fdir = flow_direction(dem)
    plain = flow_accumulation(fdir).accumulation
    scaled = flow_accumulation(fdir, weights=np.full(dem.shape, 3.0)).accumulation
    np.testing.assert_allclose(scaled, plain * 3.0, rtol=1e-9)


@SETTINGS
@given(dem=flat_dems())
def test_accumulation_is_deterministic(dem: npt.NDArray[np.float64]) -> None:
    fdir = flow_direction(dem)
    np.testing.assert_array_equal(
        flow_accumulation(fdir).accumulation, flow_accumulation(fdir).accumulation
    )
