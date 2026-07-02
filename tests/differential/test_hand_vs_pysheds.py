"""Differential test: our HAND against pysheds, on an epsilon-filled DEM.

pysheds derives its own flow directions inside `compute_hand`, so this compares
the whole chain rather than the HAND step alone. Both are given the *same* stream
mask, so the drainage network is identical and only the routing can differ.

An epsilon-filled DEM is used on purpose: it has no flats, which removes the
largest of the documented flow-direction disagreement categories and leaves the
comparison meaningful. What remains is the tie-break rule, so cells are compared
where both implementations agree on the drainage cell — plus distribution-level
checks that would catch a systematic error anywhere.
"""

from __future__ import annotations

from collections.abc import Callable

import numpy as np
import numpy.typing as npt
import pytest

from floodline.synthetic import SyntheticCatchment, make_synthetic_catchment
from floodline.terrain.fill import fill_depressions
from floodline.terrain.flowacc import flow_accumulation
from floodline.terrain.flowdir import flow_direction
from floodline.terrain.hand import hand
from floodline.terrain.streams import stream_mask

pytestmark = pytest.mark.differential

Chain = tuple[npt.NDArray[np.float64], npt.NDArray[np.int16], npt.NDArray[np.bool_]]


def _chain(catchment: SyntheticCatchment, threshold: int = 200) -> Chain:
    cellsize = catchment.cellsize
    filled = fill_depressions(catchment.dem.astype(np.float64), epsilon=1e-4)
    fdir = flow_direction(filled, cellsize=(cellsize, cellsize))
    acc = flow_accumulation(fdir).accumulation
    return filled, fdir, stream_mask(acc, fdir, threshold=threshold)


def test_agrees_where_the_routing_agrees(
    catchment: SyntheticCatchment,
    pysheds_hand: Callable[..., npt.NDArray[np.float64]],
    pysheds_flow_direction: Callable[..., npt.NDArray[np.int64]],
) -> None:
    cellsize = catchment.cellsize
    filled, fdir, streams = _chain(catchment)
    ours = hand(filled, fdir, streams).hand
    theirs = pysheds_hand(filled, streams, cellsize)

    same_route = fdir == pysheds_flow_direction(filled, cellsize)
    comparable = same_route & np.isfinite(ours) & np.isfinite(theirs)
    # About three quarters of the grid is comparable. The rest is pysheds
    # declining to route border cells off-raster, which leaves it unable to
    # resolve a drainage cell for anything draining that way — the documented
    # edge-of-data category, not a numerical disagreement.
    assert comparable.sum() > 0.7 * streams.size, "too few comparable cells"
    np.testing.assert_allclose(ours[comparable], theirs[comparable], rtol=0, atol=1e-6)
    # On the comparable cells the agreement is exact, not merely within tolerance.
    assert np.array_equal(ours[comparable], theirs[comparable])


def test_stream_cells_are_zero_in_both(
    catchment: SyntheticCatchment,
    pysheds_hand: Callable[..., npt.NDArray[np.float64]],
    data_edge_mask: Callable[..., npt.NDArray[np.bool_]],
) -> None:
    """Zero on every stream cell for us; for pysheds, on every one it can resolve.

    pysheds leaves some stream cells NaN. Every one of them is on the raster
    border, because it will not route a border cell off-raster and so cannot find
    a drainage cell for it. That is asserted rather than tolerated: if a
    *non-border* stream cell ever came back NaN, it would be a real disagreement.
    """
    cellsize = catchment.cellsize
    filled, fdir, streams = _chain(catchment)
    ours = hand(filled, fdir, streams).hand
    theirs = pysheds_hand(filled, streams, cellsize)

    assert np.all(ours[streams] == 0.0)

    unresolved = streams & np.isnan(theirs)
    edge = data_edge_mask(np.isfinite(filled))
    assert np.all(edge[unresolved]), "pysheds failed to resolve an interior stream cell"
    np.testing.assert_allclose(theirs[streams & ~unresolved], 0.0, atol=1e-9)


def test_distributions_match(
    catchment: SyntheticCatchment,
    pysheds_hand: Callable[..., npt.NDArray[np.float64]],
) -> None:
    """A systematic routing error would move the quantiles even if no cell is checked."""
    cellsize = catchment.cellsize
    filled, fdir, streams = _chain(catchment)
    ours = hand(filled, fdir, streams).hand
    theirs = pysheds_hand(filled, streams, cellsize)

    both = np.isfinite(ours) & np.isfinite(theirs)
    quantiles = [0.1, 0.25, 0.5, 0.75, 0.9, 0.99]
    np.testing.assert_allclose(
        np.quantile(ours[both], quantiles),
        np.quantile(theirs[both], quantiles),
        rtol=0.02,
        atol=0.05,
    )
    assert ours[both].max() == pytest.approx(theirs[both].max(), rel=0.05)


@pytest.mark.parametrize("threshold", [100, 400])
def test_agrees_across_drainage_densities(
    threshold: int,
    catchment: SyntheticCatchment,
    pysheds_hand: Callable[..., npt.NDArray[np.float64]],
    pysheds_flow_direction: Callable[..., npt.NDArray[np.int64]],
) -> None:
    cellsize = catchment.cellsize
    filled, fdir, streams = _chain(catchment, threshold=threshold)
    ours = hand(filled, fdir, streams).hand
    theirs = pysheds_hand(filled, streams, cellsize)

    same_route = fdir == pysheds_flow_direction(filled, cellsize)
    comparable = same_route & np.isfinite(ours) & np.isfinite(theirs)
    np.testing.assert_allclose(ours[comparable], theirs[comparable], rtol=0, atol=1e-6)


def test_agrees_on_a_rough_catchment(
    pysheds_hand: Callable[..., npt.NDArray[np.float64]],
    pysheds_flow_direction: Callable[..., npt.NDArray[np.int64]],
) -> None:
    catchment = make_synthetic_catchment(rows=90, cols=70, n_pits=4, roughness_m=0.3, seed=2)
    cellsize = catchment.cellsize
    filled, fdir, streams = _chain(catchment, threshold=120)
    ours = hand(filled, fdir, streams).hand
    theirs = pysheds_hand(filled, streams, cellsize)

    same_route = fdir == pysheds_flow_direction(filled, cellsize)
    comparable = same_route & np.isfinite(ours) & np.isfinite(theirs)
    assert comparable.sum() > 0.5 * streams.size
    np.testing.assert_allclose(ours[comparable], theirs[comparable], rtol=0, atol=1e-6)
