"""Priority-flood depression filling (Barnes, Lehman & Mutlu 2014).

Water on a raw DEM gets stuck: lidar noise, bridges over creeks and genuine
closed basins all produce cells with no downhill neighbour, and a flow-routing
pass over such a surface dead-ends there. Filling raises the DEM until every cell
has a non-increasing path to the edge of the data, which is the precondition the
rest of the terrain pipeline is written against.

The implementation is Barnes' Algorithm 2 — Priority-Flood with the plain-FIFO
improvement — plus the epsilon variant (Algorithm 4) when
`TerrainConfig.fill_epsilon` is non-zero:

* Seed a min-priority queue with every cell on the edge of the data (the raster
  border, and any cell touching nodata).
* Repeatedly take the lowest unprocessed cell and expand into its unvisited
  neighbours, raising each to at least the elevation it was reached at.
* A neighbour that has to be raised cannot be lower than anything still in the
  priority queue, so it goes on a plain FIFO queue instead — that is the
  optimisation, and it is what takes the algorithm to near-linear time.

`fill_epsilon > 0` adds a per-step increment along filled surfaces so they carry
a gradient and drain, instead of becoming a flat that flow routing has to resolve
separately.

The kernel is a pure numba function — flat arrays in, flat arrays out, no Python
objects — so `NUMBA_DISABLE_JIT=1` runs the identical code path in the debugger.
"""

from __future__ import annotations

from typing import Literal, overload

import numpy as np
import numpy.typing as npt
from numba import njit

from floodline.config import Config, Connectivity, TerrainConfig
from floodline.terrain._neighbours import neighbour_offsets

__all__ = ["fill_depressions", "undrained_mask"]


# --------------------------------------------------------------------------------
# Binary min-heap over cell indices.
#
# The key of an entry is the current elevation of its cell. That is safe because a
# cell's elevation is written once, at the moment it is pushed, and it is closed
# from then on — so a key can never change while it sits in the heap. Ties are
# broken by insertion sequence, which makes the traversal order, and therefore the
# output, fully deterministic.
# --------------------------------------------------------------------------------


@njit(cache=True, nogil=True, inline="always")
def _heap_less(
    dem: npt.NDArray[np.floating],
    heap_cell: npt.NDArray[np.int64],
    heap_seq: npt.NDArray[np.int64],
    a: int,
    b: int,
) -> bool:
    """Return True if heap slot `a` orders before heap slot `b`."""
    za = dem[heap_cell[a]]
    zb = dem[heap_cell[b]]
    if za < zb:
        return True
    if za > zb:
        return False
    return bool(heap_seq[a] < heap_seq[b])


@njit(cache=True, nogil=True)
def _heap_push(
    dem: npt.NDArray[np.floating],
    heap_cell: npt.NDArray[np.int64],
    heap_seq: npt.NDArray[np.int64],
    size: int,
    cell: int,
    seq: int,
) -> int:
    """Push `cell` onto the heap and return the new heap size."""
    heap_cell[size] = cell
    heap_seq[size] = seq
    child = size
    while child > 0:
        parent = (child - 1) // 2
        if not _heap_less(dem, heap_cell, heap_seq, child, parent):
            break
        heap_cell[child], heap_cell[parent] = heap_cell[parent], heap_cell[child]
        heap_seq[child], heap_seq[parent] = heap_seq[parent], heap_seq[child]
        child = parent
    return size + 1


@njit(cache=True, nogil=True)
def _heap_pop(
    dem: npt.NDArray[np.floating],
    heap_cell: npt.NDArray[np.int64],
    heap_seq: npt.NDArray[np.int64],
    size: int,
) -> tuple[int, int]:
    """Pop the lowest cell off the heap; return (cell, new size)."""
    top = heap_cell[0]
    size -= 1
    heap_cell[0] = heap_cell[size]
    heap_seq[0] = heap_seq[size]
    parent = 0
    while True:
        left = 2 * parent + 1
        if left >= size:
            break
        smallest = left
        right = left + 1
        if right < size and _heap_less(dem, heap_cell, heap_seq, right, left):
            smallest = right
        if not _heap_less(dem, heap_cell, heap_seq, smallest, parent):
            break
        heap_cell[parent], heap_cell[smallest] = heap_cell[smallest], heap_cell[parent]
        heap_seq[parent], heap_seq[smallest] = heap_seq[smallest], heap_seq[parent]
        parent = smallest
    return top, size


# --------------------------------------------------------------------------------
# Kernels
# --------------------------------------------------------------------------------


@njit(cache=True, nogil=True)
def _priority_flood(
    dem: npt.NDArray[np.floating],
    valid: npt.NDArray[np.bool_],
    offsets: npt.NDArray[np.int64],
    rows: int,
    cols: int,
    epsilon: float,
) -> None:
    """Fill `dem` in place by priority-flood.

    `dem` and `valid` are flat, length `rows * cols`. `dem` is modified in place;
    invalid cells are never written.
    """
    n_cells = rows * cols
    n_off = offsets.shape[0]

    closed = np.zeros(n_cells, dtype=np.bool_)
    heap_cell = np.empty(n_cells, dtype=np.int64)
    heap_seq = np.empty(n_cells, dtype=np.int64)
    pit = np.empty(n_cells, dtype=np.int64)

    heap_size = 0
    pit_head = 0
    pit_tail = 0
    seq = 0

    # Seed: every valid cell on the raster border, or touching an invalid cell.
    for row in range(rows):
        for col in range(cols):
            cell = row * cols + col
            if not valid[cell]:
                continue
            is_seed = row == 0 or col == 0 or row == rows - 1 or col == cols - 1
            if not is_seed:
                for k in range(n_off):
                    n_row = row + offsets[k, 0]
                    n_col = col + offsets[k, 1]
                    if not valid[n_row * cols + n_col]:
                        is_seed = True
                        break
            if is_seed:
                closed[cell] = True
                heap_size = _heap_push(dem, heap_cell, heap_seq, heap_size, cell, seq)
                seq += 1

    while heap_size > 0 or pit_head < pit_tail:
        # Cells on the FIFO were raised to an already-popped elevation, so they can
        # never be higher than the heap top. Drain the FIFO first — that is the whole
        # point of the optimisation — except on an exact tie, where Barnes takes the
        # heap so equal-elevation cells outside the current depression are not starved.
        if pit_head >= pit_tail:
            take_pit = False
        elif heap_size == 0:
            take_pit = True
        else:
            take_pit = dem[pit[pit_head]] != dem[heap_cell[0]]

        if take_pit:
            cell = pit[pit_head]
            pit_head += 1
        else:
            cell, heap_size = _heap_pop(dem, heap_cell, heap_seq, heap_size)

        row = cell // cols
        col = cell - row * cols
        z = dem[cell]

        for k in range(n_off):
            n_row = row + offsets[k, 0]
            n_col = col + offsets[k, 1]
            if n_row < 0 or n_row >= rows or n_col < 0 or n_col >= cols:
                continue
            neighbour = n_row * cols + n_col
            if closed[neighbour] or not valid[neighbour]:
                continue
            closed[neighbour] = True
            floor = z + epsilon
            if dem[neighbour] <= floor:
                dem[neighbour] = floor
                pit[pit_tail] = neighbour
                pit_tail += 1
            else:
                heap_size = _heap_push(dem, heap_cell, heap_seq, heap_size, neighbour, seq)
                seq += 1


@njit(cache=True, nogil=True)
def _flood_uphill(
    dem: npt.NDArray[np.floating],
    valid: npt.NDArray[np.bool_],
    offsets: npt.NDArray[np.int64],
    rows: int,
    cols: int,
) -> npt.NDArray[np.bool_]:
    """Return a flat mask of cells with a non-increasing path to the edge of the data.

    A breadth-first search that starts at every seed cell and only ever steps to a
    neighbour at the same elevation or higher. Walking that path backwards is, by
    construction, a monotone non-increasing route out — which is exactly the
    property depression filling is supposed to establish, established by a
    completely different traversal from the one that establishes it.
    """
    n_cells = rows * cols
    n_off = offsets.shape[0]
    drained = np.zeros(n_cells, dtype=np.bool_)
    queue = np.empty(n_cells, dtype=np.int64)
    head = 0
    tail = 0

    for row in range(rows):
        for col in range(cols):
            cell = row * cols + col
            if not valid[cell]:
                continue
            is_seed = row == 0 or col == 0 or row == rows - 1 or col == cols - 1
            if not is_seed:
                for k in range(n_off):
                    if not valid[(row + offsets[k, 0]) * cols + (col + offsets[k, 1])]:
                        is_seed = True
                        break
            if is_seed:
                drained[cell] = True
                queue[tail] = cell
                tail += 1

    while head < tail:
        cell = queue[head]
        head += 1
        row = cell // cols
        col = cell - row * cols
        z = dem[cell]
        for k in range(n_off):
            n_row = row + offsets[k, 0]
            n_col = col + offsets[k, 1]
            if n_row < 0 or n_row >= rows or n_col < 0 or n_col >= cols:
                continue
            neighbour = n_row * cols + n_col
            if drained[neighbour] or not valid[neighbour]:
                continue
            if dem[neighbour] >= z:
                drained[neighbour] = True
                queue[tail] = neighbour
                tail += 1

    return drained


# --------------------------------------------------------------------------------
# Public API
# --------------------------------------------------------------------------------


def _resolve_terrain(config: Config | TerrainConfig | None) -> TerrainConfig:
    """Return the `TerrainConfig` to use, defaulting when nothing is supplied."""
    if isinstance(config, Config):
        return config.terrain
    return config if config is not None else TerrainConfig()


def _prepare(
    dem: npt.NDArray[np.floating],
    nodata: float | None,
) -> tuple[npt.NDArray[np.floating], npt.NDArray[np.bool_]]:
    """Return a flat working copy of `dem` and a flat validity mask.

    NaN always means nodata. `nodata`, if given, means it too.
    """
    if dem.ndim != 2:
        raise ValueError(f"dem must be 2-D, got shape {dem.shape}")
    if not np.issubdtype(dem.dtype, np.floating):
        raise TypeError(f"dem must be a floating dtype, got {dem.dtype}")
    if min(dem.shape) < 1:
        raise ValueError(f"dem must be non-empty, got shape {dem.shape}")

    work = np.ascontiguousarray(dem).ravel().copy()
    valid = np.isfinite(work)
    if nodata is not None and np.isfinite(nodata):
        valid &= work != nodata
    return work, valid


def _check_epsilon(epsilon: float, dem: npt.NDArray[np.floating]) -> None:
    """Reject an epsilon that the DEM's dtype cannot actually represent."""
    if epsilon < 0:
        raise ValueError(f"fill_epsilon must be non-negative, got {epsilon}")
    if epsilon == 0:
        return
    finite = dem[np.isfinite(dem)]
    if finite.size == 0:
        return
    peak = np.abs(finite).max()
    if peak + dem.dtype.type(epsilon) == peak:
        raise ValueError(
            f"fill_epsilon={epsilon} vanishes at elevation {peak} in {dem.dtype}: "
            "every step along a flat would be a no-op. Use float64, or a larger epsilon."
        )


@overload
def fill_depressions(
    dem: npt.NDArray[np.floating],
    *,
    config: Config | TerrainConfig | None = ...,
    nodata: float | None = ...,
    epsilon: float | None = ...,
    connectivity: Connectivity | None = ...,
    return_raised: Literal[False] = ...,
) -> npt.NDArray[np.floating]: ...


@overload
def fill_depressions(
    dem: npt.NDArray[np.floating],
    *,
    config: Config | TerrainConfig | None = ...,
    nodata: float | None = ...,
    epsilon: float | None = ...,
    connectivity: Connectivity | None = ...,
    return_raised: Literal[True],
) -> tuple[npt.NDArray[np.floating], int]: ...


def fill_depressions(
    dem: npt.NDArray[np.floating],
    *,
    config: Config | TerrainConfig | None = None,
    nodata: float | None = None,
    epsilon: float | None = None,
    connectivity: Connectivity | None = None,
    return_raised: bool = False,
) -> npt.NDArray[np.floating] | tuple[npt.NDArray[np.floating], int]:
    """Fill the depressions in `dem` so every cell drains to the edge of the data.

    Parameters
    ----------
    dem
        2-D floating-point elevation array. NaN marks nodata.
    config
        Source of `fill_epsilon` and `fill_connectivity`. Explicit `epsilon` and
        `connectivity` arguments override it; nothing here is a bare literal.
    nodata
        An additional sentinel value treated as nodata, on top of NaN.
    epsilon
        Per-step increment applied along filled surfaces so they drain rather than
        becoming flats. Overrides `config.fill_epsilon`.
    connectivity
        Neighbourhood. Overrides `config.fill_connectivity`.
    return_raised
        Also return the number of cells the fill raised.

    Returns
    -------
    ndarray
        Filled DEM, same shape and dtype as `dem`. Nodata cells are returned
        unchanged. If `return_raised`, a `(filled, n_raised)` tuple.

    Notes
    -----
    The algorithm is global: it holds the whole grid plus about 25 bytes per cell
    of scratch, and cannot be tiled without a merge step across tile boundaries.
    """
    terrain = _resolve_terrain(config)
    eps = terrain.fill_epsilon if epsilon is None else epsilon
    conn = terrain.fill_connectivity if connectivity is None else connectivity

    work, valid = _prepare(dem, nodata)
    _check_epsilon(eps, work)

    rows, cols = dem.shape
    _priority_flood(work, valid, neighbour_offsets(conn), rows, cols, float(work.dtype.type(eps)))

    # Invalid cells are never written, so they come back bit-identical.
    filled: npt.NDArray[np.floating] = work.reshape(rows, cols)
    if not return_raised:
        return filled
    raised = int(
        np.count_nonzero(filled[valid.reshape(rows, cols)] > dem[valid.reshape(rows, cols)])
    )
    return filled, raised


def undrained_mask(
    dem: npt.NDArray[np.floating],
    *,
    config: Config | TerrainConfig | None = None,
    nodata: float | None = None,
    connectivity: Connectivity | None = None,
) -> npt.NDArray[np.bool_]:
    """Return a mask of valid cells with **no** monotone non-increasing path to the edge.

    An all-False result is the definition of "this DEM has no depressions left".
    Nodata cells are False: they are outside the domain, not undrained.

    Parameters
    ----------
    dem
        2-D floating-point elevation array. NaN marks nodata.
    config
        Source of `fill_connectivity`.
    nodata
        An additional sentinel value treated as nodata, on top of NaN.
    connectivity
        Neighbourhood. Overrides `config.fill_connectivity`.

    Returns
    -------
    ndarray of bool
        Same shape as `dem`.
    """
    terrain = _resolve_terrain(config)
    conn = terrain.fill_connectivity if connectivity is None else connectivity

    work, valid = _prepare(dem, nodata)
    rows, cols = dem.shape
    drained = _flood_uphill(work, valid, neighbour_offsets(conn), rows, cols)
    return (valid & ~drained).reshape(rows, cols)
