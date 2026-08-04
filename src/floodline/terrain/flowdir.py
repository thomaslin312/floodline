"""D8 flow direction (O'Callaghan & Mark 1984) on a conditioned DEM.

Each cell is given a single pointer to the neighbour it drains into: the one with
the steepest downhill *slope*, drop divided by centre-to-centre distance, so that
a diagonal neighbour has to be sqrt(2) times further down to beat a cardinal one,
and so that anisotropic cells (dx != dy) are ranked correctly.

Codes are the ESRI convention, which is what QGIS and ArcGIS expect to read::

    32  64  128
    16   *    1
     8   4    2

with three negative sentinels for cells that have no downstream neighbour.

Preconditions
-------------
This expects a DEM that has already been through `terrain.fill`. On a raw DEM,
every pit becomes a `FLAT` cell and flow routing dead-ends there.

Filling with `fill_epsilon = 0` leaves the filled depressions perfectly flat, and
a cell in the middle of a flat has no strictly lower neighbour at all — D8 is
undefined there, and this module says so with `FLAT` rather than inventing a
direction. Filling with `fill_epsilon > 0` gives those surfaces a gradient, and
then every non-edge cell has exactly one downstream neighbour. Resolving flats on
an epsilon-free fill (Barnes et al. 2014b) is a separate piece of work; until it
exists, epsilon filling is the supported route to a flat-free surface.
"""

from __future__ import annotations

import numpy as np
import numpy.typing as npt
from numba import njit

from floodline.config import Config, FlowDirMethod, TerrainConfig

__all__ = [
    "D8_CODES",
    "FLOW_FLAT",
    "FLOW_NODATA",
    "FLOW_OUTLET",
    "downstream_index",
    "flow_direction",
    "steps_to_outlet",
]

FLOW_NODATA = 0
"""The cell itself is nodata. Matches the ESRI convention that 0 means undefined."""

FLOW_OUTLET = -1
"""The cell drains off the edge of the data: off the raster, or into nodata."""

FLOW_FLAT = -2
"""Interior cell with no strictly lower neighbour. D8 is undefined here."""

# Neighbours in ascending ESRI code order. The order is the tie-break rule: when
# two neighbours offer the same steepest slope, the first one in this list wins,
# i.e. the lowest direction code wins. Stated here rather than left to fall out
# of the loop, because a tie-break that is not written down is a tie-break that
# silently changes when someone reorders the array.
#
# Names assume a north-up raster (row increases southward), which is what the
# raster reader guarantees; the arithmetic itself is purely row/column.
D8_CODES: npt.NDArray[np.int16] = np.array([1, 2, 4, 8, 16, 32, 64, 128], dtype=np.int16)
"""E, SE, S, SW, W, NW, N, NE."""

_D8_DROW: npt.NDArray[np.int64] = np.array([0, 1, 1, 1, 0, -1, -1, -1], dtype=np.int64)
_D8_DCOL: npt.NDArray[np.int64] = np.array([1, 1, 0, -1, -1, -1, 0, 1], dtype=np.int64)
_D8_DIAGONAL: npt.NDArray[np.bool_] = np.array(
    [False, True, False, True, False, True, False, True], dtype=np.bool_
)


def _step_distances(cellsize: tuple[float, float]) -> npt.NDArray[np.float64]:
    """Return centre-to-centre distance to each D8 neighbour, in CRS units."""
    x_size, y_size = cellsize
    if x_size <= 0 or y_size <= 0:
        raise ValueError(f"cellsize must be positive, got {cellsize}")
    diagonal = float(np.hypot(x_size, y_size))
    return np.array(
        [
            x_size,  # E
            diagonal,  # SE
            y_size,  # S
            diagonal,  # SW
            x_size,  # W
            diagonal,  # NW
            y_size,  # N
            diagonal,  # NE
        ],
        dtype=np.float64,
    )


@njit(cache=True, nogil=True)
def _d8_flow_direction(
    dem: npt.NDArray[np.floating],
    valid: npt.NDArray[np.bool_],
    rows: int,
    cols: int,
    codes: npt.NDArray[np.int16],
    drow: npt.NDArray[np.int64],
    dcol: npt.NDArray[np.int64],
    distances: npt.NDArray[np.float64],
) -> npt.NDArray[np.int16]:
    """Return flat D8 direction codes for `dem`. Pure: flat arrays in, flat array out."""
    n_cells = rows * cols
    out = np.zeros(n_cells, dtype=np.int16)

    for row in range(rows):
        for col in range(cols):
            cell = row * cols + col
            if not valid[cell]:
                out[cell] = FLOW_NODATA
                continue

            z = dem[cell]
            best_slope = 0.0
            best_code = np.int16(0)
            touches_outside = False

            for k in range(codes.shape[0]):
                n_row = row + drow[k]
                n_col = col + dcol[k]
                if n_row < 0 or n_row >= rows or n_col < 0 or n_col >= cols:
                    touches_outside = True
                    continue
                neighbour = n_row * cols + n_col
                if not valid[neighbour]:
                    touches_outside = True
                    continue
                slope = (z - dem[neighbour]) / distances[k]
                # Strictly greater, so the earliest neighbour in code order keeps
                # a tie, and so a zero slope never counts as downhill.
                if slope > best_slope:
                    best_slope = slope
                    best_code = codes[k]

            if best_code != 0:
                out[cell] = best_code
            elif touches_outside:
                out[cell] = FLOW_OUTLET
            else:
                out[cell] = FLOW_FLAT

    return out


@njit(cache=True, nogil=True)
def _downstream_index(
    flowdir: npt.NDArray[np.int16],
    rows: int,
    cols: int,
    codes: npt.NDArray[np.int16],
    drow: npt.NDArray[np.int64],
    dcol: npt.NDArray[np.int64],
) -> npt.NDArray[np.int64]:
    """Return the flat index each cell drains into, or -1 where flow terminates.

    A direction pointing off the raster yields -1, the same as a terminating cell.
    `flow_direction` never emits one, but a hand-built or externally supplied
    direction grid can, and an unchecked index here becomes an out-of-bounds write
    in every caller. numba does not bounds-check by default, so that would corrupt
    memory silently under JIT while raising cleanly with NUMBA_DISABLE_JIT=1 --
    which is how it was found.
    """
    n_cells = rows * cols
    out = np.full(n_cells, -1, dtype=np.int64)
    for row in range(rows):
        for col in range(cols):
            cell = row * cols + col
            code = flowdir[cell]
            if code <= 0:
                continue
            for k in range(codes.shape[0]):
                if codes[k] == code:
                    n_row = row + drow[k]
                    n_col = col + dcol[k]
                    if 0 <= n_row < rows and 0 <= n_col < cols:
                        out[cell] = n_row * cols + n_col
                    break
    return out


@njit(cache=True, nogil=True)
def _steps_to_outlet(receiver: npt.NDArray[np.int64]) -> tuple[npt.NDArray[np.int64], int]:
    """Return steps from each cell to where flow terminates, and any cell on a cycle.

    Memoised, so the whole grid costs one pass rather than one walk per cell. The
    `state` array doubles as the cycle detector: meeting a cell that is still on
    the current walk means the pointers loop, which on a filled DEM would be a bug.
    """
    n_cells = receiver.shape[0]
    steps = np.full(n_cells, -1, dtype=np.int64)
    state = np.zeros(n_cells, dtype=np.uint8)  # 0 unvisited, 1 on this walk, 2 done
    path = np.empty(n_cells, dtype=np.int64)

    for start in range(n_cells):
        if state[start] != 0:
            continue
        depth = 0
        cell = start
        while True:
            if cell < 0:
                tail = 0
                break
            if state[cell] == 2:
                tail = steps[cell]
                break
            if state[cell] == 1:
                return steps, cell  # cycle
            state[cell] = 1
            path[depth] = cell
            depth += 1
            cell = receiver[cell]
        for i in range(depth - 1, -1, -1):
            tail += 1
            steps[path[i]] = tail
            state[path[i]] = 2

    return steps, -1


def _resolve_terrain(config: Config | TerrainConfig | None) -> TerrainConfig:
    """Return the `TerrainConfig` to use, defaulting when nothing is supplied."""
    if isinstance(config, Config):
        return config.terrain
    return config if config is not None else TerrainConfig()


def _prepare(
    dem: npt.NDArray[np.floating], nodata: float | None
) -> tuple[npt.NDArray[np.floating], npt.NDArray[np.bool_]]:
    """Return a flat view of `dem` and a flat validity mask. NaN always means nodata."""
    if dem.ndim != 2:
        raise ValueError(f"dem must be 2-D, got shape {dem.shape}")
    if not np.issubdtype(dem.dtype, np.floating):
        raise TypeError(f"dem must be a floating dtype, got {dem.dtype}")
    flat = np.ascontiguousarray(dem).ravel()
    valid = np.isfinite(flat)
    if nodata is not None and np.isfinite(nodata):
        valid = valid & (flat != nodata)
    return flat, valid


def flow_direction(
    dem: npt.NDArray[np.floating],
    *,
    config: Config | TerrainConfig | None = None,
    nodata: float | None = None,
    cellsize: tuple[float, float] = (1.0, 1.0),
) -> npt.NDArray[np.int16]:
    """Compute D8 flow direction for a conditioned DEM.

    Parameters
    ----------
    dem
        2-D floating-point elevation array, already depression-filled. NaN is nodata.
    config
        Source of `flowdir_method`. D-infinity is not implemented.
    nodata
        An additional sentinel treated as nodata, on top of NaN.
    cellsize
        (x, y) cell size in CRS units. Pass `Raster.cellsize`; the default of
        (1, 1) only makes diagonal-versus-cardinal ranking correct on square cells.

    Returns
    -------
    ndarray of int16
        ESRI direction codes, with `FLOW_NODATA` (0) at nodata cells,
        `FLOW_OUTLET` (-1) where flow leaves the data, and `FLOW_FLAT` (-2) at
        interior cells with no strictly lower neighbour.
    """
    terrain = _resolve_terrain(config)
    if terrain.flowdir_method is not FlowDirMethod.D8:
        raise NotImplementedError(
            f"flowdir_method={terrain.flowdir_method.value!r} is not implemented; "
            "D-infinity is a stretch goal. Set terrain.flowdir_method to 'd8'."
        )

    flat, valid = _prepare(dem, nodata)
    rows, cols = dem.shape
    return _d8_flow_direction(
        flat,
        valid,
        rows,
        cols,
        D8_CODES,
        _D8_DROW,
        _D8_DCOL,
        _step_distances(cellsize),
    ).reshape(rows, cols)


def downstream_index(flowdir: npt.NDArray[np.int16]) -> npt.NDArray[np.int64]:
    """Return, for each cell, the flat index of the cell it drains into.

    Cells where flow terminates — nodata, outlets and flats — get -1. This is the
    form flow accumulation and HAND consume; the direction codes are the form that
    gets written to a raster.
    """
    if flowdir.ndim != 2:
        raise ValueError(f"flowdir must be 2-D, got shape {flowdir.shape}")
    rows, cols = flowdir.shape
    codes = np.ascontiguousarray(flowdir, dtype=np.int16).ravel()
    return _downstream_index(codes, rows, cols, D8_CODES, _D8_DROW, _D8_DCOL).reshape(rows, cols)


def steps_to_outlet(flowdir: npt.NDArray[np.int16]) -> npt.NDArray[np.int64]:
    """Return the number of D8 steps from each cell to where its flow terminates.

    A terminating cell (nodata, outlet or flat) scores 0. Following the pointers
    from any cell therefore reaches the edge of the data in a finite number of
    steps — which is the no-cycles invariant, measured rather than assumed.

    Raises
    ------
    ValueError
        If the pointers contain a cycle. On a filled DEM that is a bug, not a
        condition a caller should be expected to handle.
    """
    receiver = downstream_index(flowdir)
    rows, cols = receiver.shape
    steps, cycle_cell = _steps_to_outlet(np.ascontiguousarray(receiver).ravel())
    if cycle_cell >= 0:
        raise ValueError(
            f"flow directions contain a cycle at row {cycle_cell // cols}, col {cycle_cell % cols}"
        )
    return steps.reshape(rows, cols)
