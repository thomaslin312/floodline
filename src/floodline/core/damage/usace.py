"""USACE depth-damage curves, keyed by HAZUS occupancy code.

The bundled curves in `damage.curves` carry the shape of each published family but
not its digits, which is why they are marked unverified. These are the digits. USACE
publishes its consequences engine at github.com/USACE/go-consequences under MIT, and
`structures/occtypes.json` in that repository is the curve library it runs on: 51
occupancy types, structure and contents curves for each, sourced from the Economic
Guidance Memoranda, HEC-FIA and FEMA's coastal PFRA work.

Three things they give that the bundled set cannot:

* **Occupancy codes, not four classes.** `RES1-1SNB` is a single-family house on a
  slab; `RES1-2SWB` is two storeys with a basement, and floods very differently. The
  same codes key the National Structure Inventory, so inventory and curve join
  natively - which is the whole reason to prefer this pair over any other.
* **Contents as a separate curve.** Contents are worth roughly as much again as the
  structure, so omitting them was not a rounding error, it was leaving out half the
  loss.
* **A standard deviation at every depth.** The Monte Carlo can sample the published
  uncertainty of the curve itself rather than approximating it by switching families.

Two conventions differ from the bundled curves and are converted on load. Depths are
in feet and run from -2, because a curve that starts at zero cannot say what water
lapping the slab does; damage is in percent, not fraction. And note that these curves
are non-zero *at* the floor - RES1-1SNB is already at 13.4% when depth is 0 ft - so a
model that zeroes everything at or below floor level would silently discard it.

Eighteen of the 102 component curves have no depth-indexed function (the generic
`COM`, `IND`, `PUB` and `APT` aggregates, which exist as fallbacks rather than as
modelled types). Those codes resolve to the configured default rather than being
dropped.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from floodline.core.config import Config, CurveFamily, DamageConfig
from floodline.core.damage.curves import CurveSet, DamageCurve

__all__ = [
    "UsaceCurves",
    "load_usace_curves",
]

FEET_TO_M = 0.3048
# Damage is published in percent; every curve in the file tops out at 100.
PERCENT = 100.0
_PROVENANCE = (
    "USACE go-consequences structures/occtypes.json (MIT), from the Economic Guidance "
    "Memoranda, HEC-FIA and FEMA coastal PFRA damage functions"
)


@dataclass(frozen=True, slots=True)
class UsaceCurves:
    """Structure and contents curves for every occupancy code in the file."""

    structure: CurveSet
    contents: CurveSet
    sigma: dict[str, tuple[float, ...]]
    """Per-depth standard deviation of the structure curve, in fraction units, so the
    Monte Carlo can sample the curve's own published spread."""

    codes: tuple[str, ...]

    def for_default(self) -> DamageCurve:
        """Return the curve unclassified structures fall back to."""
        return self.structure.for_class(None)

    def has(self, code: str) -> bool:
        """Report whether `code` has its own curve rather than the default fallback."""
        return code in self.structure.curves


def _mean(distribution: dict[str, Any]) -> float:
    """Return a y-value from either distribution form the file uses."""
    parameters = distribution["parameters"]
    if "mean" in parameters:
        return float(parameters["mean"])
    # DeterministicDistribution carries a bare `value`.
    return float(parameters["value"])


def _sd(distribution: dict[str, Any]) -> float:
    """Return a y-value's standard deviation, zero for a deterministic point."""
    return float(distribution["parameters"].get("standarddeviation", 0.0))


def _depth_function(component: dict[str, Any]) -> dict[str, Any] | None:
    """Return the depth-indexed damage function, or None when the code has none."""
    functions = component.get("damagefunctions", {})
    chosen = functions.get("depth") or functions.get("default")
    return chosen if isinstance(chosen, dict) else None


def _monotone(values: list[float]) -> list[float]:
    """Return `values` made non-decreasing.

    Every curve in the published file is already monotone - this is checked, not
    assumed - so this only guards against a future release where one is not, rather
    than silently repairing data that is currently fine.
    """
    out: list[float] = []
    highest = 0.0
    for value in values:
        highest = max(highest, value)
        out.append(highest)
    return out


def load_usace_curves(
    path: Path,
    *,
    config: Config | DamageConfig | None = None,
) -> UsaceCurves:
    """Parse `occtypes.json` into structure and contents curve sets.

    Depths convert from feet to metres and damage from percent to fraction. Curves are
    marked verified: unlike the bundled constants these are the published values, read
    from the file rather than typed in from a figure.
    """
    damage = config.damage if isinstance(config, Config) else (config or DamageConfig())
    raw = json.loads(path.read_text())
    occupancies: dict[str, Any] = raw["occupancytypes"]

    structure: dict[str, DamageCurve] = {}
    contents: dict[str, DamageCurve] = {}
    sigma: dict[str, tuple[float, ...]] = {}

    for code, entry in occupancies.items():
        components = entry.get("componentdamagefunctions", {})
        for name, target in (("structure", structure), ("contents", contents)):
            component = components.get(name)
            if not component:
                continue
            function = _depth_function(component)
            if function is None:
                continue
            table = function["damagefunction"]
            depths = tuple(float(x) * FEET_TO_M for x in table["xvalues"])
            means = _monotone([_mean(y) / PERCENT for y in table["ydistributions"]])
            target[code] = DamageCurve(
                family=CurveFamily.USACE,
                building_class=code,
                depths_m=depths,
                fractions=tuple(means),
                provenance=f"{_PROVENANCE}; source field {function.get('source', '')!r}",
                verified=True,
            )
            if name == "structure":
                sigma[code] = tuple(_sd(y) / PERCENT for y in table["ydistributions"])

    if not structure:
        raise ValueError(f"{path.name} yielded no depth-indexed structure curves")

    default = damage.usace_default_occupancy
    if default not in structure:
        raise ValueError(
            f"{path.name} has no curve for the default occupancy {default!r}; "
            f"available: {sorted(structure)[:6]}..."
        )
    # Contents curves cover the same codes bar a handful; fall back to the same default.
    contents_default = default if default in contents else next(iter(contents))

    return UsaceCurves(
        structure=CurveSet(family=CurveFamily.USACE, curves=structure, default_class=default),
        contents=CurveSet(
            family=CurveFamily.USACE, curves=contents, default_class=contents_default
        ),
        sigma=sigma,
        codes=tuple(sorted(structure)),
    )
