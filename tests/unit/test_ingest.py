from __future__ import annotations

from pathlib import Path
from typing import Any

import numpy as np
import pytest
import rasterio
from rasterio.transform import from_origin

from floodline.config import Config
from floodline.io.ingest import (
    estimate_cells,
    ingest_dem,
    load_watersheds,
    select_tiles,
)
from floodline.io.raster import CrsError, read_raster, write_cog

# A small patch of the Houston AOI, in the geographic CRS 3DEP actually publishes in.
AOI = (-95.80, 29.50, -95.00, 30.10)


@pytest.fixture
def write_geographic_tile(geographic_tile_writer: Any) -> Any:
    """Alias so each test reads as though the writer were local."""
    return geographic_tile_writer


# --- the whole point: geographic in, analysis CRS out --------------------------------


def test_a_geographic_tile_is_refused_by_the_normal_reader(
    tmp_path: Path, write_geographic_tile: Any
) -> None:
    """The precondition for this module existing."""
    tile = write_geographic_tile(tmp_path / "t.tif", west=-95.6, south=29.7)
    with pytest.raises(CrsError, match="geographic"):
        read_raster(tile)


def test_ingest_produces_a_raster_the_pipeline_accepts(
    tmp_path: Path, write_geographic_tile: Any
) -> None:
    tiles = [
        write_geographic_tile(tmp_path / "a.tif", west=-95.6, south=29.7, value=12.0),
        write_geographic_tile(tmp_path / "b.tif", west=-95.4, south=29.7, value=14.0),
    ]
    config = Config()
    dem = ingest_dem(tiles, resolution_m=30.0, config=config)

    assert dem.crs.to_epsg() == config.crs.analysis.to_epsg()
    assert dem.cellsize == pytest.approx((30.0, 30.0))
    values = dem.data[np.isfinite(dem.data)]
    assert values.size > 0
    assert 11.0 <= values.min() <= values.max() <= 15.0

    # and it survives the round trip through the CRS-checking reader
    out = write_cog(tmp_path / "dem.tif", dem, config=config)
    assert read_raster(out, config=config).crs.to_epsg() == 6587


def test_source_nodata_becomes_nan(tmp_path: Path, write_geographic_tile: Any) -> None:
    tile = write_geographic_tile(tmp_path / "a.tif", west=-95.6, south=29.7, value=-999999.0)
    dem = ingest_dem([tile], resolution_m=30.0, config=Config(), clip_to_aoi=False)
    assert np.all(np.isnan(dem.data))


def test_a_tile_with_no_crs_is_refused(tmp_path: Path, write_geographic_tile: Any) -> None:
    path = tmp_path / "nocrs.tif"
    with rasterio.open(
        path,
        "w",
        driver="GTiff",
        height=8,
        width=8,
        count=1,
        dtype="float32",
        transform=from_origin(-95.6, 29.9, 0.01, 0.01),
    ) as dst:
        dst.write(np.zeros((8, 8), np.float32), 1)
    with pytest.raises(CrsError, match="no CRS"):
        ingest_dem([path], resolution_m=30.0, clip_to_aoi=False)


def test_no_tiles_is_an_error() -> None:
    with pytest.raises(ValueError, match="no tiles"):
        ingest_dem([], resolution_m=30.0)


# --- clipping and the memory cap -------------------------------------------------------


def test_clipping_trims_to_the_aoi(tmp_path: Path, write_geographic_tile: Any) -> None:
    """Tiles cover far more ground than the case; clipping early is what makes it fit."""
    tile = write_geographic_tile(tmp_path / "big.tif", west=-97.0, south=28.0, size=4.0, res=0.02)
    clipped = ingest_dem([tile], resolution_m=100.0, config=Config())
    unclipped = ingest_dem([tile], resolution_m=100.0, config=Config(), clip_to_aoi=False)
    assert clipped.data.size < unclipped.data.size / 4


def test_an_oversized_output_is_refused_before_it_is_built(
    tmp_path: Path, write_geographic_tile: Any
) -> None:
    """The terrain core is global and memory-bound; better refused here than mid-fill."""
    tile = write_geographic_tile(tmp_path / "a.tif", west=-95.6, south=29.7)
    with pytest.raises(ValueError, match=r"over the .* cap"):
        ingest_dem([tile], resolution_m=1.0, config=Config(), max_cells=1_000_000)


def test_the_cap_can_be_disabled(tmp_path: Path, write_geographic_tile: Any) -> None:
    tile = write_geographic_tile(tmp_path / "a.tif", west=-95.6, south=29.7)
    dem = ingest_dem([tile], resolution_m=200.0, config=Config(), max_cells=None)
    assert dem.data.size > 0


def test_estimate_cells() -> None:
    assert estimate_cells((0.0, 0.0, 3000.0, 2000.0), 10.0) == (300, 200)
    with pytest.raises(ValueError, match="positive"):
        estimate_cells((0.0, 0.0, 10.0, 10.0), 0.0)


# --- vintage selection -------------------------------------------------------------------


def test_duplicate_footprints_pick_the_survey_nearest_the_event(
    tmp_path: Path, write_geographic_tile: Any
) -> None:
    """Modelling a 2017 flood on 2026 terrain routes water over land that did not exist."""
    old = write_geographic_tile(tmp_path / "USGS_1_x_20180510.tif", west=-95.6, south=29.7)
    new = write_geographic_tile(tmp_path / "USGS_1_x_20260623.tif", west=-95.6, south=29.7)
    mid = write_geographic_tile(tmp_path / "USGS_1_x_20240229.tif", west=-95.6, south=29.7)

    groups = select_tiles([new, mid, old], config=Config())
    assert len(groups) == 1, "same ground must be one group"
    assert groups[0].chosen == old
    assert set(groups[0].rejected) == {new, mid}
    assert groups[0].chosen_date is not None
    assert groups[0].chosen_date.year == 2018


def test_newest_strategy_picks_the_newest(tmp_path: Path, write_geographic_tile: Any) -> None:
    old = write_geographic_tile(tmp_path / "USGS_1_x_20180510.tif", west=-95.6, south=29.7)
    new = write_geographic_tile(tmp_path / "USGS_1_x_20260623.tif", west=-95.6, south=29.7)
    config = Config.model_validate({"case": {"dem_vintage": "newest"}})
    assert select_tiles([old, new], config=config)[0].chosen == new


def test_different_footprints_stay_separate(tmp_path: Path, write_geographic_tile: Any) -> None:
    a = write_geographic_tile(tmp_path / "USGS_1_a_20180510.tif", west=-95.6, south=29.7)
    b = write_geographic_tile(tmp_path / "USGS_1_b_20180510.tif", west=-95.4, south=29.7)
    assert len(select_tiles([a, b], config=Config())) == 2


def test_undated_tiles_are_deterministic_and_flagged(
    tmp_path: Path, write_geographic_tile: Any
) -> None:
    """No date to choose on: pick deterministically, and say the date is unknown."""
    a = write_geographic_tile(tmp_path / "zzz.tif", west=-95.6, south=29.7)
    b = write_geographic_tile(tmp_path / "aaa.tif", west=-95.6, south=29.7)
    group = select_tiles([a, b], config=Config())[0]
    assert group.chosen == b
    assert group.chosen_date is None
    assert select_tiles([b, a], config=Config())[0].chosen == b


def test_grouping_is_by_bounds_not_by_filename(tmp_path: Path, write_geographic_tile: Any) -> None:
    """A change in USGS naming must not silently turn one footprint into several."""
    a = write_geographic_tile(
        tmp_path / "completely_different_20180510.tif", west=-95.6, south=29.7
    )
    b = write_geographic_tile(tmp_path / "USGS_1_n30w096_20260623.tif", west=-95.6, south=29.7)
    assert len(select_tiles([a, b], config=Config())) == 1


# --- watersheds: the correct unit of work -------------------------------------------


def _watershed_geojson(path: Path, *, huc: str = "1204010407") -> Path:
    """A square watershed inside the Houston AOI, in EPSG:4326 as WBD serves it."""
    import json

    path.write_text(
        json.dumps(
            {
                "type": "FeatureCollection",
                "features": [
                    {
                        "type": "Feature",
                        "properties": {"huc10": huc, "name": "Test Bayou", "areasqkm": 500.0},
                        "geometry": {
                            "type": "Polygon",
                            "coordinates": [
                                [
                                    [-95.60, 29.70],
                                    [-95.40, 29.70],
                                    [-95.40, 29.86],
                                    [-95.60, 29.86],
                                    [-95.60, 29.70],
                                ]
                            ],
                        },
                    },
                    {
                        "type": "Feature",
                        "properties": {"huc10": "9999999999", "name": "Small", "areasqkm": 20.0},
                        "geometry": {
                            "type": "Polygon",
                            "coordinates": [
                                [
                                    [-95.30, 29.70],
                                    [-95.28, 29.70],
                                    [-95.28, 29.72],
                                    [-95.30, 29.72],
                                    [-95.30, 29.70],
                                ]
                            ],
                        },
                    },
                ],
            }
        )
    )
    return path


def test_watersheds_load_into_the_analysis_crs(tmp_path: Path) -> None:
    sheds = load_watersheds(_watershed_geojson(tmp_path / "w.geojson"), config=Config())
    assert [w.huc for w in sheds] == ["1204010407", "9999999999"], "sorted by area, largest first"
    west, south, east, north = sheds[0].bounds
    # projected metres, not degrees
    assert abs(east - west) > 1000
    assert abs(north - south) > 1000


def test_cells_at_scales_with_resolution(tmp_path: Path) -> None:
    shed = load_watersheds(_watershed_geojson(tmp_path / "w.geojson"), config=Config())[0]
    assert shed.cells_at(10.0) == pytest.approx(shed.cells_at(30.0) * 9, rel=0.02)
    assert shed.cells_at(1.0) > shed.cells_at(10.0)


def test_empty_watershed_file_is_an_error(tmp_path: Path) -> None:
    path = tmp_path / "empty.geojson"
    path.write_text('{"type": "FeatureCollection", "features": []}')
    with pytest.raises(ValueError, match="no watershed features"):
        load_watersheds(path, config=Config())


def test_clipping_to_a_watershed_masks_outside_the_boundary(
    tmp_path: Path, write_geographic_tile: Any
) -> None:
    """Cells beyond the divide become nodata, so routing treats them as the domain edge.

    That is the point: water leaving the watershed has left the domain, and the
    filler and router should say so rather than inventing a downstream.
    """
    tile = write_geographic_tile(tmp_path / "t.tif", west=-95.7, south=29.6, size=0.4, res=0.002)
    shed = load_watersheds(_watershed_geojson(tmp_path / "w.geojson"), config=Config())[0]

    boxed = ingest_dem([tile], resolution_m=100.0, config=Config())
    clipped = ingest_dem([tile], resolution_m=100.0, config=Config(), watershed=shed)

    inside = np.isfinite(clipped.data)
    assert 0.0 < inside.mean() < 1.0, "some of the bounding box must fall outside the boundary"
    # the watershed is smaller than the AOI, so fewer valid cells
    assert inside.sum() < np.isfinite(boxed.data).sum()


def test_watershed_clipping_respects_the_cell_cap(
    tmp_path: Path, write_geographic_tile: Any
) -> None:
    tile = write_geographic_tile(tmp_path / "t.tif", west=-95.7, south=29.6, size=0.4, res=0.01)
    shed = load_watersheds(_watershed_geojson(tmp_path / "w.geojson"), config=Config())[0]
    with pytest.raises(ValueError, match=r"over the .* cap"):
        ingest_dem([tile], resolution_m=1.0, config=Config(), watershed=shed, max_cells=1000)
