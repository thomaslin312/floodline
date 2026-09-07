"""The service: three endpoints, no queue.

These routes are registered on the one application the container serves, alongside the
map interface, by `attach_api`. They were a separate ASGI app until the deployment made
the cost of that obvious: the image ran the API factory, so the map was simply absent
from the container, and `GET /` answered 404 on the only thing anyone visits. One app,
one port, one entrypoint. `/health` and `/ready` stay at the root, where an orchestrator
probes without knowing anything about the routing below them; everything else lives
under `/api`.

There is no job queue here, and that is a measurement rather than a preference.
Terrain routing runs in 0.1 to 0.6 seconds across the sixteen validation basins and a
scenario in 5 to 52 milliseconds. Against those numbers a queue would add a broker, a
worker process, a job table, a polling protocol and two more failure modes, in order to
defer work that finishes before a poll interval elapses. `POST /api/scenario` answers
synchronously.

The one genuinely slow step is fetching elevation, which is tens of seconds on a cold
basin. That is handled by ordering rather than by deferral: the cache is consulted
first, using a key computed from configuration alone, so a warm basin never opens a
socket. A cold one pays the fetch once and every later request is milliseconds.

Liveness and readiness are separate on purpose. `/health` touches nothing, because a
liveness probe that checks the database restarts a healthy container whenever the
database blinks, turning one outage into two. `/ready` checks everything and reports
each dependency by name.
"""

from __future__ import annotations

import logging
from importlib.metadata import PackageNotFoundError, version
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

from floodline.api.scenario import ScenarioOutcome, UpstreamUnavailableError, serve_scenario
from floodline.api.schemas import (
    HealthResponse,
    ReadyResponse,
    ScenarioRequest,
    ScenarioResponse,
)
from floodline.compute import WatershedNotFoundError, watershed_by_huc
from floodline.core.config import Config
from floodline.io.sources import SourceError, make_client
from floodline.limits import (
    ConcurrencyLimiter,
    RateLimiter,
    TooBusyError,
    TooManyRequestsError,
)
from floodline.settings import settings
from floodline.storage import LocalTerrainStore
from floodline.storage.base import TerrainStore

__all__ = ["API_DESCRIPTION", "API_SUMMARY", "attach_api", "create_api", "version_string"]

logger = logging.getLogger("floodline.api")

API_SUMMARY = "Screening-grade flood extent for any US watershed."
API_DESCRIPTION = (
    "Extent and depth are validated against surveyed high-water marks: median "
    "RMSE 2.16 m across 16 basins. The damage half of the model is not "
    "validated - it has no rank correlation with FEMA's own record by census "
    "tract - and this API deliberately does not serve currency figures."
)


def version_string() -> str:
    """Report the installed version, or a marker that this is an uninstalled tree."""
    try:
        return version("floodline")
    except PackageNotFoundError:  # running from a source tree without an install
        return "0.0.0+source"


def attach_api(
    app: FastAPI,
    *,
    config: Config | None = None,
    store: TerrainStore | None = None,
    heavy: ConcurrencyLimiter | None = None,
    rate: RateLimiter | None = None,
) -> FastAPI:
    """Register the service routes on an existing application.

    Every dependency is injectable so the tests can supply a store on a temporary
    directory and a config that points nowhere, which is what makes the cache-ordering
    test possible without a network.

    The limiters are injectable for a different reason: when these routes join the map
    interface, both halves guard the same machine, and two independent budgets on one
    process would let a caller spend each of them in turn. The caller passes its own.
    """
    base = config or Config()
    terrain_store = store or LocalTerrainStore()
    in_flight = heavy or ConcurrencyLimiter(limit=settings().max_concurrent)
    budget = rate or RateLimiter(per_minute=settings().rate_per_minute, burst=settings().rate_burst)

    def guard(request: Request) -> None:
        """Refuse over-rate before doing any work, and say when to come back."""
        who = request.client.host if request.client else "unknown"
        try:
            budget.check(who)
        except TooManyRequestsError as exc:
            raise HTTPException(
                429, str(exc), headers={"Retry-After": str(max(1, int(exc.retry_after_s)))}
            ) from exc

    @app.get("/health", response_model=HealthResponse, tags=["operations"])
    def health() -> HealthResponse:
        """Liveness. Answers from memory, touches nothing, cannot fail on a dependency."""
        return HealthResponse(version=version_string())

    @app.get("/ready", response_model=ReadyResponse, tags=["operations"])
    def ready() -> JSONResponse:
        """Readiness. Checks every dependency and names the one that is unhappy."""
        checks: dict[str, Any] = {}

        try:
            from floodline.db.session import check_ready

            ok, detail = check_ready()
        except ImportError as exc:  # the db extra is not installed
            ok, detail = False, f"database support not installed: {exc}"
        checks["database"] = {"ok": ok, "detail": detail}

        root = getattr(terrain_store, "root", None)
        writable = False
        detail = "no local root; store is not filesystem-backed"
        if root is not None:
            try:
                root.mkdir(parents=True, exist_ok=True)
                probe = root / ".readycheck"
                probe.write_text("ok")
                probe.unlink()
                writable, detail = True, f"writable at {root}"
            except OSError as exc:
                detail = f"{type(exc).__name__}: {exc}"
        checks["terrain_store"] = {"ok": writable or root is None, "detail": detail}

        everything = all(check["ok"] for check in checks.values())
        body = ReadyResponse(ready=everything, checks=checks)
        # 503 when not ready, so an orchestrator reads the status rather than the body.
        return JSONResponse(body.model_dump(), status_code=200 if everything else 503)

    @app.post(
        "/api/scenario",
        response_model=ScenarioResponse,
        tags=["model"],
        responses={
            422: {"description": "The request could not be a scenario."},
            404: {"description": "No such hydrologic unit."},
            502: {"description": "A public data source failed. The body names which."},
            503: {"description": "Too much work in flight, or a dependency is down."},
        },
    )
    def scenario(
        body: ScenarioRequest, request: Request, _: None = Depends(guard)
    ) -> ScenarioResponse:
        """Run one discharge over one watershed and return the result.

        Synchronous. A warm basin answers in milliseconds; a cold one pays for its
        elevation once, which is tens of seconds, and is warm afterwards.
        """
        try:
            with make_client(base.sources) as http:
                unit, local = watershed_by_huc(body.huc, config=base, client=http)
        except WatershedNotFoundError as exc:
            raise HTTPException(404, str(exc)) from exc
        except SourceError as exc:
            raise HTTPException(
                502,
                detail={
                    "detail": str(exc)[:300],
                    "upstream": "usgs-wbd",
                    "retryable": True,
                },
            ) from exc

        try:
            with in_flight:
                outcome = serve_scenario(
                    unit,
                    local,
                    discharge_cms=body.discharge_cms,
                    resolution_m=body.resolution_m,
                    store=terrain_store,
                )
        except TooBusyError as exc:
            raise HTTPException(503, str(exc), headers={"Retry-After": "30"}) from exc
        except UpstreamUnavailableError as exc:
            logger.warning("upstream %s failed: %s", exc.upstream, exc.detail)
            raise HTTPException(
                502,
                detail={
                    "detail": exc.detail,
                    "upstream": exc.upstream,
                    "retryable": exc.retryable,
                },
            ) from exc

        return _response(body, unit.name, outcome)

    return app


def create_api(
    *,
    config: Config | None = None,
    store: TerrainStore | None = None,
) -> FastAPI:
    """Build the service on its own, without the map interface.

    What the container serves is the merged application from `floodline.service`; this
    is the same routes with nothing else attached, which is what the API tests exercise
    so a failure there names the service rather than the page.
    """
    app = FastAPI(
        title="floodline",
        version=version_string(),
        docs_url="/api/docs",
        summary=API_SUMMARY,
        description=API_DESCRIPTION,
    )
    return attach_api(app, config=config, store=store)


def _response(body: ScenarioRequest, name: str, outcome: ScenarioOutcome) -> ScenarioResponse:
    """Shape an outcome for the wire."""
    flood = outcome.scenario.flood
    flooded_km2 = flood.n_wet * outcome.terrain.cell_area_m2 / 1e6
    depth = outcome.scenario.depth_m
    return ScenarioResponse(
        huc=body.huc,
        name=name,
        discharge_cms=body.discharge_cms,
        resolution_m=body.resolution_m,
        flooded_km2=round(flooded_km2, 2),
        basin_km2=round(outcome.basin_km2, 1),
        flooded_fraction=round(flooded_km2 / max(outcome.basin_km2, 1e-9), 4),
        max_depth_m=round(float(depth[depth > 0].max()) if (depth > 0).any() else 0.0, 2),
        wet_cells=int(flood.n_wet),
        terrain_cached=outcome.cached,
        params_hash=outcome.terrain.params_hash,
        terrain_seconds=round(outcome.terrain_seconds, 3),
        scenario_seconds=round(outcome.scenario_seconds, 4),
        total_seconds=round(outcome.terrain_seconds + outcome.scenario_seconds, 3),
        notes=list(outcome.scenario.notes),
    )
