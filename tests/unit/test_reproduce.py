"""Drift detection for published results."""

from __future__ import annotations

import json
import math
from pathlib import Path

from floodline.reproduce import (
    TARGETS,
    Target,
    published_values,
    reproduce,
    spearman,
    write_results,
)


def test_every_target_says_where_it_appears() -> None:
    """A moved number is only actionable if a reader can find what it moved in."""
    assert TARGETS
    for target in TARGETS:
        assert target.describes.strip(), target.name
        assert 0.0 < target.tolerance < 1.0, target.name
    assert len({t.name for t in TARGETS}) == len(TARGETS), "target names must be unique"


def test_a_value_that_moved_is_reported(tmp_path: Path) -> None:
    path = tmp_path / "RESULTS.json"
    path.write_text(json.dumps({"targets": {"t": {"a": 100.0, "b": 5.0}}}))
    moved = Target("t", "somewhere", lambda: {"a": 100.5, "b": 9.0}, tolerance=0.01)
    original = TARGETS
    try:
        import floodline.reproduce as module

        module.TARGETS = (moved,)  # type: ignore[assignment]
        result = reproduce(["t"], results_path=path)[0]
    finally:
        module.TARGETS = original  # type: ignore[assignment]

    assert set(result.drift) == {"b"}, "a is within tolerance, b is not"
    assert result.drift["b"] == (5.0, 9.0)
    assert not result.ok


def test_a_broken_target_does_not_hide_the_others(tmp_path: Path) -> None:
    path = tmp_path / "RESULTS.json"

    def boom() -> dict[str, float]:
        raise RuntimeError("upstream is down")

    import floodline.reproduce as module

    original = module.TARGETS
    try:
        module.TARGETS = (  # type: ignore[assignment]
            Target("broken", "x", boom),
            Target("fine", "y", lambda: {"v": 1.0}),
        )
        results = reproduce(results_path=path)
    finally:
        module.TARGETS = original  # type: ignore[assignment]

    assert results[0].error is not None and "upstream is down" in results[0].error
    assert results[1].ok, "the second target still ran"


def test_writing_one_target_does_not_erase_the_rest(tmp_path: Path) -> None:
    """A selective run must not delete the provenance of what it did not touch."""
    path = tmp_path / "RESULTS.json"
    path.write_text(json.dumps({"targets": {"kept": {"v": 1.0}, "replaced": {"v": 2.0}}}))

    import floodline.reproduce as module

    original = module.TARGETS
    try:
        module.TARGETS = (Target("replaced", "x", lambda: {"v": 3.0}),)  # type: ignore[assignment]
        write_results(reproduce(["replaced"], results_path=path), results_path=path)
    finally:
        module.TARGETS = original  # type: ignore[assignment]

    standing = published_values(path)
    assert standing["kept"] == {"v": 1.0}
    assert standing["replaced"] == {"v": 3.0}


def test_a_failed_target_does_not_overwrite_its_published_value(tmp_path: Path) -> None:
    """Recording an empty result would erase the number the failure could not check."""
    path = tmp_path / "RESULTS.json"
    path.write_text(json.dumps({"targets": {"t": {"v": 7.0}}}))

    import floodline.reproduce as module

    original = module.TARGETS

    def boom() -> dict[str, float]:
        raise RuntimeError("no network")

    try:
        module.TARGETS = (Target("t", "x", boom),)  # type: ignore[assignment]
        write_results(reproduce(["t"], results_path=path), results_path=path)
    finally:
        module.TARGETS = original  # type: ignore[assignment]

    assert published_values(path)["t"] == {"v": 7.0}


def test_spearman_is_tie_corrected() -> None:
    assert spearman([1.0, 2.0, 3.0, 4.0], [1.0, 2.0, 3.0, 4.0]) == 1.0
    assert spearman([1.0, 2.0, 3.0, 4.0], [4.0, 3.0, 2.0, 1.0]) == -1.0
    # Monotone but not linear: rank correlation is 1 where Pearson would not be.
    assert spearman([1.0, 2.0, 3.0, 4.0], [1.0, 4.0, 9.0, 16.0]) == 1.0
    # Degenerate inputs return NaN rather than raising: a target with one tract, or a
    # constant column, must not take down the whole reproduction run.
    assert math.isnan(spearman([1.0, 1.0, 1.0], [1.0, 2.0, 3.0]))
    assert math.isnan(spearman([1.0], [1.0]))
    assert math.isnan(spearman([1.0, 2.0], [1.0, 2.0, 3.0]))
