"""Put back the channel a lidar DEM cannot see.

Airborne lidar does not penetrate water. A 3DEP DEM records the water surface on the
day of the flight, so every channel in it is a lid: the cross-section stops at whatever
was flowing that morning and the volume underneath is missing. HAND is measured from
that lid, the synthetic rating curve is built on it, and the consequence is a channel
with too little conveyance. Flow that should have stayed between the banks is pushed
overbank instead, and the model floods too readily at low stage.

The correction is to burn an estimated bed back in before HAND is computed. Depth comes
from downstream hydraulic geometry - `d = c * A^e` on upstream drainage area - which is
a regional average, not a survey. It is right across many reaches and wrong on any
particular one, which is the same bargain the rest of this model makes.

Order matters. The burn has to happen after the stream network is known, because it
needs drainage area, and before HAND, because HAND is measured to the bed. It does not
re-run the fill: deepening cells that already drain cannot create a new depression along
the channel, since the burn increases monotonically downstream with drainage area.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import numpy.typing as npt

from floodline.core.config import BathymetryConfig, Config

__all__ = ["BurnedChannel", "burn_channel", "channel_depth_m", "channel_width_m"]


def _settings(config: Config | BathymetryConfig | None) -> BathymetryConfig:
    if isinstance(config, Config):
        return config.bathymetry
    return config or BathymetryConfig()


def channel_depth_m(
    drainage_area_km2: npt.ArrayLike, *, config: Config | BathymetryConfig | None = None
) -> npt.NDArray[np.float64]:
    """Bankfull mean depth from upstream drainage area, in metres.

    `d = coefficient * A^exponent`, capped at `max_depth_m`. A power law has no upper
    bound, and the Mississippi would otherwise have a canyon burned into it.

    Zero and negative areas return zero rather than raising: a cell with no upstream
    contribution is not a channel, and callers pass whole arrays including hillslope.
    """
    settings = _settings(config)
    area = np.asarray(drainage_area_km2, dtype=np.float64)
    depth = np.where(
        area > 0.0,
        settings.depth_coefficient_m * np.power(np.maximum(area, 1e-9), settings.depth_exponent),
        0.0,
    )
    return np.minimum(depth, settings.max_depth_m)


def channel_width_m(
    drainage_area_km2: npt.ArrayLike, *, config: Config | BathymetryConfig | None = None
) -> npt.NDArray[np.float64]:
    """Bankfull width from upstream drainage area, in metres.

    Only used to decide how many cells across the burn should run. At 30 m almost every
    channel in a screening-sized watershed is narrower than one cell, so this changes
    nothing until the raster is fine or the river is large.
    """
    settings = _settings(config)
    area = np.asarray(drainage_area_km2, dtype=np.float64)
    return np.where(
        area > 0.0,
        settings.width_coefficient_m * np.power(np.maximum(area, 1e-9), settings.width_exponent),
        0.0,
    )


@dataclass(frozen=True, slots=True)
class BurnedChannel:
    """A DEM with an estimated channel bed cut into it, and the depths that were cut."""

    dem: npt.NDArray[np.floating]
    depth_m: npt.NDArray[np.float64]
    """Metres removed at each cell. Zero everywhere off the burned channel."""

    n_cells: int
    max_depth_m: float
    mean_depth_m: float


def burn_channel(
    dem: npt.NDArray[np.floating],
    accumulation: npt.NDArray[np.floating],
    streams: npt.NDArray[np.bool_],
    *,
    cell_area_m2: float,
    cellsize_m: float,
    config: Config | BathymetryConfig | None = None,
) -> BurnedChannel:
    """Lower the DEM along the stream network by an estimated bankfull depth.

    Parameters
    ----------
    dem
        The conditioned surface, after filling. Burned out of place; the input is not
        modified.
    accumulation
        Upstream cell count per cell, from `flow_accumulation`.
    streams
        The extracted channel mask. Only these cells are burned, plus their neighbours
        where the estimated width exceeds one cell.
    cell_area_m2, cellsize_m
        Cell geometry, for turning an accumulated cell count into an area in km2 and
        for deciding how wide the burn is in cells.
    config
        Supplies the width and depth relations and the depth ceiling.

    Returns
    -------
    BurnedChannel
        The burned surface and what was removed. When `enabled` is False the surface
        comes back untouched, so a caller can always run this and let config decide.
    """
    settings = _settings(config)
    depth = np.zeros(dem.shape, dtype=np.float64)
    if not settings.enabled or not streams.any():
        return BurnedChannel(dem=dem, depth_m=depth, n_cells=0, max_depth_m=0.0, mean_depth_m=0.0)

    cells = np.asarray(accumulation, dtype=np.float64)
    area_km2 = np.where(streams, cells * cell_area_m2 / 1e6, 0.0)
    depth[streams] = channel_depth_m(area_km2[streams], config=settings)

    # Widen where the estimated channel is wider than a cell. A one-cell burn on a
    # 200 m river at 30 m would cut a slot rather than a channel, and HAND downstream
    # of it would then be measured to a bed the flow could never occupy.
    width = np.zeros(dem.shape, dtype=np.float64)
    width[streams] = channel_width_m(area_km2[streams], config=settings)
    extra = int(np.floor((np.max(width, initial=0.0) / max(cellsize_m, 1e-9) - 1.0) / 2.0))
    for _ in range(max(extra, 0)):
        # Dilate by one ring, keeping each cell's own depth, so the channel widens at
        # the depth its own drainage area implies rather than its neighbour's.
        grown = depth.copy()
        for shift, axis in ((1, 0), (-1, 0), (1, 1), (-1, 1)):
            moved = np.roll(depth, shift, axis=axis)
            # np.roll wraps; blank the wrapped edge so depth cannot cross the raster.
            index: list[slice | int] = [slice(None), slice(None)]
            index[axis] = 0 if shift > 0 else -1
            moved[tuple(index)] = 0.0
            grown = np.maximum(grown, moved)
        # Only widen where the local width actually calls for it.
        allow = np.maximum.reduce(
            [np.roll(width, s, axis=a) for s, a in ((1, 0), (-1, 0), (1, 1), (-1, 1))]
        )
        depth = np.where(allow > cellsize_m, grown, depth)

    burned = np.asarray(dem, dtype=np.float64) - depth
    # Nodata stays nodata. Subtracting from NaN is already NaN, but an explicit mask
    # keeps a sentinel-carrying DEM from being quietly turned into a real elevation.
    burned = np.where(np.isfinite(dem), burned, dem)
    cut = depth[depth > 0.0]
    return BurnedChannel(
        dem=burned,
        depth_m=depth,
        n_cells=int(cut.size),
        max_depth_m=float(cut.max()) if cut.size else 0.0,
        mean_depth_m=float(cut.mean()) if cut.size else 0.0,
    )
