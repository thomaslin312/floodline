from __future__ import annotations

from collections.abc import Callable

import numpy as np
import pytest


@pytest.fixture(scope="session")
def pysheds_fill_depressions() -> Callable[[np.ndarray], np.ndarray]:
    """Return a function that fills depressions with pysheds, or skip the test.

    pysheds is an oracle, not a dependency: it exists so our own implementation has
    something independent to be wrong against. Nothing under `src/` imports it.
    """
    pytest.importorskip("pysheds", reason="oracle not installed (uv sync --group oracle)")
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
