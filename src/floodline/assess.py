"""The whole chain for one watershed, on real data: terrain to damage.

`compute_watershed` produces the web bundle - downsampled and 8-bit encoded, which is
right for a map and wrong for exposure, where a building's depth has to come off the
full-resolution grid. This runs the same terrain and hydraulics at native resolution
and carries on into footprints, population, damage and an uncertainty band, then
places the modelled discharge in the gauge's own history.

Nothing here invents an input. Every stage that could not get its data says so in
`Assessment.gaps` and leaves its numbers as None, because a screening model that
silently substitutes a default is worse than one that reports a hole.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

import geopandas as gpd
import httpx
import numpy as np
import numpy.typing as npt
import rasterio

from floodline.compute import gauge_for_watershed, wgs84_bounds
from floodline.config import Config
from floodline.damage.estimate import DamageEstimate, estimate_damage
from floodline.damage.uncertainty import DamageInterval, monte_carlo_damage
from floodline.exposure.buildings import BuildingExposure, building_depths
from floodline.exposure.population import PopulationExposure, population_affected
from floodline.hydraulics.frequency import HistoricalContext, flood_frequency
from floodline.hydraulics.inundate import inundate
from floodline.hydraulics.rating import (
    build_rating_curves,
    discharge_by_area_ratio,
    reach_catchments,
)
from floodline.hydraulics.stage import stage_field_from_discharge
from floodline.io.ingest import Watershed, ingest_dem
from floodline.io.overture import fetch_overture_buildings
from floodline.io.population import PopulationProduct, read_population_window
from floodline.io.raster import Raster
from floodline.io.sources import FetchContext, SourceError, find_dem_tiles, make_client
from floodline.terrain.route import route_terrain
from floodline.terrain.streams import link_raster

__all__ = ["Assessment", "NoDischargeError", "assess_watershed"]


class NoDischargeError(RuntimeError):
    """No gauge in the watershed and no discharge supplied.

    Raised rather than defaulted. `compute_watershed` stands in a severe-flood
    scenario so the map has something to draw, which is right for a map and wrong
    here: building counts and damage totals attached to an invented discharge read
    as measurements no matter how they are labelled.
    """


@dataclass(frozen=True, slots=True)
class Assessment:
    """Everything the model can say about one watershed and one discharge."""

    unit: Watershed
    resolution_m: float
    discharge_cms: float
    gauged: bool

    depth: Raster
    """Water depth in metres on the analysis grid."""

    margin: npt.NDArray[np.float64]
    """`stage - HAND`, unclamped, so dry ground is negative. What the Monte Carlo needs."""

    n_wet_cells: int
    flooded_km2: float
    max_depth_m: float

    history: HistoricalContext | None
    buildings: BuildingExposure | None
    people: PopulationExposure | None
    damage: DamageEstimate | None
    interval: DamageInterval | None

    seconds: dict[str, float] = field(default_factory=dict)
    gaps: list[str] = field(default_factory=list)
    """Stages that could not run, and why. Empty means every input was found."""

    warnings: list[str] = field(default_factory=list)


def assess_watershed(
    unit: Watershed,
    *,
    config: Config,
    resolution_m: float = 30.0,
    discharge_cms: float | None = None,
    client: httpx.Client | None = None,
    max_cells: int = 60_000_000,
    with_buildings: bool = True,
    with_population: bool = True,
    download_population: bool = False,
    population_product: PopulationProduct = PopulationProduct.WORLDPOP_CONSTRAINED,
    samples: int | None = None,
) -> Assessment:
    """Run terrain, hydraulics, exposure and damage for one watershed.

    Parameters
    ----------
    unit
        From `compute.watershed_by_huc` or `watershed_for_point`.
    discharge_cms
        Overrides the gauge. When None, the watershed's own gauge peak is used, and
        where there is no gauge the run is flagged rather than given a scenario:
        exposure numbers attached to an invented discharge would read as measurements.

    Returns
    -------
    Assessment
    """
    from floodline.compute import VSICURL_ENV

    timings: dict[str, float] = {}
    gaps: list[str] = []
    warnings: list[str] = []

    owned = client is None
    active = client or make_client(config.sources)
    context = FetchContext(config=config, dest=config.paths.raw, client=active)

    try:
        # ---- terrain -------------------------------------------------------
        started = time.perf_counter()
        west, south, east, north = wgs84_bounds(unit, config)
        tiles = find_dem_tiles(context, int(resolution_m), (west, south, east, north))
        urls = [f"/vsicurl/{t['downloadURL']}" for t in tiles if t.get("downloadURL")]
        if not urls:
            raise SourceError(f"no {resolution_m:g} m 3DEP tiles cover {unit.name}")
        with rasterio.Env(**VSICURL_ENV):
            dem = ingest_dem(
                urls,
                resolution_m=resolution_m,
                config=config,
                watershed=unit,
                max_cells=max_cells,
            )
        timings["dem"] = time.perf_counter() - started

        started = time.perf_counter()
        chain = route_terrain(dem.data, config=config, nodata=dem.nodata, cellsize=dem.cellsize)
        timings["terrain"] = time.perf_counter() - started
        if not chain.drains_completely:
            warnings.append(
                f"{chain.accumulation.cells_draining_to_flats:,} cells drain into flats"
            )

        # ---- hydraulics ----------------------------------------------------
        started = time.perf_counter()
        ids, links = link_raster(chain.streams, chain.flowdir)
        reach_of = reach_catchments(chain.hand.drainage_index, ids)
        curves = build_rating_curves(
            chain.hand.hand, chain.filled, links, reach_of, config=config, cellsize=dem.cellsize
        )

        gauge: dict[str, Any] | None = None
        history: HistoricalContext | None = None
        if discharge_cms is None:
            gauge = gauge_for_watershed(unit, chain, dem, context)
            if gauge is None:
                raise NoDischargeError(
                    f"no USGS gauge inside {unit.name} ({unit.huc}), so there is no observed "
                    "discharge to drive the model. Pass one explicitly to get exposure and "
                    "damage numbers, and label them as a scenario when you do."
                )
            discharge_cms = float(gauge["discharge_cms"])

        gauged = gauge is not None
        area_cells = (
            float(gauge["area_cells"])
            if gauge is not None
            else unit.area_km2 * 1e6 / dem.cell_area_m2
        )
        flows = discharge_by_area_ratio(
            discharge_cms, area_cells, links, chain.accumulation.accumulation, config=config
        )
        stages = stage_field_from_discharge(reach_of, curves, flows)
        flood = inundate(
            chain.hand.hand,
            stages.stage_m,
            streams=chain.streams,
            config=config,
            cell_area_m2=dem.cell_area_m2,
        )
        # Unclamped: negative where the water stopped short. The depth raster floors
        # at zero and so cannot say how far short, which the Monte Carlo needs.
        margin = np.where(np.isfinite(chain.hand.hand), stages.stage_m - chain.hand.hand, -np.inf)
        timings["hydraulics"] = time.perf_counter() - started

        if gauge is not None and gauge.get("series"):
            fit = flood_frequency(gauge["series"], site=str(gauge.get("site", "")))
            history = fit.context_for(discharge_cms)

        depth_raster = dem.with_data(flood.depth)

        # ---- exposure ------------------------------------------------------
        exposure: BuildingExposure | None = None
        if with_buildings:
            started = time.perf_counter()
            try:
                fetched = fetch_overture_buildings(
                    (west, south, east, north),
                    config=config,
                    default_class=config.damage.default_class,
                )
                footprints = fetched.buildings.to_crs(config.crs.analysis)
                # Overture is queried on the watershed's bounding box, which over a
                # meandering HUC holds far more ground than the unit itself. On the
                # first Houston run that made the denominator 499,769 buildings and
                # put 19,851 of them outside the DEM entirely. Clip to the polygon so
                # "x of y buildings" compares like with like.
                n_box = len(footprints)
                inside = footprints.geometry.intersects(unit.geometry)
                footprints = footprints.loc[inside].reset_index(drop=True)
                dropped = n_box - len(footprints)
                if dropped:
                    warnings.append(
                        f"{dropped:,} of {n_box:,} footprints in the query box fall "
                        "outside the watershed and were dropped"
                    )
                exposure = building_depths(
                    depth_raster.data,
                    depth_raster.transform,
                    footprints,
                    unclamped_depth=margin,
                    config=config,
                    default_class=config.damage.default_class,
                )
            except Exception as exc:
                gaps.append(f"building footprints unavailable: {type(exc).__name__}: {exc}")
            timings["buildings"] = time.perf_counter() - started

        people: PopulationExposure | None = None
        if with_population:
            started = time.perf_counter()
            try:
                grid = read_population_window(
                    depth_raster, product=population_product, download=download_population
                )
                people = population_affected(depth_raster.data, grid.raster.data, config=config)
            except Exception as exc:
                gaps.append(f"population grid unavailable: {type(exc).__name__}: {exc}")
            timings["population"] = time.perf_counter() - started

        # ---- damage --------------------------------------------------------
        damage: DamageEstimate | None = None
        interval: DamageInterval | None = None
        if exposure is not None and len(exposure.buildings):
            started = time.perf_counter()
            frame = exposure.buildings
            args = (
                frame["floor_depth_m"].to_numpy(),
                frame["floor_area_m2"].to_numpy(),
                frame["building_class"].to_numpy(dtype=object),
            )
            storeys = frame["storeys"].to_numpy()
            damage = estimate_damage(*args, storeys=storeys, config=config)
            mc = config.monte_carlo
            if samples is not None:
                mc = mc.model_copy(update={"n_samples": samples})
            interval = monte_carlo_damage(
                *args,
                storeys=storeys,
                floor_margin_m=frame["floor_margin_m"].to_numpy(),
                monte_carlo=mc,
                damage=config.damage,
            )
            timings["damage"] = time.perf_counter() - started

        return Assessment(
            unit=unit,
            resolution_m=resolution_m,
            discharge_cms=discharge_cms,
            gauged=gauged,
            depth=depth_raster,
            margin=margin,
            n_wet_cells=flood.n_wet,
            flooded_km2=flood.area_m2 / 1e6,
            max_depth_m=flood.max_depth_m,
            history=history,
            buildings=exposure,
            people=people,
            damage=damage,
            interval=interval,
            seconds=timings,
            gaps=gaps,
            warnings=warnings,
        )
    finally:
        if owned:
            active.close()


def buildings_geoparquet(assessment: Assessment) -> gpd.GeoDataFrame | None:
    """Return the priced building table, or None when exposure did not run."""
    if assessment.buildings is None or assessment.damage is None:
        return None
    frame = assessment.buildings.buildings.copy()
    frame["damage"] = assessment.damage.per_building
    return frame
