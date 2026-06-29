from __future__ import annotations

import numpy as np
import pytest

from floodline.config import Config
from floodline.synthetic import SyntheticCatchment
from floodline.terrain.fill import fill_depressions
from floodline.terrain.flowdir import (
    FLOW_FLAT,
    FLOW_NODATA,
    FLOW_OUTLET,
    downstream_index,
    flow_direction,
    steps_to_outlet,
)

# ESRI codes, for readability in the assertions below.
E, SE, S, SW, W, NW, N, NE = 1, 2, 4, 8, 16, 32, 64, 128


def test_plane_drains_south_and_off_the_bottom_edge() -> None:
    dem = np.tile(np.arange(5.0, 0.0, -1.0).reshape(-1, 1), (1, 5))
    fdir = flow_direction(dem)
    assert np.all(fdir[:4, :] == S)
    assert np.all(fdir[4, :] == FLOW_OUTLET)


@pytest.mark.parametrize(
    ("low_cell", "expected"),
    [
        ((1, 2), E),
        ((2, 2), SE),
        ((2, 1), S),
        ((2, 0), SW),
        ((1, 0), W),
        ((0, 0), NW),
        ((0, 1), N),
        ((0, 2), NE),
    ],
)
def test_every_direction_code(low_cell: tuple[int, int], expected: int) -> None:
    """Each of the eight codes points where the ESRI diagram in the module says."""
    dem = np.full((3, 3), 10.0)
    dem[low_cell] = 0.0
    assert flow_direction(dem)[1, 1] == expected


def test_steepest_is_by_slope_not_by_drop() -> None:
    """A diagonal has to be sqrt(2) times further down to beat a cardinal neighbour."""
    dem = np.full((3, 3), 20.0)
    dem[1, 1] = 10.0
    dem[1, 2] = 9.0  # East: drop 1.0 over 1.0 -> slope 1.00
    dem[2, 2] = 8.7  # SE:   drop 1.3 over 1.414 -> slope 0.92
    assert flow_direction(dem)[1, 1] == E

    dem[2, 2] = 8.5  # SE:   drop 1.5 over 1.414 -> slope 1.06
    assert flow_direction(dem)[1, 1] == SE


def test_anisotropic_cells_are_ranked_by_true_distance() -> None:
    """With 2 m columns and 1 m rows, an equal drop north is twice the gradient."""
    dem = np.full((3, 3), 20.0)
    dem[1, 1] = 10.0
    dem[1, 2] = 9.0  # East, 2 m away
    dem[0, 1] = 9.0  # North, 1 m away
    assert flow_direction(dem, cellsize=(2.0, 1.0))[1, 1] == N
    assert flow_direction(dem, cellsize=(1.0, 2.0))[1, 1] == E


def test_ties_go_to_the_lowest_direction_code() -> None:
    """The documented tie-break, pinned so a reordered offsets array is caught."""
    dem = np.full((3, 3), 20.0)
    dem[1, 1] = 10.0
    for cell in ((0, 1), (1, 2), (2, 1), (1, 0)):  # N, E, S, W all equally low
        dem[cell] = 9.0
    assert flow_direction(dem)[1, 1] == E  # code 1 beats 4, 16 and 64


def test_flat_cells_are_marked_not_invented() -> None:
    dem = np.full((7, 7), 20.0)
    dem[2:5, 2:5] = 12.0
    dem[0:2, 3] = 12.0
    fdir = flow_direction(dem)
    assert fdir[3, 3] == FLOW_FLAT
    assert fdir[2, 3] == FLOW_FLAT


def test_nodata_cell_gets_zero_and_its_neighbours_drain_into_it() -> None:
    dem = np.full((5, 5), 20.0)
    dem[2, 2] = np.nan
    fdir = flow_direction(dem)
    assert fdir[2, 2] == FLOW_NODATA
    # [2, 1] has no lower valid neighbour but touches nodata, so it is an outlet
    assert fdir[2, 1] == FLOW_OUTLET


def test_sentinel_nodata_is_honoured() -> None:
    dem = np.full((5, 5), 20.0)
    dem[0, :] = -9999.0
    fdir = flow_direction(dem, nodata=-9999.0)
    assert np.all(fdir[0, :] == FLOW_NODATA)


def test_dinf_is_refused_rather_than_silently_d8() -> None:
    cfg = Config.model_validate({"terrain": {"flowdir_method": "dinf"}})
    with pytest.raises(NotImplementedError, match="D-infinity"):
        flow_direction(np.zeros((4, 4)), config=cfg)


@pytest.mark.parametrize("bad", [(0.0, 1.0), (1.0, -1.0)])
def test_bad_cellsize_rejected(bad: tuple[float, float]) -> None:
    with pytest.raises(ValueError, match="cellsize"):
        flow_direction(np.zeros((4, 4)), cellsize=bad)


def test_integer_dem_rejected() -> None:
    with pytest.raises(TypeError, match="floating"):
        flow_direction(np.zeros((4, 4), dtype=np.int32))


def test_three_dimensional_dem_rejected() -> None:
    with pytest.raises(ValueError, match="2-D"):
        flow_direction(np.zeros((2, 4, 4)))


# --- downstream_index and steps_to_outlet ----------------------------------------


def test_downstream_index_points_at_the_right_cell() -> None:
    dem = np.full((3, 3), 10.0)
    dem[2, 2] = 0.0
    receiver = downstream_index(flow_direction(dem))
    assert receiver[1, 1] == 2 * 3 + 2  # centre drains SE into (2, 2)


def test_downstream_index_terminates_at_outlets_flats_and_nodata() -> None:
    dem = np.full((7, 7), 20.0)
    dem[2:5, 2:5] = 12.0
    dem[3, 3] = np.nan
    receiver = downstream_index(flow_direction(dem))
    assert receiver[0, 0] == -1  # border outlet
    assert receiver[3, 3] == -1  # nodata


def test_steps_to_outlet_counts_the_path() -> None:
    dem = np.tile(np.arange(6.0, 0.0, -1.0).reshape(-1, 1), (1, 3))
    steps = steps_to_outlet(flow_direction(dem))
    assert steps[5, 1] == 1  # bottom row is the outlet itself
    assert steps[0, 1] == 6  # five steps south, then off the edge


def test_steps_to_outlet_detects_a_cycle() -> None:
    """Hand-built cyclic pointers: two cells pointing at each other."""
    fdir = np.zeros((3, 3), dtype=np.int16)
    fdir[1, 1] = E
    fdir[1, 2] = W
    with pytest.raises(ValueError, match="cycle"):
        steps_to_outlet(fdir)


# --- the synthetic catchment ------------------------------------------------------


def test_catchment_routes_without_cycles(catchment: SyntheticCatchment) -> None:
    filled = fill_depressions(catchment.dem.astype(np.float64), epsilon=1e-4)
    steps = steps_to_outlet(flow_direction(filled, cellsize=(catchment.cellsize,) * 2))
    assert steps.min() >= 0
    assert steps.max() < filled.size


def test_catchment_flow_converges_on_the_valley(catchment: SyntheticCatchment) -> None:
    """Cells beside the thalweg should point towards it, not away."""
    filled = fill_depressions(catchment.dem.astype(np.float64), epsilon=1e-4)
    receiver = downstream_index(flow_direction(filled, cellsize=(catchment.cellsize,) * 2))
    rows, cols = filled.shape
    moved_closer = 0
    checked = 0
    for row in range(10, rows - 10, 5):
        thalweg = int(catchment.channel_cols[row])
        for col in (thalweg - 4, thalweg + 4):
            target = receiver[row, col]
            if target < 0:
                continue
            checked += 1
            next_col = int(target % cols)
            next_thalweg = int(catchment.channel_cols[int(target // cols)])
            if abs(next_col - next_thalweg) <= abs(col - thalweg):
                moved_closer += 1
    assert checked > 0
    assert moved_closer == checked
