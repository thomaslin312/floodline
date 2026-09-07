from __future__ import annotations

import numpy as np
import pytest

from floodline.core.config import Config, Connectivity
from floodline.core.terrain.fill import fill_depressions, undrained_mask
from floodline.synthetic import SyntheticCatchment, make_synthetic_catchment

# --- hand-built surfaces where the right answer is known by inspection ---------


def test_single_pit_is_raised_to_its_spill_level() -> None:
    dem = np.full((5, 5), 10.0)
    dem[2, 2] = 4.0
    filled = fill_depressions(dem)
    assert filled[2, 2] == pytest.approx(10.0)
    assert np.array_equal(np.delete(filled.ravel(), 12), np.delete(dem.ravel(), 12))


def test_bowl_fills_to_the_lowest_rim_cell_not_the_highest() -> None:
    """The spill elevation is the *lowest* point on the rim: the water finds the gap."""
    dem = np.full((7, 7), 20.0)
    dem[2:5, 2:5] = 5.0
    dem[0, 3] = 12.0  # a notch in the rim, reached through the border cell
    dem[1, 3] = 12.0
    filled = fill_depressions(dem)
    assert np.all(filled[2:5, 2:5] == pytest.approx(12.0))
    assert filled[0, 3] == pytest.approx(12.0)


def test_plane_without_pits_is_untouched() -> None:
    dem = np.tile(np.arange(10.0, 0.0, -1.0), (8, 1))
    np.testing.assert_array_equal(fill_depressions(dem), dem)


def test_pit_on_the_border_drains_off_the_edge() -> None:
    dem = np.full((5, 5), 10.0)
    dem[0, 2] = 3.0  # on the border, so it is an outlet, not a pit
    np.testing.assert_array_equal(fill_depressions(dem), dem)


def test_nested_depressions() -> None:
    """A pit inside a pit fills to the outer spill in one pass."""
    dem = np.full((9, 9), 30.0)
    dem[2:7, 2:7] = 20.0
    dem[4, 4] = 5.0
    dem[0:2, 4] = 25.0  # a notch through the outer rim, all the way to the border
    filled = fill_depressions(dem)
    assert np.all(filled[2:7, 2:7] == pytest.approx(25.0))
    assert filled[4, 4] == pytest.approx(25.0)


# --- nodata --------------------------------------------------------------------


def test_nan_cells_are_outlets_and_survive() -> None:
    dem = np.full((7, 7), 10.0)
    dem[3, 3] = 2.0
    dem[3, 4] = np.nan  # the pit now touches nodata, so it can drain into it
    filled = fill_depressions(dem)
    assert np.isnan(filled[3, 4])
    assert filled[3, 3] == pytest.approx(2.0)


def test_sentinel_nodata_is_honoured_and_returned_unchanged() -> None:
    dem = np.full((7, 7), 10.0)
    dem[3, 3] = 2.0
    dem[0:2, :] = -9999.0
    filled = fill_depressions(dem, nodata=-9999.0)
    assert np.all(filled[0:2, :] == -9999.0)
    assert filled[3, 3] == pytest.approx(10.0)  # still enclosed by valid cells


def test_sentinel_ignored_when_not_declared() -> None:
    """-9999 is just a very deep pit unless the caller says it is nodata."""
    dem = np.full((7, 7), 10.0)
    dem[3, 3] = -9999.0
    filled = fill_depressions(dem)
    assert filled[3, 3] == pytest.approx(10.0)


def test_all_nodata_grid_is_a_no_op() -> None:
    dem = np.full((5, 5), np.nan)
    assert np.all(np.isnan(fill_depressions(dem)))
    assert not undrained_mask(dem).any()


# --- epsilon -------------------------------------------------------------------


def test_epsilon_gives_the_flat_a_gradient() -> None:
    dem = np.full((9, 9), 10.0)
    dem[2:7, 2:7] = 5.0
    flat = fill_depressions(dem, epsilon=0.0)
    sloped = fill_depressions(dem, epsilon=0.01)
    assert len(np.unique(flat[2:7, 2:7])) == 1
    assert len(np.unique(sloped[2:7, 2:7])) > 1
    assert np.all(sloped >= flat)


def test_epsilon_is_read_from_config() -> None:
    dem = np.full((9, 9), 10.0)
    dem[2:7, 2:7] = 5.0
    cfg = Config.model_validate({"terrain": {"fill_epsilon": 0.01}})
    np.testing.assert_array_equal(
        fill_depressions(dem, config=cfg), fill_depressions(dem, epsilon=0.01)
    )


def test_negative_epsilon_rejected() -> None:
    with pytest.raises(ValueError, match="non-negative"):
        fill_depressions(np.zeros((4, 4)), epsilon=-0.1)


def test_epsilon_that_vanishes_in_the_dtype_is_rejected() -> None:
    dem = np.full((5, 5), 1.0e7, dtype=np.float32)
    dem[2, 2] = 1.0
    with pytest.raises(ValueError, match="vanishes"):
        fill_depressions(dem, epsilon=1e-6)


# --- connectivity ---------------------------------------------------------------


def test_d4_cannot_escape_through_a_diagonal_gap() -> None:
    """A pit whose only lower neighbour is diagonal drains under D8 but not D4."""
    dem = np.full((5, 5), 10.0)
    dem[2, 2] = 4.0
    dem[1, 1] = 4.0
    dem[0, 0] = 3.0
    d8 = fill_depressions(dem, connectivity=Connectivity.EIGHT)
    d4 = fill_depressions(dem, connectivity=Connectivity.FOUR)
    assert d8[2, 2] == pytest.approx(4.0)
    assert d4[2, 2] == pytest.approx(10.0)


# --- dtype and shape guards ------------------------------------------------------


@pytest.mark.parametrize("dtype", [np.float32, np.float64])
def test_dtype_is_preserved(dtype: type) -> None:
    dem = np.full((6, 6), 10.0, dtype=dtype)
    dem[3, 3] = 1.0
    filled = fill_depressions(dem)
    assert filled.dtype == dtype
    assert filled[3, 3] == pytest.approx(10.0)


def test_input_is_not_mutated() -> None:
    dem = np.full((6, 6), 10.0)
    dem[3, 3] = 1.0
    before = dem.copy()
    fill_depressions(dem)
    np.testing.assert_array_equal(dem, before)


def test_integer_dem_rejected() -> None:
    with pytest.raises(TypeError, match="floating"):
        fill_depressions(np.zeros((4, 4), dtype=np.int32))


def test_three_dimensional_dem_rejected() -> None:
    with pytest.raises(ValueError, match="2-D"):
        fill_depressions(np.zeros((2, 4, 4)))


def test_single_row_grid() -> None:
    dem = np.array([[5.0, 1.0, 5.0]])
    np.testing.assert_array_equal(fill_depressions(dem), dem)  # all cells are border


def test_return_raised_counts_cells() -> None:
    dem = np.full((7, 7), 10.0)
    dem[3, 3] = 1.0
    dem[3, 4] = 2.0
    _, raised = fill_depressions(dem, return_raised=True)
    assert raised == 2


# --- undrained_mask --------------------------------------------------------------


def test_undrained_mask_finds_the_pit() -> None:
    dem = np.full((7, 7), 10.0)
    dem[3, 3] = 1.0
    mask = undrained_mask(dem)
    assert mask[3, 3]
    assert mask.sum() == 1


def test_undrained_mask_is_empty_after_filling() -> None:
    dem = np.full((7, 7), 10.0)
    dem[3, 3] = 1.0
    assert not undrained_mask(fill_depressions(dem)).any()


def test_undrained_mask_excludes_nodata() -> None:
    dem = np.full((7, 7), 10.0)
    dem[3, 3] = 1.0
    dem[0, 0] = np.nan
    mask = undrained_mask(dem)
    assert not mask[0, 0]


# --- the synthetic catchment -----------------------------------------------------


def test_synthetic_catchment_has_exactly_the_pits_we_punched(
    catchment: SyntheticCatchment,
) -> None:
    mask = undrained_mask(catchment.dem.astype(np.float64))
    assert mask.any()
    for row, col in catchment.sinks:
        assert mask[row, col]


def test_filling_the_synthetic_catchment_removes_every_depression(
    catchment: SyntheticCatchment,
) -> None:
    dem = catchment.dem.astype(np.float64)
    filled, raised = fill_depressions(dem, return_raised=True)
    assert raised > 0
    assert not undrained_mask(filled).any()
    assert np.all(filled >= dem)
    # the valley and the plane are untouched; only the pits move
    assert np.count_nonzero(filled > dem) < dem.size // 10


def test_filling_respects_the_nodata_border(
    catchment_with_nodata: SyntheticCatchment,
) -> None:
    dem = catchment_with_nodata.dem.astype(np.float64)
    filled = fill_depressions(dem, nodata=catchment_with_nodata.nodata)
    border = catchment_with_nodata.nodata_mask
    np.testing.assert_array_equal(filled[border], dem[border])
    assert not undrained_mask(filled, nodata=catchment_with_nodata.nodata).any()


def test_pit_free_catchment_is_a_fixed_point() -> None:
    dem = make_synthetic_catchment(rows=60, cols=48, n_pits=0, seed=0).dem.astype(np.float64)
    assert not undrained_mask(dem).any()
    np.testing.assert_array_equal(fill_depressions(dem), dem)
