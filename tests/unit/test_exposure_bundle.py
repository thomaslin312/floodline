from __future__ import annotations

import base64
import io

import geopandas as gpd
import numpy as np
import pytest
from PIL import Image
from rasterio.transform import Affine
from shapely.geometry import Point

from floodline.core.config import Config
from floodline.report.exposure_bundle import (
    DAMAGE_CEILING,
    DAMAGE_FLOOR,
    build_exposure_bundle,
    encode_damage,
    rasterise_damage,
)

# 10 m cells, origin top-left at (0, 100).
TRANSFORM = Affine.translation(0.0, 100.0) * Affine.scale(10.0, -10.0)
SHAPE = (10, 10)


def _buildings(points: list[tuple[float, float, float]]) -> gpd.GeoDataFrame:
    return gpd.GeoDataFrame(
        {"damage": [d for _, _, d in points]},
        geometry=[Point(x, y) for x, y, _ in points],
        crs="EPSG:6587",
    )


def test_damage_lands_in_the_cell_containing_the_building() -> None:
    damage, count = rasterise_damage(_buildings([(25.0, 85.0, 1000.0)]), TRANSFORM, SHAPE)
    assert damage[1, 2] == pytest.approx(1000.0)
    assert count[1, 2] == 1
    assert damage.sum() == pytest.approx(1000.0)


def test_several_buildings_in_one_cell_are_summed_not_overwritten() -> None:
    points = [(25.0, 85.0, 1000.0), (26.0, 86.0, 2500.0), (24.0, 84.0, 500.0)]
    damage, count = rasterise_damage(_buildings(points), TRANSFORM, SHAPE)
    assert damage[1, 2] == pytest.approx(4000.0)
    assert count[1, 2] == 3


def test_undamaged_buildings_are_not_counted() -> None:
    damage, count = rasterise_damage(
        _buildings([(25.0, 85.0, 0.0), (25.0, 85.0, 100.0)]), TRANSFORM, SHAPE
    )
    assert count[1, 2] == 1
    assert damage[1, 2] == pytest.approx(100.0)


def test_buildings_outside_the_grid_are_dropped_not_wrapped() -> None:
    damage, count = rasterise_damage(_buildings([(9999.0, 9999.0, 5000.0)]), TRANSFORM, SHAPE)
    assert damage.sum() == 0.0
    assert count.sum() == 0


def test_an_empty_table_produces_an_empty_grid() -> None:
    damage, _count = rasterise_damage(_buildings([]), TRANSFORM, SHAPE)
    assert damage.shape == SHAPE
    assert damage.sum() == 0.0


def test_undamaged_ground_is_transparent_not_black() -> None:
    """An RGB image has no way to say "nothing here".

    The first version drew a black rectangle over the whole bounding box for exactly
    that reason, which is what made the layer unusable on the map.
    """
    rgba = encode_damage(np.array([[0.0, DAMAGE_FLOOR * 10]]))
    assert rgba.shape[-1] == 4
    assert rgba[0, 0, 3] == 0
    assert rgba[0, 1, 3] > 0


def test_encoding_is_logarithmic_so_a_shed_and_a_hospital_both_show() -> None:
    rgba = encode_damage(np.array([[DAMAGE_FLOOR, 1e5, 1e7, DAMAGE_CEILING]]))
    # Walking up the warm ramp: pale at the floor, dark red at the ceiling.
    greens = rgba[0, :, 1].astype(int)
    assert greens[0] > greens[-1], "colour must darken as damage rises"
    assert len({tuple(rgba[0, i, :3]) for i in range(4)}) == 4, "log spacing separates them"


def test_encoding_saturates_rather_than_wrapping() -> None:
    beyond = encode_damage(np.array([[DAMAGE_CEILING * 1000]]))
    top = encode_damage(np.array([[DAMAGE_CEILING]]))
    np.testing.assert_array_equal(beyond, top)


def test_below_the_floor_reads_as_undamaged() -> None:
    assert encode_damage(np.array([[DAMAGE_FLOOR - 1]]))[0, 0, 3] == 0


def test_bundle_preserves_the_watershed_total_through_reduction() -> None:
    points = [(x * 10.0 + 5.0, 95.0 - y * 10.0, 1000.0) for x in range(10) for y in range(10)]
    frame = _buildings(points)
    bundle = build_exposure_bundle(
        "1204010403",
        "nsi",
        frame,
        TRANSFORM,
        SHAPE,
        (0.0, 0.0, 100.0, 100.0),
        reduction=2,
        config=Config(),
    )
    raw = base64.b64decode(bundle.damage_png.split(",", 1)[1])
    image = np.asarray(Image.open(io.BytesIO(raw)))
    assert image.shape == (bundle.height, bundle.width, 4)
    # This fixture puts a building in every cell, so every cell is drawn. Transparency
    # where nothing was damaged is covered separately.
    assert int(image[..., 3].max()) > 0


def test_bundle_carries_provenance_and_stats() -> None:
    bundle = build_exposure_bundle(
        "1204010403",
        "nsi",
        _buildings([(25.0, 85.0, 1000.0)]),
        TRANSFORM,
        SHAPE,
        (0.0, 0.0, 100.0, 100.0),
        reduction=1,
        config=Config(),
        currency="USD",
        stats={"inundated": 1},
        notes=["contents omitted"],
    )
    assert bundle.inventory == "nsi"
    assert bundle.currency == "USD"
    assert bundle.stats["inundated"] == 1
    assert bundle.notes == ["contents omitted"]
    assert bundle.damage_png.startswith("data:image/png;base64,")


def test_bundle_bounds_are_web_mercator_not_the_analysis_crs() -> None:
    """The map places this layer by corner coordinates in EPSG:3857.

    Handing it the analysis CRS's own bounds put the damage layer nowhere at all on
    the first real run: UTM northings near 3,300,000 are a valid Web Mercator y, just
    one somewhere off the coast of Antarctica.
    """
    # A real Houston window in UTM 15N, with the config's analysis CRS to match.
    utm = Config.model_validate({"crs": {"analysis": "EPSG:26915"}})
    transform = Affine.translation(235853.0, 3316922.0) * Affine.scale(30.0, -30.0)
    frame = gpd.GeoDataFrame(
        {"damage": [50_000.0]},
        geometry=[Point(240000.0, 3300000.0)],
        crs="EPSG:26915",
    )
    bundle = build_exposure_bundle(
        "1204010403",
        "nsi",
        frame,
        transform,
        (200, 200),
        (0.0, 0.0, 1.0, 1.0),
        reduction=1,
        config=utm,
    )
    west, south, east, north = bundle.bounds
    # Houston is about -10.6 million easting and 3.5 million northing in Web Mercator.
    assert -1.08e7 < west < -1.05e7, west
    assert 3.4e6 < south < 3.6e6, south
    assert west < east and south < north
