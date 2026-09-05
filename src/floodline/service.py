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
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, JSONResponse
from rasterio.errors import RasterioIOError

from floodline.assess import NoDischargeError, assess_watershed, buildings_geoparquet
from floodline.compute import (
    WatershedNotFoundError,
    compute_watershed,
    geometry_wgs84,
    watershed_by_huc,
    watershed_for_point,
)
from floodline.config import Config
from floodline.io.sources import SourceError, make_client
from floodline.report.exposure_bundle import build_exposure_bundle

__all__ = ["create_app"]

logger = logging.getLogger("floodline.service")

CACHE_SCHEMA = 2
"""Shape of a cached payload. Bump it whenever a field is added, removed or
reinterpreted, and every older entry becomes a miss instead of being served to code
that expects something else.

Learned the hard way: the exposure payload gained a damage ladder and four reference
multipliers, and caches written before that were still served afterwards. The new
decoder read the old image's channels as something they were not and drew a damage
layer covering most of the watershed for a flood that reached 6% of it."""


def _fresh(payload: dict[str, Any]) -> bool:
    """Report whether a cached payload was written by this version of the schema."""
    return int(payload.get("schema", 0)) == CACHE_SCHEMA


WEB_ROOT = Path(__file__).parent / "web"
ZCTA = (
    "https://tigerweb.geo.census.gov/arcgis/rest/services/TIGERweb/"
    "tigerWMS_Current/MapServer/2/query"
)
ONELINE = "https://geocoding.geo.census.gov/geocoder/locations/onelineaddress"


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
    """
    base = config or Config()
    cache = cache_dir or Path("outputs/cache")
    cache.mkdir(parents=True, exist_ok=True)
    marks = marks_path or (base.paths.raw / "validation" / "high_water_marks_national.json")
    app = FastAPI(title="floodline", docs_url="/api/docs")

    def client() -> httpx.Client:
        return make_client(base.sources)

    @app.get("/")
    def index() -> FileResponse:
        return FileResponse(WEB_ROOT / "index.html")

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
        except SourceError as exc:
            raise HTTPException(404, str(exc)) from exc
        return _describe(unit, local)

    @app.get("/api/watershed/{huc}")
    def watershed_by_code(huc: str) -> dict[str, Any]:
        """Identify a watershed by its HUC code."""
        if not huc.isdigit() or len(huc) % 2 or not 2 <= len(huc) <= 16:
            raise HTTPException(400, "a HUC code is an even number of digits, 2 to 16")
        try:
            with client() as http:
                unit, local = watershed_by_huc(huc, config=base, client=http)
        except WatershedNotFoundError as exc:
            raise HTTPException(404, str(exc)) from exc
        except SourceError as exc:
            raise HTTPException(502, f"upstream data source failed: {exc}") from exc
        return _describe(unit, local)

    def _describe(unit: Any, local: Config) -> dict[str, Any]:
        cols, rows = _grid(unit)
        return {
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
        huc: str,
        resolution: float = Query(default=10.0, ge=1.0, le=100.0),
        refresh: bool = False,
    ) -> JSONResponse:
        """Compute a watershed's model, or return the cached one.

        Typically 11 to 25 seconds cold, depending on the watershed's size, dominated
        by reading the DEM. Cached afterwards.
        """
        path = _cache_path(cache, huc, resolution)
        if path.exists() and not refresh:
            payload = json.loads(path.read_text())
            if _fresh(payload):
                payload["cached"] = True
                return JSONResponse(payload)
            logger.info("cache for %s is an older schema; recomputing", huc)

        try:
            with client() as http:
                unit, local = watershed_by_huc(huc, config=base, client=http)
                result = compute_watershed(
                    unit,
                    resolution_m=resolution,
                    config=local,
                    client=http,
                    max_cells=max_cells,
                    marks_path=marks if marks.exists() else None,
                )
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
        path = _exposure_cache_path(cache, huc, resolution, samples)
        if path.exists() and not refresh:
            payload = json.loads(path.read_text())
            if _fresh(payload):
                payload["cached"] = True
                return JSONResponse(payload)
            logger.info("exposure cache for %s is an older schema; recomputing", huc)

        try:
            with client() as http:
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
        logger.info("exposure %s at %gm in %.1fs", huc, resolution, sum(result.seconds.values()))
        return JSONResponse(payload)

    @app.get("/api/health")
    def health() -> dict[str, Any]:
        cached = sorted(p.name for p in cache.glob("*.json"))
        return {
            "ok": True,
            "cached_watersheds": len(cached),
            "max_cells": max_cells,
            "high_water_marks": marks.exists(),
        }

    return app
