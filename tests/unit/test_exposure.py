from __future__ import annotations

import geopandas as gpd
import numpy as np
import pytest
from rasterio.transform import Affine
from shapely.geometry import box

from floodline.config import BuildingDepthStat, ExposureConfig
from floodline.exposure.buildings import building_depths
from floodline.exposure.population import population_affected

# A 10x10 grid of 1 m cells with its origin at (0, 10) so row 0 is the top.
TRANSFORM = Affine.translation(0.0, 10.0) * Affine.scale(1.0, -1.0)
CRS = "EPSG:6587"


def _depth(values: dict[tuple[int, int], float]) -> np.ndarray:
    grid = np.zeros((10, 10), dtype=np.float64)
    for (row, col), value in values.items():
        grid[row, col] = value
    return grid


def _config(**kwargs: object) -> ExposureConfig:
    """Test footprints are metre-scale, so the real 10 m2 noise filter would eat them."""
    return ExposureConfig(min_building_area_m2=0.1, **kwargs)  # type: ignore[arg-type]


def _buildings(*boxes: tuple[float, float, float, float], **columns: object) -> gpd.GeoDataFrame:
    return gpd.GeoDataFrame(
        {"id": list(range(len(boxes))), **columns},
        geometry=[box(*b) for b in boxes],
        crs=CRS,
    )


def test_depth_is_read_from_under_the_footprint() -> None:
    # Cell (row 1, col 2) spans x 2..3, y 8..9.
    grid = _depth({(1, 2): 1.4})
    result = building_depths(grid, TRANSFORM, _buildings((2.1, 8.1, 2.9, 8.9)), config=_config())
    assert result.buildings["depth_m"].iloc[0] == pytest.approx(1.4)


def test_floor_freeboard_is_subtracted_from_ground_depth() -> None:
    grid = _depth({(1, 2): 1.0})
    config = _config(floor_height_m=0.4)
    result = building_depths(grid, TRANSFORM, _buildings((2.1, 8.1, 2.9, 8.9)), config=config)
    assert result.buildings["floor_depth_m"].iloc[0] == pytest.approx(0.6)


def test_water_below_the_floor_does_not_inundate() -> None:
    grid = _depth({(1, 2): 0.1})
    config = _config(floor_height_m=0.15)
    result = building_depths(grid, TRANSFORM, _buildings((2.1, 8.1, 2.9, 8.9)), config=config)
    assert result.n_wet_ground == 1
    assert result.n_inundated == 0


@pytest.mark.parametrize(
    ("stat", "expected"),
    [
        (BuildingDepthStat.MAX, 4.0),
        (BuildingDepthStat.MEAN, 1.0),
        # linear interpolation on [0, 0, 0, 4] at the 90th percentile
        (BuildingDepthStat.P90, 2.8),
    ],
)
def test_depth_statistics_reduce_the_footprint_differently(
    stat: BuildingDepthStat, expected: float
) -> None:
    # Four cells under one footprint: 0, 0, 0, 4.
    grid = _depth({(1, 1): 0.0, (1, 2): 0.0, (2, 1): 0.0, (2, 2): 4.0})
    result = building_depths(
        grid,
        TRANSFORM,
        _buildings((1.1, 7.1, 2.9, 8.9)),
        config=_config(building_depth_stat=stat, floor_height_m=0.01),
    )
    assert result.buildings["depth_m"].iloc[0] == pytest.approx(expected)


def test_p90_resists_a_single_deep_cell_that_max_follows() -> None:
    grid = np.zeros((10, 10))
    grid[1:4, 1:4] = 0.5
    grid[2, 2] = 9.0  # one pit the DEM dug
    footprint = _buildings((1.05, 6.05, 3.95, 8.95))
    p90 = building_depths(
        grid, TRANSFORM, footprint, config=_config(building_depth_stat=BuildingDepthStat.P90)
    )
    biggest = building_depths(
        grid, TRANSFORM, footprint, config=_config(building_depth_stat=BuildingDepthStat.MAX)
    )
    assert biggest.buildings["depth_m"].iloc[0] == pytest.approx(9.0)
    assert p90.buildings["depth_m"].iloc[0] < 3.0


def test_small_footprints_are_dropped_as_noise() -> None:
    grid = _depth({(1, 2): 1.0})
    result = building_depths(
        grid,
        TRANSFORM,
        _buildings((2.0, 8.0, 3.0, 9.0), (5.0, 5.0, 5.5, 5.5)),
        config=ExposureConfig(min_building_area_m2=0.5),
    )
    assert result.n_input == 2
    assert result.n_dropped_small == 1
    assert len(result.buildings) == 1


def test_nan_depth_counts_as_dry_not_deep() -> None:
    grid = np.full((10, 10), np.nan)
    result = building_depths(grid, TRANSFORM, _buildings((2.0, 8.0, 3.0, 9.0)), config=_config())
    assert result.buildings["depth_m"].iloc[0] == 0.0
    assert result.n_inundated == 0


def test_footprints_off_the_raster_are_counted_not_crashed() -> None:
    grid = _depth({(1, 2): 1.0})
    result = building_depths(
        grid, TRANSFORM, _buildings((500.0, 500.0, 505.0, 505.0)), config=_config()
    )
    assert result.n_outside_raster == 1
    assert result.buildings["depth_m"].iloc[0] == 0.0


def test_missing_crs_is_refused_rather_than_guessed() -> None:
    frame = gpd.GeoDataFrame({"id": [0]}, geometry=[box(2.0, 8.0, 3.0, 9.0)], crs=None)
    with pytest.raises(ValueError, match="no CRS"):
        building_depths(_depth({}), TRANSFORM, frame, config=_config())


def test_storeys_come_from_height_when_no_count_is_carried() -> None:
    grid = _depth({(1, 2): 1.0})
    result = building_depths(
        grid, TRANSFORM, _buildings((2.0, 8.0, 3.0, 9.0), height=[9.0]), config=_config()
    )
    assert result.buildings["storeys"].iloc[0] == pytest.approx(3.0)
    assert result.buildings["floor_area_m2"].iloc[0] == pytest.approx(3.0)


def test_explicit_storey_count_beats_height() -> None:
    grid = _depth({(1, 2): 1.0})
    result = building_depths(
        grid,
        TRANSFORM,
        _buildings((2.0, 8.0, 3.0, 9.0), height=[9.0], num_floors=[2.0]),
        config=_config(),
    )
    assert result.buildings["storeys"].iloc[0] == pytest.approx(2.0)


def test_population_counts_only_cells_over_the_threshold() -> None:
    depth = np.array([[0.0, 0.2], [0.5, 1.0]])
    people = np.array([[10.0, 10.0], [10.0, 10.0]])
    result = population_affected(depth, people, threshold_m=0.3)
    assert result.people_affected == pytest.approx(20.0)
    assert result.people_total == pytest.approx(40.0)
    assert result.share_affected == pytest.approx(0.5)
    assert result.n_cells_affected == 2


def test_population_grid_must_match_the_depth_grid() -> None:
    with pytest.raises(ValueError, match="resample"):
        population_affected(np.zeros((2, 2)), np.zeros((3, 3)))


def test_negative_population_is_refused() -> None:
    with pytest.raises(ValueError, match="negative"):
        population_affected(np.zeros((2, 2)), np.full((2, 2), -1.0))


def test_nan_population_counts_as_empty() -> None:
    depth = np.full((2, 2), 1.0)
    people = np.array([[np.nan, 5.0], [5.0, np.nan]])
    result = population_affected(depth, people)
    assert result.people_affected == pytest.approx(10.0)


def test_margin_is_signed_when_an_unclamped_field_is_supplied() -> None:
    grid = _depth({(1, 2): 0.0})
    unclamped = np.full((10, 10), -3.0)
    result = building_depths(
        grid,
        TRANSFORM,
        _buildings((2.1, 8.1, 2.9, 8.9)),
        unclamped_depth=unclamped,
        config=_config(floor_height_m=0.15),
    )
    assert result.has_margin is True
    assert result.buildings["floor_margin_m"].iloc[0] == pytest.approx(-3.15)


def test_without_an_unclamped_field_the_margin_is_only_the_freeboard() -> None:
    grid = _depth({(1, 2): 0.0})
    result = building_depths(
        grid, TRANSFORM, _buildings((2.1, 8.1, 2.9, 8.9)), config=_config(floor_height_m=0.15)
    )
    assert result.has_margin is False
    assert result.buildings["floor_margin_m"].iloc[0] == pytest.approx(-0.15)


def test_unclamped_field_must_share_the_depth_grid() -> None:
    with pytest.raises(ValueError, match="unclamped_depth shape"):
        building_depths(
            _depth({}),
            TRANSFORM,
            _buildings((2.1, 8.1, 2.9, 8.9)),
            unclamped_depth=np.zeros((3, 3)),
            config=_config(),
        )
