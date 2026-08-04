"""Runtime benchmarks for the terrain kernels.

Not an assertion about speed — a number to put in the write-up, and a tripwire if
the kernel ever loses its JIT. Skipped when the JIT is off, where the same code
runs as pure Python and the timing means nothing.
"""

from __future__ import annotations

import os

import numpy as np
import pytest
from pytest_benchmark.fixture import BenchmarkFixture

from floodline.config import Config
from floodline.synthetic import make_synthetic_catchment
from floodline.terrain.fill import fill_depressions
from floodline.terrain.flowdir import flow_direction
from floodline.terrain.route import route_terrain

pytestmark = pytest.mark.slow

JIT_DISABLED = os.environ.get("NUMBA_DISABLE_JIT", "0") not in {"0", "", "false"}


@pytest.mark.skipif(JIT_DISABLED, reason="timing a pure-Python fallback is meaningless")
@pytest.mark.parametrize("size", [512, 1024])
def test_fill_benchmark(benchmark: BenchmarkFixture, size: int) -> None:
    dem = make_synthetic_catchment(
        rows=size, cols=size, n_pits=40, roughness_m=0.2, seed=1
    ).dem.astype(np.float32)
    fill_depressions(dem)  # pay for JIT compilation outside the timed section
    filled = benchmark(fill_depressions, dem)
    assert np.all(filled >= dem)


@pytest.mark.skipif(JIT_DISABLED, reason="timing a pure-Python fallback is meaningless")
@pytest.mark.parametrize("size", [512, 1024])
def test_flow_direction_benchmark(benchmark: BenchmarkFixture, size: int) -> None:
    dem = make_synthetic_catchment(
        rows=size, cols=size, n_pits=40, roughness_m=0.2, seed=1
    ).dem.astype(np.float32)
    filled = fill_depressions(dem, epsilon=1e-3)
    flow_direction(filled)  # pay for JIT compilation outside the timed section
    fdir = benchmark(flow_direction, filled, cellsize=(5.0, 5.0))
    assert fdir.shape == (size, size)


@pytest.mark.skipif(JIT_DISABLED, reason="timing a pure-Python fallback is meaningless")
@pytest.mark.parametrize("size", [1024, 4096])
def test_terrain_chain_benchmark(benchmark: BenchmarkFixture, size: int) -> None:
    """The whole chain: fill, flow direction, flat resolution, accumulation, streams, HAND.

    4096 x 4096 is about 17 million cells, which is the order of a Lismore 1 m tile
    set, so this is the number that says whether the terrain core is fast enough to
    be run repeatedly during the resolution experiment.
    """
    config = Config.model_validate(
        {"terrain": {"stream_threshold_cells": max(200, size * size // 5000)}}
    )
    dem = make_synthetic_catchment(
        rows=size, cols=size, n_pits=size // 16, roughness_m=0.2, seed=3
    ).dem.astype(np.float32)

    route_terrain(dem, config=config, cellsize=(5.0, 5.0))  # pay for JIT compilation

    chain = benchmark(route_terrain, dem, config=config, cellsize=(5.0, 5.0))
    assert chain.drains_completely
    assert chain.flat_cells_after == 0
    assert chain.streams.any()
