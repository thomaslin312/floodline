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

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
import numpy as np
import numpy.typing as npt
import rasterio
from pyproj import CRS, Transformer
from rasterio.crs import CRS as RioCRS
from rasterio.enums import Resampling
from rasterio.transform import array_bounds
from rasterio.warp import calculate_default_transform, reproject
from shapely.geometry import Point, mapping, shape
from shapely.geometry.base import BaseGeometry
from shapely.ops import transform as shapely_transform
from shapely.prepared import prep

from floodline.config import Config
from floodline.hydraulics.frequency import flood_frequency
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
    find_gauges,
    get_json,
    make_client,
    peak_discharge,
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

__all__ = [
    "ComputeResult",
    "WatershedNotFoundError",
    "compute_watershed",
    "discharge_ladder",
    "gauge_for_watershed",
    "geometry_wgs84",
    "marks_within",
    "to_web_mercator",
    "utm_crs_for",
    "watershed_by_huc",
    "watershed_for_point",
    "wgs84_bounds",
]

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


class WatershedNotFoundError(SourceError):
    """No hydrologic unit matches the code or point asked for.

    A subclass of SourceError so anything catching that still catches this, but
    distinguishable where it matters: "that watershed does not exist" is the caller's
    mistake and a 404, while "the WBD service is down" is not and is a 502. Reporting
    either as the other sends whoever is debugging in the wrong direction.
    """


def utm_crs_for(lon: float, lat: float) -> CRS:
    """Return the NAD83 UTM zone covering a longitude, as a projected metre CRS.

    A fixed analysis CRS only works for a fixed study area. `EPSG:6587` is Texas
    South Central: correct for Houston and meaningless in Oregon. For a tool that
    accepts any watershed in the country, the CRS has to follow the watershed, and
    UTM is the standard answer - conformal, metric, and accurate enough over the few
    hundred kilometres a hydrologic unit spans.

    NAD83 rather than WGS84 because 3DEP publishes in NAD83, so this keeps the whole
    chain on one datum. Zones are numbered from 180 deg W in 6 deg steps; the
    continental United States spans zones 10 to 19.
    """
    if not -180.0 <= lon <= 180.0:
        raise ValueError(f"longitude out of range: {lon}")
    if lat < 0:
        raise ValueError(
            f"latitude {lat} is in the southern hemisphere; only NAD83 northern UTM "
            "zones are mapped here, which covers the United States."
        )
    zone = int((lon + 180.0) // 6.0) + 1
    return CRS.from_epsg(26900 + min(max(zone, 1), 60))


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


def _watershed_from_feature(
    feature: dict[str, Any], level: int, config: Config
) -> tuple[Watershed, Config]:
    """Build a `Watershed` from a WBD feature, in a CRS chosen to suit its location.

    Returns the watershed alongside a config whose analysis CRS is the UTM zone the
    watershed sits in, so everything downstream works in true metres wherever the
    user clicked.
    """
    properties = feature.get("properties", {})
    geographic = shape(feature["geometry"])
    centroid = geographic.centroid
    local = config.model_copy(
        update={
            "crs": config.crs.model_copy(update={"analysis": utm_crs_for(centroid.x, centroid.y)})
        }
    )
    return (
        Watershed(
            huc=str(properties.get(f"huc{level}", "")),
            name=str(properties.get("name", "")),
            area_km2=float(properties.get("areasqkm") or 0.0),
            geometry=_to_analysis(geographic, local),
        ),
        local,
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
) -> tuple[Watershed, Config]:
    """Look up one watershed by its HUC code, anywhere in the United States.

    The HUC's digit count selects the level, so `watershed_by_huc("1204010403")` is a
    HUC-10 and a 12-digit code is a subwatershed. No local data is needed. The
    returned config carries a UTM analysis CRS chosen for the watershed's location.
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
        raise WatershedNotFoundError(
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
) -> tuple[Watershed, Config]:
    """Return the watershed containing a longitude/latitude, which is what a map click gives.

    Also returns a config whose analysis CRS is the UTM zone for that location, so the
    same call works anywhere in the country.
    """
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
        raise WatershedNotFoundError(f"no HUC-{level} watershed contains ({lon}, {lat})")

    for feature in features:
        unit, local = _watershed_from_feature(feature, level, resolved)
        if unit.geometry.contains(_to_analysis(Point(lon, lat), local)):
            return unit, local
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
    marks_path: Path | None = None,
    find_gauge: bool = True,
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
        omitted, a scenario discharge is used instead:
        `area_km2 x hydraulics.default_specific_discharge`, which by default is the
        specific discharge Harvey delivered at Whiteoak Bayou. That makes any
        watershed immediately explorable, and `UnitBundle.gauged` records that the
        number was assumed rather than measured.
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
        west, south, east, north = wgs84_bounds(unit, resolved)
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
    # Prefer a real gauge inside this watershed over a scenario. Its peak of record
    # is the worst flow it has actually measured, which is a far better anchor than a
    # specific discharge borrowed from somewhere else.
    gauge: dict[str, Any] | None = None
    if discharge_cms is None and find_gauge:
        start = time.perf_counter()
        owned_g = client is None
        active_g = client or make_client(resolved.sources)
        try:
            gauge = gauge_for_watershed(
                unit,
                chain,
                dem,
                FetchContext(config=resolved, dest=resolved.paths.raw, client=active_g),
            )
        finally:
            if owned_g:
                active_g.close()
        timings["find_gauge"] = time.perf_counter() - start
        if gauge is not None:
            discharge_cms = gauge["discharge_cms"]
            gauge_area_cells = gauge["area_cells"]

    gauged = discharge_cms is not None
    if discharge_cms is not None:
        reference_discharge = float(discharge_cms)
        reference_area = gauge_area_cells or (unit.area_km2 * 1e6 / dem.cell_area_m2)
    else:
        # No gauge: stand in a severe-flood scenario scaled to this catchment, so the
        # watershed is explorable rather than uniformly dry. Flagged, not disguised.
        reference_discharge = unit.area_km2 * resolved.hydraulics.default_specific_discharge
        reference_area = unit.area_km2 * 1e6 / dem.cell_area_m2
        warnings.append(
            f"no gauge in this watershed: discharge is a scenario of "
            f"{resolved.hydraulics.default_specific_discharge:g} m3/s per km2 "
            f"({reference_discharge:,.0f} m3/s over {unit.area_km2:,.0f} km2), not an observation"
        )
    flows = discharge_by_area_ratio(
        reference_discharge, reference_area, links, chain.accumulation.accumulation, config=resolved
    )
    timings["rating"] = time.perf_counter() - start

    start = time.perf_counter()
    ladder = discharge_ladder(resolved, multipliers)
    factor = max(1, int(np.ceil(dem.data.shape[1] / target_width)))
    hand_r = block_reduce(chain.hand.hand, factor, how="mean")
    reach_r = block_reduce(
        np.where(reach_of >= 0, reach_of, np.nan).astype(float), factor, how="max"
    )
    reach_i = np.where(np.isfinite(reach_r), np.nan_to_num(reach_r), -1).astype(np.int64)

    # A web map draws in Web Mercator. The analysis grid is UTM and north-up there,
    # which is *not* axis-aligned in Web Mercator, so an overlay placed by its corner
    # coordinates would be visibly skewed. Warping the display arrays - only those, at
    # a few hundred pixels - puts them on the map's own grid. Analysis stays in UTM,
    # where the metres are real.
    display_transform = dem.transform * rasterio.Affine.scale(factor, factor)
    hand_r, mercator_bounds = to_web_mercator(hand_r, display_transform, resolved, "bilinear")
    reach_f, _ = to_web_mercator(
        np.where(reach_i >= 0, reach_i, np.nan).astype(np.float64),
        display_transform,
        resolved,
        "nearest",
    )
    reach_i = np.where(np.isfinite(reach_f), np.nan_to_num(reach_f), -1).astype(np.int64)
    height, width = hand_r.shape

    marks = marks_within(unit, resolved, marks_path) if marks_path else []

    # Drive the model with the flood the marks came from, not the largest on record.
    matched_gauge = event_matched_gauge(gauge, marks)
    if matched_gauge is not gauge and matched_gauge is not None:
        gauge = matched_gauge
        reference_discharge = float(gauge["event_discharge_cms"])
        flows = discharge_by_area_ratio(
            reference_discharge,
            reference_area,
            links,
            chain.accumulation.accumulation,
            config=resolved,
        )
    if marks:
        # Place each mark on the display grid so the page can draw it, and record the
        # model's own ground elevation there, which is what a residual is measured from.
        inverse = ~dem.transform
        forward = Transformer.from_crs(CRS.from_epsg(4326), resolved.crs.analysis, always_xy=True)
        placed = []
        for mark in marks:
            x, y = forward.transform(mark["lon"], mark["lat"])
            col, row = inverse * (x, y)
            row, col = int(row), int(col)
            if not (0 <= row < chain.filled.shape[0] and 0 <= col < chain.filled.shape[1]):
                continue
            ground = float(chain.filled[row, col])
            if not np.isfinite(ground):
                continue
            placed.append({**mark, "ground_m": round(ground, 2)})
        marks = placed

    bundle = UnitBundle(
        huc=unit.huc,
        name=unit.name,
        area_km2=round(unit.area_km2),
        width=width,
        height=height,
        reduction=factor,
        bounds=mercator_bounds,
        n_reaches=len(links),
        base_discharge_cms=round(reference_discharge, 1),
        multipliers=[round(float(m), 3) for m in ladder],
        gauged=gauged,
        hand=to_data_uri(encode_hand(hand_r)),
        reach=to_data_uri(encode_reach_ids(reach_i)),
        stage_table=to_data_uri(encode_stage_table(len(links), curves, flows, ladder)),
        gauge=_with_history(gauge, reference_discharge),
        marks=marks,
        stats={
            "cells": int(dem.data.size),
            "curves": len(curves),
            "flats": int(chain.flat_cells_before),
            "streams": int(chain.streams.sum()),
            "valid_km2": round(
                float(np.isfinite(chain.hand.hand).sum()) * dem.cell_area_m2 / 1e6, 1
            ),
            "resolution_m": resolution_m,
            "analysis_crs": resolved.crs.analysis.to_string(),
            "display_crs": "EPSG:3857",
        },
    )
    timings["encode"] = time.perf_counter() - start

    return ComputeResult(bundle=bundle, seconds=timings, tiles_read=len(urls), warnings=warnings)


def _with_history(gauge: dict[str, Any] | None, discharge_cms: float) -> dict[str, Any] | None:
    """Add where this discharge sits in the gauge's own record.

    The annual peak series is already fetched to pick the event's own peak, so the
    rank and return period cost nothing beyond the arithmetic. A depth map answers
    "how deep"; this answers "how unusual", which is the question a reader asks first.
    """
    if gauge is None or not gauge.get("series"):
        return gauge
    fit = flood_frequency(gauge["series"], site=str(gauge.get("site", "")))
    context = fit.context_for(discharge_cms)
    return {
        **gauge,
        "history": {
            "rank": context.rank,
            "n_years": context.n_years,
            "exceeds_record": context.exceeds_record,
            "empirical_return_period_years": round(context.empirical_return_period_years, 1),
            "fitted_return_period_years": (
                round(context.fitted_return_period_years)
                if context.fitted_return_period_years is not None
                else None
            ),
            "extrapolated": context.extrapolated,
            "fit_saturated": context.fit_saturated,
            "summary": context.summary(),
            "larger_floods": [
                {"water_year": p.water_year, "cms": round(p.discharge_cms), "date": p.date}
                for p in context.larger_floods[:5]
            ],
        },
    }


def wgs84_bounds(unit: Watershed, config: Config) -> tuple[float, float, float, float]:
    """Return the watershed's bounds in EPSG:4326, which is what the tile query wants."""
    back = Transformer.from_crs(config.crs.analysis, CRS.from_epsg(4326), always_xy=True)
    west, south, east, north = unit.bounds
    xs, ys = back.transform([west, east, east, west], [south, south, north, north])
    return min(xs), min(ys), max(xs), max(ys)


def gauge_for_watershed(
    unit: Watershed,
    chain: Any,
    dem: Any,
    context: FetchContext,
    *,
    snap_cells: int = 40,
) -> dict[str, Any] | None:
    """Find the gauge that best represents this watershed's outflow, and its peak.

    Every NWIS gauge publishing discharge inside the watershed is snapped to our own
    stream network, and the one draining the largest area wins - that is the station
    nearest the outlet, whose flow stands for the whole unit. Contributing area comes
    from our own flow accumulation rather than the published figure, so the discharge
    and the area it is divided by are measured on the same grid.

    Its peak of record is used as the discharge: the worst flow that gauge has
    actually measured, rather than a design figure from a regression.
    """
    west, south, east, north = wgs84_bounds(unit, context.config)
    # A failed lookup is not an absent gauge. Swallowing the error here returned None,
    # which every caller reports as "no USGS gauge inside this watershed" - so a DNS
    # blip told the reader their basin is ungauged and withheld exposure and damage on
    # the strength of it. Let it propagate: SourceError already means "upstream is
    # broken" everywhere else, and the service turns it into a 502 rather than a
    # statement about the watershed.
    sites = find_gauges(context, (west, south, east, north))
    if not sites:
        return None

    forward = Transformer.from_crs(CRS.from_epsg(4326), context.config.crs.analysis, always_xy=True)
    rows, cols = chain.streams.shape
    best: dict[str, Any] | None = None

    for site in sites:
        try:
            lon, lat = float(site["dec_long_va"]), float(site["dec_lat_va"])
        except (KeyError, ValueError):
            continue
        x, y = forward.transform(lon, lat)
        if not unit.geometry.contains(Point(x, y)):
            continue
        col, row = ~dem.transform * (x, y)
        row, col = int(row), int(col)

        snapped = None
        for radius in range(snap_cells + 1):
            found = [
                (dr * dr + dc * dc, row + dr, col + dc)
                for dr in range(-radius, radius + 1)
                for dc in range(-radius, radius + 1)
                if abs(dr) == radius or abs(dc) == radius
                if 0 <= row + dr < rows
                and 0 <= col + dc < cols
                and chain.streams[row + dr, col + dc]
            ]
            if found:
                snapped = min(found)
                break
        if snapped is None:
            continue
        _, grow, gcol = snapped
        area_cells = float(chain.accumulation.accumulation[grow, gcol])
        if best is not None and area_cells <= best["area_cells"]:
            continue
        best = {
            "site": site["site_no"],
            "name": site.get("station_nm", "").strip(),
            "lon": lon,
            "lat": lat,
            "row": grow,
            "col": gcol,
            "area_cells": area_cells,
            "area_km2": round(area_cells * dem.cell_area_m2 / 1e6, 1),
        }

    if best is None:
        return None
    peak = peak_discharge(context, best["site"])
    if peak is None:
        return None
    return {**best, **peak}


def snap_gauges(
    unit: Watershed,
    chain: Any,
    dem: Any,
    context: FetchContext,
    *,
    snap_cells: int = 40,
) -> list[dict[str, Any]]:
    """Place every gauge inside the watershed on the stream network.

    The plural of `gauge_for_watershed`, which keeps only the station draining the
    largest area. A basin usually has several, each of which measured a water level,
    and throwing all but one away is how stage came to be modelled twice over -
    transferred by area ratio and then converted through a synthetic rating curve -
    when parts of it were observed directly.

    Sites that fall outside the polygon, or that will not snap to a channel within
    `snap_cells`, are dropped. The rest come back with their snapped cell, their
    contributing area measured on our own grid, and whatever the site record says
    about datum, which the caller needs to decide if a stage is usable.
    """
    west, south, east, north = wgs84_bounds(unit, context.config)
    sites = find_gauges(context, (west, south, east, north))
    if not sites:
        return []

    forward = Transformer.from_crs(CRS.from_epsg(4326), context.config.crs.analysis, always_xy=True)
    rows, cols = chain.streams.shape
    placed: list[dict[str, Any]] = []

    for site in sites:
        try:
            lon, lat = float(site["dec_long_va"]), float(site["dec_lat_va"])
        except (KeyError, ValueError):
            continue
        x, y = forward.transform(lon, lat)
        if not unit.geometry.contains(Point(x, y)):
            continue
        col, row = ~dem.transform * (x, y)
        row, col = int(row), int(col)

        snapped = None
        for radius in range(snap_cells + 1):
            found = [
                (dr * dr + dc * dc, row + dr, col + dc)
                for dr in range(-radius, radius + 1)
                for dc in range(-radius, radius + 1)
                if abs(dr) == radius or abs(dc) == radius
                if 0 <= row + dr < rows
                and 0 <= col + dc < cols
                and chain.streams[row + dr, col + dc]
            ]
            if found:
                snapped = min(found)
                break
        if snapped is None:
            continue
        _, grow, gcol = snapped
        area_cells = float(chain.accumulation.accumulation[grow, gcol])
        placed.append(
            {
                "site": site["site_no"],
                "name": (site.get("station_nm") or "").strip(),
                "lon": lon,
                "lat": lat,
                "row": grow,
                "col": gcol,
                "area_cells": area_cells,
                "area_km2": round(area_cells * dem.cell_area_m2 / 1e6, 1),
                "alt_va": (site.get("alt_va") or "").strip(),
                "alt_datum_cd": (site.get("alt_datum_cd") or "").strip(),
            }
        )
    return placed


def marks_within(unit: Watershed, config: Config, path: Path) -> list[dict[str, Any]]:
    """Return the cached national high-water marks that fall inside a watershed.

    The Short-Term Network's whole holding is about 26 MB and takes fourteen seconds
    to fetch, so it is cached once and filtered per watershed. Marks come from every
    event STN has surveyed, not one, so a watershed carries whatever ground truth
    exists for it.
    """
    if not path.exists():
        return []
    forward = Transformer.from_crs(CRS.from_epsg(4326), config.crs.analysis, always_xy=True)
    prepared = prep(unit.geometry)
    out: list[dict[str, Any]] = []
    for mark in json.loads(path.read_text()):
        lon, lat = mark["longitude_dd"], mark["latitude_dd"]
        x, y = forward.transform(lon, lat)
        if not prepared.contains(Point(x, y)):
            continue
        out.append(
            {
                "lon": lon,
                "lat": lat,
                "elev_m": round(float(mark["elev_ft"]) * 0.3048, 2),
                "event": mark.get("eventName", "") or "unnamed event",
                "event_date": mark.get("eventDate", ""),
                "quality": mark.get("hwm_quality_id"),
                # USGS labels each mark Riverine or Coastal. HAND models a river,
                # so a coastal mark is the wrong physics rather than a hard case,
                # and scoring against one measures the absence of a surge model.
                "environment": mark.get("hwm_environment") or "",
                "height_above_gnd_m": (
                    round(float(mark["height_above_gnd"]) * 0.3048, 2)
                    if isinstance(mark.get("height_above_gnd"), int | float)
                    else None
                ),
            }
        )
    return out


def event_matched_gauge(
    gauge: dict[str, Any] | None, marks: list[dict[str, Any]]
) -> dict[str, Any] | None:
    """Re-point a gauge at the flood its marks came from, if they share one.

    A watershed whose peak of record is 1935 scored against marks surveyed after a
    2017 storm produces a residual that measures the difference between two events,
    not the model's error. Where the marks agree on a year and the gauge has a peak
    in it, that peak is the discharge to model.

    Returns the gauge unchanged when there is nothing to match - no marks, no annual
    series, no dominant year, or no gauged peak in that year. Callers read
    `event_discharge_cms` to find out whether a match happened.

    Lives here rather than in either caller because both the map and the assessment
    need it and they must not disagree: for eight months only `compute_watershed`
    did this, and the reference watershed was the one basin where it made no
    difference, because Harvey *is* Whiteoak Bayou's peak of record.
    """
    if not marks or not gauge or not gauge.get("series"):
        return gauge
    counts: dict[str, int] = {}
    for mark in marks:
        year = (mark.get("event_date") or "")[:4]
        if year:
            counts[year] = counts.get(year, 0) + 1
    if not counts:
        return gauge
    dominant = max(counts, key=lambda y: counts[y])
    matched = [p for p in gauge["series"] if p["date"][:4] == dominant]
    if not matched:
        return gauge
    best = max(matched, key=lambda p: p["cms"])
    return {
        **gauge,
        "event_year": dominant,
        "event_discharge_cms": best["cms"],
        "event_date": best["date"],
        "matched_marks": counts[dominant],
    }


def scorable_marks(
    marks: list[dict[str, Any]], *, graded_only: bool = True
) -> tuple[list[dict[str, Any]], int]:
    """Split marks into the ones this model can be judged by, and count what was cut.

    Two exclusions, for different reasons. Grade 3 and below are rough surveys - on
    Whiteoak Bayou they carried an RMSE of 4.9 m against 1.0 m for the good ones, so
    scoring everything lets them set the headline. Coastal marks are excluded because
    HAND has no surge term at all: on Monterey Bay the model leaves 95% of them dry,
    which is not a fit to improve but a mechanism the method does not contain.

    Returns the usable marks and the number of coastal ones dropped, so a caller can
    say why a watershed has little or no ground truth left.
    """
    graded = [m for m in marks if not graded_only or m.get("quality") in (1, 2)]
    usable = [m for m in graded if (m.get("environment") or "").lower() != "coastal"]
    return usable, len(graded) - len(usable)


def discharge_ladder(
    config: Config, override: npt.NDArray[np.float64] | None = None
) -> npt.NDArray[np.float64]:
    """Return the discharge multiplier ladder both the map and the damage curve use.

    One definition, because the stage table and the damage ladder have to agree about
    what "1.00x" means. The step is validated to divide 1.0, so the observed discharge
    is a rung rather than something interpolated between two.
    """
    if override is not None:
        return override
    damage = config.damage
    steps = round(damage.discharge_ladder_max / damage.discharge_ladder_step) + 1
    return np.asarray(np.linspace(0.0, float(damage.discharge_ladder_max), steps))


def to_web_mercator(
    array: npt.NDArray[np.floating],
    transform: rasterio.Affine,
    config: Config,
    resampling: str,
) -> tuple[npt.NDArray[np.float64], tuple[float, float, float, float]]:
    """Warp a display-resolution array from the analysis CRS to Web Mercator.

    Returns the warped array and its bounds, which a web map uses directly. Reach
    ids resample by nearest - averaging two reach numbers would produce a third
    reach that does not exist.
    """
    source = RioCRS.from_wkt(config.crs.analysis.to_wkt())
    target = RioCRS.from_epsg(3857)
    rows, cols = array.shape
    dst_transform, dst_width, dst_height = calculate_default_transform(
        source, target, cols, rows, *array_bounds(rows, cols, transform)
    )
    out = np.full((dst_height, dst_width), np.nan, dtype=np.float64)
    reproject(
        source=np.asarray(array, dtype=np.float64),
        destination=out,
        src_transform=transform,
        src_crs=source,
        dst_transform=dst_transform,
        dst_crs=target,
        src_nodata=np.nan,
        dst_nodata=np.nan,
        resampling=Resampling[resampling],
    )
    west, south, east, north = array_bounds(dst_height, dst_width, dst_transform)
    return out, (float(west), float(south), float(east), float(north))


def geometry_wgs84(unit: Watershed, config: Config) -> dict[str, Any]:
    """Return a watershed's boundary as WGS84 GeoJSON, for drawing on a web map.

    The `Watershed` carries its geometry in the analysis CRS, which is where the
    modelling happens. A map wants degrees.
    """
    back = Transformer.from_crs(config.crs.analysis, CRS.from_epsg(4326), always_xy=True).transform
    return mapping(shapely_transform(back, unit.geometry))
