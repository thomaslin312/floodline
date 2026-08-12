"""Building footprints against the depth raster: one water depth per building.

Reducing a footprint's worth of cells to a single number is the step where most of
the exposure error is decided, so the statistic is configurable and the default is
deliberate. `p90` - the 90th percentile of depth under the footprint - is the
default because `max` is set by whichever single cell the DEM happens to have dug
lowest, and `centroid` misses buildings whose centre sits on a locally high cell
while the rest of the slab is under water. `p90` keeps most of max's sensitivity
without letting one cell decide.

Two corrections separate "water depth on the ground" from "water depth in the
building", and both matter more than the choice of statistic at shallow depths:

* `floor_height_m` - finished floor level sits above ground. Water below it wets the
  slab, not the contents, and every depth-damage curve is defined above floor level.
  Depth in the building is `ground depth - floor_height_m`, floored at zero.
* `min_building_area_m2` - footprint databases carry sheds, awnings and digitising
  noise. Anything smaller is dropped rather than priced as a dwelling.

What this cannot do: it has no idea whether a building has a basement, is on piers,
or has been retrofitted. Overture carries height often enough to be useful and
storey counts rarely, so `default_storeys` fills the gap and the Monte Carlo does
not sample it - meaning floor area is a stated assumption, not an estimate.
"""

from __future__ import annotations

from dataclasses import dataclass

import geopandas as gpd
import numpy as np
import numpy.typing as npt
from rasterio.features import geometry_mask
from rasterio.transform import Affine, rowcol

from floodline.config import BuildingDepthStat, Config, ExposureConfig

__all__ = ["BuildingExposure", "building_depths"]


@dataclass(frozen=True, slots=True)
class BuildingExposure:
    """Per-building depths, and what was dropped on the way."""

    buildings: gpd.GeoDataFrame
    """One row per retained footprint, with `depth_m`, `floor_depth_m`, `area_m2`,
    `floor_area_m2` and `building_class` columns added."""

    n_input: int
    n_dropped_small: int
    """Footprints below `min_building_area_m2`."""

    n_outside_raster: int
    """Footprints whose geometry does not overlap the depth raster at all."""

    has_margin: bool
    """True when `floor_margin_m` came from an unclamped depth field and so carries
    how far *below* the floor the water stopped. Without it the Monte Carlo cannot
    tell a building missed by a centimetre from one missed by five metres."""

    @property
    def n_inundated(self) -> int:
        """Buildings with water above finished floor level."""
        return int((self.buildings["floor_depth_m"] > 0.0).sum())

    @property
    def n_wet_ground(self) -> int:
        """Buildings with water on the ground, whether or not it reached the floor."""
        return int((self.buildings["depth_m"] > 0.0).sum())


def _resolve(config: Config | ExposureConfig | None) -> ExposureConfig:
    """Return the `ExposureConfig` to use, defaulting when nothing is supplied."""
    if isinstance(config, Config):
        return config.exposure
    return config if config is not None else ExposureConfig()


def _reduce(values: npt.NDArray[np.float64], stat: BuildingDepthStat) -> float:
    """Reduce the depths under one footprint to a single depth."""
    if values.size == 0:
        return 0.0
    if not np.any(np.isfinite(values)):
        return float(values[0])
    if stat is BuildingDepthStat.MAX:
        return float(values.max())
    if stat is BuildingDepthStat.MEAN:
        return float(values.mean())
    if stat is BuildingDepthStat.P90:
        return float(np.percentile(values, 90))
    raise ValueError(f"{stat} is not reduced from a cell sample")


def building_depths(
    depth: npt.NDArray[np.floating],
    transform: Affine,
    buildings: gpd.GeoDataFrame,
    *,
    unclamped_depth: npt.NDArray[np.floating] | None = None,
    config: Config | ExposureConfig | None = None,
    storeys_column: str = "num_floors",
    height_column: str = "height",
    class_column: str = "building_class",
    default_class: str = "residential",
) -> BuildingExposure:
    """Attach a water depth to every building footprint.

    Parameters
    ----------
    depth
        Depth raster in metres, as produced by `hydraulics.inundate`. NaN is treated
        as dry, since NaN there means HAND was undefined rather than deep.
    transform
        The raster's affine transform. The footprints must already be in the same
        CRS; this function does not reproject, because a silent reprojection is how
        an exposure table ends up describing the wrong continent.
    unclamped_depth
        `stage - HAND` before the floor at zero, so dry ground carries a negative
        value. Optional, and worth supplying: the depth raster records every dry
        building as exactly 0, which loses the difference between a building the
        water missed by a centimetre and one it missed by five metres. The Monte
        Carlo needs that difference, because perturbing a value clamped at zero can
        only ever invent flooding.
    buildings
        Footprints. Any of `storeys_column`, `height_column` and `class_column` that
        are present are used; missing ones fall back to config defaults.

    Returns
    -------
    BuildingExposure
    """
    exposure = _resolve(config)
    n_input = len(buildings)

    if buildings.crs is None:
        raise ValueError("building footprints have no CRS; refusing to guess")

    grid = np.asarray(depth, dtype=np.float64)
    grid = np.where(np.isfinite(grid), grid, 0.0)
    rows, cols = grid.shape

    has_margin = unclamped_depth is not None
    if unclamped_depth is None:
        margin_grid = grid
    else:
        margin_grid = np.asarray(unclamped_depth, dtype=np.float64)
        if margin_grid.shape != grid.shape:
            raise ValueError(
                f"unclamped_depth shape {margin_grid.shape} does not match depth {grid.shape}"
            )
        # NaN means HAND was undefined, not that the ground is high; keep it dry but
        # finite so the reduction below has something to work with.
        margin_grid = np.where(np.isfinite(margin_grid), margin_grid, -np.inf)

    areas = buildings.geometry.area
    keep = areas >= exposure.min_building_area_m2
    n_dropped_small = int((~keep).sum())
    kept = buildings.loc[keep].copy()
    kept["area_m2"] = areas.loc[keep]

    stat = exposure.building_depth_stat
    depths: list[float] = []
    margins: list[float] = []
    outside = 0

    for geom in kept.geometry:
        left, bottom, right, top = geom.bounds
        r0, c0 = rowcol(transform, left, top, op=np.floor)
        r1, c1 = rowcol(transform, right, bottom, op=np.ceil)
        r0, r1 = max(int(r0), 0), min(int(r1) + 1, rows)
        c0, c1 = max(int(c0), 0), min(int(c1) + 1, cols)
        if r0 >= r1 or c0 >= c1:
            outside += 1
            depths.append(0.0)
            margins.append(-np.inf if has_margin else 0.0)
            continue

        window = grid[r0:r1, c0:c1]
        margin_window = margin_grid[r0:r1, c0:c1]
        if stat is BuildingDepthStat.CENTROID:
            point = geom.centroid
            row, col = rowcol(transform, point.x, point.y)
            inside = 0 <= int(row) < rows and 0 <= int(col) < cols
            depths.append(float(grid[int(row), int(col)]) if inside else 0.0)
            margins.append(float(margin_grid[int(row), int(col)]) if inside else -np.inf)
            continue

        # geometry_mask returns True *outside* the shape by default.
        covered = ~geometry_mask(
            [geom],
            out_shape=window.shape,
            transform=transform * Affine.translation(c0, r0),
            all_touched=True,
        )
        depths.append(_reduce(window[covered], stat))
        margins.append(_reduce(margin_window[covered], stat))

    kept["depth_m"] = np.asarray(depths, dtype=np.float64)
    # Depth-damage curves are defined above finished floor level, not above ground.
    kept["floor_depth_m"] = np.maximum(kept["depth_m"] - exposure.floor_height_m, 0.0)
    # Signed distance from the water surface to the finished floor. Negative means the
    # water stopped short, and how far short is what the Monte Carlo perturbs.
    kept["floor_margin_m"] = np.asarray(margins, dtype=np.float64) - exposure.floor_height_m

    storeys = _storeys(kept, exposure, storeys_column, height_column)
    kept["storeys"] = storeys
    kept["floor_area_m2"] = kept["area_m2"] * storeys

    if class_column in kept.columns:
        kept["building_class"] = kept[class_column].fillna(default_class).astype(str)
    else:
        kept["building_class"] = default_class

    return BuildingExposure(
        buildings=kept,
        n_input=n_input,
        n_dropped_small=n_dropped_small,
        n_outside_raster=outside,
        has_margin=has_margin,
    )


def _storeys(
    buildings: gpd.GeoDataFrame,
    exposure: ExposureConfig,
    storeys_column: str,
    height_column: str,
) -> npt.NDArray[np.float64]:
    """Return a storey count per building from whatever the footprints carry.

    Preference order is an explicit storey count, then height divided by a nominal
    3 m storey, then the configured default. Height is the common case in Overture
    and storey counts are rare, so most rows take the middle branch or the default.
    """
    n = len(buildings)
    out = np.full(n, float(exposure.default_storeys), dtype=np.float64)

    if height_column in buildings.columns:
        height = np.asarray(buildings[height_column], dtype=np.float64)
        usable = np.isfinite(height) & (height > 0)
        out[usable] = np.maximum(np.round(height[usable] / 3.0), 1.0)

    if storeys_column in buildings.columns:
        storeys = np.asarray(buildings[storeys_column], dtype=np.float64)
        usable = np.isfinite(storeys) & (storeys > 0)
        out[usable] = storeys[usable]

    return out
