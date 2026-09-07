"""Hypothesis property tests for D8 flow direction.

The three invariants:

* every interior non-nodata cell has exactly one downstream neighbour;
* edge cells may drain off-raster;
* no cycles — following the pointers from any cell reaches the edge of the data
  in a bounded number of steps.

"Interior" means a cell that is neither on the raster border nor adjacent to
nodata: those are the edges of the *data*, and the whole point of the outlet
sentinel is that flow leaves there. "Exactly one downstream neighbour" needs the
DEM to be epsilon-filled — an epsilon-free fill leaves flats, where D8 is
genuinely undefined and the module reports FLOW_FLAT rather than guessing. Both
cases are covered below.
"""

from __future__ import annotations

from collections.abc import Callable

import numpy as np
import numpy.typing as npt
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from hypothesis.extra import numpy as hnp

from floodline.core.terrain.fill import fill_depressions
from floodline.core.terrain.flowdir import (
    D8_CODES,
    FLOW_FLAT,
    FLOW_NODATA,
    FLOW_OUTLET,
    downstream_index,
    flow_direction,
    steps_to_outlet,
)

EPSILON = 1e-3

SETTINGS = settings(
    max_examples=150,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)


@st.composite
def dems(draw: st.DrawFn, *, allow_nodata: bool = False) -> npt.NDArray[np.float64]:
    """Draw a small elevation grid, optionally with NaN nodata punched through it."""
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


# --- invariant 1: exactly one downstream neighbour --------------------------------


@SETTINGS
@given(dem=dems(allow_nodata=True))
def test_interior_cells_have_exactly_one_downstream_neighbour(
    dem: npt.NDArray[np.float64],
    data_edge_mask: Callable[..., npt.NDArray[np.bool_]],
) -> None:
    filled = fill_depressions(dem, epsilon=EPSILON)
    fdir = flow_direction(filled)
    valid = np.isfinite(dem)
    interior = valid & ~data_edge_mask(valid)

    codes = fdir[interior]
    assert np.all(np.isin(codes, D8_CODES)), "an interior cell has no single direction"

    # "Exactly one" is about the pointer being single-valued: the receiver is one
    # cell, it is a real neighbour, and it is strictly downhill.
    receiver = downstream_index(fdir)
    cols = dem.shape[1]
    idx = np.argwhere(interior)
    for row, col in idx:
        target = receiver[row, col]
        assert target >= 0
        t_row, t_col = divmod(int(target), cols)
        assert max(abs(t_row - row), abs(t_col - col)) == 1
        assert filled[t_row, t_col] < filled[row, col]


@SETTINGS
@given(dem=dems(allow_nodata=True))
def test_a_flat_fill_marks_flats_instead_of_inventing_a_direction(
    dem: npt.NDArray[np.float64],
    data_edge_mask: Callable[..., npt.NDArray[np.bool_]],
    lower_neighbour_counts: Callable[..., npt.NDArray[np.int64]],
) -> None:
    """With epsilon = 0 the flats are real, and every one of them is reported."""
    filled = fill_depressions(dem, epsilon=0.0)
    fdir = flow_direction(filled)
    valid = np.isfinite(dem)
    stuck = valid & (lower_neighbour_counts(filled, valid) == 0)
    interior_stuck = stuck & ~data_edge_mask(valid)
    assert np.all(fdir[interior_stuck] == FLOW_FLAT)
    assert np.all(fdir[valid & ~stuck] > 0)


@SETTINGS
@given(dem=dems(allow_nodata=True))
def test_a_direction_always_points_strictly_downhill(dem: npt.NDArray[np.float64]) -> None:
    filled = fill_depressions(dem, epsilon=EPSILON)
    fdir = flow_direction(filled)
    receiver = downstream_index(fdir)
    routed = fdir > 0
    cols = dem.shape[1]
    for row, col in np.argwhere(routed):
        target = int(receiver[row, col])
        assert filled[divmod(target, cols)] < filled[row, col]


# --- invariant 2: edge cells may drain off-raster ---------------------------------


@SETTINGS
@given(dem=dems(allow_nodata=True))
def test_only_edge_cells_drain_off_raster(
    dem: npt.NDArray[np.float64],
    data_edge_mask: Callable[..., npt.NDArray[np.bool_]],
) -> None:
    fdir = flow_direction(fill_depressions(dem, epsilon=EPSILON))
    valid = np.isfinite(dem)
    assert np.all(data_edge_mask(valid)[fdir == FLOW_OUTLET])


@SETTINGS
@given(dem=dems(allow_nodata=True))
def test_nodata_cells_carry_the_nodata_code_and_nothing_else_does(
    dem: npt.NDArray[np.float64],
) -> None:
    fdir = flow_direction(fill_depressions(dem, epsilon=EPSILON))
    np.testing.assert_array_equal(fdir == FLOW_NODATA, ~np.isfinite(dem))


@SETTINGS
@given(dem=dems(allow_nodata=True))
def test_every_cell_gets_a_code_from_the_documented_alphabet(
    dem: npt.NDArray[np.float64],
) -> None:
    fdir = flow_direction(fill_depressions(dem, epsilon=EPSILON))
    alphabet = np.concatenate([D8_CODES.astype(np.int64), [FLOW_NODATA, FLOW_OUTLET, FLOW_FLAT]])
    assert np.all(np.isin(fdir, alphabet))


# --- invariant 3: no cycles --------------------------------------------------------


@SETTINGS
@given(dem=dems(allow_nodata=True))
def test_following_the_pointers_terminates(dem: npt.NDArray[np.float64]) -> None:
    """From any cell, the path reaches the edge of the data within N steps."""
    fdir = flow_direction(fill_depressions(dem, epsilon=EPSILON))
    steps = steps_to_outlet(fdir)  # raises if the pointers cycle
    assert np.all(steps >= 0)
    assert np.all(steps <= dem.size)


@SETTINGS
@given(dem=dems(allow_nodata=True))
def test_every_path_ends_at_the_edge_of_the_data(
    dem: npt.NDArray[np.float64],
    data_edge_mask: Callable[..., npt.NDArray[np.bool_]],
) -> None:
    """Walked explicitly, rather than trusting the memoised step count."""
    fdir = flow_direction(fill_depressions(dem, epsilon=EPSILON))
    receiver = downstream_index(fdir)
    valid = np.isfinite(dem)
    edge = data_edge_mask(valid)
    cols = dem.shape[1]
    budget = dem.size

    for row, col in np.argwhere(valid):
        cell = row * cols + col
        for _ in range(budget + 1):
            target = int(receiver.ravel()[cell])
            if target < 0:
                break
            cell = target
        else:  # pragma: no cover - would mean a cycle
            raise AssertionError("path did not terminate")
        end_row, end_col = divmod(cell, cols)
        assert edge[end_row, end_col] or not valid[end_row, end_col]


@SETTINGS
@given(dem=dems(allow_nodata=True))
def test_steps_strictly_decrease_downstream(dem: npt.NDArray[np.float64]) -> None:
    """A monotone decreasing quantity along every path is a cycle-freeness witness."""
    fdir = flow_direction(fill_depressions(dem, epsilon=EPSILON))
    receiver = downstream_index(fdir)
    steps = steps_to_outlet(fdir)
    routed = np.argwhere(fdir > 0)
    cols = dem.shape[1]
    for row, col in routed:
        target = int(receiver[row, col])
        assert steps[divmod(target, cols)] < steps[row, col]


@SETTINGS
@given(dem=dems())
def test_result_is_deterministic(dem: npt.NDArray[np.float64]) -> None:
    filled = fill_depressions(dem, epsilon=EPSILON)
    np.testing.assert_array_equal(flow_direction(filled), flow_direction(filled))
