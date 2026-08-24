from __future__ import annotations

import base64
import io

import geopandas as gpd
import numpy as np
import pytest
from PIL import Image
from rasterio.transform import Affine
from shapely.geometry import Point

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


def test_encoding_is_logarithmic_so_a_shed_and_a_hospital_both_show() -> None:
    damage = np.array([[0.0, DAMAGE_FLOOR, 1e5, 1e7, DAMAGE_CEILING]])
    rgb = encode_damage(damage, np.zeros(damage.shape, dtype=np.int32))
    red = rgb[..., 0][0]
    assert red[0] == 0
    assert red[1] == 0  # exactly at the floor is the bottom of the ramp
    assert red[4] == 255
    # Log spacing: the mid values are spread out, not crushed against zero.
    assert 40 < int(red[2]) < 150
    assert 150 < int(red[3]) < 255


def test_encoding_saturates_rather_than_wrapping() -> None:
    rgb = encode_damage(np.array([[DAMAGE_CEILING * 1000]]), np.array([[9999]], dtype=np.int32))
    assert rgb[0, 0, 0] == 255
    assert rgb[0, 0, 1] == 255  # count clips at 255 too


def test_below_the_floor_reads_as_undamaged() -> None:
    rgb = encode_damage(np.array([[DAMAGE_FLOOR - 1]]), np.zeros((1, 1), dtype=np.int32))
    assert rgb[0, 0, 0] == 0


def test_bundle_preserves_the_watershed_total_through_reduction() -> None:
    points = [(x * 10.0 + 5.0, 95.0 - y * 10.0, 1000.0) for x in range(10) for y in range(10)]
    frame = _buildings(points)
    bundle = build_exposure_bundle(
        "1204010403", "nsi", frame, TRANSFORM, SHAPE, (0.0, 0.0, 100.0, 100.0), reduction=2
    )
    # 100 buildings at 1000 each, reduced 2x: the sum must survive, so the grid halves.
    assert bundle.width == 5
    assert bundle.height == 5
    raw = base64.b64decode(bundle.damage_png.split(",", 1)[1])
    image = np.asarray(Image.open(io.BytesIO(raw)))
    assert image.shape == (5, 5, 3)
    # Each reduced cell holds 4 buildings at 1000 = 4000, well inside the ramp.
    assert int(image[..., 1].max()) == 4


def test_bundle_carries_provenance_and_stats() -> None:
    bundle = build_exposure_bundle(
        "1204010403",
        "nsi",
        _buildings([(25.0, 85.0, 1000.0)]),
        TRANSFORM,
        SHAPE,
        (0.0, 0.0, 100.0, 100.0),
        reduction=1,
        currency="USD",
        stats={"inundated": 1},
        notes=["contents omitted"],
    )
    assert bundle.inventory == "nsi"
    assert bundle.currency == "USD"
    assert bundle.stats["inundated"] == 1
    assert bundle.notes == ["contents omitted"]
    assert bundle.damage_png.startswith("data:image/png;base64,")
