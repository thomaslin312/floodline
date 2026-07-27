from __future__ import annotations

from collections.abc import Callable

import numpy as np
import pytest

pysheds_fill: Callable[[np.ndarray], np.ndarray]


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
