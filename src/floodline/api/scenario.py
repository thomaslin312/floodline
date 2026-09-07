"""Serving one scenario, in the order the measurements say it has to happen.

The performance finding that shaped this: terrain routing takes a few tenths of a
second, and fetching the elevation it runs on takes tens of seconds. So the cache is
not an optimisation over the expensive step - the fetch *is* the expensive step, and a
cache that is consulted after it saves almost nothing.

Hence the order enforced here, which is the whole point of the module:

1. Build the cache key from configuration alone. `params_hash` needs no array, so this
   costs nothing and can happen before anything is fetched.
2. Ask the store. On a hit, the DEM is never requested and no socket is opened.
3. Only on a miss, fetch the elevation, compute terrain, and write it back.
4. Run the scenario, which is milliseconds either way.

`test_cache_hit_makes_no_network_calls` asserts step 2 short-circuits step 3, by
substituting a transport that raises on any request. That test is the reason this
module exists as its own unit rather than as a branch inside a route handler: an
ordering constraint that is only documented is one that gets reordered.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

import httpx
import rasterio

from floodline.core.config import Config
from floodline.io.ingest import Watershed, ingest_dem
from floodline.io.sources import FetchContext, SourceError, find_dem_tiles, make_client
from floodline.pipeline import (
    ScenarioResult,
    TerrainResult,
    compute_terrain,
    params_hash,
    run_scenario,
    terrain_params,
)
from floodline.settings import settings
from floodline.storage.base import TerrainStore

__all__ = ["ScenarioOutcome", "UpstreamUnavailableError", "serve_scenario"]


class UpstreamUnavailableError(RuntimeError):
    """A public data source failed, named, with whether retrying is worth it.

    Carried rather than flattened into a string so the route can map it to a status
    code and tell the caller whether to come back. A 502 that says "the elevation
    service is unreachable, retry" is actionable; a 500 is not.
    """

    def __init__(self, upstream: str, detail: str, *, retryable: bool = True) -> None:
        super().__init__(f"{upstream}: {detail}")
        self.upstream = upstream
        self.detail = detail
        self.retryable = retryable


@dataclass(frozen=True, slots=True)
class ScenarioOutcome:
    """Everything the response needs, including how the answer was arrived at."""

    terrain: TerrainResult
    scenario: ScenarioResult
    cached: bool
    terrain_seconds: float
    scenario_seconds: float
    basin_km2: float


def _fetch_terrain(
    unit: Watershed,
    config: Config,
    resolution_m: float,
    client: httpx.Client,
) -> TerrainResult:
    """Fetch the elevation and compute terrain. The slow path, and the only one.

    Every upstream failure is translated here rather than allowed to surface as a
    rasterio or httpx exception, because those name a library rather than a service and
    a caller cannot act on them.
    """
    from floodline.compute import VSICURL_ENV, wgs84_bounds

    context = FetchContext(config=config, dest=config.paths.raw, client=client)
    try:
        tiles = find_dem_tiles(context, int(resolution_m), wgs84_bounds(unit, config))
    except SourceError as exc:
        raise UpstreamUnavailableError("usgs-3dep-index", str(exc)[:300]) from exc

    urls = [f"/vsicurl/{tile['downloadURL']}" for tile in tiles if tile.get("downloadURL")]
    if not urls:
        raise UpstreamUnavailableError(
            "usgs-3dep-index",
            f"no {resolution_m:g} m elevation tiles cover {unit.name}",
            retryable=False,
        )
    try:
        with rasterio.Env(**VSICURL_ENV):
            dem = ingest_dem(
                urls,
                resolution_m=resolution_m,
                config=config,
                watershed=unit,
                max_cells=settings().max_cells,
            )
    except MemoryError as exc:
        raise UpstreamUnavailableError(
            "local", f"{unit.name} is too large to hold at {resolution_m:g} m", retryable=False
        ) from exc
    except Exception as exc:
        # rasterio wraps every read failure in one type, so the message is the only
        # thing distinguishing "tile is missing" from "S3 is refusing range reads".
        detail = f"{type(exc).__name__}: {exc}"[:300]
        raise UpstreamUnavailableError("usgs-3dep-tiles", detail) from exc

    return compute_terrain(
        dem.data,
        config=config,
        cellsize=dem.cellsize,
        transform=tuple(dem.transform)[:6],
        nodata=dem.nodata,
        resolution_m=resolution_m,
    )


def serve_scenario(
    unit: Watershed,
    config: Config,
    *,
    discharge_cms: float,
    resolution_m: float,
    store: TerrainStore,
    client: httpx.Client | None = None,
) -> ScenarioOutcome:
    """Answer one scenario, consulting the cache before fetching anything.

    The ordering is the contract. `store.exists` is called before any client is
    constructed, so a hit cannot reach the network even by accident - there is no
    client to reach it with.
    """
    key = params_hash(terrain_params(config, resolution_m))

    started = time.perf_counter()
    cached = store.exists(unit.huc, key)
    if cached:
        # Reconstituting from the store is deliberately not a partial recompute: the
        # artefact carries the five grids a scenario needs precisely so this path does
        # no routing.
        terrain = _terrain_from_store(store, unit.huc, key, config, resolution_m)
    else:
        owned = client is None
        active = client or make_client(config.sources)
        try:
            terrain = _fetch_terrain(unit, config, resolution_m, active)
        finally:
            if owned:
                active.close()
        store.put_hand(unit.huc, key, terrain.to_artifact())
    terrain_seconds = time.perf_counter() - started

    started = time.perf_counter()
    scenario = run_scenario(terrain, discharge_cms, config=config)
    scenario_seconds = time.perf_counter() - started

    return ScenarioOutcome(
        terrain=terrain,
        scenario=scenario,
        cached=cached,
        terrain_seconds=terrain_seconds,
        scenario_seconds=scenario_seconds,
        basin_km2=unit.area_km2,
    )


def _terrain_from_store(
    store: TerrainStore,
    huc: str,
    key: str,
    config: Config,
    resolution_m: float,
) -> TerrainResult:
    """Rebuild a `TerrainResult` from cached grids, without touching the network.

    The reach partition and the rating curves are derived from the cached arrays rather
    than stored, because they are cheap - milliseconds - and because a list of Python
    objects is a worse thing to serialise than the grids they come from. Nothing here
    re-runs depression filling or flow routing, which is what the five-grid artefact
    exists to make possible.
    """
    import numpy as np

    from floodline.core.hydro.rating import build_rating_curves, reach_catchments
    from floodline.core.terrain.flowacc import flow_accumulation
    from floodline.core.terrain.hand import HandResult
    from floodline.core.terrain.route import TerrainChain
    from floodline.core.terrain.streams import link_raster

    artifact = store.get_hand(huc, key)
    flowdir = artifact.flowdir
    accumulation = flow_accumulation(flowdir)
    finite = np.isfinite(artifact.hand)
    hand = HandResult(
        hand=artifact.hand.astype(np.float64),
        drainage_index=artifact.drainage_index,
        # Recounted from the arrays rather than stored: they are derived facts about
        # the grids, and a stored count could disagree with the grids it describes.
        n_valid=int(finite.sum()),
        cells_without_drainage=int((artifact.drainage_index < 0).sum()),
    )
    chain = TerrainChain(
        filled=artifact.filled.astype(np.float64),
        flowdir=flowdir,
        accumulation=accumulation,
        streams=artifact.streams,
        hand=hand,
        cells_raised_by_fill=0,
        flat_cells_before=0,
        flat_cells_after=0,
    )
    ids, links = link_raster(chain.streams, chain.flowdir)
    reach_of = reach_catchments(chain.hand.drainage_index, ids)
    transform = artifact.transform
    cellsize = (abs(transform[0]), abs(transform[4]))
    curves = build_rating_curves(
        chain.hand.hand, chain.filled, links, reach_of, config=config, cellsize=cellsize
    )
    return TerrainResult(
        chain=chain,
        link_ids=ids,
        links=links,
        reach_of_cell=reach_of,
        curves=curves,
        transform=transform,
        crs=artifact.crs,
        cellsize=cellsize,
        cell_area_m2=abs(cellsize[0] * cellsize[1]),
        params_hash=key,
        seconds=0.0,
    )
