from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import numpy as np
import numpy.typing as npt
import pytest

from floodline.core.config import Config
from floodline.core.terrain._neighbours import D8_OFFSETS
from floodline.synthetic import SyntheticCatchment, make_synthetic_catchment

BoolGrid = npt.NDArray[np.bool_]
FloatGrid = npt.NDArray[np.floating]


@pytest.fixture(scope="session")
def config() -> Config:
    return Config()


@pytest.fixture(scope="session")
def catchment() -> SyntheticCatchment:
    """Small synthetic catchment: tilted plane, meandering valley, 5 pits."""
    return make_synthetic_catchment(rows=120, cols=90, n_pits=5, seed=0)


@pytest.fixture(scope="session")
def tiny_catchment() -> SyntheticCatchment:
    """A catchment small enough for tests that run the whole grid many times."""
    return make_synthetic_catchment(rows=40, cols=30, cellsize=5.0, n_pits=2, seed=7)


@pytest.fixture(scope="session")
def catchment_with_nodata() -> SyntheticCatchment:
    return make_synthetic_catchment(rows=60, cols=48, n_pits=3, nodata_border_cells=3, seed=3)


@pytest.fixture
def dem_with_nan(catchment: SyntheticCatchment) -> np.ndarray:
    """The synthetic DEM as float64 with a NaN blob punched through it."""
    dem = catchment.dem.astype(np.float64).copy()
    dem[5:12, 5:12] = np.nan
    return dem


# --- grid helpers shared by the terrain test suites -------------------------------
#
# Deliberately written with numpy shifts rather than by reusing the numba kernels
# they check. A helper that shares an implementation with the thing under test
# cannot catch that implementation being wrong.


def _data_edge_mask(valid: BoolGrid) -> BoolGrid:
    """Return valid cells on the raster border or touching a nodata cell."""
    rows, cols = valid.shape
    edge = np.zeros((rows, cols), dtype=np.bool_)
    edge[0, :] = edge[-1, :] = True
    edge[:, 0] = edge[:, -1] = True
    padded = np.pad(valid, 1, constant_values=False)
    for d_row, d_col in D8_OFFSETS:
        edge |= ~padded[1 + d_row : 1 + d_row + rows, 1 + d_col : 1 + d_col + cols]
    return edge & valid


def _lower_neighbour_counts(dem: FloatGrid, valid: BoolGrid) -> npt.NDArray[np.int64]:
    """Return, per cell, how many valid D8 neighbours are strictly lower."""
    rows, cols = dem.shape
    counts = np.zeros((rows, cols), dtype=np.int64)
    padded_z = np.pad(dem, 1, constant_values=np.inf)
    padded_ok = np.pad(valid, 1, constant_values=False)
    for d_row, d_col in D8_OFFSETS:
        window_z = padded_z[1 + d_row : 1 + d_row + rows, 1 + d_col : 1 + d_col + cols]
        window_ok = padded_ok[1 + d_row : 1 + d_row + rows, 1 + d_col : 1 + d_col + cols]
        counts += (window_ok & (window_z < dem)).astype(np.int64)
    return counts


def _steepest_tie_mask(dem: FloatGrid, valid: BoolGrid, cellsize: float) -> BoolGrid:
    """Return cells where two or more neighbours share the steepest downhill slope.

    These are the cells where any two D8 implementations are free to disagree
    without either being wrong, so they have to be identified independently of
    whatever tie-break either one happens to use.
    """
    rows, cols = dem.shape
    diagonal = float(np.hypot(cellsize, cellsize))
    padded_z = np.pad(dem, 1, constant_values=np.inf)
    padded_ok = np.pad(valid, 1, constant_values=False)

    slopes = []
    for d_row, d_col in D8_OFFSETS:
        distance = diagonal if (d_row != 0 and d_col != 0) else cellsize
        window_z = padded_z[1 + d_row : 1 + d_row + rows, 1 + d_col : 1 + d_col + cols]
        window_ok = padded_ok[1 + d_row : 1 + d_row + rows, 1 + d_col : 1 + d_col + cols]
        slopes.append(np.where(window_ok, (dem - window_z) / distance, -np.inf))

    stacked = np.stack(slopes)
    best = stacked.max(axis=0)
    tied = (np.abs(stacked - best) < 1e-12).sum(axis=0)
    return (tied > 1) & (best > 0) & valid


@pytest.fixture(scope="session")
def data_edge_mask() -> Callable[[BoolGrid], BoolGrid]:
    return _data_edge_mask


@pytest.fixture(scope="session")
def lower_neighbour_counts() -> Callable[[FloatGrid, BoolGrid], npt.NDArray[np.int64]]:
    return _lower_neighbour_counts


@pytest.fixture(scope="session")
def steepest_tie_mask() -> Callable[[FloatGrid, BoolGrid, float], BoolGrid]:
    return _steepest_tie_mask


@pytest.fixture(scope="session")
def geographic_tile_writer() -> Callable[..., Path]:
    """Return a helper that writes a tile in a geographic CRS.

    USGS 3DEP delivers EPSG:4269, which `read_raster` refuses, so several suites
    need to fabricate one to exercise the ingest path.
    """
    import rasterio
    from rasterio.crs import CRS as RioCRS
    from rasterio.transform import from_origin

    def write(
        path: Path,
        *,
        west: float,
        south: float,
        size: float = 0.2,
        res: float = 0.01,
        value: float = 10.0,
        epsg: int = 4269,
        nodata: float = -999999.0,
    ) -> Path:
        n = round(size / res)
        with rasterio.open(
            path,
            "w",
            driver="GTiff",
            height=n,
            width=n,
            count=1,
            dtype="float32",
            crs=RioCRS.from_epsg(epsg),
            transform=from_origin(west, south + size, res, res),
            nodata=nodata,
        ) as dst:
            dst.write(np.full((n, n), value, dtype=np.float32), 1)
        return path

    return write
