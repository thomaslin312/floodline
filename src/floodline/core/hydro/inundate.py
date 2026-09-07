"""HAND stage to an inundation extent and depth raster.

The model in one line: a cell is wet when its height above the nearest drainage is
below the water depth in that drainage, and the depth there is the difference.

    depth = stage - HAND   where positive, 0 elsewhere

Two filters sit on top, both configurable, both there because the bare threshold
produces artefacts a reader would otherwise mistake for results:

* `min_depth_m` — a cell a centimetre under water is not usefully "flooded", and at
  1 m lidar resolution it is inside the DEM's own vertical error. Below the
  threshold, cells are dry.
* `require_connectivity` — the bare threshold wets every low-lying hollow whose
  HAND happens to be small, including ones separated from the river by higher
  ground. Water cannot teleport, so wet regions with no path to a stream cell are
  dropped. This is the filter that stops the map showing flooded paddocks a
  kilometre from the channel.

What this does not represent: HAND assumes the water surface is parallel to the
drainage line. There is no backwater, no levee, no culvert and no routing across a
catchment divide. The connectivity filter is a topological check on the *result*,
not a hydraulic one — it removes disconnected water but cannot add water that
should have arrived by a route HAND does not know about.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import numpy.typing as npt
from numba import njit

from floodline.core.config import Config, Connectivity, HydraulicsConfig
from floodline.core.terrain._neighbours import neighbour_offsets

__all__ = ["Inundation", "inundate"]


@dataclass(frozen=True, slots=True)
class Inundation:
    """An inundation extent and its depth raster."""

    depth: npt.NDArray[np.float64]
    """Water depth in metres. Zero where dry, NaN where HAND is undefined."""

    wet: npt.NDArray[np.bool_]
    """True where the cell is inundated at or above `min_depth_m`."""

    n_wet: int
    """Number of inundated cells."""

    n_removed_by_connectivity: int
    """Cells that passed the depth threshold but had no path to a stream."""

    cell_area_m2: float
    """Area of one cell, so `area_m2` means something."""

    @property
    def area_m2(self) -> float:
        """Total inundated area."""
        return self.n_wet * self.cell_area_m2

    @property
    def max_depth_m(self) -> float:
        """Deepest inundated cell, or 0.0 if nothing is wet."""
        return float(self.depth[self.wet].max()) if self.n_wet else 0.0


@njit(cache=True, nogil=True)
def _connected_to_streams(
    wet: npt.NDArray[np.bool_],
    streams: npt.NDArray[np.bool_],
    offsets: npt.NDArray[np.int64],
    rows: int,
    cols: int,
) -> npt.NDArray[np.bool_]:
    """Return the wet cells reachable from a wet stream cell through wet cells."""
    n_cells = rows * cols
    n_off = offsets.shape[0]
    reached = np.zeros(n_cells, dtype=np.bool_)
    queue = np.empty(n_cells, dtype=np.int64)
    head = 0
    tail = 0

    for cell in range(n_cells):
        if wet[cell] and streams[cell]:
            reached[cell] = True
            queue[tail] = cell
            tail += 1

    while head < tail:
        cell = queue[head]
        head += 1
        row = cell // cols
        col = cell - row * cols
        for k in range(n_off):
            n_row = row + offsets[k, 0]
            n_col = col + offsets[k, 1]
            if n_row < 0 or n_row >= rows or n_col < 0 or n_col >= cols:
                continue
            neighbour = n_row * cols + n_col
            if reached[neighbour] or not wet[neighbour]:
                continue
            reached[neighbour] = True
            queue[tail] = neighbour
            tail += 1

    return reached


def _resolve(config: Config | HydraulicsConfig | None) -> HydraulicsConfig:
    """Return the `HydraulicsConfig` to use, defaulting when nothing is supplied."""
    if isinstance(config, Config):
        return config.hydraulics
    return config if config is not None else HydraulicsConfig()


def inundate(
    hand: npt.NDArray[np.floating],
    stage: npt.NDArray[np.floating] | float,
    *,
    streams: npt.NDArray[np.bool_] | None = None,
    config: Config | HydraulicsConfig | None = None,
    cell_area_m2: float = 1.0,
    min_depth: float | None = None,
    require_connectivity: bool | None = None,
    connectivity: Connectivity | None = None,
) -> Inundation:
    """Flood a HAND raster to a given stage.

    Parameters
    ----------
    hand
        Height above nearest drainage. NaN where a cell has no drainage; those
        cells are never wet, because the model has nothing to say about them.
    stage
        Water depth in the drainage, either a scalar or a per-cell field from
        `hydraulics.stage`.
    streams
        Channel mask. Required when the connectivity filter is on, since "connected
        to what" is otherwise undefined.
    config
        Source of `min_depth_m`, `require_connectivity` and `connectivity`.
    cell_area_m2
        Area of one cell, used for `Inundation.area_m2`.
    min_depth, require_connectivity, connectivity
        Per-call overrides of the corresponding config fields.

    Returns
    -------
    Inundation
    """
    hydraulics = _resolve(config)
    floor = hydraulics.min_depth_m if min_depth is None else min_depth
    connect = (
        hydraulics.require_connectivity if require_connectivity is None else require_connectivity
    )
    neighbourhood = hydraulics.connectivity if connectivity is None else connectivity

    stage_field = np.asarray(stage, dtype=np.float64)
    if stage_field.ndim == 0:
        stage_field = np.full(hand.shape, float(stage_field), dtype=np.float64)
    elif stage_field.shape != hand.shape:
        raise ValueError(f"stage shape {stage_field.shape} does not match hand {hand.shape}")
    if np.any(stage_field < 0):
        raise ValueError("stage must be non-negative everywhere")

    heights = np.asarray(hand, dtype=np.float64)
    defined = np.isfinite(heights)

    depth = np.full(heights.shape, np.nan, dtype=np.float64)
    depth[defined] = np.maximum(stage_field[defined] - heights[defined], 0.0)

    wet = defined & (depth >= floor) & (depth > 0.0)
    removed = 0

    if connect:
        if streams is None:
            raise ValueError(
                "require_connectivity is on but no stream mask was given; "
                "'connected to a stream' has no meaning without one"
            )
        if streams.shape != hand.shape:
            raise ValueError(f"streams shape {streams.shape} does not match hand {hand.shape}")
        rows, cols = hand.shape
        reached = _connected_to_streams(
            np.ascontiguousarray(wet).ravel(),
            np.ascontiguousarray(streams).ravel(),
            neighbour_offsets(neighbourhood),
            rows,
            cols,
        ).reshape(rows, cols)
        removed = int((wet & ~reached).sum())
        wet = wet & reached

    depth[defined & ~wet] = 0.0

    return Inundation(
        depth=depth,
        wet=wet,
        n_wet=int(wet.sum()),
        n_removed_by_connectivity=removed,
        cell_area_m2=cell_area_m2,
    )
