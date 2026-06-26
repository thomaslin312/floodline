"""Hypothesis property tests for the terrain invariants the spec names for filling.

The two the spec calls out:

* after filling, every cell has a monotone non-increasing path to the edge of the
  data (no pits);
* filling never lowers a cell: ``filled - original >= 0`` everywhere.

The rest are the corollaries that make those two meaningful rather than
vacuous — a function that returns ``+inf`` everywhere satisfies both.
"""

from __future__ import annotations

import numpy as np
import numpy.typing as npt
import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from hypothesis.extra import numpy as hnp

from floodline.config import Connectivity
from floodline.terrain.fill import fill_depressions, undrained_mask

CONNECTIVITIES = [Connectivity.EIGHT, Connectivity.FOUR]

# Small grids keep the pure-Python (NUMBA_DISABLE_JIT=1) path fast enough to run
# in CI, and depressions are just as expressible at this size.
grid_shapes = st.tuples(st.integers(2, 12), st.integers(2, 12))

elevations = st.floats(
    min_value=-500.0, max_value=4000.0, allow_nan=False, allow_infinity=False, width=32
)


@st.composite
def dems(
    draw: st.DrawFn, *, allow_nodata: bool = False, dtype: type = np.float64
) -> npt.NDArray[np.floating]:
    """Draw a small elevation grid, optionally with NaN nodata punched through it."""
    shape = draw(grid_shapes)
    dem = draw(hnp.arrays(dtype=dtype, shape=shape, elements=elevations))
    if allow_nodata and draw(st.booleans()):
        holes = draw(hnp.arrays(dtype=np.bool_, shape=shape, elements=st.booleans()))
        if not holes.all():  # keep at least one valid cell
            dem = dem.copy()
            dem[holes] = np.nan
    return dem


SETTINGS = settings(
    max_examples=150,
    deadline=None,  # the first call per type signature pays for JIT compilation
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)


# --- the two invariants the spec names -------------------------------------------


@SETTINGS
@given(dem=dems(allow_nodata=True), connectivity=st.sampled_from(CONNECTIVITIES))
def test_no_depressions_survive(dem: npt.NDArray[np.float64], connectivity: Connectivity) -> None:
    """Every cell of the filled DEM has a monotone non-increasing path to the edge."""
    filled = fill_depressions(dem, connectivity=connectivity)
    assert not undrained_mask(filled, connectivity=connectivity).any()


@SETTINGS
@given(dem=dems(allow_nodata=True), connectivity=st.sampled_from(CONNECTIVITIES))
def test_filling_never_lowers_a_cell(
    dem: npt.NDArray[np.float64], connectivity: Connectivity
) -> None:
    filled = fill_depressions(dem, connectivity=connectivity)
    valid = np.isfinite(dem)
    assert np.all(filled[valid] >= dem[valid])


# --- corollaries that stop the two above being satisfiable by cheating -----------


@SETTINGS
@given(dem=dems(allow_nodata=True))
def test_filling_never_exceeds_the_original_maximum(dem: npt.NDArray[np.float64]) -> None:
    """Water cannot pile above the highest ground; that would be inventing terrain."""
    valid = np.isfinite(dem)
    filled = fill_depressions(dem)
    assert np.all(filled[valid] <= dem[valid].max())


@SETTINGS
@given(dem=dems(allow_nodata=True), connectivity=st.sampled_from(CONNECTIVITIES))
def test_only_undrained_cells_are_raised(
    dem: npt.NDArray[np.float64], connectivity: Connectivity
) -> None:
    """A cell that already drained is left exactly alone."""
    filled = fill_depressions(dem, connectivity=connectivity)
    undrained = undrained_mask(dem, connectivity=connectivity)
    assert np.all(filled[~undrained & np.isfinite(dem)] == dem[~undrained & np.isfinite(dem)])


@SETTINGS
@given(dem=dems(allow_nodata=True), connectivity=st.sampled_from(CONNECTIVITIES))
def test_filling_is_idempotent(dem: npt.NDArray[np.float64], connectivity: Connectivity) -> None:
    once = fill_depressions(dem, connectivity=connectivity)
    twice = fill_depressions(once, connectivity=connectivity)
    np.testing.assert_array_equal(once, twice)


@SETTINGS
@given(dem=dems(allow_nodata=True))
def test_nodata_is_returned_bit_for_bit(dem: npt.NDArray[np.float64]) -> None:
    filled = fill_depressions(dem)
    assert np.array_equal(np.isnan(filled), np.isnan(dem))


@SETTINGS
@given(dem=dems())
def test_monotone_in_the_input(dem: npt.NDArray[np.float64]) -> None:
    """Raising the terrain everywhere cannot lower the filled surface anywhere."""
    higher = dem + 1.0
    assert np.all(fill_depressions(higher) >= fill_depressions(dem))


@SETTINGS
@given(dem=dems(), shift=st.floats(-100.0, 100.0))
def test_translation_equivariance(dem: npt.NDArray[np.float64], shift: float) -> None:
    """Fill commutes with adding a constant: it has no absolute elevation anywhere in it."""
    np.testing.assert_allclose(
        fill_depressions(dem + shift), fill_depressions(dem) + shift, rtol=0, atol=1e-9
    )


@SETTINGS
@given(dem=dems(allow_nodata=True))
def test_d4_fills_at_least_as_much_as_d8(dem: npt.NDArray[np.float64]) -> None:
    """Fewer escape routes cannot mean less water: D4 is an upper bound on D8."""
    d8 = fill_depressions(dem, connectivity=Connectivity.EIGHT)
    d4 = fill_depressions(dem, connectivity=Connectivity.FOUR)
    valid = np.isfinite(dem)
    assert np.all(d4[valid] >= d8[valid])


@SETTINGS
@given(dem=dems())
def test_transpose_equivariance(dem: npt.NDArray[np.float64]) -> None:
    """The result does not depend on which way the array is stored."""
    np.testing.assert_array_equal(fill_depressions(dem.T), fill_depressions(dem).T)


@SETTINGS
@given(dem=dems(), connectivity=st.sampled_from(CONNECTIVITIES))
def test_flip_equivariance(dem: npt.NDArray[np.float64], connectivity: Connectivity) -> None:
    """No preferred scan direction leaks out of the row-major traversal."""
    np.testing.assert_array_equal(
        fill_depressions(dem[::-1], connectivity=connectivity),
        fill_depressions(dem, connectivity=connectivity)[::-1],
    )


@SETTINGS
@given(dem=dems())
def test_result_is_deterministic(dem: npt.NDArray[np.float64]) -> None:
    np.testing.assert_array_equal(fill_depressions(dem), fill_depressions(dem))


# --- the epsilon variant ----------------------------------------------------------


@SETTINGS
@given(dem=dems(), epsilon=st.floats(1e-4, 1e-2))
def test_epsilon_still_never_lowers_a_cell(dem: npt.NDArray[np.float64], epsilon: float) -> None:
    assert np.all(fill_depressions(dem, epsilon=epsilon) >= dem)


@SETTINGS
@given(dem=dems(), epsilon=st.floats(1e-4, 1e-2))
def test_epsilon_dominates_the_flat_fill(dem: npt.NDArray[np.float64], epsilon: float) -> None:
    assert np.all(fill_depressions(dem, epsilon=epsilon) >= fill_depressions(dem, epsilon=0.0))


@SETTINGS
@given(dem=dems(), epsilon=st.floats(1e-3, 1e-2), connectivity=st.sampled_from(CONNECTIVITIES))
def test_epsilon_leaves_no_flats_to_route_across(
    dem: npt.NDArray[np.float64], epsilon: float, connectivity: Connectivity
) -> None:
    """Every interior cell of an epsilon-filled DEM has a *strictly* lower neighbour."""
    filled = fill_depressions(dem, epsilon=epsilon, connectivity=connectivity)
    assert not _cells_without_a_strict_descent(filled, connectivity).any()


def _cells_without_a_strict_descent(
    dem: npt.NDArray[np.floating], connectivity: Connectivity
) -> npt.NDArray[np.bool_]:
    """Return interior cells with no strictly lower neighbour."""
    from floodline.terrain._neighbours import neighbour_offsets

    rows, cols = dem.shape
    stuck = np.zeros((rows, cols), dtype=np.bool_)
    offsets = neighbour_offsets(connectivity)
    for row in range(1, rows - 1):
        for col in range(1, cols - 1):
            z = dem[row, col]
            if not any(dem[row + dr, col + dc] < z for dr, dc in offsets):
                stuck[row, col] = True
    return stuck


# --- dtypes -----------------------------------------------------------------------


@SETTINGS
@given(dem=dems(dtype=np.float32))
def test_float32_holds_the_invariants(dem: npt.NDArray[np.float32]) -> None:
    filled = fill_depressions(dem)
    assert filled.dtype == np.float32
    assert np.all(filled >= dem)
    assert not undrained_mask(filled).any()


@SETTINGS
@given(dem=dems())
def test_float32_and_float64_agree(dem: npt.NDArray[np.float64]) -> None:
    as32 = fill_depressions(dem.astype(np.float32))
    as64 = fill_depressions(dem)
    np.testing.assert_allclose(as32, as64.astype(np.float32), rtol=1e-6, atol=1e-4)


@pytest.mark.parametrize("connectivity", CONNECTIVITIES)
def test_a_flat_grid_is_entirely_drained(connectivity: Connectivity) -> None:
    """Degenerate but worth pinning: a perfectly flat surface has no depressions."""
    dem = np.full((6, 6), 3.0)
    assert not undrained_mask(dem, connectivity=connectivity).any()
    np.testing.assert_array_equal(fill_depressions(dem, connectivity=connectivity), dem)
