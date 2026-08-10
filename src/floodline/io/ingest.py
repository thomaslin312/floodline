"""Turn fetched DEM tiles into one analysis-ready raster.

Downloaded tiles are not usable as they arrive. USGS 3DEP publishes in EPSG:4269,
a *geographic* CRS, so `read_raster` refuses them — correctly, because a cell size
in degrees makes every slope, area and distance in the terrain code wrong. This
module is the one place allowed to read a raster in whatever CRS it came in, and
its job is to hand back something the rest of the pipeline will accept.

Three steps:

1. **Choose a vintage.** 3DEP publishes several surveys of the same ground; Houston
   has 2018, 2024 and 2026 versions of the same tile footprint. Modelling a 2017
   flood on 2026 terrain would route water over land that did not exist yet, so the
   default picks the survey nearest the event. Tiles are grouped by footprint, not
   by filename, so the rule does not depend on a naming convention holding.
2. **Reproject and mosaic.** Each tile is opened as a `WarpedVRT` into the analysis
   CRS and merged, so the warp happens lazily per block instead of materialising
   every tile at full size first.
3. **Clip to the AOI.** The tiles cover far more ground than the case does — 13
   tiles spanning four degrees for an AOI of less than one — and the terrain core is
   memory-bound, so trimming early is what makes the run fit.

Vertical datum is *not* transformed. 3DEP elevations and USGS gauge datums are both
NAVD88, so they are already consistent; a case that mixes vertical datums would need
a step here that does not exist yet, and this docstring is the warning.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from pathlib import Path

import numpy as np
import numpy.typing as npt
import rasterio
from pyproj import CRS
from rasterio.crs import CRS as RioCRS
from rasterio.enums import Resampling
from rasterio.merge import merge
from rasterio.vrt import WarpedVRT
from rasterio.warp import transform_bounds

from floodline.config import Config, TileVintage
from floodline.io.raster import CrsError, Raster

__all__ = ["TileGroup", "estimate_cells", "ingest_dem", "select_tiles"]

_DATE_IN_NAME = re.compile(r"(?<!\d)(\d{8})(?!\d)")
_FOOTPRINT_PRECISION = 4
"""Decimal places used to group tiles by footprint. Enough to separate adjacent
tiles, loose enough that floating-point noise in the bounds does not split a group."""


@dataclass(frozen=True, slots=True)
class TileGroup:
    """Tiles covering the same ground, and the one chosen from them."""

    footprint: tuple[float, ...]
    chosen: Path
    rejected: tuple[Path, ...]
    chosen_date: date | None


def _tile_date(path: Path) -> date | None:
    """Return the survey date encoded in a 3DEP filename, if there is one."""
    match = _DATE_IN_NAME.search(path.stem)
    if match is None:
        return None
    try:
        return date(int(match.group(1)[:4]), int(match.group(1)[4:6]), int(match.group(1)[6:8]))
    except ValueError:
        return None


def select_tiles(paths: Sequence[Path], *, config: Config | None = None) -> list[TileGroup]:
    """Group tiles by footprint and pick one vintage from each group.

    Grouping is by the tile's actual bounds rather than by its name, so a change in
    USGS naming cannot silently turn one footprint into several.
    """
    resolved = config or Config()
    event = resolved.case.event_start
    strategy = resolved.case.dem_vintage

    groups: dict[tuple[float, ...], list[Path]] = {}
    for path in paths:
        with rasterio.open(path) as src:
            key = tuple(round(v, _FOOTPRINT_PRECISION) for v in src.bounds)
        groups.setdefault(key, []).append(path)

    selected: list[TileGroup] = []
    for footprint, members in sorted(groups.items()):
        dated = [(p, _tile_date(p)) for p in members]
        known = [(p, d) for p, d in dated if d is not None]

        if not known:
            # No date to choose on; take the first by name so the result is at least
            # deterministic, and say so by leaving chosen_date None.
            ordered = sorted(members)
            chosen, chosen_date = ordered[0], None
        elif strategy is TileVintage.NEWEST:
            chosen, chosen_date = max(known, key=lambda item: item[1])
        else:
            chosen, chosen_date = min(known, key=lambda item: abs((item[1] - event).days))

        selected.append(
            TileGroup(
                footprint=footprint,
                chosen=chosen,
                rejected=tuple(sorted(p for p in members if p != chosen)),
                chosen_date=chosen_date,
            )
        )
    return selected


def estimate_cells(
    bounds: tuple[float, float, float, float], resolution_m: float
) -> tuple[int, int]:
    """Return (columns, rows) for `bounds` in projected metres at `resolution_m`."""
    if resolution_m <= 0:
        raise ValueError(f"resolution must be positive, got {resolution_m}")
    west, south, east, north = bounds
    return (
        max(1, round((east - west) / resolution_m)),
        max(1, round((north - south) / resolution_m)),
    )


def ingest_dem(
    paths: Sequence[Path],
    *,
    resolution_m: float,
    config: Config | None = None,
    clip_to_aoi: bool = True,
    max_cells: int | None = 400_000_000,
) -> Raster:
    """Reproject, mosaic and clip DEM tiles into one analysis-CRS raster.

    Parameters
    ----------
    paths
        Tiles to ingest. Duplicated footprints are resolved by `select_tiles`.
    resolution_m
        Output cell size in metres. This is a real choice, not the source
        resolution: reprojecting from degrees has no natural metre equivalent.
    config
        Supplies the analysis CRS, the AOI, resampling and nodata.
    clip_to_aoi
        Trim to `case.aoi_bbox_wgs84`. The tiles cover far more ground than the case.
    max_cells
        Refuse an output larger than this. The terrain core is memory-bound and
        global, so a raster it cannot hold is better refused here than discovered
        several minutes into a priority-flood. None disables the check.

    Returns
    -------
    Raster
        In the analysis CRS, with NaN at nodata.
    """
    resolved = config or Config()
    if not paths:
        raise ValueError("no tiles to ingest")

    analysis = resolved.crs.analysis
    dst_crs = RioCRS.from_wkt(analysis.to_wkt())
    groups = select_tiles(paths, config=resolved)
    chosen = [group.chosen for group in groups]

    bounds: tuple[float, float, float, float] | None = None
    if clip_to_aoi:
        bounds = transform_bounds(
            RioCRS.from_epsg(4326), dst_crs, *resolved.case.aoi_bbox_wgs84, densify_pts=21
        )
        cols, rows = estimate_cells(bounds, resolution_m)
        if max_cells is not None and cols * rows > max_cells:
            raise ValueError(
                f"the AOI at {resolution_m:g} m is {cols} x {rows} = "
                f"{cols * rows / 1e6:.0f}M cells, over the {max_cells / 1e6:.0f}M cap. "
                "Depression filling is global and holds the whole grid plus about "
                "25 bytes of scratch per cell, so it cannot be tiled. Narrow "
                "case.aoi_bbox_wgs84 or use a coarser resolution."
            )

    resampling = Resampling[resolved.raster.warp_resampling]
    nodata = float(resolved.raster.nodata)

    vrts: list[WarpedVRT] = []
    handles: list[rasterio.DatasetReader] = []
    try:
        for path in chosen:
            src = rasterio.open(path)
            if src.crs is None:
                src.close()
                raise CrsError(f"{path} has no CRS; floodline will not guess one.")
            handles.append(src)
            vrts.append(
                WarpedVRT(
                    src,
                    crs=dst_crs,
                    resampling=resampling,
                    src_nodata=src.nodata,
                    nodata=nodata,
                    warp_mem_limit=resolved.raster.warp_memory_limit_mb,
                )
            )
        data, transform = merge(
            vrts, bounds=bounds, res=(resolution_m, resolution_m), nodata=nodata
        )
    finally:
        for vrt in vrts:
            vrt.close()
        for handle in handles:
            handle.close()

    array: npt.NDArray[np.float32] = np.asarray(data[0], dtype=np.float32)
    array = np.where(array == np.float32(nodata), np.nan, array)
    return Raster(
        data=np.ascontiguousarray(array),
        transform=transform,
        crs=CRS.from_wkt(dst_crs.to_wkt()),
        nodata=nodata,
    )
