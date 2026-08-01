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
from pathlib import Path
from typing import Any

import httpx
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, JSONResponse
from rasterio.errors import RasterioIOError

from floodline.compute import (
    compute_watershed,
    geometry_wgs84,
    watershed_by_huc,
    watershed_for_point,
)
from floodline.config import Config
from floodline.io.sources import SourceError, make_client

__all__ = ["create_app"]

logger = logging.getLogger("floodline.service")

WEB_ROOT = Path(__file__).parent / "web"
ZCTA = (
    "https://tigerweb.geo.census.gov/arcgis/rest/services/TIGERweb/"
    "tigerWMS_Current/MapServer/2/query"
)
ONELINE = "https://geocoding.geo.census.gov/geocoder/locations/onelineaddress"


def _cache_path(root: Path, huc: str, resolution_m: float) -> Path:
    """Return the cache file for one watershed at one resolution."""
    return root / f"{huc}_{resolution_m:g}m.json"


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
        except SourceError as exc:
            raise HTTPException(404, str(exc)) from exc
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
            payload["cached"] = True
            return JSONResponse(payload)

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
