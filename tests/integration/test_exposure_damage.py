"""Terrain to damage on the synthetic catchment, end to end and offline."""

from __future__ import annotations

import geopandas as gpd
import numpy as np
import pytest
from rasterio.transform import Affine
from shapely.geometry import box

from floodline.config import Config, CurveFamily, ExposureConfig, MonteCarloConfig
from floodline.damage.curves import BUNDLED_FAMILIES
from floodline.damage.estimate import estimate_damage
from floodline.damage.uncertainty import monte_carlo_damage
from floodline.exposure.buildings import building_depths
from floodline.exposure.population import population_affected
from floodline.hydraulics.inundate import inundate
from floodline.synthetic import make_synthetic_catchment
from floodline.terrain.route import route_terrain

CELL = 10.0


@pytest.fixture(scope="module")
def flooded() -> tuple[np.ndarray, Affine]:
    """A depth raster from the synthetic catchment, on a 10 m grid."""
    catchment = make_synthetic_catchment(rows=64, cols=64, n_pits=3, seed=11)
    chain = route_terrain(catchment.dem, cellsize=(CELL, CELL))
    result = inundate(
        chain.hand.hand,
        6.0,
        streams=chain.streams,
        cell_area_m2=CELL * CELL,
        require_connectivity=False,
    )
    transform = Affine.translation(0.0, 64 * CELL) * Affine.scale(CELL, -CELL)
    return result.depth, transform


def _grid_of_buildings(transform: Affine, rows: int, cols: int) -> gpd.GeoDataFrame:
    """One 12 x 12 m footprint in the middle of every fourth cell."""
    shapes = []
    for row in range(2, rows - 2, 4):
        for col in range(2, cols - 2, 4):
            x, y = transform * (col + 0.5, row + 0.5)
            shapes.append(box(x - 6, y - 6, x + 6, y + 6))
    return gpd.GeoDataFrame(
        {"height": [6.0] * len(shapes), "building_class": ["residential"] * len(shapes)},
        geometry=shapes,
        crs="EPSG:6587",
    )


def test_chain_runs_from_depth_raster_to_a_damage_interval(
    flooded: tuple[np.ndarray, Affine],
) -> None:
    depth, transform = flooded
    buildings = _grid_of_buildings(transform, 64, 64)

    exposed = building_depths(depth, transform, buildings, config=Config())
    assert exposed.n_input == len(buildings)
    assert exposed.n_inundated > 0, "the synthetic flood should reach some buildings"
    assert exposed.n_inundated <= exposed.n_wet_ground

    point = estimate_damage(
        exposed.buildings["floor_depth_m"].to_numpy(),
        exposed.buildings["floor_area_m2"].to_numpy(),
        exposed.buildings["building_class"].to_numpy(dtype=object),
        storeys=exposed.buildings["storeys"].to_numpy(),
    )
    assert point.total > 0
    assert point.n_damaged == exposed.n_inundated
    assert point.total <= point.exposed_value_total

    interval = monte_carlo_damage(
        exposed.buildings["floor_depth_m"].to_numpy(),
        exposed.buildings["floor_area_m2"].to_numpy(),
        exposed.buildings["building_class"].to_numpy(dtype=object),
        storeys=exposed.buildings["storeys"].to_numpy(),
        monte_carlo=MonteCarloConfig(n_samples=100, seed=3),
    )
    assert interval.lower <= interval.median <= interval.upper
    assert interval.lower > 0


def test_a_higher_stage_floods_more_buildings_and_costs_more() -> None:
    catchment = make_synthetic_catchment(rows=64, cols=64, n_pits=3, seed=11)
    chain = route_terrain(catchment.dem, cellsize=(CELL, CELL))
    transform = Affine.translation(0.0, 64 * CELL) * Affine.scale(CELL, -CELL)
    buildings = _grid_of_buildings(transform, 64, 64)

    totals, counts = [], []
    for stage in (3.0, 9.0):
        depth = inundate(
            chain.hand.hand, stage, streams=chain.streams, require_connectivity=False
        ).depth
        exposed = building_depths(depth, transform, buildings)
        result = estimate_damage(
            exposed.buildings["floor_depth_m"].to_numpy(),
            exposed.buildings["floor_area_m2"].to_numpy(),
            exposed.buildings["building_class"].to_numpy(dtype=object),
            storeys=exposed.buildings["storeys"].to_numpy(),
        )
        totals.append(result.total)
        counts.append(exposed.n_inundated)

    assert counts[1] > counts[0]
    assert totals[1] > totals[0]


def test_curve_family_choice_moves_the_total_more_than_the_stage_error(
    flooded: tuple[np.ndarray, Affine],
) -> None:
    """The reason families are sampled rather than one being picked."""
    depth, transform = flooded
    exposed = building_depths(depth, transform, _grid_of_buildings(transform, 64, 64))
    args = (
        exposed.buildings["floor_depth_m"].to_numpy(),
        exposed.buildings["floor_area_m2"].to_numpy(),
        exposed.buildings["building_class"].to_numpy(dtype=object),
    )
    storeys = exposed.buildings["storeys"].to_numpy()

    by_family = {
        family: estimate_damage(*args, storeys=storeys, family=family).total
        for family in BUNDLED_FAMILIES
    }
    spread = max(by_family.values()) - min(by_family.values())

    single = monte_carlo_damage(
        *args,
        storeys=storeys,
        monte_carlo=MonteCarloConfig(
            n_samples=200, cost_sigma_frac=1e-9, curve_family_weights={CurveFamily.HAZUS: 1.0}
        ),
    )
    assert spread > (single.upper - single.lower)


def test_population_and_buildings_agree_on_where_the_water_is(
    flooded: tuple[np.ndarray, Affine],
) -> None:
    depth, transform = flooded
    people = np.where(np.isfinite(depth), 4.0, 0.0)
    counted = population_affected(depth, people, config=Config())

    exposed = building_depths(
        depth,
        transform,
        _grid_of_buildings(transform, 64, 64),
        config=ExposureConfig(floor_height_m=0.01),
    )
    # Both are reading the same wet cells, so neither can be zero while the other is not.
    assert (counted.people_affected > 0) == (exposed.n_inundated > 0)
    assert 0.0 <= counted.share_affected <= 1.0
