"""Bounding the caches that grow with the number of watersheds anyone looks at.

Only the served bundles were bounded. The terrain store and the structure inventory
were not, and this service offers every watershed in the country: 4.5 MB of terrain at
30 m and 16 MB of inventory per basin, accumulating for as long as the machine runs.
A full disk takes the service down with a failure that names nothing.
"""

from __future__ import annotations

import os
import time
from pathlib import Path

from floodline.cache import evict_to_budget

MB = 1024 * 1024


def _write(path: Path, megabytes: float, age_s: float = 0.0) -> Path:
    """Write a file of a given size, optionally backdated."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"\0" * int(megabytes * MB))
    if age_s:
        stamp = time.time() - age_s
        os.utime(path, (stamp, stamp))
    return path


def test_it_evicts_until_the_budget_is_met(tmp_path: Path) -> None:
    for index in range(5):
        _write(tmp_path / f"f{index}.tif", 1.0, age_s=100 - index)
    removed = evict_to_budget(tmp_path, budget_mb=3.0, pattern="*.tif")
    assert removed == 2
    total = sum(f.stat().st_size for f in tmp_path.glob("*.tif"))
    assert total <= 3 * MB


def test_it_evicts_the_oldest_first(tmp_path: Path) -> None:
    """A cache that evicted the newest would throw away what is about to be asked for."""
    old = _write(tmp_path / "old.tif", 2.0, age_s=1000)
    new = _write(tmp_path / "new.tif", 2.0, age_s=1)
    evict_to_budget(tmp_path, budget_mb=2.0, pattern="*.tif")
    assert not old.exists()
    assert new.exists()


def test_it_leaves_a_cache_under_budget_alone(tmp_path: Path) -> None:
    kept = _write(tmp_path / "small.tif", 1.0)
    assert evict_to_budget(tmp_path, budget_mb=100.0, pattern="*.tif") == 0
    assert kept.exists()


def test_it_only_touches_files_matching_the_pattern(tmp_path: Path) -> None:
    """The terrain store and the inventory share no directory, but a caller passing a
    parent by mistake must not delete the other cache, or a bug becomes data loss."""
    artefact = _write(tmp_path / "a.tif", 5.0, age_s=500)
    other = _write(tmp_path / "keep.parquet", 5.0, age_s=1000)
    evict_to_budget(tmp_path, budget_mb=1.0, pattern="*.tif")
    assert not artefact.exists()
    assert other.exists(), "an older non-matching file must survive"


def test_it_recurses_and_clears_the_directories_it_empties(tmp_path: Path) -> None:
    """The terrain store nests per watershed; thousands of empty ones is its own mess."""
    _write(tmp_path / "hand" / "1204010403" / "a.tif", 2.0, age_s=900)
    _write(tmp_path / "hand" / "0708020901" / "b.tif", 2.0, age_s=800)
    kept = _write(tmp_path / "hand" / "1003010405" / "c.tif", 2.0, age_s=1)
    assert evict_to_budget(tmp_path, budget_mb=2.0, pattern="*.tif") == 2
    assert kept.exists()
    assert not (tmp_path / "hand" / "1204010403").exists()
    assert (tmp_path / "hand" / "1003010405").exists()


def test_a_missing_root_is_not_an_error(tmp_path: Path) -> None:
    """Housekeeping runs after a write; it must never be the thing that fails."""
    assert evict_to_budget(tmp_path / "nothing-here", budget_mb=1.0) == 0
