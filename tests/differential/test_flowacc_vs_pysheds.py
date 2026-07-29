"""Differential test: our flow accumulation against pysheds.

Accumulation is downstream of flow direction, so it inherits every disagreement
the direction comparison already documents — a single differently-routed cell
high in a catchment moves a whole sub-basin's worth of area into a different
channel. Comparing the arrays cell by cell would therefore measure the tie-break
rule, not the accumulation algorithm.

What is compared instead is what accumulation is actually *for*:

* the grand totals, which are conservation statements and must match exactly;
* the large-accumulation cells, which is where the stream network comes from and
  where a real algorithmic error would show;
* agreement on an epsilon-filled DEM restricted to cells both implementations
  route identically, where the two must agree exactly.
"""

from __future__ import annotations

from collections.abc import Callable

import numpy as np
import numpy.typing as npt
import pytest

from floodline.synthetic import SyntheticCatchment, make_synthetic_catchment
from floodline.terrain.fill import fill_depressions
from floodline.terrain.flowacc import flow_accumulation
from floodline.terrain.flowdir import downstream_index, flow_direction

pytestmark = pytest.mark.differential


def test_totals_match_on_the_synthetic_catchment(
    catchment: SyntheticCatchment,
    pysheds_accumulation: Callable[..., npt.NDArray[np.float64]],
) -> None:
    """Conservation: both implementations move exactly one unit per cell."""
    cellsize = catchment.cellsize
    filled = fill_depressions(catchment.dem.astype(np.float64), epsilon=1e-4)
    ours = flow_accumulation(flow_direction(filled, cellsize=(cellsize, cellsize)))
    theirs = pysheds_accumulation(filled, cellsize)

    assert ours.accumulation.sum() == pytest.approx(theirs.sum(), rel=1e-9)
    assert ours.accumulation.max() == pytest.approx(theirs.max(), rel=1e-9)


def test_agrees_exactly_where_the_routing_agrees(
    catchment: SyntheticCatchment,
    pysheds_accumulation: Callable[..., npt.NDArray[np.float64]],
    pysheds_flow_direction: Callable[..., npt.NDArray[np.int64]],
) -> None:
    """Restricted to cells whose whole upstream area is routed identically."""
    cellsize = catchment.cellsize
    filled = fill_depressions(catchment.dem.astype(np.float64), epsilon=1e-4)
    fdir = flow_direction(filled, cellsize=(cellsize, cellsize))
    ours = flow_accumulation(fdir).accumulation
    theirs = pysheds_accumulation(filled, cellsize)

    # Propagate "my upstream is routed the same as yours" downstream: a cell is
    # comparable only if it and everything above it agree.
    same = fdir == pysheds_flow_direction(filled, cellsize)
    receiver = downstream_index(fdir)
    cols = filled.shape[1]
    order = np.argsort(ours, axis=None)  # ascending accumulation is a topological order
    clean = same.copy()
    for cell in order:
        row, col = divmod(int(cell), cols)
        target = int(receiver[row, col])
        if target >= 0 and not clean[row, col]:
            clean[divmod(target, cols)] = False

    assert clean.sum() > 0.5 * clean.size, "too few comparable cells for a real check"
    np.testing.assert_allclose(ours[clean], theirs[clean], rtol=1e-9)


def test_the_main_channel_matches(
    catchment: SyntheticCatchment,
    pysheds_accumulation: Callable[..., npt.NDArray[np.float64]],
) -> None:
    """The largest accumulation values are the stream network; they must agree."""
    cellsize = catchment.cellsize
    filled = fill_depressions(catchment.dem.astype(np.float64), epsilon=1e-4)
    ours = flow_accumulation(flow_direction(filled, cellsize=(cellsize, cellsize))).accumulation
    theirs = pysheds_accumulation(filled, cellsize)

    threshold = np.percentile(theirs, 99.0)
    np.testing.assert_allclose(
        np.sort(ours[ours >= threshold]), np.sort(theirs[theirs >= threshold]), rtol=1e-9
    )


@pytest.mark.parametrize("seed", [1, 2])
def test_totals_match_on_rough_catchments(
    seed: int, pysheds_accumulation: Callable[..., npt.NDArray[np.float64]]
) -> None:
    catchment = make_synthetic_catchment(rows=90, cols=70, n_pits=4, roughness_m=0.3, seed=seed)
    cellsize = catchment.cellsize
    filled = fill_depressions(catchment.dem.astype(np.float64), epsilon=1e-4)
    ours = flow_accumulation(flow_direction(filled, cellsize=(cellsize, cellsize)))
    theirs = pysheds_accumulation(filled, cellsize)
    assert ours.accumulation.sum() == pytest.approx(theirs.sum(), rel=1e-9)
    assert ours.cells_draining_to_outlets == ours.n_valid
