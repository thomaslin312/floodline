"""HAND — height above nearest drainage (Rennó et al. 2008, Nobre et al. 2011).

For every cell, follow the D8 pointers downhill until a stream cell is reached;
HAND is the elevation difference between the cell and that stream cell. It is the
whole basis of the inundation model: a reach at stage `s` floods exactly the cells
whose HAND is below `s`.

    HAND[cell] = filled[cell] - filled[drainage(cell)]

where `drainage(cell)` is the first stream cell on the cell's flow path. Stream
cells drain to themselves, so their HAND is zero by construction, not by rounding.

The walk is memoised: once a cell's drainage outlet is known, every cell that
flows into it inherits it, so the whole grid costs one pass rather than one walk
per cell.

Cells with no drainage
----------------------
A cell whose flow path leaves the data, or dead-ends in a flat, without ever
touching a stream has no nearest drainage and gets NaN. That is a real condition,
not a failure: it is what a hillslope draining straight off the edge of the tile
looks like, and it is exactly the region where an inundation depth would be
meaningless. `HandResult` reports how many such cells there are so the caller can
see the size of the hole rather than discovering it later as a blank patch on a
map.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import numpy.typing as npt
from numba import njit

from floodline.core.terrain.flowdir import FLOW_NODATA, downstream_index

__all__ = ["HandResult", "hand"]


@dataclass(frozen=True, slots=True)
class HandResult:
    """HAND raster plus the drainage cell each cell was measured against."""

    hand: npt.NDArray[np.float64]
    """Height above the nearest downstream drainage, in DEM units. NaN where none."""

    drainage_index: npt.NDArray[np.int64]
    """Flat index of the stream cell each cell drains to, or -1 where there is none."""

    n_valid: int
    """Number of non-nodata cells."""

    cells_without_drainage: int
    """Valid cells whose flow path never reaches a stream cell."""

    @property
    def undrained_fraction(self) -> float:
        """Fraction of valid cells with no nearest drainage."""
        return self.cells_without_drainage / self.n_valid if self.n_valid else 0.0


@njit(cache=True, nogil=True)
def _nearest_drainage(
    receiver: npt.NDArray[np.int64],
    is_stream: npt.NDArray[np.bool_],
    valid: npt.NDArray[np.bool_],
) -> npt.NDArray[np.int64]:
    """Return the first stream cell on each cell's flow path, or -1 if there is none.

    Memoised with an explicit path stack: walk downstream until reaching a stream
    cell, a cell whose answer is already known, or the end of the flow path, then
    write the answer back up the path just walked. Every cell is walked once.
    """
    n_cells = receiver.shape[0]
    drainage = np.full(n_cells, -1, dtype=np.int64)
    resolved = np.zeros(n_cells, dtype=np.bool_)
    path = np.empty(n_cells, dtype=np.int64)

    for start in range(n_cells):
        if resolved[start] or not valid[start]:
            continue

        depth = 0
        cell = start
        answer = -1
        while True:
            if cell < 0 or not valid[cell]:
                break
            if resolved[cell]:
                answer = drainage[cell]
                break
            if is_stream[cell]:
                answer = cell
                # Resolve the stream cell itself here; it drains to itself.
                drainage[cell] = cell
                resolved[cell] = True
                break
            path[depth] = cell
            depth += 1
            resolved[cell] = True  # provisional: prevents revisiting inside this walk
            cell = receiver[cell]

        for i in range(depth):
            drainage[path[i]] = answer

    return drainage


def hand(
    filled_dem: npt.NDArray[np.floating],
    flowdir: npt.NDArray[np.int16],
    stream_mask: npt.NDArray[np.bool_],
    *,
    nodata: float | None = None,
) -> HandResult:
    """Compute height above nearest drainage.

    Parameters
    ----------
    filled_dem
        The **conditioned** DEM the flow directions were derived from. Using a
        different surface here silently breaks the HAND >= 0 invariant, because
        elevation is only guaranteed non-increasing downstream on the filled one.
    flowdir
        D8 direction codes from `terrain.flowdir.flow_direction`.
    stream_mask
        Channel mask from `terrain.streams`.
    nodata
        An additional sentinel treated as nodata, on top of NaN.

    Returns
    -------
    HandResult
    """
    if not (filled_dem.shape == flowdir.shape == stream_mask.shape):
        raise ValueError("filled_dem, flowdir and stream_mask must have the same shape")
    if not np.issubdtype(filled_dem.dtype, np.floating):
        raise TypeError(f"filled_dem must be a floating dtype, got {filled_dem.dtype}")

    rows, cols = filled_dem.shape
    elevation = np.ascontiguousarray(filled_dem, dtype=np.float64).ravel()
    valid = np.isfinite(elevation) & (np.ascontiguousarray(flowdir).ravel() != FLOW_NODATA)
    if nodata is not None and np.isfinite(nodata):
        valid &= elevation != nodata

    receiver = np.ascontiguousarray(downstream_index(flowdir)).ravel()
    streams = np.ascontiguousarray(stream_mask).ravel() & valid

    drainage = _nearest_drainage(receiver, streams, valid)

    heights = np.full(rows * cols, np.nan, dtype=np.float64)
    found = drainage >= 0
    heights[found] = elevation[found] - elevation[drainage[found]]

    return HandResult(
        hand=heights.reshape(rows, cols),
        drainage_index=drainage.reshape(rows, cols),
        n_valid=int(valid.sum()),
        cells_without_drainage=int((valid & ~found).sum()),
    )
