from __future__ import annotations

from pathlib import Path

import geopandas as gpd
import pytest
from pyproj import CRS
from shapely.geometry import LineString

from floodline.config import Config
from floodline.io.raster import CrsError
from floodline.io.vector import read_vector, write_vector


def _frame(epsg: int | None = 7856) -> gpd.GeoDataFrame:
    return gpd.GeoDataFrame(
        {"link_id": [0, 1], "strahler": [1, 2]},
        geometry=[LineString([(0, 0), (10, 10)]), LineString([(10, 10), (20, 5)])],
        crs=CRS.from_epsg(epsg) if epsg is not None else None,
    )


def test_geoparquet_roundtrip(tmp_path: Path) -> None:
    path = write_vector(tmp_path / "net.parquet", _frame())
    back = read_vector(path)
    assert list(back.columns) == list(_frame().columns)
    assert back.crs.to_epsg() == 7856
    assert len(back) == 2
    assert back.geometry.iloc[0].length == pytest.approx(LineString([(0, 0), (10, 10)]).length)


def test_write_refuses_geographic_crs(tmp_path: Path) -> None:
    with pytest.raises(CrsError, match="geographic"):
        write_vector(tmp_path / "bad.parquet", _frame(epsg=4326))


def test_write_refuses_missing_crs(tmp_path: Path) -> None:
    with pytest.raises(CrsError, match="no CRS"):
        write_vector(tmp_path / "bad.parquet", _frame(epsg=None))


def test_read_refuses_crs_mismatch(tmp_path: Path) -> None:
    path = write_vector(tmp_path / "utm.parquet", _frame(epsg=32756))
    with pytest.raises(CrsError, match="analysis CRS"):
        read_vector(path, config=Config())


def test_read_allows_mismatch_when_permitted(tmp_path: Path) -> None:
    path = write_vector(tmp_path / "utm.parquet", _frame(epsg=32756))
    cfg = Config.model_validate({"crs": {"allow_reprojection": True}})
    assert read_vector(path, config=cfg).crs.to_epsg() == 32756


def test_overwrite_guard(tmp_path: Path) -> None:
    path = write_vector(tmp_path / "net.parquet", _frame())
    with pytest.raises(FileExistsError):
        write_vector(path, _frame(), overwrite=False)


def test_column_subset(tmp_path: Path) -> None:
    path = write_vector(tmp_path / "net.parquet", _frame())
    back = read_vector(path, columns=["link_id", "geometry"])
    assert "strahler" not in back.columns
    assert "link_id" in back.columns
