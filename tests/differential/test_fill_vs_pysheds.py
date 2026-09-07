"""Differential test: our priority-flood against pysheds' depression filling.

pysheds fills by morphological reconstruction, a completely different algorithm
from Barnes' priority-flood. Where the two agree, both are probably right; where
they disagree, the disagreement has to be explained rather than tolerated.

Only the epsilon-free fill is compared. The epsilon variant deliberately produces
a different surface — a drained gradient rather than a flat — and pysheds has no
equivalent, so there is nothing to compare it against.
"""

from __future__ import annotations

from collections.abc import Callable

import numpy as np
import numpy.typing as npt
import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from hypothesis.extra import numpy as hnp

from floodline.core.config import Connectivity
from floodline.core.terrain.fill import fill_depressions
from floodline.synthetic import SyntheticCatchment, make_synthetic_catchment

pytestmark = pytest.mark.differential

Filler = Callable[[np.ndarray], np.ndarray]

SETTINGS = settings(
    max_examples=100,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)


@SETTINGS
@given(
    dem=hnp.arrays(
        dtype=np.float64,
        shape=st.tuples(st.integers(3, 14), st.integers(3, 14)),
        elements=st.floats(-100.0, 1000.0, allow_nan=False, allow_infinity=False, width=32),
    )
)
def test_agrees_with_pysheds_on_random_grids(
    dem: npt.NDArray[np.float64], pysheds_fill_depressions: Filler
) -> None:
    ours = fill_depressions(dem, connectivity=Connectivity.EIGHT)
    theirs = pysheds_fill_depressions(dem)
    np.testing.assert_allclose(ours, theirs, rtol=0, atol=1e-9)


def test_agrees_with_pysheds_on_the_synthetic_catchment(
    catchment: SyntheticCatchment, pysheds_fill_depressions: Filler
) -> None:
    dem = catchment.dem.astype(np.float64)
    ours = fill_depressions(dem, connectivity=Connectivity.EIGHT)
    theirs = pysheds_fill_depressions(dem)
    np.testing.assert_allclose(ours, theirs, rtol=0, atol=1e-9)


@pytest.mark.parametrize("seed", [1, 2, 3])
def test_agrees_on_rough_catchments(seed: int, pysheds_fill_depressions: Filler) -> None:
    """Roughness makes thousands of tiny pits, which is where tie-breaking would show."""
    dem = make_synthetic_catchment(
        rows=90, cols=70, n_pits=4, roughness_m=0.3, seed=seed
    ).dem.astype(np.float64)
    np.testing.assert_allclose(
        fill_depressions(dem, connectivity=Connectivity.EIGHT),
        pysheds_fill_depressions(dem),
        rtol=0,
        atol=1e-9,
    )


def test_agrees_with_pysheds_where_there_is_nodata(
    catchment_with_nodata: SyntheticCatchment, pysheds_fill_depressions: Filler
) -> None:
    """Nodata is an outlet for both implementations, and neither writes into it."""
    dem = catchment_with_nodata.dem.astype(np.float64)
    dem[catchment_with_nodata.nodata_mask] = np.nan
    ours = fill_depressions(dem)
    theirs = pysheds_fill_depressions(dem)
    valid = ~catchment_with_nodata.nodata_mask
    np.testing.assert_allclose(ours[valid], theirs[valid], rtol=0, atol=1e-9)
    assert np.all(np.isnan(ours[~valid]))
