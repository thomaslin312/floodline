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

import json
import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from pathlib import Path

import numpy as np
import rasterio
from pyproj import CRS, Transformer
from rasterio.crs import CRS as RioCRS
from rasterio.enums import Resampling
from rasterio.errors import RasterioIOError
from rasterio.features import geometry_mask
from rasterio.transform import from_origin
from rasterio.vrt import WarpedVRT
from rasterio.warp import transform_bounds
from shapely.geometry import shape
from shapely.geometry.base import BaseGeometry
from shapely.ops import transform as shapely_transform

from floodline.config import Config, TileVintage
from floodline.io.raster import CrsError, Raster

__all__ = [
    "TileGroup",
    "Watershed",
    "estimate_cells",
    "ingest_dem",
    "load_watersheds",
    "select_tiles",
]

_DATE_IN_NAME = re.compile(r"(?<!\d)(\d{8})(?!\d)")
_FOOTPRINT_PRECISION = 4
"""Decimal places used to group tiles by footprint. Enough to separate adjacent
tiles, loose enough that floating-point noise in the bounds does not split a group."""


@dataclass(frozen=True, slots=True)
class Watershed:
    """One hydrologic unit, reprojected into the analysis CRS.

    This is the unit the terrain chain should run over. Flow accumulation at a cell
    depends on everything upstream of it, so a chain run over an arbitrary box is
    wrong near the box's edges; a watershed is complete by construction.
    """

    huc: str
    name: str
    area_km2: float
    geometry: BaseGeometry
    """Boundary in the analysis CRS."""

    @property
    def bounds(self) -> tuple[float, float, float, float]:
        """(west, south, east, north) in the analysis CRS."""
        west, south, east, north = self.geometry.bounds
        return float(west), float(south), float(east), float(north)

    def cells_at(self, resolution_m: float) -> int:
        """Cells in this watershed's bounding box at `resolution_m`."""
        cols, rows = estimate_cells(self.bounds, resolution_m)
        return cols * rows


def load_watersheds(path: Path, *, config: Config | None = None) -> list[Watershed]:
    """Read fetched WBD polygons and reproject them into the analysis CRS.

    Sorted by area descending, so the largest -- the one most likely to be too big
    for a given resolution -- is the first thing a caller sees.
    """
    resolved = config or Config()
    payload = json.loads(path.read_text())
    features = payload.get("features", [])
    if not features:
        raise ValueError(f"{path} contains no watershed features")

    field = f"huc{resolved.case.huc_level}"
    project = Transformer.from_crs(
        CRS.from_epsg(4326), resolved.crs.analysis, always_xy=True
    ).transform

    sheds: list[Watershed] = []
    for feature in features:
        properties = feature.get("properties", {})
        sheds.append(
            Watershed(
                huc=str(properties.get(field, "")),
                name=str(properties.get("name", "")),
                area_km2=float(properties.get("areasqkm") or 0.0),
                geometry=shapely_transform(project, shape(feature["geometry"])),
            )
        )
    return sorted(sheds, key=lambda w: w.area_km2, reverse=True)


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


def select_tiles(paths: Sequence[Path | str], *, config: Config | None = None) -> list[TileGroup]:
    """Group tiles by footprint and pick one vintage from each group.

    Grouping is by the tile's actual bounds rather than by its name, so a change in
    USGS naming cannot silently turn one footprint into several.
    """
    resolved = config or Config()
    event = resolved.case.event_start
    strategy = resolved.case.dem_vintage

    groups: dict[tuple[float, ...], list[Path]] = {}
    unreadable: list[str] = []
    for source in paths:
        try:
            with rasterio.open(source) as src:
                key = tuple(round(v, _FOOTPRINT_PRECISION) for v in src.bounds)
        except RasterioIOError as exc:
            # The 3DEP catalogue occasionally lists a tile that is no longer served.
            # One stale entry should cost that tile's footprint, not the whole
            # watershed, so it is skipped and reported.
            unreadable.append(f"{source} ({exc})")
            continue
        groups.setdefault(key, []).append(Path(str(source)))

    if not groups:
        raise CrsError(
            "none of the elevation tiles could be opened. "
            + ("; ".join(unreadable[:3]) if unreadable else "no tiles were supplied")
        )

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


def _union_bounds(
    paths: Sequence[Path | str], dst_crs: RioCRS
) -> tuple[float, float, float, float]:
    """Return the combined extent of `paths` in `dst_crs`."""
    west = south = float("inf")
    east = north = float("-inf")
    for path in paths:
        try:
            with rasterio.open(path) as src:
                if src.crs is None:
                    raise CrsError(f"{path} has no CRS; floodline will not guess one.")
                b = transform_bounds(src.crs, dst_crs, *src.bounds, densify_pts=21)
        except RasterioIOError:
            continue
        west, south = min(west, b[0]), min(south, b[1])
        east, north = max(east, b[2]), max(north, b[3])
    if west == float("inf"):
        raise CrsError("no elevation tile could be opened to establish an extent")
    return west, south, east, north


def ingest_dem(
    paths: Sequence[Path | str],
    *,
    resolution_m: float,
    config: Config | None = None,
    clip_to_aoi: bool = True,
    watershed: Watershed | None = None,
    max_cells: int | None = 400_000_000,
) -> Raster:
    """Reproject, mosaic and clip DEM tiles into one analysis-CRS raster.

    Parameters
    ----------
    paths
        Tiles to ingest, as local paths or GDAL virtual filesystem URLs such as
        `/vsicurl/https://...`. A remote cloud-optimised GeoTIFF is read by range
        request, so only the blocks the output touches are transferred. Duplicated
        footprints are resolved by `select_tiles`.
    resolution_m
        Output cell size in metres. This is a real choice, not the source
        resolution: reprojecting from degrees has no natural metre equivalent.
    config
        Supplies the analysis CRS, the AOI, resampling and nodata.
    clip_to_aoi
        Trim to `case.aoi_bbox_wgs84`. The tiles cover far more ground than the case.
    watershed
        Clip to this hydrologic unit instead of the AOI box, and mask cells outside
        its boundary to nodata. This is the correct unit of work: a chain run over an
        arbitrary box has no way to know about contributing area outside it.
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
    chosen: list[Path | str] = [group.chosen for group in groups]

    bounds: tuple[float, float, float, float] | None = None
    if watershed is not None:
        bounds = watershed.bounds
    elif clip_to_aoi:
        bounds = transform_bounds(
            RioCRS.from_epsg(4326), dst_crs, *resolved.case.aoi_bbox_wgs84, densify_pts=21
        )
    if bounds is not None:
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

    if bounds is None:
        bounds = _union_bounds(chosen, dst_crs)
        cols, rows = estimate_cells(bounds, resolution_m)
    west, _, _, north = bounds
    transform = from_origin(west, north, resolution_m, resolution_m)

    # Every WarpedVRT is pinned to the *output* grid rather than left to size itself
    # from the source. An unpinned VRT over a 10812x10812 3DEP tile is a 123M-cell
    # warp grid; pinned to a 10 km window it is 0.1M. GDAL then reads only the source
    # blocks that window touches, which is the difference between seconds and tens of
    # minutes over a network. It also means every VRT shares one grid, so combining
    # them is a per-pixel choice with no resampling left to do.
    array = np.full((rows, cols), np.nan, dtype=np.float32)
    read = 0
    for path in chosen:
        try:
            src = rasterio.open(path)
        except RasterioIOError:
            continue
        with src:
            if src.crs is None:
                raise CrsError(f"{path} has no CRS; floodline will not guess one.")
            with WarpedVRT(
                src,
                crs=dst_crs,
                transform=transform,
                width=cols,
                height=rows,
                resampling=resampling,
                src_nodata=src.nodata,
                nodata=nodata,
                warp_mem_limit=resolved.raster.warp_memory_limit_mb,
            ) as vrt:
                block = vrt.read(1).astype(np.float32)
        read += 1
        block = np.where(block == np.float32(nodata), np.nan, block)
        gaps = np.isnan(array) & ~np.isnan(block)
        if gaps.any():
            array[gaps] = block[gaps]
        if not np.isnan(array).any():
            break

    if read == 0:
        raise CrsError("no elevation tile could be read for this area")

    if watershed is not None:
        # Everything outside the boundary is nodata. Filling and routing then treat
        # it as the edge of the data, which is exactly right: water leaving the
        # watershed has left the domain.
        outside = ~geometry_mask(
            [watershed.geometry],
            out_shape=array.shape,
            transform=transform,
            invert=False,
        )
        array = np.where(outside, array, np.nan)

    return Raster(
        data=np.ascontiguousarray(array),
        transform=transform,
        crs=CRS.from_wkt(dst_crs.to_wkt()),
        nodata=nodata,
    )
