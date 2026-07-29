from __future__ import annotations

from collections.abc import Callable
from typing import Any

import numpy as np
import pytest


def _restore_numpy_in1d() -> None:
    """Put `np.in1d` back so the pysheds oracle can run under NumPy 2.

    pysheds 0.5 predates NumPy 2.0, which removed the `np.in1d` alias in favour of
    `np.isin`. Everything else in pysheds works fine. The alternatives were to pin
    NumPy below 2 for the whole project, which would hold the pipeline back for the
    sake of a test-only oracle, or to drop the accumulation differential tests
    entirely. Restoring one removed alias, in test code, only for the process that
    imports the oracle, is the smallest of the three.

    `in1d` flattened its first argument and always returned 1-D; `isin` preserves
    shape. The shim reproduces the old contract rather than aliasing them naively.
    """
    if hasattr(np, "in1d"):
        return

    def in1d(ar1: Any, ar2: Any, **kwargs: Any) -> np.ndarray:
        return np.isin(np.asarray(ar1).ravel(), ar2, **kwargs)

    np.in1d = in1d  # type: ignore[attr-defined]


@pytest.fixture(scope="session")
def pysheds_fill_depressions() -> Callable[[np.ndarray], np.ndarray]:
    """Return a function that fills depressions with pysheds, or skip the test.

    pysheds is an oracle, not a dependency: it exists so our own implementation has
    something independent to be wrong against. Nothing under `src/` imports it.
    """
    pytest.importorskip("pysheds", reason="oracle not installed (uv sync --group oracle)")
    _restore_numpy_in1d()
    import pyproj
    from affine import Affine
    from pysheds.grid import Grid
    from pysheds.view import Raster as PyshedsRaster
    from pysheds.view import ViewFinder

    def fill(dem: np.ndarray) -> np.ndarray:
        data = np.ascontiguousarray(dem, dtype=np.float64)
        finder = ViewFinder(
            affine=Affine(1.0, 0.0, 0.0, 0.0, -1.0, 0.0),
            shape=data.shape,
            nodata=np.float64(np.nan),
            crs=pyproj.Proj("EPSG:7856"),
        )
        raster = PyshedsRaster(data, viewfinder=finder)
        return np.asarray(Grid(viewfinder=finder).fill_depressions(raster), dtype=np.float64)

    return fill


@pytest.fixture(scope="session")
def pysheds_flow_direction() -> Callable[..., np.ndarray]:
    """Return a function computing D8 flow direction with pysheds, or skip the test.

    Called with pysheds' own sentinels made explicit: `flats=-1`, `pits=-2`, and
    the default ESRI dirmap. On a filled DEM there are no pits, so -1 is the only
    sentinel that shows up in practice.
    """
    pytest.importorskip("pysheds", reason="oracle not installed (uv sync --group oracle)")
    _restore_numpy_in1d()
    import pyproj
    from affine import Affine
    from pysheds.grid import Grid
    from pysheds.view import Raster as PyshedsRaster
    from pysheds.view import ViewFinder

    def flowdir(dem: np.ndarray, cellsize: float = 1.0) -> np.ndarray:
        data = np.ascontiguousarray(dem, dtype=np.float64)
        finder = ViewFinder(
            affine=Affine(cellsize, 0.0, 0.0, 0.0, -cellsize, 0.0),
            shape=data.shape,
            nodata=np.float64(np.nan),
            crs=pyproj.Proj("EPSG:7856"),
        )
        raster = PyshedsRaster(data, viewfinder=finder)
        grid = Grid(viewfinder=finder)
        return np.asarray(grid.flowdir(raster, flats=-1, pits=-2), dtype=np.int64)

    return flowdir


@pytest.fixture(scope="session")
def pysheds_accumulation() -> Callable[..., np.ndarray]:
    """Return a function computing flow accumulation with pysheds, or skip the test."""
    pytest.importorskip("pysheds", reason="oracle not installed (uv sync --group oracle)")
    _restore_numpy_in1d()
    import pyproj
    from affine import Affine
    from pysheds.grid import Grid
    from pysheds.view import Raster as PyshedsRaster
    from pysheds.view import ViewFinder

    def accumulate(dem: np.ndarray, cellsize: float = 1.0) -> np.ndarray:
        data = np.ascontiguousarray(dem, dtype=np.float64)
        finder = ViewFinder(
            affine=Affine(cellsize, 0.0, 0.0, 0.0, -cellsize, 0.0),
            shape=data.shape,
            nodata=np.float64(np.nan),
            crs=pyproj.Proj("EPSG:7856"),
        )
        raster = PyshedsRaster(data, viewfinder=finder)
        grid = Grid(viewfinder=finder)
        fdir = grid.flowdir(raster, flats=-1, pits=-2)
        return np.asarray(grid.accumulation(fdir), dtype=np.float64)

    return accumulate
