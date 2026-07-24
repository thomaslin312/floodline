from __future__ import annotations

import numpy as np
import pytest

from floodline.config import Config
from floodline.synthetic import SyntheticCatchment, make_synthetic_catchment


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
