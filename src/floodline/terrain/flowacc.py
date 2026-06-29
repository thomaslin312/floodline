"""Flow accumulation by topological order over the D8 pointers.

Every cell contributes itself plus everything upstream of it. Because D8 gives
each cell exactly one receiver and the pointers are acyclic on a filled DEM, the
whole grid can be accumulated in one pass in topological order (Kahn's algorithm):
start from the cells nothing drains into, push each cell's total into its receiver,
and release the receiver once its last contributor has been counted.

Where the water ends up
-----------------------
Flow terminates in one of two places, and the difference matters enough to be
reported rather than buried:

* **Outlets** — the edge of the data. This is where accumulation is supposed to end.
* **Flats** — cells that `flowdir` could not assign a direction to, because on an
  epsilon-free fill the middle of a filled depression has no lower neighbour at
  all. Everything upstream of such a cell piles up there and never reaches an
  outlet.

So the spec's invariant, "total accumulation at outlets = number of non-nodata
cells", holds only once no water drains into a flat. `FlowAccumulation` reports
both totals, and `cells_draining_to_flats` is the number to watch: it is a direct
measure of how much of the grid the flat problem is costing, and it goes to zero
once flat resolution is switched on.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import numpy.typing as npt
from numba import njit

from floodline.terrain.flowdir import FLOW_FLAT, FLOW_NODATA, downstream_index

__all__ = ["FlowAccumulation", "flow_accumulation"]


@dataclass(frozen=True, slots=True)
class FlowAccumulation:
    """Accumulated upstream contribution per cell, plus where the water ended up."""

    accumulation: npt.NDArray[np.float64]
    """Cells (or summed weights) draining through each cell, including itself."""

    n_valid: int
    """Number of non-nodata cells."""

    cells_draining_to_outlets: int
    """Cells whose flow path reaches the edge of the data."""

    cells_draining_to_flats: int
    """Cells whose flow path dead-ends in a flat. Zero is the goal."""

    @property
    def flat_drainage_fraction(self) -> float:
        """Fraction of valid cells whose water never reaches an outlet."""
        return self.cells_draining_to_flats / self.n_valid if self.n_valid else 0.0


@njit(cache=True, nogil=True)
def _accumulate(
    receiver: npt.NDArray[np.int64], weights: npt.NDArray[np.float64]
) -> tuple[npt.NDArray[np.float64], int]:
    """Accumulate `weights` downstream in topological order.

    Returns the accumulation and the number of cells processed. A count short of
    the grid size means some cells were never released, which can only happen if
    the pointers contain a cycle.
    """
    n_cells = receiver.shape[0]
    acc = weights.copy()
    indegree = np.zeros(n_cells, dtype=np.int64)

    for cell in range(n_cells):
        target = receiver[cell]
        if target >= 0:
            indegree[target] += 1

    queue = np.empty(n_cells, dtype=np.int64)
    head = 0
    tail = 0
    for cell in range(n_cells):
        if indegree[cell] == 0:
            queue[tail] = cell
            tail += 1

    processed = 0
    while head < tail:
        cell = queue[head]
        head += 1
        processed += 1
        target = receiver[cell]
        if target < 0:
            continue
        acc[target] += acc[cell]
        indegree[target] -= 1
        if indegree[target] == 0:
            queue[tail] = target
            tail += 1

    return acc, processed


def flow_accumulation(
    flowdir: npt.NDArray[np.int16],
    *,
    weights: npt.NDArray[np.floating] | None = None,
) -> FlowAccumulation:
    """Accumulate upstream contributing area over D8 flow directions.

    Parameters
    ----------
    flowdir
        2-D ESRI direction codes from `terrain.flowdir.flow_direction`.
    weights
        Per-cell contribution. Defaults to 1.0 at every valid cell and 0.0 at
        nodata, which makes the accumulation a count of contributing cells.
        Supply cell areas, or a rainfall field, to weight it.

    Returns
    -------
    FlowAccumulation

    Notes
    -----
    Accumulation is carried in float64 throughout, one code path for weighted and
    unweighted alike. float64 represents integers exactly to 2**53, so a plain
    cell count stays exact for any raster that fits in memory.

    Raises
    ------
    ValueError
        If the flow directions contain a cycle.
    """
    if flowdir.ndim != 2:
        raise ValueError(f"flowdir must be 2-D, got shape {flowdir.shape}")

    rows, cols = flowdir.shape
    valid = flowdir != FLOW_NODATA

    if weights is None:
        weight_flat = valid.astype(np.float64).ravel()
    else:
        if weights.shape != flowdir.shape:
            raise ValueError(
                f"weights shape {weights.shape} does not match flowdir {flowdir.shape}"
            )
        weight_flat = np.where(valid, weights, 0.0).astype(np.float64).ravel()

    receiver = np.ascontiguousarray(downstream_index(flowdir)).ravel()
    acc_flat, processed = _accumulate(receiver, np.ascontiguousarray(weight_flat))
    if processed != rows * cols:
        raise ValueError(
            f"flow directions contain a cycle: {rows * cols - processed} cells "
            "were never released by the topological sort"
        )

    accumulation = acc_flat.reshape(rows, cols)

    # Flow terminates only at outlets, flats and nodata. Nodata carries zero
    # weight and nothing drains into it, so summing the two live terminal classes
    # partitions every valid cell by where its water ended up.
    terminal_flat = flowdir == FLOW_FLAT
    terminal_outlet = (~terminal_flat) & valid & (receiver.reshape(rows, cols) < 0)

    return FlowAccumulation(
        accumulation=accumulation,
        n_valid=int(valid.sum()),
        cells_draining_to_outlets=round(float(accumulation[terminal_outlet].sum())),
        cells_draining_to_flats=round(float(accumulation[terminal_flat].sum())),
    )
