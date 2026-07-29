from __future__ import annotations

import numpy as np
import pytest

from floodline.synthetic import SyntheticCatchment
from floodline.terrain.fill import fill_depressions
from floodline.terrain.flowacc import flow_accumulation
from floodline.terrain.flowdir import FLOW_FLAT, flow_direction

E, SE, S, SW, W, NW, N, NE = 1, 2, 4, 8, 16, 32, 64, 128


def test_plane_accumulates_down_the_columns() -> None:
    """Five rows draining south: the outlet row carries the whole column."""
    dem = np.tile(np.arange(5.0, 0.0, -1.0).reshape(-1, 1), (1, 3))
    result = flow_accumulation(flow_direction(dem))
    np.testing.assert_array_equal(result.accumulation[:, 1], [1, 2, 3, 4, 5])
    assert result.cells_draining_to_outlets == 15
    assert result.cells_draining_to_flats == 0
    assert result.n_valid == 15


def test_accumulation_is_one_plus_the_upstream_sum() -> None:
    """The defining recurrence, checked cell by cell against the pointers."""
    dem = np.tile(np.arange(6.0, 0.0, -1.0).reshape(-1, 1), (1, 4))
    fdir = flow_direction(dem)
    acc = flow_accumulation(fdir).accumulation

    from floodline.terrain.flowdir import downstream_index

    receiver = downstream_index(fdir)
    upstream_sum = np.zeros_like(acc)
    rows, cols = acc.shape
    for row in range(rows):
        for col in range(cols):
            target = int(receiver[row, col])
            if target >= 0:
                upstream_sum[divmod(target, cols)] += acc[row, col]
    np.testing.assert_allclose(acc, 1.0 + upstream_sum)


def test_single_outlet_carries_every_cell() -> None:
    """A cone draining to one corner: that corner accumulates the whole grid."""
    rows = cols = 7
    row_idx, col_idx = np.mgrid[0:rows, 0:cols]
    dem = (row_idx + col_idx).astype(np.float64)  # lowest at (0, 0)
    result = flow_accumulation(flow_direction(dem))
    assert result.accumulation[0, 0] == pytest.approx(rows * cols)
    assert result.cells_draining_to_outlets == rows * cols


def test_weights_are_honoured() -> None:
    dem = np.tile(np.arange(4.0, 0.0, -1.0).reshape(-1, 1), (1, 2))
    weights = np.full(dem.shape, 2.5)
    acc = flow_accumulation(flow_direction(dem), weights=weights).accumulation
    np.testing.assert_allclose(acc[:, 0], [2.5, 5.0, 7.5, 10.0])


def test_weights_shape_is_checked() -> None:
    dem = np.zeros((4, 4))
    with pytest.raises(ValueError, match="does not match"):
        flow_accumulation(flow_direction(dem), weights=np.ones((3, 3)))


def test_nodata_contributes_nothing() -> None:
    dem = np.tile(np.arange(5.0, 0.0, -1.0).reshape(-1, 1), (1, 3))
    dem[2, 1] = np.nan
    result = flow_accumulation(flow_direction(dem))
    assert result.accumulation[2, 1] == 0.0
    assert result.n_valid == 14
    assert result.cells_draining_to_outlets + result.cells_draining_to_flats == 14


def test_cells_draining_into_flats_are_counted() -> None:
    """A filled bowl is flat; everything that runs into it stops there."""
    dem = np.full((11, 11), 30.0)
    dem[3:8, 3:8] = 10.0
    dem[0:3, 5] = 20.0  # a notch so the bowl is not the whole story
    filled = fill_depressions(dem, epsilon=0.0)
    fdir = flow_direction(filled)
    assert (fdir == FLOW_FLAT).any()

    result = flow_accumulation(fdir)
    assert result.cells_draining_to_flats > 0
    assert result.cells_draining_to_outlets + result.cells_draining_to_flats == result.n_valid
    assert 0.0 < result.flat_drainage_fraction < 1.0


def test_epsilon_fill_sends_everything_to_the_outlets() -> None:
    dem = np.full((11, 11), 30.0)
    dem[3:8, 3:8] = 10.0
    dem[0:3, 5] = 20.0
    result = flow_accumulation(flow_direction(fill_depressions(dem, epsilon=1e-3)))
    assert result.cells_draining_to_flats == 0
    assert result.cells_draining_to_outlets == result.n_valid
    assert result.flat_drainage_fraction == 0.0


def test_cycle_is_reported() -> None:
    fdir = np.zeros((3, 3), dtype=np.int16)
    fdir[1, 1] = E
    fdir[1, 2] = W
    with pytest.raises(ValueError, match="cycle"):
        flow_accumulation(fdir)


def test_three_dimensional_input_rejected() -> None:
    with pytest.raises(ValueError, match="2-D"):
        flow_accumulation(np.zeros((2, 3, 3), dtype=np.int16))


def test_synthetic_catchment_concentrates_in_the_valley(
    catchment: SyntheticCatchment,
) -> None:
    filled = fill_depressions(catchment.dem.astype(np.float64), epsilon=1e-4)
    result = flow_accumulation(flow_direction(filled, cellsize=(catchment.cellsize,) * 2))
    acc = result.accumulation
    assert result.cells_draining_to_flats == 0
    assert result.cells_draining_to_outlets == result.n_valid

    # the thalweg should carry far more than the hillslopes beside it
    rows = range(20, acc.shape[0] - 20, 10)
    for row in rows:
        thalweg = int(catchment.channel_cols[row])
        assert acc[row, thalweg] > acc[row, max(0, thalweg - 10)] * 3
