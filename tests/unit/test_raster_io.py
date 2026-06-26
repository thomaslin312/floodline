from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest
import rasterio
from rasterio.crs import CRS as RioCRS
from rasterio.transform import from_origin
from rasterio.windows import Window

from floodline.config import Config
from floodline.io.raster import (
    CrsError,
    Raster,
    iter_windows,
    read_raster,
    require_projected_crs,
    write_cog,
)
from floodline.synthetic import SyntheticCatchment


def _write_plain_tif(
    path: Path, data: np.ndarray, epsg: int | None, nodata: float = -9999.0
) -> Path:
    profile = {
        "driver": "GTiff",
        "height": data.shape[0],
        "width": data.shape[1],
        "count": 1,
        "dtype": data.dtype.name,
        "transform": from_origin(500_000.0, 6_800_000.0, 5.0, 5.0),
        "nodata": nodata,
    }
    if epsg is not None:
        profile["crs"] = RioCRS.from_epsg(epsg)
    with rasterio.open(path, "w", **profile) as dst:
        dst.write(data, 1)
    return path


# --- CRS refusal ---------------------------------------------------------------


def test_require_projected_crs_accepts_mga56() -> None:
    assert require_projected_crs(RioCRS.from_epsg(7856)).to_epsg() == 7856


def test_require_projected_crs_refuses_none() -> None:
    with pytest.raises(CrsError, match="no CRS"):
        require_projected_crs(None, source="tile.tif")


def test_require_projected_crs_refuses_geographic() -> None:
    with pytest.raises(CrsError, match="geographic"):
        require_projected_crs(RioCRS.from_epsg(4326), source="tile.tif")


def test_read_refuses_geographic_raster(tmp_path: Path) -> None:
    path = _write_plain_tif(tmp_path / "wgs84.tif", np.zeros((8, 8), np.float32), 4326)
    with pytest.raises(CrsError, match="geographic"):
        read_raster(path)


def test_read_refuses_crsless_raster(tmp_path: Path) -> None:
    path = _write_plain_tif(tmp_path / "nocrs.tif", np.zeros((8, 8), np.float32), None)
    with pytest.raises(CrsError, match="no CRS"):
        read_raster(path)


def test_read_refuses_crs_mismatch_without_reprojection(tmp_path: Path) -> None:
    path = _write_plain_tif(tmp_path / "utm.tif", np.zeros((8, 8), np.float32), 32756)
    with pytest.raises(CrsError, match="analysis CRS"):
        read_raster(path, config=Config())


def test_read_allows_crs_mismatch_when_permitted(tmp_path: Path) -> None:
    path = _write_plain_tif(tmp_path / "utm.tif", np.zeros((8, 8), np.float32), 32756)
    cfg = Config.model_validate({"crs": {"allow_reprojection": True}})
    assert read_raster(path, config=cfg).crs.to_epsg() == 32756


def test_write_refuses_geographic_raster(tmp_path: Path) -> None:
    from pyproj import CRS as PyCRS

    bad = Raster(
        data=np.zeros((8, 8), np.float32),
        transform=from_origin(150.0, -28.0, 0.001, 0.001),
        crs=PyCRS.from_epsg(4326),
        nodata=-9999.0,
    )
    with pytest.raises(CrsError):
        write_cog(tmp_path / "out.tif", bad)


# --- read / write roundtrip ----------------------------------------------------


def test_cog_roundtrip_preserves_values(tmp_path: Path, catchment: SyntheticCatchment) -> None:
    out = write_cog(tmp_path / "dem.tif", catchment.as_raster())
    back = read_raster(out)
    assert back.shape == catchment.shape
    assert back.crs.to_epsg() == 7856
    assert back.nodata == pytest.approx(catchment.nodata)
    assert back.cellsize == pytest.approx((catchment.cellsize, catchment.cellsize))
    np.testing.assert_allclose(back.data, catchment.dem, rtol=0, atol=1e-4)


def test_written_file_is_a_cog(tmp_path: Path, catchment: SyntheticCatchment) -> None:
    out = write_cog(tmp_path / "dem.tif", catchment.as_raster())
    with rasterio.open(out) as src:
        assert src.driver == "GTiff"
        assert src.profile["tiled"] is True
        assert src.nodata is not None
        assert src.overviews(1)  # COG copy built overviews
    assert not list(tmp_path.glob("*.tmp.tif"))


def test_no_temp_file_left_on_failure(tmp_path: Path, catchment: SyntheticCatchment) -> None:
    ref = catchment.as_raster()
    bad = Raster(
        data=np.zeros((2, 4, 4), np.float32),
        transform=ref.transform,
        crs=ref.crs,
        nodata=ref.nodata,
    )
    with pytest.raises(ValueError, match="2-D"):
        write_cog(tmp_path / "bad.tif", bad)
    assert not list(tmp_path.glob("*.tmp.tif"))


def test_nan_becomes_nodata_on_write(tmp_path: Path, catchment: SyntheticCatchment) -> None:
    data = catchment.dem.astype(np.float32).copy()
    data[0:3, 0:3] = np.nan
    out = write_cog(tmp_path / "nan.tif", catchment.as_raster().with_data(data))
    with rasterio.open(out) as src:
        raw = src.read(1)
    assert np.all(raw[0:3, 0:3] == pytest.approx(catchment.nodata))
    # and reading it back masks those cells out again
    assert np.all(np.isnan(read_raster(out).data[0:3, 0:3]))


def test_overwrite_guard(tmp_path: Path, catchment: SyntheticCatchment) -> None:
    out = tmp_path / "dem.tif"
    write_cog(out, catchment.as_raster())
    with pytest.raises(FileExistsError):
        write_cog(out, catchment.as_raster(), overwrite=False)


def test_windowed_read_matches_full_read(tmp_path: Path, catchment: SyntheticCatchment) -> None:
    out = write_cog(tmp_path / "dem.tif", catchment.as_raster())
    full = read_raster(out)
    window = Window(col_off=10, row_off=20, width=17, height=13)
    part = read_raster(out, window=window)
    assert part.shape == (13, 17)
    np.testing.assert_allclose(part.data, full.data[20:33, 10:27])
    # the window transform points at the right place on the ground
    assert part.transform @ (0, 0) == full.transform @ (10, 20)


# --- helpers -------------------------------------------------------------------


def test_iter_windows_tiles_exactly() -> None:
    shape = (37, 23)
    cover = np.zeros(shape, dtype=int)
    for win in iter_windows(shape, 16):
        rows = slice(win.row_off, win.row_off + win.height)
        cols = slice(win.col_off, win.col_off + win.width)
        cover[rows, cols] += 1
    assert np.all(cover == 1)


def test_iter_windows_rejects_bad_block_size() -> None:
    with pytest.raises(ValueError, match="positive"):
        list(iter_windows((10, 10), 0))


def test_valid_mask_excludes_nodata_and_nan(catchment_with_nodata: SyntheticCatchment) -> None:
    raster = catchment_with_nodata.as_raster()
    mask = raster.valid_mask()
    assert not mask[0, 0]
    assert mask[30, 24]
    assert mask.sum() == catchment_with_nodata.valid_mask.sum()


def test_cell_area(catchment: SyntheticCatchment) -> None:
    assert catchment.as_raster().cell_area_m2 == pytest.approx(catchment.cellsize**2)


def test_with_data_shape_guard(catchment: SyntheticCatchment) -> None:
    with pytest.raises(ValueError, match="shape mismatch"):
        catchment.as_raster().with_data(np.zeros((3, 3), np.float32))
