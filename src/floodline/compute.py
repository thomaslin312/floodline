"""Compute a watershed's whole model from nothing but its identifier.

The precomputed atlas is limited to the watersheds someone thought to prepare. This
is the path that removes that limit: give it a HUC code and it fetches the boundary,
finds the 3DEP tiles that intersect it, reads them **over HTTP range requests
without downloading them**, runs the terrain chain and the rating curves, and packs
the result into the ~180 kB a browser needs to re-run the flood at any discharge.

The range-read part is what makes this viable. USGS publishes 3DEP as cloud-optimised
GeoTIFFs - 512x512 internal tiles, LZW, with overviews - so reading a 500 km2
watershed out of a 10812x10812 tile costs the blocks that watershed touches, not the
400 MB the tile weighs. A watershed anywhere in the United States can be computed
without a byte of it being on disk beforehand.

This function is deliberately shaped like a request handler: identifier in, bundle
out, no local state. That is what a service wraps.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

import httpx
import numpy as np
import rasterio
from pyproj import CRS, Transformer
from shapely.geometry import Point, shape
from shapely.geometry.base import BaseGeometry
from shapely.ops import transform as shapely_transform

from floodline.config import Config
from floodline.hydraulics.rating import (
    build_rating_curves,
    discharge_by_area_ratio,
    reach_catchments,
)
from floodline.io.ingest import Watershed, estimate_cells, ingest_dem
from floodline.io.sources import (
    WBD,
    WBD_LAYER_BY_HUC_LEVEL,
    FetchContext,
    SourceError,
    find_dem_tiles,
    get_json,
    make_client,
)
from floodline.report.bundle import (
    UnitBundle,
    encode_hand,
    encode_reach_ids,
    encode_stage_table,
    to_data_uri,
)
from floodline.report.figures import block_reduce
from floodline.terrain.route import route_terrain
from floodline.terrain.streams import link_raster

__all__ = ["ComputeResult", "compute_watershed", "watershed_by_huc", "watershed_for_point"]

# GDAL settings that make /vsicurl range reads on a COG behave. Without
# GDAL_DISABLE_READDIR_ON_OPEN, GDAL lists the whole S3 prefix on every open, which
# costs more than the read does.
VSICURL_ENV: dict[str, object] = {
    "GDAL_DISABLE_READDIR_ON_OPEN": "EMPTY_DIR",
    "CPL_VSIL_CURL_ALLOWED_EXTENSIONS": ".tif,.TIF",
    "GDAL_HTTP_MULTIPLEX": "YES",
    "GDAL_HTTP_VERSION": "2",
    # A truncated range read is a normal fact of life over HTTP and GDAL does not
    # retry unless told to. Without these, one short read fails the whole watershed
    # several minutes into the job.
    "GDAL_HTTP_MAX_RETRY": 5,
    "GDAL_HTTP_RETRY_DELAY": 1,
    "GDAL_HTTP_TIMEOUT": 60,
    # Read whole 512x512 blocks once and keep them: the warp revisits neighbouring
    # blocks constantly, and a cache miss here is a network round trip.
    "VSI_CACHE": True,
    "VSI_CACHE_SIZE": 128 * 1024 * 1024,
    "GDAL_CACHEMAX": 1024,
    "GDAL_NUM_THREADS": "ALL_CPUS",
    "CPL_VSIL_CURL_CHUNK_SIZE": 1024 * 1024,
}


@dataclass(frozen=True, slots=True)
class ComputeResult:
    """A finished watershed, and what it cost to produce."""

    bundle: UnitBundle
    seconds: dict[str, float] = field(default_factory=dict)
    tiles_read: int = 0
    warnings: list[str] = field(default_factory=list)

    @property
    def total_seconds(self) -> float:
        """Wall time across every stage."""
        return sum(self.seconds.values())


def _to_analysis(geometry: BaseGeometry, config: Config) -> BaseGeometry:
    """Reproject a WGS84 geometry into the analysis CRS."""
    project = Transformer.from_crs(
        CRS.from_epsg(4326), config.crs.analysis, always_xy=True
    ).transform
    return shapely_transform(project, geometry)


def _watershed_from_feature(feature: dict[str, Any], level: int, config: Config) -> Watershed:
    """Build a `Watershed` from a WBD GeoJSON feature."""
    properties = feature.get("properties", {})
    return Watershed(
        huc=str(properties.get(f"huc{level}", "")),
        name=str(properties.get("name", "")),
        area_km2=float(properties.get("areasqkm") or 0.0),
        geometry=_to_analysis(shape(feature["geometry"]), config),
    )


def _query_wbd(context: FetchContext, level: int, params: dict[str, Any]) -> list[dict[str, Any]]:
    """Query a WBD layer and return its features."""
    layer = WBD_LAYER_BY_HUC_LEVEL.get(level)
    if layer is None:
        raise SourceError(
            f"no WBD layer for HUC level {level}; known: {sorted(WBD_LAYER_BY_HUC_LEVEL)}"
        )
    payload = get_json(
        context,
        f"{WBD}/{layer}/query",
        {
            "outFields": f"huc{level},name,areasqkm",
            "returnGeometry": "true",
            "outSR": 4326,
            "f": "geojson",
            **params,
        },
    )
    return payload.get("features", []) if isinstance(payload, dict) else []


def watershed_by_huc(
    huc: str, *, config: Config | None = None, client: httpx.Client | None = None
) -> Watershed:
    """Look up one watershed by its HUC code, anywhere in the United States.

    The HUC's digit count selects the level, so `watershed_by_huc("1204010403")` is a
    HUC-10 and a 12-digit code is a subwatershed. No local data is needed.
    """
    resolved = config or Config()
    level = len(huc)
    owned = client is None
    active = client or make_client(resolved.sources)
    try:
        context = FetchContext(config=resolved, dest=resolved.paths.raw, client=active)
        features = _query_wbd(context, level, {"where": f"huc{level}='{huc}'"})
    finally:
        if owned:
            active.close()
    if not features:
        raise SourceError(
            f"no HUC-{level} watershed with code {huc!r}. Codes have an even number of "
            "digits from 2 to 16, and the digit count selects the level."
        )
    return _watershed_from_feature(features[0], level, resolved)


def watershed_for_point(
    lon: float,
    lat: float,
    *,
    level: int = 10,
    config: Config | None = None,
    client: httpx.Client | None = None,
) -> Watershed:
    """Return the watershed containing a longitude/latitude, which is what a map click gives."""
    resolved = config or Config()
    owned = client is None
    active = client or make_client(resolved.sources)
    try:
        context = FetchContext(config=resolved, dest=resolved.paths.raw, client=active)
        features = _query_wbd(
            context,
            level,
            {
                "geometry": f"{lon},{lat}",
                "geometryType": "esriGeometryPoint",
                "inSR": 4326,
                "spatialRel": "esriSpatialRelIntersects",
            },
        )
    finally:
        if owned:
            active.close()
    if not features:
        raise SourceError(f"no HUC-{level} watershed contains ({lon}, {lat})")

    point = _to_analysis(Point(lon, lat), resolved)
    for feature in features:
        unit = _watershed_from_feature(feature, level, resolved)
        if unit.geometry.contains(point):
            return unit
    return _watershed_from_feature(features[0], level, resolved)


def compute_watershed(
    unit: Watershed,
    *,
    resolution_m: float = 10.0,
    config: Config | None = None,
    client: httpx.Client | None = None,
    target_width: int = 800,
    discharge_cms: float | None = None,
    gauge_area_cells: float | None = None,
    multipliers: np.ndarray | None = None,
    max_cells: int = 60_000_000,
) -> ComputeResult:
    """Run the whole model for one watershed, reading the DEM over the network.

    Parameters
    ----------
    unit
        The watershed, from `watershed_by_huc` or `watershed_for_point`.
    resolution_m
        Output cell size. 10 m is the finest 3DEP resolution with full coverage here.
    discharge_cms, gauge_area_cells
        An observed discharge and the contributing area it was measured over. When
        omitted, discharge is left unscaled at 1 m3/s per gauge-equivalent area, which
        makes the returned stage table a *relative* curve: useful for exploring shape,
        not for a flood depth. The bundle records which case applies.
    max_cells
        Refuse a watershed larger than this at the requested resolution. Depression
        filling is global, so the whole grid must fit in memory at once.

    Returns
    -------
    ComputeResult
    """
    resolved = config or Config()
    timings: dict[str, float] = {}
    warnings: list[str] = []

    cols, rows = estimate_cells(unit.bounds, resolution_m)
    if cols * rows > max_cells:
        raise ValueError(
            f"{unit.name} at {resolution_m:g} m is {cols} x {rows} = "
            f"{cols * rows / 1e6:.0f}M cells, over the {max_cells / 1e6:.0f}M limit. "
            "Depression filling is global and cannot be tiled, so the whole grid has "
            "to fit in memory. Use a coarser resolution or a smaller hydrologic unit."
        )

    owned = client is None
    active = client or make_client(resolved.sources)
    try:
        start = time.perf_counter()
        context = FetchContext(config=resolved, dest=resolved.paths.raw, client=active)
        west, south, east, north = _to_wgs84_bounds(unit, resolved)
        items = find_dem_tiles(context, int(resolution_m), (west, south, east, north))
        urls = [f"/vsicurl/{item['downloadURL']}" for item in items if item.get("downloadURL")]
        timings["find_tiles"] = time.perf_counter() - start
    finally:
        if owned:
            active.close()

    if not urls:
        raise SourceError(f"no {resolution_m:g} m 3DEP tiles cover {unit.name}")

    start = time.perf_counter()
    with rasterio.Env(**VSICURL_ENV):
        dem = ingest_dem(
            urls,
            resolution_m=resolution_m,
            config=resolved,
            watershed=unit,
            max_cells=max_cells,
        )
    timings["read_dem"] = time.perf_counter() - start

    start = time.perf_counter()
    chain = route_terrain(dem.data, config=resolved, nodata=dem.nodata, cellsize=dem.cellsize)
    timings["terrain"] = time.perf_counter() - start
    if not chain.drains_completely:
        warnings.append(f"{chain.accumulation.cells_draining_to_flats:,} cells drain into flats")

    start = time.perf_counter()
    ids, links = link_raster(chain.streams, chain.flowdir)
    reach_of = reach_catchments(chain.hand.drainage_index, ids)
    curves = build_rating_curves(
        chain.hand.hand, chain.filled, links, reach_of, config=resolved, cellsize=dem.cellsize
    )
    reference_area = gauge_area_cells or (215e6 / dem.cell_area_m2)
    flows = discharge_by_area_ratio(
        discharge_cms if discharge_cms is not None else 1.0,
        reference_area,
        links,
        chain.accumulation.accumulation,
        config=resolved,
    )
    timings["rating"] = time.perf_counter() - start
    if discharge_cms is None:
        warnings.append("no discharge supplied; the stage table is relative, not a depth")

    start = time.perf_counter()
    ladder = multipliers if multipliers is not None else np.linspace(0.0, 3.0, 33)
    factor = max(1, int(np.ceil(dem.data.shape[1] / target_width)))
    hand_r = block_reduce(chain.hand.hand, factor, how="mean")
    reach_r = block_reduce(
        np.where(reach_of >= 0, reach_of, np.nan).astype(float), factor, how="max"
    )
    reach_i = np.where(np.isfinite(reach_r), np.nan_to_num(reach_r), -1).astype(np.int64)
    height, width = hand_r.shape

    bundle = UnitBundle(
        huc=unit.huc,
        name=unit.name,
        area_km2=round(unit.area_km2),
        width=width,
        height=height,
        reduction=factor,
        bounds=unit.bounds,
        n_reaches=len(links),
        base_discharge_cms=round(float(discharge_cms or 0.0), 1),
        multipliers=[round(float(m), 3) for m in ladder],
        gauged=discharge_cms is not None,
        hand=to_data_uri(encode_hand(hand_r)),
        reach=to_data_uri(encode_reach_ids(reach_i)),
        stage_table=to_data_uri(encode_stage_table(len(links), curves, flows, ladder)),
        stats={
            "cells": int(dem.data.size),
            "curves": len(curves),
            "flats": int(chain.flat_cells_before),
            "streams": int(chain.streams.sum()),
            "valid_km2": round(
                float(np.isfinite(chain.hand.hand).sum()) * dem.cell_area_m2 / 1e6, 1
            ),
            "resolution_m": resolution_m,
        },
    )
    timings["encode"] = time.perf_counter() - start

    return ComputeResult(bundle=bundle, seconds=timings, tiles_read=len(urls), warnings=warnings)


def _to_wgs84_bounds(unit: Watershed, config: Config) -> tuple[float, float, float, float]:
    """Return the watershed's bounds in EPSG:4326, which is what the tile query wants."""
    back = Transformer.from_crs(config.crs.analysis, CRS.from_epsg(4326), always_xy=True)
    west, south, east, north = unit.bounds
    xs, ys = back.transform([west, east, east, west], [south, south, north, north])
    return min(xs), min(ys), max(xs), max(ys)
