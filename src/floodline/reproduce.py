"""Regenerate every number this project publishes, and say when one has moved.

The repository's rule is that no result stands in it that was not actually produced.
That rule is only enforceable if the results can be produced again, so each published
figure and table is a named target here with the code that makes it and the value it
last made. Running a target recomputes it from source data and reports the drift.

Targets are expensive on purpose. They fetch real elevation, real gauges and real
claims rather than reading a cached summary, because a reproduction that reads its own
output proves nothing. That means a full run is hours and needs network, which is why
targets are selectable and why each one records how long it took.

Drift is reported, never silently accepted. A number that has moved is either a bug
introduced since it was published or a change in the upstream data, and both are worth
a reader's attention; deciding which is a person's job.
"""

from __future__ import annotations

import json
import math
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

__all__ = ["TARGETS", "Target", "TargetResult", "published_values", "reproduce"]

RESULTS = Path("docs/RESULTS.json")
"""Where the last reproduced values live. Committed, so drift is visible in a diff."""


@dataclass(frozen=True, slots=True)
class Target:
    """One published table or figure, and the function that regenerates it."""

    name: str
    describes: str
    """Where in the documentation this appears, so a reader can find what moved."""

    run: Callable[[], dict[str, Any]]
    tolerance: float = 0.01
    """Relative drift treated as reproduction rather than change. Monte Carlo targets
    need a looser one than deterministic ones; the default suits a deterministic
    target and each looser one says why."""


@dataclass(slots=True)
class TargetResult:
    """What one target produced this run, and how it differs from what is published."""

    name: str
    values: dict[str, Any]
    seconds: float
    drift: dict[str, tuple[Any, Any]] = field(default_factory=dict)
    """Keys whose value moved beyond tolerance, as (published, reproduced).

    Not narrowed to numbers: a changed inventory name or curve family is drift too,
    and those come through as strings."""

    error: str | None = None

    @property
    def ok(self) -> bool:
        """Whether this target ran and reproduced every value it is meant to."""
        return self.error is None and not self.drift


def published_values(path: Path = RESULTS) -> dict[str, dict[str, Any]]:
    """Return the values currently standing in the documentation, keyed by target."""
    if not path.exists():
        return {}
    loaded = json.loads(path.read_text())
    targets = loaded.get("targets", {})
    return {str(k): dict(v) for k, v in targets.items()}


def _drift(
    published: dict[str, Any], produced: dict[str, Any], tolerance: float
) -> dict[str, tuple[Any, Any]]:
    """Numeric keys whose value moved by more than `tolerance`, relatively.

    Non-numeric values are compared exactly; a changed inventory name or a changed
    curve family is drift however small the numbers around it are. Keys absent from
    either side are not drift: a target that gained a field has not moved its
    published numbers, and reporting it as movement would train a reader to ignore
    this output.
    """
    moved: dict[str, tuple[Any, Any]] = {}
    for key, was in published.items():
        if key not in produced:
            continue
        now = produced[key]
        exact = (
            isinstance(was, bool)
            or isinstance(now, bool)
            or not isinstance(was, int | float)
            or not isinstance(now, int | float)
        )
        if exact:
            if was != now:
                moved[key] = (was, now)
            continue
        scale = max(abs(float(was)), 1e-12)
        if abs(float(now) - float(was)) / scale > tolerance:
            moved[key] = (float(was), float(now))
    return moved


def _uncertainty_budget() -> dict[str, Any]:
    """Each Monte Carlo term sampled alone, as a share of the point estimate.

    The table in the README's damage section and in `uncertainty.py`'s docstring.
    """
    from floodline.assess import assess_watershed
    from floodline.compute import watershed_by_huc
    from floodline.config import Config

    cases = {
        "all": {},
        "stage": {"dem_sigma_m": 0.0, "cost_sigma_frac": 1e-9, "sample_across_families": False},
        "dem": {"stage_sigma_m": 0.0, "cost_sigma_frac": 1e-9, "sample_across_families": False},
        "cost": {"stage_sigma_m": 0.0, "dem_sigma_m": 0.0, "sample_across_families": False},
        "curve": {"stage_sigma_m": 0.0, "dem_sigma_m": 0.0, "cost_sigma_frac": 1e-9},
    }
    out: dict[str, Any] = {}
    for label, over in cases.items():
        base = Config()
        base = base.model_copy(update={"monte_carlo": base.monte_carlo.model_copy(update=over)})
        unit, cfg = watershed_by_huc("1204010403", config=base)
        cfg = cfg.model_copy(update={"monte_carlo": cfg.monte_carlo.model_copy(update=over)})
        assessment = assess_watershed(
            unit, config=cfg, resolution_m=30.0, inventory="nsi", samples=400
        )
        interval, damage = assessment.interval, assessment.damage
        if interval is None or damage is None:
            raise RuntimeError(f"no interval for {label}")
        out[f"{label}_width_pct"] = round(
            100.0 * (interval.upper - interval.lower) / damage.total, 1
        )
        if label == "all":
            out["point_usd"] = round(damage.total, 0)
    return out


def _fema_tracts() -> dict[str, Any]:
    """Spearman correlations behind the README's dollars-against-FEMA section."""
    from floodline.validate.fema import compare_to_fema

    return compare_to_fema()


TARGETS: tuple[Target, ...] = (
    Target(
        name="uncertainty-budget",
        describes="README 'What the Monte Carlo covers' table; uncertainty.py docstring",
        run=_uncertainty_budget,
        # Four hundred draws of a stochastic quantity will not land on the same
        # percentile twice. Anything under a tenth is the sampler, not a change.
        tolerance=0.10,
    ),
    Target(
        name="fema-tracts",
        describes="README 'Dollars against FEMA's own record' table and figure",
        run=_fema_tracts,
        # Correlations are reported to three decimals and the model side is a point
        # estimate, so this one should barely move at all.
        tolerance=0.02,
    ),
)


def reproduce(
    names: list[str] | None = None, *, results_path: Path = RESULTS
) -> list[TargetResult]:
    """Run the named targets and report what moved.

    Returns a result per target rather than raising, so one broken upstream does not
    hide the state of everything else. The caller decides what a failure means.
    """
    standing = published_values(results_path)
    chosen = [t for t in TARGETS if names is None or t.name in names]
    results: list[TargetResult] = []
    for target in chosen:
        started = time.perf_counter()
        try:
            values = target.run()
        # Deliberately broad: one broken upstream must not hide the state of the rest.
        except Exception as exc:
            results.append(
                TargetResult(
                    name=target.name,
                    values={},
                    seconds=round(time.perf_counter() - started, 1),
                    error=f"{type(exc).__name__}: {exc}",
                )
            )
            continue
        results.append(
            TargetResult(
                name=target.name,
                values=values,
                seconds=round(time.perf_counter() - started, 1),
                drift=_drift(standing.get(target.name, {}), values, target.tolerance),
            )
        )
    return results


def write_results(results: list[TargetResult], *, results_path: Path = RESULTS) -> None:
    """Record what the targets produced, merged over what was already standing.

    Merged rather than replaced: running one target must not delete the record of the
    others, or a selective run would quietly erase the provenance of everything it did
    not touch.
    """
    standing = published_values(results_path)
    for result in results:
        if result.error is None:
            standing[result.name] = result.values
    results_path.parent.mkdir(parents=True, exist_ok=True)
    results_path.write_text(
        json.dumps(
            {
                "generated": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "targets": standing,
            },
            indent=1,
            sort_keys=True,
        )
        + "\n"
    )


def format_report(results: list[TargetResult]) -> str:
    """Render a short account of what ran, what it cost, and what moved."""
    lines = []
    for result in results:
        if result.error:
            lines.append(f"  {result.name:<20} FAILED  {result.error[:90]}")
            continue
        state = "reproduced" if not result.drift else f"{len(result.drift)} value(s) moved"
        lines.append(f"  {result.name:<20} {state:<22} {result.seconds:>7.1f}s")
        for key, (was, now) in sorted(result.drift.items()):
            if isinstance(was, int | float) and isinstance(now, int | float):
                scale = max(abs(float(was)), 1e-12)
                shift = 100.0 * (float(now) - float(was)) / scale
                lines.append(f"      {key:<28} {was:>16,.4g} -> {now:>16,.4g}  ({shift:+.1f}%)")
            else:
                lines.append(f"      {key:<28} {was!r} -> {now!r}")
    return "\n".join(lines)


def spearman(xs: list[float], ys: list[float]) -> float:
    """Rank correlation, tie-corrected.

    Written out rather than imported: scipy is not a dependency of this project and
    adding one for a dozen lines of ranking would be the largest thing in the tree
    that only one function needs.
    """
    if len(xs) != len(ys) or len(xs) < 3:
        return float("nan")

    def rank(values: list[float]) -> list[float]:
        order = sorted(range(len(values)), key=lambda i: values[i])
        out = [0.0] * len(values)
        index = 0
        while index < len(order):
            stop = index
            while stop + 1 < len(order) and values[order[stop + 1]] == values[order[index]]:
                stop += 1
            shared = (index + stop) / 2.0 + 1.0
            for position in range(index, stop + 1):
                out[order[position]] = shared
            index = stop + 1
        return out

    rx, ry = rank(xs), rank(ys)
    n = len(rx)
    mx, my = sum(rx) / n, sum(ry) / n
    numerator = sum((a - mx) * (b - my) for a, b in zip(rx, ry, strict=True))
    denominator = math.sqrt(sum((a - mx) ** 2 for a in rx) * sum((b - my) ** 2 for b in ry))
    return numerator / denominator if denominator else float("nan")
