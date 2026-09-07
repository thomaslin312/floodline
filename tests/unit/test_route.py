from __future__ import annotations

import numpy as np
import pytest

from floodline.core.config import Config
from floodline.core.terrain.flowdir import FLOW_FLAT, steps_to_outlet
from floodline.core.terrain.route import route_terrain
from floodline.synthetic import SyntheticCatchment, make_synthetic_catchment


def test_chain_runs_end_to_end(catchment: SyntheticCatchment) -> None:
    chain = route_terrain(
        catchment.dem.astype(np.float64),
        config=Config(),
        cellsize=(catchment.cellsize,) * 2,
        stream_threshold=200,
    )
    assert chain.cells_raised_by_fill > 0
    assert chain.flat_cells_before > 0
    assert chain.flat_cells_after == 0
    assert chain.drains_completely
    assert chain.streams.any()
    assert np.all(chain.hand.hand[chain.streams] == 0.0)
    steps_to_outlet(chain.flowdir)


def test_default_config_drains_completely(catchment: SyntheticCatchment) -> None:
    """The shipped defaults must not strand water; that is the point of step 4."""
    chain = route_terrain(catchment.dem.astype(np.float64), cellsize=(catchment.cellsize,) * 2)
    assert chain.drains_completely
    assert chain.accumulation.cells_draining_to_outlets == chain.accumulation.n_valid


def test_disabling_flat_resolution_strands_water(catchment: SyntheticCatchment) -> None:
    cfg = Config.model_validate({"terrain": {"resolve_flats": False, "fill_epsilon": 0.0}})
    chain = route_terrain(
        catchment.dem.astype(np.float64), config=cfg, cellsize=(catchment.cellsize,) * 2
    )
    assert not chain.drains_completely
    assert chain.flat_cells_after == chain.flat_cells_before
    assert (chain.flowdir == FLOW_FLAT).any()


def test_epsilon_fill_is_the_other_route_to_the_same_place(
    catchment: SyntheticCatchment,
) -> None:
    cfg = Config.model_validate({"terrain": {"resolve_flats": False, "fill_epsilon": 1e-4}})
    chain = route_terrain(
        catchment.dem.astype(np.float64), config=cfg, cellsize=(catchment.cellsize,) * 2
    )
    assert chain.flat_cells_before == 0
    assert chain.drains_completely


@pytest.mark.parametrize("seed", [0, 3, 11])
def test_defaults_drain_every_fixture(seed: int) -> None:
    catchment = make_synthetic_catchment(rows=100, cols=80, n_pits=5, roughness_m=0.3, seed=seed)
    chain = route_terrain(catchment.dem.astype(np.float64), cellsize=(catchment.cellsize,) * 2)
    assert chain.drains_completely
    assert chain.flat_cells_after == 0


def test_nodata_survives_the_chain(catchment_with_nodata: SyntheticCatchment) -> None:
    dem = catchment_with_nodata.dem.astype(np.float64)
    chain = route_terrain(
        dem,
        nodata=catchment_with_nodata.nodata,
        cellsize=(catchment_with_nodata.cellsize,) * 2,
        stream_threshold=100,
    )
    border = catchment_with_nodata.nodata_mask
    np.testing.assert_array_equal(chain.filled[border], dem[border])
    assert not chain.streams[border].any()
    assert chain.drains_completely
