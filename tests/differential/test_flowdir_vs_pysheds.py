"""Differential test: our D8 flow direction against pysheds.

Both use the ESRI dirmap and both rank neighbours by slope rather than raw drop,
so the great majority of cells must agree exactly. Three categories are allowed
to differ, and every one of them is a difference of *policy*, not of correctness:

1. **Edge of the data.** We route a cell off-raster (``FLOW_OUTLET``) when it has
   no strictly lower neighbour inside the data — that is what makes flow
   accumulation terminate at the boundary, and it is consistent with depression
   filling, which already treats the border and nodata as outlets. pysheds never
   routes off-raster: such a cell is simply reported as a flat. Same cells,
   different label.

2. **Flats.** On an epsilon-free fill, a cell in the middle of a filled
   depression has no strictly lower neighbour at all and D8 is undefined there.
   We say ``FLOW_FLAT`` (-2), pysheds says ``flats`` (-1). Again the same cells.

3. **Tied steepest descent.** When two neighbours offer exactly the same steepest
   slope, either choice is correct and the tie-break is a free parameter. Ours is
   *the lowest ESRI direction code wins*, i.e. the first of E, SE, S, SW, W, NW,
   N, NE that achieves the maximum. pysheds prefers the first entry of its
   dirmap, which is North. Neither is more right than the other; ours is written
   down in `flowdir.py` next to the loop that implements it.

The test asserts the disagreement set is contained in the union of those three,
so a disagreement of any *other* kind fails. It also asserts the sets are found
independently: the flat and tie masks are computed with numpy shifts in
`conftest.py`, not by asking either implementation.
"""

from __future__ import annotations

from collections.abc import Callable

import numpy as np
import numpy.typing as npt
import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from hypothesis.extra import numpy as hnp

from floodline.core.terrain.fill import fill_depressions
from floodline.core.terrain.flowdir import FLOW_FLAT, flow_direction
from floodline.synthetic import SyntheticCatchment, make_synthetic_catchment

pytestmark = pytest.mark.differential

Grid = npt.NDArray[np.float64]
Mask = npt.NDArray[np.bool_]

SETTINGS = settings(
    max_examples=60,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)


def _classify(
    filled: Grid,
    ours: npt.NDArray[np.int16],
    theirs: npt.NDArray[np.int64],
    cellsize: float,
    data_edge_mask: Callable[..., Mask],
    lower_neighbour_counts: Callable[..., npt.NDArray[np.int64]],
    steepest_tie_mask: Callable[..., Mask],
) -> tuple[Mask, Mask, Mask, Mask]:
    """Return (disagreements, edge, flat, tie) masks over the valid cells."""
    valid = np.isfinite(filled)
    disagree = (ours != theirs) & valid
    edge = data_edge_mask(valid)
    flat = valid & (lower_neighbour_counts(filled, valid) == 0)
    tie = steepest_tie_mask(filled, valid, cellsize)
    return disagree, edge, flat, tie


def test_disagreements_are_only_edges_flats_and_ties(
    catchment: SyntheticCatchment,
    pysheds_flow_direction: Callable[..., npt.NDArray[np.int64]],
    data_edge_mask: Callable[..., Mask],
    lower_neighbour_counts: Callable[..., npt.NDArray[np.int64]],
    steepest_tie_mask: Callable[..., Mask],
) -> None:
    cellsize = catchment.cellsize
    filled = fill_depressions(catchment.dem.astype(np.float64), epsilon=0.0)
    ours = flow_direction(filled, cellsize=(cellsize, cellsize))
    theirs = pysheds_flow_direction(filled, cellsize)

    disagree, edge, flat, tie = _classify(
        filled,
        ours,
        theirs,
        cellsize,
        data_edge_mask,
        lower_neighbour_counts,
        steepest_tie_mask,
    )
    unexplained = disagree & ~edge & ~flat & ~tie
    assert not unexplained.any(), (
        f"{unexplained.sum()} cells disagree with pysheds for no documented reason, "
        f"first at {np.argwhere(unexplained)[0].tolist()}"
    )
    # and the test must not be vacuous: the overwhelming majority agree exactly
    valid = np.isfinite(filled)
    assert disagree.sum() / valid.sum() < 0.05


def test_flat_cells_are_the_same_cells_in_both(
    catchment: SyntheticCatchment,
    pysheds_flow_direction: Callable[..., npt.NDArray[np.int64]],
    data_edge_mask: Callable[..., Mask],
) -> None:
    """Away from the border, our FLOW_FLAT (-2) and pysheds' flats (-1) coincide."""
    cellsize = catchment.cellsize
    filled = fill_depressions(catchment.dem.astype(np.float64), epsilon=0.0)
    ours = flow_direction(filled, cellsize=(cellsize, cellsize))
    theirs = pysheds_flow_direction(filled, cellsize)

    interior = np.isfinite(filled) & ~data_edge_mask(np.isfinite(filled))
    np.testing.assert_array_equal((ours == FLOW_FLAT)[interior], (theirs == -1)[interior])
    assert (ours == FLOW_FLAT).any(), "fixture should contain filled flats"


def test_epsilon_fill_removes_the_flat_disagreements(
    catchment: SyntheticCatchment,
    pysheds_flow_direction: Callable[..., npt.NDArray[np.int64]],
    data_edge_mask: Callable[..., Mask],
    lower_neighbour_counts: Callable[..., npt.NDArray[np.int64]],
    steepest_tie_mask: Callable[..., Mask],
) -> None:
    """With a gradient on the filled surfaces, only edges and ties can differ."""
    cellsize = catchment.cellsize
    filled = fill_depressions(catchment.dem.astype(np.float64), epsilon=1e-4)
    ours = flow_direction(filled, cellsize=(cellsize, cellsize))
    theirs = pysheds_flow_direction(filled, cellsize)

    disagree, edge, flat, tie = _classify(
        filled,
        ours,
        theirs,
        cellsize,
        data_edge_mask,
        lower_neighbour_counts,
        steepest_tie_mask,
    )
    interior_flats = flat & ~edge
    assert not interior_flats.any(), "epsilon fill should leave no interior flats"
    assert not (disagree & ~edge & ~tie).any()


@pytest.mark.parametrize("seed", [1, 2, 3])
def test_agrees_on_rough_catchments(
    seed: int,
    pysheds_flow_direction: Callable[..., npt.NDArray[np.int64]],
    data_edge_mask: Callable[..., Mask],
    lower_neighbour_counts: Callable[..., npt.NDArray[np.int64]],
    steepest_tie_mask: Callable[..., Mask],
) -> None:
    """Roughness makes many small pits and many exact ties: the stress case."""
    catchment = make_synthetic_catchment(rows=90, cols=70, n_pits=4, roughness_m=0.3, seed=seed)
    cellsize = catchment.cellsize
    filled = fill_depressions(catchment.dem.astype(np.float64), epsilon=0.0)
    ours = flow_direction(filled, cellsize=(cellsize, cellsize))
    theirs = pysheds_flow_direction(filled, cellsize)

    disagree, edge, flat, tie = _classify(
        filled,
        ours,
        theirs,
        cellsize,
        data_edge_mask,
        lower_neighbour_counts,
        steepest_tie_mask,
    )
    assert not (disagree & ~edge & ~flat & ~tie).any()


@SETTINGS
@given(
    dem=hnp.arrays(
        dtype=np.float64,
        shape=st.tuples(st.integers(4, 12), st.integers(4, 12)),
        elements=st.floats(-50.0, 500.0, allow_nan=False, allow_infinity=False, width=32),
    )
)
def test_agrees_on_random_grids(
    dem: Grid,
    pysheds_flow_direction: Callable[..., npt.NDArray[np.int64]],
    data_edge_mask: Callable[..., Mask],
    lower_neighbour_counts: Callable[..., npt.NDArray[np.int64]],
    steepest_tie_mask: Callable[..., Mask],
) -> None:
    filled = fill_depressions(dem, epsilon=0.0)
    ours = flow_direction(filled)
    theirs = pysheds_flow_direction(filled)

    disagree, edge, flat, tie = _classify(
        filled,
        ours,
        theirs,
        1.0,
        data_edge_mask,
        lower_neighbour_counts,
        steepest_tie_mask,
    )
    assert not (disagree & ~edge & ~flat & ~tie).any()
