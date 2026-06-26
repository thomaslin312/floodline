"""Runtime benchmark for depression filling.

Not an assertion about speed — a number to put in the write-up, and a tripwire if
the kernel ever loses its JIT. Skipped when the JIT is off, where the same code
runs as pure Python and the timing means nothing.
"""

from __future__ import annotations

import os

import numpy as np
import pytest
from pytest_benchmark.fixture import BenchmarkFixture

from floodline.synthetic import make_synthetic_catchment
from floodline.terrain.fill import fill_depressions

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
