"""The split at the discharge boundary, and that it is a split rather than a rewrite."""

from __future__ import annotations

import numpy as np

from floodline.core.config import Config
from floodline.pipeline import compute_terrain, run_scenario, terrain_params
from floodline.storage import LocalTerrainStore, params_hash

TRANSFORM = (30.0, 0.0, 500000.0, 0.0, -30.0, 3300000.0)


def _valley(rows: int = 90, cols: int = 90) -> np.ndarray:
    y, x = np.mgrid[0:rows, 0:cols]
    rng = np.random.default_rng(0)
    return 40.0 - 0.02 * y + 0.004 * np.abs(x - cols / 2) ** 1.6 + rng.normal(0, 0.02, (rows, cols))


def test_terrain_is_independent_of_discharge() -> None:
    """The expensive half must not depend on the per-request half, or the cache is void."""
    cfg = Config()
    terrain = compute_terrain(_valley(), config=cfg, cellsize=(30.0, 30.0), transform=TRANSFORM)
    low = run_scenario(terrain, 50.0, config=cfg)
    high = run_scenario(terrain, 500.0, config=cfg)

    assert high.flood.n_wet >= low.flood.n_wet, "more water floods more"
    assert low.discharge_cms == 50.0 and high.discharge_cms == 500.0
    # The terrain object is untouched by either scenario: same arrays, same identity.
    assert terrain.hand is terrain.chain.hand.hand


def test_the_same_scenario_twice_gives_the_same_answer() -> None:
    cfg = Config()
    terrain = compute_terrain(_valley(), config=cfg, cellsize=(30.0, 30.0), transform=TRANSFORM)
    first = run_scenario(terrain, 120.0, config=cfg)
    second = run_scenario(terrain, 120.0, config=cfg)
    np.testing.assert_array_equal(first.depth_m, second.depth_m)
    np.testing.assert_array_equal(first.margin_m, second.margin_m)


def test_the_margin_is_signed_where_the_depth_is_clamped() -> None:
    """The Monte Carlo needs how far short the water stopped; depth floors at zero."""
    cfg = Config()
    terrain = compute_terrain(_valley(), config=cfg, cellsize=(30.0, 30.0), transform=TRANSFORM)
    out = run_scenario(terrain, 80.0, config=cfg)
    # NaN is nodata off the raster, so the floor applies to real cells only.
    real = np.isfinite(out.depth_m)
    assert (out.depth_m[real] >= 0).all()
    finite = np.isfinite(out.margin_m)
    assert (out.margin_m[finite] < 0).any(), "somewhere the water stopped short"


def test_hand_and_the_network_survive_a_cache_roundtrip(tmp_path) -> None:
    """They are a matched pair: HAND is measured *to* the network."""
    cfg = Config()
    terrain = compute_terrain(_valley(), config=cfg, cellsize=(30.0, 30.0), transform=TRANSFORM)
    store = LocalTerrainStore(root=tmp_path)
    store.put_hand("1204010403", terrain.params_hash, terrain.to_artifact())
    back = store.get_hand("1204010403", terrain.params_hash)

    np.testing.assert_allclose(
        np.nan_to_num(back.hand, nan=-1.0),
        np.nan_to_num(terrain.hand.astype(np.float32), nan=-1.0),
        rtol=0,
        atol=1e-5,
    )
    np.testing.assert_array_equal(back.streams, terrain.streams)


def test_the_cache_key_ignores_what_terrain_does_not_depend_on() -> None:
    """Manning's roughness enters through the rating curves, not the cached grids."""
    base = Config()
    rougher = base.model_copy(
        update={"hydraulics": base.hydraulics.model_copy(update={"manning_n": 0.09})}
    )
    assert params_hash(terrain_params(base, 30.0)) == params_hash(terrain_params(rougher, 30.0))

    finer = params_hash(terrain_params(base, 10.0))
    assert finer != params_hash(terrain_params(base, 30.0)), "resolution changes the grids"

    thinner = base.model_copy(
        update={"terrain": base.terrain.model_copy(update={"stream_threshold_cells": 4000})}
    )
    assert params_hash(terrain_params(thinner, 30.0)) != params_hash(terrain_params(base, 30.0))
