"""The two halves of a flood model, separated at the point where discharge enters.

Everything before that point depends on the watershed and a handful of parameters, and
on nothing a request supplies: read the elevation, fill its depressions, route flow
across it, extract the channels, measure height above nearest drainage, and build a
rating curve for each reach. It is the expensive half - minutes - and it is the same
answer for every request about that basin.

Everything after depends on a discharge: transfer it to each reach, convert it to a
stage through that reach's curve, and compare the stage against HAND. It is the cheap
half - a second or two - and it is different for every request.

Before this module the two were one function, which meant a slider moving the discharge
re-ran depression filling. Splitting them is what makes a request cheap enough to serve
and a cache worth keeping.

**Neither half is new code.** Both call the same functions in the same order as
`compute_watershed` and `assess_watershed` always did; this module draws a line through
that sequence rather than changing anything on either side of it. That is what lets the
16-basin validation come back numerically identical.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

import numpy as np
import numpy.typing as npt

from floodline.core.config import Config
from floodline.core.hydro.inundate import Inundation, inundate
from floodline.core.hydro.rating import (
    RatingCurve,
    build_rating_curves,
    discharge_by_area_ratio,
    reach_catchments,
)
from floodline.core.hydro.stage import ReachStages, stage_field_from_discharge
from floodline.core.terrain.route import TerrainChain, route_terrain
from floodline.core.terrain.streams import link_raster
from floodline.storage.base import TerrainArtifact, params_hash

__all__ = [
    "ScenarioResult",
    "TerrainResult",
    "compute_terrain",
    "params_hash",
    "run_scenario",
    "terrain_params",
]


def terrain_params(config: Config, resolution_m: float, dem_source: str = "3dep") -> dict[str, Any]:
    """Return the parameters that determine a terrain result, for hashing into a key.

    Only what actually changes the arrays. Manning's roughness is not here: it enters
    through the rating curves, which are rebuilt per scenario, so including it would
    split the cache on a value the cached grids do not depend on. Nor is discharge, for
    the same reason and more obviously.

    Adding a parameter here invalidates every existing entry, which is the correct
    behaviour and the reason the hash is over values rather than a version number.
    """
    terrain = config.terrain
    return {
        "dem_source": dem_source,
        "resolution_m": float(resolution_m),
        "fill_epsilon": float(terrain.fill_epsilon),
        "fill_connectivity": terrain.fill_connectivity,
        "resolve_flats": bool(terrain.resolve_flats),
        "stream_threshold_cells": int(terrain.stream_threshold_cells),
        "min_stream_length_cells": int(terrain.min_stream_length_cells),
        "bathymetry_enabled": bool(config.bathymetry.enabled),
        "bathymetry_depth_coefficient_m": float(config.bathymetry.depth_coefficient_m),
        "bathymetry_depth_exponent": float(config.bathymetry.depth_exponent),
        "crs": config.crs.analysis.to_string(),
    }


@dataclass(frozen=True, slots=True)
class TerrainResult:
    """The request-independent half: everything a scenario needs and none of its inputs."""

    chain: TerrainChain
    link_ids: npt.NDArray[np.int64]
    links: list[list[int]]
    reach_of_cell: npt.NDArray[np.int64]
    curves: dict[int, RatingCurve]

    transform: tuple[float, float, float, float, float, float]
    crs: str
    cellsize: tuple[float, float]
    cell_area_m2: float
    params_hash: str
    seconds: float = 0.0

    @property
    def hand(self) -> npt.NDArray[np.floating]:
        """Height above nearest drainage, the surface every scenario is measured on."""
        return self.chain.hand.hand

    @property
    def streams(self) -> npt.NDArray[np.bool_]:
        """The channel network HAND is measured to. Matched to `hand` by construction."""
        return self.chain.streams

    def to_artifact(self) -> TerrainArtifact:
        """Render as the cacheable unit, for a `TerrainStore`."""
        return TerrainArtifact(
            hand=np.asarray(self.chain.hand.hand, dtype=np.float32),
            streams=np.asarray(self.chain.streams, dtype=bool),
            filled=np.asarray(self.chain.filled, dtype=np.float32),
            flowdir=np.asarray(self.chain.flowdir, dtype=np.int16),
            drainage_index=np.asarray(self.chain.hand.drainage_index, dtype=np.int64),
            transform=self.transform,
            crs=self.crs,
        )


@dataclass(frozen=True, slots=True)
class ScenarioResult:
    """The per-request half: one discharge turned into depths."""

    depth_m: npt.NDArray[np.floating]
    stages: ReachStages
    flood: Inundation
    discharge_cms: float
    margin_m: npt.NDArray[np.float64]
    """Signed distance from the water surface to the ground. Negative where the water
    stopped short, which the Monte Carlo needs and a clamped depth cannot say."""

    seconds: float = 0.0
    notes: list[str] = field(default_factory=list)


def compute_terrain(
    dem_data: npt.NDArray[np.floating],
    *,
    config: Config,
    cellsize: tuple[float, float],
    transform: tuple[float, float, float, float, float, float],
    nodata: float | None = None,
    resolution_m: float = 30.0,
    dem_source: str = "3dep",
) -> TerrainResult:
    """Condition the terrain, route it, and build a rating curve for every reach.

    Takes an elevation array rather than a watershed code, because that is what makes
    this half testable and cacheable: the caller decides where the array came from, and
    two callers with the same array and the same parameters get the same answer.

    Expensive and request-independent. Cache the result and call `run_scenario` against
    it as many times as there are discharges to ask about.

    The cache key does not depend on the array: `params_hash(terrain_params(...))` is
    computable from the configuration alone. So a caller should check the store
    *before* fetching a DEM, not after - on these basins the routing is a few tenths of
    a second and reading the elevation is tens of seconds, so a hit that still fetched
    would save almost nothing.
    """
    started = time.perf_counter()
    chain = route_terrain(dem_data, config=config, nodata=nodata, cellsize=cellsize)
    ids, links = link_raster(chain.streams, chain.flowdir)
    reach_of = reach_catchments(chain.hand.drainage_index, ids)
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
        crs=config.crs.analysis.to_string(),
        cellsize=cellsize,
        cell_area_m2=abs(cellsize[0] * cellsize[1]),
        params_hash=params_hash(terrain_params(config, resolution_m, dem_source)),
        seconds=time.perf_counter() - started,
    )


def run_scenario(
    terrain: TerrainResult,
    discharge_cms: float,
    *,
    config: Config,
    reference_area_cells: float | None = None,
) -> ScenarioResult:
    """Turn one discharge into a depth field over cached terrain.

    Cheap, and the only half a request has to pay for. The discharge is transferred to
    each reach by drainage-area ratio, converted to a stage through that reach's own
    synthetic rating curve, and compared against HAND.

    `reference_area_cells` is the contributing area the discharge was measured over -
    a gauge's, normally. Without it the whole raster's accumulation maximum is used,
    which is right when the discharge describes the outlet and wrong when it describes
    a gauge partway up, so a caller with a gauge should pass its area.
    """
    started = time.perf_counter()
    accumulation = terrain.chain.accumulation.accumulation
    area_cells = (
        float(reference_area_cells)
        if reference_area_cells is not None
        else float(np.nanmax(accumulation))
    )
    flows = discharge_by_area_ratio(
        discharge_cms, area_cells, terrain.links, accumulation, config=config
    )
    stages = stage_field_from_discharge(terrain.reach_of_cell, terrain.curves, flows)
    flood = inundate(
        terrain.hand,
        stages.stage_m,
        streams=terrain.streams,
        config=config,
        cell_area_m2=terrain.cell_area_m2,
    )
    # Unclamped: negative where the water stopped short. The depth raster floors at
    # zero and so cannot say how far short, which the Monte Carlo needs.
    hand = terrain.hand
    margin = np.where(np.isfinite(hand), stages.stage_m - hand, -np.inf)
    notes: list[str] = []
    if stages.reaches_without_a_curve:
        notes.append(
            f"{stages.reaches_without_a_curve} reach(es) had no rating curve and were "
            "left dry rather than guessed at"
        )
    return ScenarioResult(
        depth_m=flood.depth,
        stages=stages,
        flood=flood,
        discharge_cms=discharge_cms,
        margin_m=margin,
        seconds=time.perf_counter() - started,
        notes=notes,
    )
