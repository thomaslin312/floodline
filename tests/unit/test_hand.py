from __future__ import annotations

import numpy as np
import pytest

from floodline.core.terrain.fill import fill_depressions
from floodline.core.terrain.flowacc import flow_accumulation
from floodline.core.terrain.flowdir import flow_direction
from floodline.core.terrain.hand import hand
from floodline.core.terrain.streams import stream_mask
from floodline.synthetic import SyntheticCatchment


def _chain(
    dem: np.ndarray, cellsize: float = 1.0, threshold: int = 5
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    filled = fill_depressions(dem, epsilon=1e-4)
    fdir = flow_direction(filled, cellsize=(cellsize, cellsize))
    acc = flow_accumulation(fdir).accumulation
    return filled, fdir, stream_mask(acc, fdir, threshold=threshold)


def test_hand_on_a_plane_is_the_drop_to_the_channel() -> None:
    """Rows draining south into a channel: HAND is the elevation above it."""
    dem = np.tile(np.arange(6.0, 0.0, -1.0).reshape(-1, 1), (1, 4))
    filled, fdir, _ = _chain(dem)
    streams = np.zeros_like(dem, dtype=bool)
    streams[5, :] = True  # the bottom row is the channel

    result = hand(filled, fdir, streams)
    np.testing.assert_allclose(result.hand[:, 1], [5, 4, 3, 2, 1, 0], atol=1e-3)
    assert result.cells_without_drainage == 0


def test_stream_cells_have_exactly_zero_hand() -> None:
    dem = np.tile(np.arange(8.0, 0.0, -1.0).reshape(-1, 1), (1, 5))
    filled, fdir, streams = _chain(dem, threshold=5)
    result = hand(filled, fdir, streams)
    assert np.all(result.hand[streams] == 0.0)


def test_drainage_index_points_at_a_stream_cell() -> None:
    dem = np.tile(np.arange(8.0, 0.0, -1.0).reshape(-1, 1), (1, 5))
    filled, fdir, streams = _chain(dem, threshold=5)
    result = hand(filled, fdir, streams)
    found = result.drainage_index >= 0
    cols = dem.shape[1]
    for row, col in np.argwhere(found):
        assert streams[divmod(int(result.drainage_index[row, col]), cols)]


def test_hand_equals_the_elevation_difference() -> None:
    """The defining identity, checked against the drainage index it reports."""
    dem = np.tile(np.arange(9.0, 0.0, -1.0).reshape(-1, 1), (1, 6))
    filled, fdir, streams = _chain(dem, threshold=6)
    result = hand(filled, fdir, streams)
    cols = dem.shape[1]
    for row, col in np.argwhere(result.drainage_index >= 0):
        outlet = divmod(int(result.drainage_index[row, col]), cols)
        assert result.hand[row, col] == pytest.approx(filled[row, col] - filled[outlet])


def test_cells_that_never_reach_a_stream_are_nan() -> None:
    dem = np.tile(np.arange(6.0, 0.0, -1.0).reshape(-1, 1), (1, 4))
    filled, fdir, _ = _chain(dem)
    empty = np.zeros_like(dem, dtype=bool)
    result = hand(filled, fdir, empty)
    assert np.all(np.isnan(result.hand))
    assert result.cells_without_drainage == result.n_valid
    assert result.undrained_fraction == 1.0


def test_nodata_stays_nan_and_is_not_counted() -> None:
    dem = np.tile(np.arange(6.0, 0.0, -1.0).reshape(-1, 1), (1, 4))
    dem[2, 2] = np.nan
    filled, fdir, _ = _chain(dem)
    streams = np.zeros_like(dem, dtype=bool)
    streams[5, :] = True
    result = hand(filled, fdir, streams)
    assert np.isnan(result.hand[2, 2])
    assert result.n_valid == dem.size - 1


def test_shape_mismatch_rejected() -> None:
    dem = np.zeros((5, 5))
    filled, fdir, _ = _chain(dem)
    with pytest.raises(ValueError, match="same shape"):
        hand(filled, fdir, np.zeros((3, 3), dtype=bool))


def test_integer_dem_rejected() -> None:
    dem = np.zeros((5, 5))
    _, fdir, streams = _chain(dem)
    with pytest.raises(TypeError, match="floating"):
        hand(np.zeros((5, 5), dtype=np.int32), fdir, streams)


def test_synthetic_catchment_hand(catchment: SyntheticCatchment) -> None:
    filled, fdir, streams = _chain(
        catchment.dem.astype(np.float64), catchment.cellsize, threshold=200
    )
    result = hand(filled, fdir, streams, nodata=None)

    finite = np.isfinite(result.hand)
    assert finite.any()
    assert np.all(result.hand[finite] >= 0.0)
    assert np.all(result.hand[streams] == 0.0)
    # hillslopes stand above the valley, but not by more than the total relief
    assert result.hand[finite].max() <= np.ptp(filled) + 1e-9
    assert result.hand[finite].max() > 1.0


def test_hand_grows_away_from_the_channel(catchment: SyntheticCatchment) -> None:
    filled, fdir, streams = _chain(
        catchment.dem.astype(np.float64), catchment.cellsize, threshold=200
    )
    result = hand(filled, fdir, streams)
    rows = range(30, result.hand.shape[0] - 30, 10)
    for row in rows:
        thalweg = int(catchment.channel_cols[row])
        near = result.hand[row, thalweg]
        far = result.hand[row, max(0, thalweg - 15)]
        if np.isfinite(near) and np.isfinite(far):
            assert far > near
