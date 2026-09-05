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

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import geopandas as gpd
import httpx
import numpy as np
import numpy.typing as npt
import rasterio
import shapely
from pyproj import CRS, Transformer
from rasterio.transform import rowcol

from floodline.compute import gauge_for_watershed, geometry_wgs84, marks_within, wgs84_bounds
from floodline.config import Config
from floodline.damage.estimate import NO_WATER, DamageEstimate, estimate_damage
from floodline.damage.ladder import DamageLadder, damage_ladder
from floodline.damage.uncertainty import DamageInterval, monte_carlo_damage
from floodline.damage.usace import UsaceCurves, ensure_usace_curves, load_usace_curves
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
from floodline.io.nsi import fetch_nsi_structures, structure_footprints
from floodline.io.overture import fetch_overture_buildings
from floodline.io.population import PopulationProduct, read_population_window
from floodline.io.raster import Raster
from floodline.io.sources import FetchContext, SourceError, find_dem_tiles, make_client
from floodline.terrain.route import route_terrain
from floodline.terrain.streams import link_raster
from floodline.validate.metrics import MarkMetrics, mark_metrics

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

    inventory: str = "none"
    """Which structure inventory the exposure came from."""

    ladder: DamageLadder | None = None
    """Damage at every discharge on the slider's ladder, so the map can answer "what
    would a bigger flood cost" without another pass over the raster."""

    marks: MarkMetrics | None = None
    """Modelled water surface scored against surveyed high-water marks. This is the
    project's only real extent validation - CSI needs an observed polygon and there
    is none - so it belongs in the result object rather than only in the map's
    JavaScript, where it used to live."""

    n_marks_available: int = 0
    night_population: float | None = None
    """Residents in the structures the model floods, from NSI's own per-structure
    counts. More direct than a gridded product: people in flooded buildings, not
    people in flooded cells."""

    day_population: float | None = None
    contents_damage: float | None = None

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
    marks_path: Path | None = None,
    graded_marks_only: bool = True,
    inventory: str = "nsi",
    download_curves: bool = False,
    multipliers: npt.NDArray[np.float64] | None = None,
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
        ladder_steps = multipliers if multipliers is not None else np.linspace(0.0, 3.0, 33)
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

        # ---- validation against surveyed marks -----------------------------
        scored: MarkMetrics | None = None
        n_marks = 0
        # Same file the service uses, so the CLI and the map validate against
        # identical ground truth rather than two copies that can drift apart.
        cache = marks_path or (config.paths.raw / "validation" / "high_water_marks_national.json")
        try:
            found = marks_within(unit, config, cache)
        except Exception as exc:
            found = []
            gaps.append(f"high-water marks unavailable: {type(exc).__name__}: {exc}")
        # USGS grades every mark; 1 and 2 are surveys to a few centimetres and 3 and
        # below are progressively rougher. On this watershed the rough ones carried an
        # RMSE of 4.9 m against 1.0 m for the good, so scoring everything would let
        # them set the headline number.
        usable = [m for m in found if not graded_marks_only or m.get("quality") in (1, 2)]
        n_marks = len(usable)
        if usable:
            forward = Transformer.from_crs(CRS.from_epsg(4326), config.crs.analysis, always_xy=True)
            surveyed, modelled_surface, ground = [], [], []
            rows, cols = depth_raster.data.shape
            for mark in usable:
                x, y = forward.transform(mark["lon"], mark["lat"])
                row, col = rowcol(depth_raster.transform, x, y)
                if not (0 <= int(row) < rows and 0 <= int(col) < cols):
                    continue
                cell_depth = float(depth_raster.data[int(row), int(col)])
                terrain = float(dem.data[int(row), int(col)])
                surveyed.append(mark["elev_m"])
                ground.append(terrain)
                modelled_surface.append(terrain + cell_depth if cell_depth > 0 else float("nan"))
            if surveyed:
                scored = mark_metrics(surveyed, modelled_surface, ground)

        # ---- exposure ------------------------------------------------------
        exposure: BuildingExposure | None = None
        damage_curves: UsaceCurves | None = None
        night_pop: float | None = None
        day_pop: float | None = None
        used_inventory = "none"

        if with_buildings and inventory == "nsi":
            started = time.perf_counter()
            try:
                # NSI is the only open US inventory that carries a value per structure,
                # and a depth-damage fraction is a fraction *of* something. Overture has
                # better geometry and no valuation; a flat rate per class overstated a
                # Houston sample by 1.4x.
                shape = shapely.from_geojson(json.dumps(geometry_wgs84(unit, config)))
                nsi = fetch_nsi_structures(shape, cache_key=unit.huc)
                if not len(nsi.structures):
                    gaps.append("NSI returned no structures inside this watershed")
                else:
                    boxes = structure_footprints(nsi.structures.to_crs(config.crs.analysis))
                    boxes["building_class"] = boxes["occtype"]
                    boxes["num_floors"] = boxes["num_story"]
                    exposure = building_depths(
                        depth_raster.data,
                        depth_raster.transform,
                        boxes,
                        unclamped_depth=margin,
                        hand=chain.hand.hand,
                        reach=reach_of,
                        config=config,
                        class_column="building_class",
                        storeys_column="num_floors",
                        default_class=config.damage.usace_default_occupancy,
                    )
                    frame = exposure.buildings
                    # One definition of how deep the water is at a building, derived
                    # the same way the ladder derives it: stage in its reach, minus its
                    # height above drainage, minus its own foundation height. The
                    # alternative was reducing the depth raster under the footprint,
                    # which is a different sampling of the same quantity - and the two
                    # disagreed by 12% on the damage total, which is exactly the sort
                    # of gap that makes a panel and a slider tell different stories.
                    stage_at = np.array(
                        [stages.by_reach.get(int(r), 0.0) for r in frame["reach_id"]],
                        dtype=np.float64,
                    )
                    hand_at = np.asarray(frame["hand_m"].to_numpy(), dtype=np.float64)
                    water = np.where(np.isfinite(hand_at), stage_at - hand_at, -np.inf)
                    frame["depth_m"] = np.maximum(water, 0.0)
                    found_at = np.asarray(frame["found_ht_m"].to_numpy(), dtype=np.float64)
                    frame["floor_margin_m"] = np.where(water > 0.0, water - found_at, -np.inf)
                    frame["floor_depth_m"] = np.maximum(frame["floor_margin_m"], 0.0)
                    wet = frame["floor_depth_m"] > 0.0
                    night_pop = float(frame.loc[wet, "pop_night"].sum())
                    day_pop = float(frame.loc[wet, "pop_day"].sum())
                    used_inventory = "nsi"
            except Exception as exc:
                gaps.append(f"NSI unavailable: {type(exc).__name__}: {exc}")
            timings["nsi"] = time.perf_counter() - started

            started = time.perf_counter()
            try:
                damage_curves = load_usace_curves(
                    ensure_usace_curves(download=download_curves), config=config
                )
            except Exception as exc:
                gaps.append(
                    f"USACE curve library unavailable ({type(exc).__name__}: {exc}), so "
                    "damage falls back to the unverified bundled constants"
                )
            timings["curves"] = time.perf_counter() - started

        elif with_buildings:
            started = time.perf_counter()
            try:
                used_inventory = "overture"
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
        ladder: DamageLadder | None = None
        if exposure is not None and len(exposure.buildings):
            started = time.perf_counter()
            frame = exposure.buildings
            # The signed margin, not the clamped depth. The USACE curves are defined
            # from -0.61 m and are non-zero at 0, so a clamped depth would charge every
            # dry building the curve's at-floor value: 13.4% of a house, times 205,754
            # of them on this watershed alone. -inf where HAND is undefined is finite
            # enough for the curve, which holds its first value below its first point.
            margins = np.where(
                np.isfinite(frame["floor_margin_m"].to_numpy()),
                frame["floor_margin_m"].to_numpy(),
                NO_WATER,
            )
            args = (
                margins,
                frame["floor_area_m2"].to_numpy(),
                frame["building_class"].to_numpy(dtype=object),
            )
            storeys = frame["storeys"].to_numpy()
            extra: dict[str, Any] = {}
            if damage_curves is not None and "val_struct" in frame.columns:
                extra = {
                    "structure_value": frame["val_struct"].to_numpy(),
                    "contents_value": frame["val_cont"].to_numpy(),
                    "contents_curves": damage_curves.contents,
                    "curves": damage_curves.structure,
                }
            damage = estimate_damage(*args, storeys=storeys, config=config, **extra)

            # ---- damage as a function of discharge -------------------------
            # Nothing about a building changes with flow; only the stage in its reach
            # does. So the whole ladder costs a gather per rung rather than another
            # pass over the raster.
            if "hand_m" in frame.columns and "reach_id" in frame.columns:
                stage_table = np.zeros((len(curves) and max(curves) + 1, len(ladder_steps)))
                for step, factor in enumerate(ladder_steps):
                    stepped = discharge_by_area_ratio(
                        discharge_cms * float(factor),
                        area_cells,
                        links,
                        chain.accumulation.accumulation,
                        config=config,
                    )
                    for reach, curve in curves.items():
                        stage_table[reach, step] = curve.stage_for_discharge(
                            stepped.get(reach, 0.0)
                        )
                ladder = damage_ladder(
                    frame["hand_m"].to_numpy(),
                    frame["reach_id"].to_numpy(),
                    frame["found_ht_m"].to_numpy()
                    if "found_ht_m" in frame.columns
                    else np.full(len(frame), config.exposure.floor_height_m),
                    frame["floor_area_m2"].to_numpy(),
                    frame["building_class"].to_numpy(dtype=object),
                    storeys,
                    stage_by_multiplier=stage_table,
                    multipliers=np.asarray(ladder_steps, dtype=np.float64),
                    base_discharge_cms=discharge_cms,
                    structure_value=extra.get("structure_value"),
                    contents_value=extra.get("contents_value"),
                    curves=extra.get("curves"),
                    contents_curves=extra.get("contents_curves"),
                    residents=frame["pop_night"].to_numpy()
                    if "pop_night" in frame.columns
                    else None,
                    # Four rungs the map layer interpolates between. They bracket the
                    # slider closely enough that the error between them is smaller than
                    # the model's own, and four is what fits in an RGBA image.
                    reference_multipliers=tuple(
                        float(m) for m in config.damage.map_reference_multipliers
                    ),
                    config=config,
                )

            mc = config.monte_carlo
            if samples is not None:
                mc = mc.model_copy(update={"n_samples": samples})
            interval = monte_carlo_damage(
                *args,
                storeys=storeys,
                floor_margin_m=margins,
                monte_carlo=mc,
                damage=config.damage,
                # The published per-depth spread on each curve. With one library there
                # is no family term, so this is the only curve uncertainty in the band.
                curve_sigma=damage_curves.sigma if damage_curves is not None else None,
                **extra,
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
            ladder=ladder,
            marks=scored,
            n_marks_available=n_marks,
            inventory=used_inventory,
            night_population=night_pop,
            day_population=day_pop,
            contents_damage=damage.contents_total if damage is not None else None,
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
