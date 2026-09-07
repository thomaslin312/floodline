"""The terrain cache: stable keys, atomic writes, and an indivisible artefact."""

from __future__ import annotations

import numpy as np
import pytest

from floodline.storage import LocalTerrainStore, TerrainArtifact, params_hash


def _artifact(shape: tuple[int, int] = (6, 7)) -> TerrainArtifact:
    rng = np.random.default_rng(0)
    return TerrainArtifact(
        hand=rng.random(shape).astype(np.float32),
        streams=rng.random(shape) > 0.5,
        filled=(rng.random(shape) * 100).astype(np.float32),
        flowdir=rng.integers(0, 8, shape).astype(np.int16),
        drainage_index=rng.integers(0, shape[0] * shape[1], shape).astype(np.int64),
        transform=(30.0, 0.0, 500000.0, 0.0, -30.0, 3300000.0),
        crs="EPSG:26915",
    )


def test_the_key_is_stable_across_processes() -> None:
    """Python salts string hashing per process, so `hash()` would miss every restart."""
    params = {"resolution_m": 30.0, "fill_epsilon": 0.0, "crs": "EPSG:26915"}
    assert params_hash(params) == params_hash(dict(reversed(list(params.items()))))
    assert params_hash(params) != params_hash({**params, "resolution_m": 10.0})
    assert len(params_hash(params)) == 16


def test_a_value_with_no_canonical_form_is_refused() -> None:
    """A repr carrying a memory address would make every run a cache miss."""

    class Opaque:
        pass

    with pytest.raises(TypeError, match="no canonical form"):
        params_hash({"thing": Opaque()})


def test_a_roundtrip_returns_what_went_in(tmp_path) -> None:
    store = LocalTerrainStore(root=tmp_path)
    artifact = _artifact()
    assert not store.exists("1204010403", "abc123")
    store.put_hand("1204010403", "abc123", artifact)
    assert store.exists("1204010403", "abc123")

    back = store.get_hand("1204010403", "abc123")
    np.testing.assert_allclose(back.hand, artifact.hand, rtol=0, atol=1e-6)
    np.testing.assert_array_equal(back.streams, artifact.streams)
    np.testing.assert_allclose(back.filled, artifact.filled, rtol=0, atol=1e-4)
    np.testing.assert_array_equal(back.flowdir, artifact.flowdir)
    # The drainage index is a flat cell index: a rounded one points at the wrong cell.
    np.testing.assert_array_equal(back.drainage_index, artifact.drainage_index)
    assert back.crs == artifact.crs


def test_a_missing_entry_raises_rather_than_returning_empty(tmp_path) -> None:
    store = LocalTerrainStore(root=tmp_path)
    with pytest.raises(KeyError, match="no terrain cached"):
        store.get_hand("1204010403", "nothere")


def test_a_partial_write_never_looks_cached(tmp_path) -> None:
    """A truncated file under the real name would return wrong numbers, not raise."""
    store = LocalTerrainStore(root=tmp_path)
    path = tmp_path / "hand" / "1204010403" / "abc123.tif"
    path.parent.mkdir(parents=True)
    # What a killed process leaves behind: a temp file, and no destination.
    (path.parent / ".abc123.tif.999.partial").write_bytes(b"half a raster")
    assert not store.exists("1204010403", "abc123")

    store.put_hand("1204010403", "abc123", _artifact())
    assert store.exists("1204010403", "abc123")
    leftovers = list(path.parent.glob("*.partial"))
    assert not [p for p in leftovers if "999" not in p.name], "own temp files cleaned up"


def test_the_grids_must_describe_one_raster() -> None:
    """A HAND grid paired with someone else's network is wrong and does not raise."""
    good = _artifact()
    with pytest.raises(ValueError, match="different rasters"):
        TerrainArtifact(
            hand=good.hand,
            streams=np.zeros((3, 3), dtype=bool),
            filled=good.filled,
            flowdir=good.flowdir,
            drainage_index=good.drainage_index,
            transform=good.transform,
            crs=good.crs,
        )


def test_a_key_cannot_walk_out_of_the_cache_root(tmp_path) -> None:
    store = LocalTerrainStore(root=tmp_path)
    for huc, key in (("../etc", "abc"), ("1204010403", "../../abc"), ("a/b", "c")):
        with pytest.raises(ValueError, match="alphanumeric"):
            store.exists(huc, key)
