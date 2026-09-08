"""A small HTTP service that computes any US watershed on request.

The precomputed atlas is limited to the watersheds someone prepared in advance.
This removes that limit: click a point or type a postcode, and the whole model is
built for that watershed in the time it takes to read the DEM - measured at 11 to
25 seconds depending on size, over an ordinary connection.

Why a service rather than a self-contained page: a page published as an Artifact
cannot call out. Its content security policy forbids `fetch` to any external host,
so an Artifact can only ever carry what was inlined into it. Serving the page from
the same origin as the API removes that restriction entirely, and the same
application deploys unchanged to anything that runs Python.

Finished bundles are cached on disk by watershed and resolution, so the second
person to ask about a watershed waits for a file read rather than a computation.
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict
from pathlib import Path
from typing import Any

import httpx
import numpy as np
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from rasterio.errors import RasterioIOError

from floodline.api.app import API_DESCRIPTION, API_SUMMARY, attach_api, version_string
from floodline.assess import NoDischargeError, assess_watershed, buildings_geoparquet
from floodline.compute import (
    WatershedNotFoundError,
    compute_watershed,
    geometry_wgs84,
    watershed_by_huc,
    watershed_for_point,
    wgs84_bounds,
)
from floodline.core.config import Config
from floodline.io.sources import FetchContext, SourceError, find_gauges, make_client
from floodline.limits import (
    ConcurrencyLimiter,
    RateLimiter,
    TooBusyError,
    TooManyRequestsError,
)
from floodline.report.exposure_bundle import build_exposure_bundle
from floodline.settings import settings

__all__ = ["create_app"]

logger = logging.getLogger("floodline.service")

CACHE_SCHEMA = 3
"""Shape of a cached payload. Bump it whenever a field is added, removed or
reinterpreted, and every older entry becomes a miss instead of being served to code
that expects something else.

Learned the hard way: the exposure payload gained a damage ladder and four reference
multipliers, and caches written before that were still served afterwards. The new
decoder read the old image's channels as something they were not and drew a damage
layer covering most of the watershed for a flood that reached 6% of it.

Bumped to 3 because the numbers themselves moved, not the fields: the assessment now
event-matches the gauge and samples across curve families, so a cached discharge and
a cached damage interval from schema 2 are answers to a question this code no longer
asks. A stale value that still parses is the dangerous kind."""


def _fresh(payload: dict[str, Any]) -> bool:
    """Report whether a cached payload was written by this version of the schema."""
    return int(payload.get("schema", 0)) == CACHE_SCHEMA


WEB_ROOT = Path(__file__).parent / "web"
# The map is a built artefact now: `web/` at the repository root is the TypeScript
# source, and `npm run build` emits here. `methodology.html` is still hand-written and
# still lives beside it, which is why the build does not own the whole directory.
WEB_DIST = WEB_ROOT / "dist"
# The layer path is part of the query, not of the deployment: a different TIGERweb
# host still serves layer 2 of tigerWMS_Current. Only the base is a setting.
ZCTA = f"{settings().tigerweb_url}/tigerWMS_Current/MapServer/2/query"
ONELINE = settings().census_geocode_url


def evict_cache(cache: Path, budget_mb: float) -> int:
    """Delete the least recently used bundles until the cache fits its budget.

    Unbounded growth fills the volume and takes the service down with a failure that
    looks nothing like its cause, so this runs after every write rather than on a
    timer that somebody has to remember to start.

    Ordered by modification time rather than access time. Many filesystems mount with
    relatime or noatime, so access time is not reliably maintained, and a
    least-recently-*used* policy built on it would quietly become arbitrary.
    """
    files = sorted(cache.glob("*.json"), key=lambda f: f.stat().st_mtime)
    total = sum(f.stat().st_size for f in files)
    budget = budget_mb * 1024 * 1024
    removed = 0
    while files and total > budget:
        oldest = files.pop(0)
        try:
            total -= oldest.stat().st_size
            oldest.unlink()
            removed += 1
        except OSError:
            continue
    if removed:
        logger.info("evicted %d cached bundle(s) to stay under %.0f MB", removed, budget_mb)
    return removed


def _cache_path(root: Path, huc: str, resolution_m: float) -> Path:
    """Return the cache file for one watershed at one resolution."""
    return root / f"{huc}_{resolution_m:g}m.json"


def _exposure_cache_path(root: Path, huc: str, resolution_m: float, samples: int) -> Path:
    """Return the cache file for one exposure run.

    The sample count belongs in the key. It is a query parameter the caller may set
    anywhere from 50 to 5000, and it sets the width of the reported interval directly
    - a 400-sample run and a 5000-sample run are different answers, not the same
    answer computed twice. Keying on the watershed alone served whichever the first
    caller happened to ask for, silently, to everyone after.
    """
    return root / f"{huc}_{resolution_m:g}m_{samples}s_exposure.json"


def _exposure_stats(result: Any, config: Config) -> dict[str, Any]:
    """Flatten an assessment's exposure and damage into JSON-safe summary numbers."""
    exposed = result.buildings
    damage = result.damage
    interval = result.interval
    low, high = interval.count_interval if interval is not None else (0, 0)
    return {
        "inventory": result.inventory,
        "structures": len(exposed.buildings) if exposed is not None else 0,
        "inundated": int(exposed.n_inundated) if exposed is not None else 0,
        "inundated_low": int(low),
        "inundated_high": int(high),
        "wet_ground": int(exposed.n_wet_ground) if exposed is not None else 0,
        "night_population": result.night_population,
        "day_population": result.day_population,
        "damage": damage.total if damage is not None else None,
        "damage_structure": (damage.total - damage.contents_total if damage is not None else None),
        "damage_contents": damage.contents_total if damage is not None else None,
        "damage_low": interval.lower if interval is not None else None,
        "damage_high": interval.upper if interval is not None else None,
        "quantiles": list(interval.quantiles) if interval is not None else None,
        "exposed_value": damage.exposed_value_total if damage is not None else None,
        "loss_ratio": damage.loss_ratio if damage is not None else None,
        "curves_verified": bool(interval.curves_verified) if interval is not None else False,
        # Separate from the above: the point estimate can be priced against a
        # transcribed library while the band around it is widened by approximations.
        "all_families_verified": (
            bool(interval.all_families_verified) if interval is not None else False
        ),
        "curve_family": damage.family.value if damage is not None else None,
        "by_class": (
            dict(sorted(damage.by_class.items(), key=lambda kv: -kv[1])[:8])
            if damage is not None
            else {}
        ),
        "ladder": (
            {
                "multipliers": list(result.ladder.multipliers),
                "discharge_cms": list(result.ladder.discharge_cms),
                "damage": list(result.ladder.damage),
                "structure": list(result.ladder.structure),
                "contents": list(result.ladder.contents),
                "inundated": list(result.ladder.inundated),
                "in_channel": list(result.ladder.in_channel),
                "max_in_channel_share": result.ladder.max_in_channel_share,
                "residents": list(result.ladder.residents),
            }
            if result.ladder is not None
            else None
        ),
        "flooded_km2": result.flooded_km2,
        "max_depth_m": result.max_depth_m,
        "discharge_cms": result.discharge_cms,
    }


def create_app(
    *,
    config: Config | None = None,
    cache_dir: Path | None = None,
    max_cells: int = 40_000_000,
    marks_path: Path | None = None,
    max_concurrent: int = 2,
    rate_per_minute: float = 30.0,
    rate_burst: int = 10,
    cache_budget_mb: float = 2048.0,
) -> FastAPI:
    """Build the application.

    Parameters
    ----------
    config
        Base configuration. The analysis CRS is replaced per request with the UTM
        zone for the watershed in question, so one server works nationwide.
    cache_dir
        Where finished bundles are kept. Created if missing.
    max_cells
        Largest grid the service will attempt. Depression filling is global, so the
        whole watershed has to fit in memory; a request over this is refused with an
        explanation rather than being allowed to exhaust the machine.
    max_concurrent
        Watershed computations allowed in flight at once. This guards the machine:
        each one holds its whole grid resident, so a few in parallel exhaust memory
        whatever the request rate is.
    rate_per_minute, rate_burst
        Per-client token bucket. This guards everyone upstream - every request here
        becomes range reads against USGS and calls to USACE, all keyless and all
        wearing this machine's identity.
    cache_budget_mb
        Disk the finished bundles may occupy. Past it, the least recently used are
        deleted. Unbounded growth fills the volume and takes the service down with a
        failure that looks nothing like its cause.
    """
    base = config or Config()
    cache = cache_dir or settings().bundle_cache_dir
    cache.mkdir(parents=True, exist_ok=True)
    marks = marks_path or (base.paths.raw / "validation" / "high_water_marks_national.json")
    app = FastAPI(
        title="Floodline",
        version=version_string(),
        docs_url="/api/docs",
        summary=API_SUMMARY,
        description=API_DESCRIPTION,
    )
    heavy = ConcurrencyLimiter(limit=max_concurrent)
    rate = RateLimiter(per_minute=rate_per_minute, burst=rate_burst)

    # `/health` and `/ready` at the root, `POST /api/scenario` beside the routes the
    # page already calls. Both halves share these limiters rather than keeping one
    # budget each, because they are guarding the same process.
    attach_api(app, config=base, heavy=heavy, rate=rate)

    def client() -> httpx.Client:
        return make_client(base.sources)

    def guard(request: Request) -> None:
        """Refuse before doing the work, not after.

        A rejection has to be cheap and it has to say when to come back, or a caller
        retries immediately and the limit achieves nothing.
        """
        who = request.client.host if request.client else "unknown"
        try:
            rate.check(who)
        except TooManyRequestsError as exc:
            raise HTTPException(
                429,
                str(exc),
                headers={"Retry-After": str(max(1, int(exc.retry_after_s)))},
            ) from exc

    def evict() -> int:
        return evict_cache(cache, cache_budget_mb)

    # Hashed filenames, so they are immutable and can be cached hard. The document
    # itself is not: it names this build's bundle, and a stale one would ask for a
    # file that no longer exists.
    app.mount("/assets", StaticFiles(directory=WEB_DIST / "assets"), name="assets")

    @app.get("/")
    def index() -> FileResponse:
        return FileResponse(WEB_DIST / "index.html")

    @app.get("/methodology")
    def methodology() -> FileResponse:
        """Where the numbers come from, for a reader who wants that before trusting them.

        A page rather than a modal: it is long, it is linkable, and someone reading it
        is not mid-task on the map.
        """
        return FileResponse(WEB_ROOT / "methodology.html")

    @app.get("/api/geocode")
    def geocode(q: str = Query(min_length=3, max_length=200)) -> dict[str, Any]:
        """Resolve a postcode or street address to a coordinate.

        A bare five-digit postcode goes to the Census ZCTA layer, which has centroids
        for them; anything else goes to the Census address geocoder. Both are public
        and keyless, and both are the same federal source as the rest of the inputs.
        """
        text = q.strip()
        with client() as http:
            if text.isdigit() and len(text) == 5:
                response = http.get(
                    ZCTA,
                    params={
                        "where": f"ZCTA5='{text}'",
                        "outFields": "ZCTA5,CENTLAT,CENTLON",
                        "returnGeometry": "false",
                        "f": "json",
                    },
                )
                features = response.json().get("features", [])
                if not features:
                    raise HTTPException(404, f"no ZIP code area matching {text!r}")
                attributes = features[0]["attributes"]
                return {
                    "lon": float(attributes["CENTLON"]),
                    "lat": float(attributes["CENTLAT"]),
                    "label": f"ZIP {attributes['ZCTA5']}",
                }

            response = http.get(
                ONELINE,
                params={"address": text, "benchmark": "Public_AR_Current", "format": "json"},
            )
            matches = response.json().get("result", {}).get("addressMatches", [])
            if not matches:
                raise HTTPException(
                    404,
                    f"no match for {text!r}. A five-digit ZIP code works, or a full "
                    "street address with city and state.",
                )
            best = matches[0]
            return {
                "lon": float(best["coordinates"]["x"]),
                "lat": float(best["coordinates"]["y"]),
                "label": str(best["matchedAddress"]),
            }

    @app.get("/api/watershed")
    def watershed(
        lon: float = Query(ge=-180, le=180),
        lat: float = Query(ge=-90, le=90),
        level: int = Query(default=12, ge=2, le=16),
    ) -> dict[str, Any]:
        """Identify the watershed containing a point, and hand back its outline."""
        try:
            with client() as http:
                unit, local = watershed_for_point(lon, lat, level=level, config=base, client=http)
                # Inside the `with`: the gauge lookup reuses this client, and describing
                # the unit after it closed would mean opening a second one.
                return _describe(unit, local, http)
        except SourceError as exc:
            raise HTTPException(404, str(exc)) from exc

    @app.get("/api/watershed/{huc}")
    def watershed_by_code(huc: str) -> dict[str, Any]:
        """Identify a watershed by its HUC code."""
        if not huc.isdigit() or len(huc) % 2 or not 2 <= len(huc) <= 16:
            raise HTTPException(400, "a HUC code is an even number of digits, 2 to 16")
        try:
            with client() as http:
                unit, local = watershed_by_huc(huc, config=base, client=http)
                return _describe(unit, local, http)
        except WatershedNotFoundError as exc:
            raise HTTPException(404, str(exc)) from exc
        except SourceError as exc:
            raise HTTPException(502, f"upstream data source failed: {exc}") from exc

    def _gauges_in_bbox(unit: Any, local: Config, http: httpx.Client) -> int | None:
        """Count NWIS discharge gauges in the unit's bounding box, or None if unknown.

        A one-sided test, and cheap: a bbox query against the NWIS site service, no
        terrain and no DEM. Zero sites in the box proves there is no gauge in the
        watershed, because the box contains the polygon - which is worth knowing
        before someone spends two minutes routing terrain to be told the same thing.

        A non-zero count proves nothing. `gauge_for_watershed` additionally requires a
        site to snap to our own stream network within 40 cells and to carry a peak
        record, so some of these will not survive. Only the zero case is reported as
        certain; the rest is left for the model to settle.

        None means the lookup itself failed. That is not evidence of absence and must
        not be shown as one, so the caller stays quiet.
        """
        try:
            context = FetchContext(config=local, dest=cache, client=http)
            return len(find_gauges(context, wgs84_bounds(unit, local)))
        except (SourceError, httpx.HTTPError, OSError):
            return None

    def _describe(unit: Any, local: Config, http: httpx.Client | None = None) -> dict[str, Any]:
        cols, rows = _grid(unit)
        gauges = _gauges_in_bbox(unit, local, http) if http is not None else None
        return {
            # None where the lookup failed, so the page can tell "no gauge" from
            # "could not ask" and only warn about the first.
            "gauges_in_bbox": gauges,
            "huc": unit.huc,
            "name": unit.name,
            "area_km2": round(unit.area_km2, 1),
            "geometry": geometry_wgs84(unit, local),
            "analysis_crs": local.crs.analysis.to_string(),
            "cells_at": {
                "10": unit.cells_at(10.0),
                "30": unit.cells_at(30.0),
            },
            "too_big_at_10m": unit.cells_at(10.0) > max_cells,
            "grid": [cols, rows],
        }

    def _grid(unit: Any) -> tuple[int, int]:
        from floodline.io.ingest import estimate_cells

        return estimate_cells(unit.bounds, 10.0)

    @app.get("/api/compute/{huc}")
    def compute(
        request: Request,
        huc: str,
        resolution: float = Query(default=10.0, ge=1.0, le=100.0),
        refresh: bool = False,
    ) -> JSONResponse:
        """Compute a watershed's model, or return the cached one.

        Typically 11 to 25 seconds cold, depending on the watershed's size, dominated
        by reading the DEM. Cached afterwards.
        """
        guard(request)
        path = _cache_path(cache, huc, resolution)
        if path.exists() and not refresh:
            payload = json.loads(path.read_text())
            if _fresh(payload):
                payload["cached"] = True
                return JSONResponse(payload)
            logger.info("cache for %s is an older schema; recomputing", huc)

        try:
            # The slot is taken only for real work. A cache hit above never reaches
            # here, so a warm watershed stays instant however busy the machine is.
            with heavy, client() as http:
                unit, local = watershed_by_huc(huc, config=base, client=http)
                result = compute_watershed(
                    unit,
                    resolution_m=resolution,
                    config=local,
                    client=http,
                    max_cells=max_cells,
                    marks_path=marks if marks.exists() else None,
                )
        except TooBusyError as exc:
            raise HTTPException(503, str(exc), headers={"Retry-After": "30"}) from exc
        except WatershedNotFoundError as exc:
            raise HTTPException(404, str(exc)) from exc
        except SourceError as exc:
            raise HTTPException(502, f"upstream data source failed: {exc}") from exc
        except RasterioIOError as exc:
            raise HTTPException(
                502,
                "the elevation tiles for this watershed could not be read. The USGS "
                f"catalogue sometimes lists a tile it no longer serves. ({exc})",
            ) from exc
        except ValueError as exc:
            raise HTTPException(413, str(exc)) from exc

        payload = {
            **result.bundle.__dict__,
            "geometry": geometry_wgs84(unit, local),
            "seconds": {k: round(v, 2) for k, v in result.seconds.items()},
            "tiles_read": result.tiles_read,
            "warnings": result.warnings,
            "cached": False,
            "schema": CACHE_SCHEMA,
        }
        path.write_text(json.dumps(payload, default=float))
        evict()
        logger.info(
            "computed %s at %gm in %.1fs (%d tiles)",
            huc,
            resolution,
            result.total_seconds,
            result.tiles_read,
        )
        return JSONResponse(payload)

    @app.get("/api/exposure/{huc}")
    def exposure(
        request: Request,
        huc: str,
        resolution: float = Query(default=30.0, ge=10.0, le=100.0),
        samples: int = Query(default=400, ge=50, le=5000),
        refresh: bool = False,
    ) -> JSONResponse:
        """Value the structures a watershed's flood reaches, and price the damage.

        Much slower than `/api/compute` and deliberately a separate call: the National
        Structure Inventory takes a couple of minutes for a watershed this size, so a
        map should ask for this only when a reader wants it, not on every click.
        Cached afterwards like the compute bundle.
        """
        guard(request)
        path = _exposure_cache_path(cache, huc, resolution, samples)
        if path.exists() and not refresh:
            payload = json.loads(path.read_text())
            if _fresh(payload):
                payload["cached"] = True
                return JSONResponse(payload)
            logger.info("exposure cache for %s is an older schema; recomputing", huc)

        try:
            with heavy, client() as http:
                unit, local = watershed_by_huc(huc, config=base, client=http)
                result = assess_watershed(
                    unit,
                    config=local,
                    resolution_m=resolution,
                    client=http,
                    max_cells=max_cells,
                    samples=samples,
                    with_population=False,
                )
        except TooBusyError as exc:
            raise HTTPException(503, str(exc), headers={"Retry-After": "60"}) from exc
        except NoDischargeError as exc:
            raise HTTPException(422, str(exc)) from exc
        except WatershedNotFoundError as exc:
            raise HTTPException(404, str(exc)) from exc
        except SourceError as exc:
            raise HTTPException(502, f"upstream data source failed: {exc}") from exc
        except RasterioIOError as exc:
            raise HTTPException(502, f"the elevation tiles could not be read ({exc})") from exc
        except ValueError as exc:
            raise HTTPException(413, str(exc)) from exc

        frame = buildings_geoparquet(result)
        if frame is None or result.damage is None:
            raise HTTPException(
                502,
                "exposure could not be built for this watershed: "
                + ("; ".join(result.gaps) or "no structures returned"),
            )

        bundle = build_exposure_bundle(
            huc=huc,
            inventory=result.inventory,
            buildings=frame,
            transform=result.depth.transform,
            shape=result.depth.data.shape,
            bounds=result.depth.bounds,
            reduction=max(1, int(np.ceil(result.depth.data.shape[1] / 800))),
            config=local,
            currency=local.damage.currency,
            stats=_exposure_stats(result, local),
            notes=list(result.damage.notes),
            damage_by_multiplier=(
                dict(result.ladder.per_building)
                if result.ladder is not None and result.ladder.per_building
                else None
            ),
        )
        payload = {
            # ExposureBundle uses slots, so it has no __dict__ to splat.
            **asdict(bundle),
            "gaps": result.gaps,
            "warnings": result.warnings,
            "seconds": {k: round(v, 2) for k, v in result.seconds.items()},
            "cached": False,
            "schema": CACHE_SCHEMA,
        }
        path.write_text(json.dumps(payload, default=float))
        evict()
        logger.info("exposure %s at %gm in %.1fs", huc, resolution, sum(result.seconds.values()))
        return JSONResponse(payload)

    @app.get("/api/health")
    def health() -> dict[str, Any]:
        # Count what can actually be served, not what is on disk. After a schema bump
        # every older file is a miss, and reporting those as "cached" says the next
        # click will be instant when it is about to recompute the whole watershed.
        fresh = 0
        stale = 0
        for path in cache.glob("*.json"):
            try:
                fresh += _fresh(json.loads(path.read_text()))
            except (OSError, ValueError):
                stale += 1
                continue
        stale += len(list(cache.glob("*.json"))) - fresh - stale
        return {
            "ok": True,
            "cached_watersheds": fresh,
            "stale_entries": stale,
            "cache_schema": CACHE_SCHEMA,
            "max_cells": max_cells,
            "high_water_marks": marks.exists(),
        }

    return app
