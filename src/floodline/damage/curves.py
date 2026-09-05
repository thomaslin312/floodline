"""Depth-damage curves: the function from water depth to fraction of value lost.

A curve is a small table of (depth, damage fraction) points, linearly interpolated,
held at its terminal value beyond `max_curve_depth_m`, and optionally clamped into
[0, 1]. Three families are bundled, and they disagree with each other by more than
most of the rest of the model's error budget - which is the point of sampling across
them in the Monte Carlo rather than picking one and reporting a single number.

**The bundled constants are not transcribed from the source tables.** They carry the
shape of each published family - HAZUS residential saturating near two-thirds, the
JRC continental curves rising faster and reaching unity - but the digits have not
been checked against the source documents, and every bundled curve is therefore
marked `verified=False`. Anything derived from them travels with that flag: a
`DamageEstimate` built on unverified curves says so, and the CLI prints it. Fractions
of buildings inundated are unaffected; absolute currency figures must not be quoted
until real tables are loaded with `load_curves`.

Sources to transcribe from, when that happens:

* HAZUS - FEMA, *Hazus Flood Model Technical Manual*, depth-damage functions by
  occupancy class (USACE/FIA generic curves).
* JRC - Huizinga, de Moel & Szewczyk (2017), *Global flood depth-damage functions:
  methodology and the database with guidelines*, EUR 28552 EN, JRC105688.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
import numpy.typing as npt

from floodline.config import Config, CurveFamily, DamageConfig

__all__ = [
    "BUNDLED_FAMILIES",
    "CurveLookup",
    "CurveSet",
    "DamageCurve",
    "bundled_curves",
    "load_curves",
]

# The lookup grid is the union of every curve's own breakpoints, not a uniform ladder.
# A uniform grid cuts the corner at any kink that does not land on it: the bundled
# curves break at 0.5 m and land on a 5 mm grid exactly, but the USACE curves break at
# whole feet and did not, which made the "fast" path disagree with the direct one by
# 6.5e-8 of the total. Interpolating between the breakpoints themselves is exact for
# every piecewise-linear curve, and the grid is ~25 points rather than ~1,700.

# Depth in metres above finished floor level. The 0/0.5/1/1.5/2/3/4/5/6 ladder is the
# one the JRC database publishes on, so a transcribed table drops straight in.
_DEPTHS: tuple[float, ...] = (0.0, 0.5, 1.0, 1.5, 2.0, 3.0, 4.0, 5.0, 6.0)

_UNVERIFIED = (
    "Shape of the published family; digits not transcribed from the source table. "
    "Do not quote absolute damages from this curve."
)

# family -> building class -> damage fraction at each depth in _DEPTHS
_BUNDLED: dict[CurveFamily, dict[str, tuple[float, ...]]] = {
    CurveFamily.HAZUS: {
        "residential": (0.00, 0.21, 0.30, 0.37, 0.43, 0.53, 0.60, 0.65, 0.68),
        "commercial": (0.00, 0.15, 0.24, 0.31, 0.38, 0.49, 0.57, 0.62, 0.66),
        "industrial": (0.00, 0.12, 0.20, 0.27, 0.33, 0.44, 0.52, 0.58, 0.62),
        "other": (0.00, 0.16, 0.25, 0.32, 0.38, 0.49, 0.57, 0.62, 0.66),
    },
    CurveFamily.JRC_OCEANIA: {
        "residential": (0.00, 0.48, 0.68, 0.82, 0.90, 0.98, 1.00, 1.00, 1.00),
        "commercial": (0.00, 0.38, 0.57, 0.71, 0.81, 0.94, 0.99, 1.00, 1.00),
        "industrial": (0.00, 0.31, 0.49, 0.63, 0.74, 0.90, 0.97, 1.00, 1.00),
        "other": (0.00, 0.39, 0.58, 0.72, 0.82, 0.94, 0.99, 1.00, 1.00),
    },
    CurveFamily.JRC_GLOBAL: {
        "residential": (0.00, 0.25, 0.40, 0.50, 0.60, 0.75, 0.85, 0.95, 1.00),
        "commercial": (0.00, 0.22, 0.35, 0.45, 0.55, 0.70, 0.82, 0.91, 1.00),
        "industrial": (0.00, 0.15, 0.27, 0.40, 0.52, 0.70, 0.84, 0.93, 1.00),
        "other": (0.00, 0.21, 0.34, 0.45, 0.55, 0.71, 0.84, 0.93, 1.00),
    },
}


@dataclass(frozen=True, slots=True)
class DamageCurve:
    """One family's curve for one building class."""

    family: CurveFamily
    building_class: str
    depths_m: tuple[float, ...]
    """Depths above finished floor level, strictly ascending."""

    fractions: tuple[float, ...]
    """Fraction of replacement value lost at each depth, non-decreasing."""

    provenance: str
    verified: bool
    """False when the constants have not been checked against the source table."""

    def __post_init__(self) -> None:
        """Refuse a curve that cannot be interpolated meaningfully."""
        if len(self.depths_m) != len(self.fractions):
            raise ValueError(
                f"{self.family}/{self.building_class}: {len(self.depths_m)} depths "
                f"but {len(self.fractions)} fractions"
            )
        if len(self.depths_m) < 2:
            raise ValueError(f"{self.family}/{self.building_class}: need at least two points")
        depths = np.asarray(self.depths_m, dtype=np.float64)
        fractions = np.asarray(self.fractions, dtype=np.float64)
        if np.any(np.diff(depths) <= 0):
            raise ValueError(f"{self.family}/{self.building_class}: depths must ascend strictly")
        # A curve that falls with depth would mean deeper water doing less harm.
        if np.any(np.diff(fractions) < 0):
            raise ValueError(f"{self.family}/{self.building_class}: fractions must not decrease")
        if np.any(fractions < 0) or np.any(fractions > 1):
            raise ValueError(f"{self.family}/{self.building_class}: fractions must lie in [0, 1]")

    def damage_fraction(
        self,
        depth_m: npt.ArrayLike,
        *,
        config: Config | DamageConfig | None = None,
    ) -> npt.NDArray[np.float64]:
        """Return the fraction of value lost at each depth above finished floor.

        Depths at or below the curve's first point give its first fraction, which is
        zero for every bundled curve. Depths beyond `max_curve_depth_m` are held at
        the curve's value there rather than extrapolated: the published tables stop,
        and a linear continuation past the last point would invent damage above 100%.
        """
        damage = _resolve(config)
        depths = np.asarray(depth_m, dtype=np.float64)
        capped = np.minimum(depths, damage.max_curve_depth_m)
        out = np.interp(
            capped,
            np.asarray(self.depths_m, dtype=np.float64),
            np.asarray(self.fractions, dtype=np.float64),
        )
        # No clamp at zero. np.interp already holds the curve's first value below its
        # first point, which is what the curve itself says should happen there. The
        # bundled curves start at (0 m, 0), so nothing changes for them; the USACE
        # curves start at -0.61 m and are already at 13% when water reaches the floor,
        # and a blanket "zero at or below zero" would throw that away.
        if damage.clamp_damage_fraction:
            out = np.clip(out, 0.0, 1.0)
        return np.asarray(out, dtype=np.float64)


@dataclass(frozen=True, slots=True)
class CurveLookup:
    """Every curve in a set resampled onto one depth grid, for repeated evaluation.

    `CurveSet.damage_fraction` interpolates once per distinct class present, so its
    cost grows with the number of classes: over 258,527 Houston structures it took
    67 ms with 4 occupancy codes and 285 ms with 42, which at 500 Monte Carlo draws is
    the difference between 34 s and 143 s. The classes do not change between draws, so
    the work can be done once. Evaluation then becomes one integer index per building,
    independent of how many classes are in play.
    """

    depths_m: npt.NDArray[np.float64]
    """The common grid: every breakpoint of every curve in the set, ascending."""

    table: npt.NDArray[np.float64]
    """Damage fraction, one row per curve, one column per grid depth."""

    index_of: dict[str, int]
    default_index: int

    sigma: npt.NDArray[np.float64] | None = None
    """Standard deviation of the curve at each grid depth, same shape as `table`.
    Present only where the source publishes it, which currently means USACE."""

    def indices_for(self, building_class: npt.ArrayLike) -> npt.NDArray[np.int64]:
        """Map class names to rows once, so draws can reuse the result."""
        classes = np.asarray(building_class, dtype=object)
        out = np.full(classes.shape, self.default_index, dtype=np.int64)
        for name, row in self.index_of.items():
            out[classes == name] = row
        return out

    def fraction(
        self,
        depth_m: npt.ArrayLike,
        class_index: npt.NDArray[np.int64],
        *,
        sigma_z: float = 0.0,
    ) -> npt.NDArray[np.float64]:
        """Return the damage fraction for each (depth, curve-row) pair.

        `sigma_z` shifts every curve by that many standard deviations, for sampling
        the published uncertainty of the curves themselves. One value for the whole
        call, not one per building: the spread is uncertainty about where the curve
        sits, and drawing it independently per building would average to nothing
        across a quarter of a million of them, which would understate the term rather
        than model it.
        """
        depths = np.asarray(depth_m, dtype=np.float64)
        first = self.depths_m[0]
        last = self.depths_m[-1]
        # Below the first grid point the curve holds its first value, above the last it
        # holds its last -- the same rule np.interp applies, and the same rule the
        # max_curve_depth_m cap already imposed at the top end.
        clipped = np.clip(depths, first, last)
        # The grid is not evenly spaced, so find the bracketing pair by search rather
        # than by arithmetic. Every curve is piecewise linear with breakpoints in this
        # grid, so interpolating between them reproduces the direct path exactly.
        left = np.searchsorted(self.depths_m, clipped, side="right") - 1
        np.clip(left, 0, self.table.shape[1] - 2, out=left)
        span = self.depths_m[left + 1] - self.depths_m[left]
        weight = np.where(span > 0, (clipped - self.depths_m[left]) / span, 0.0)
        low = self.table[class_index, left]
        high = self.table[class_index, left + 1]
        out = low + (high - low) * weight
        if sigma_z and self.sigma is not None:
            spread_low = self.sigma[class_index, left]
            spread_high = self.sigma[class_index, left + 1]
            out = out + sigma_z * (spread_low + (spread_high - spread_low) * weight)
            np.clip(out, 0.0, 1.0, out=out)
        return np.asarray(out, dtype=np.float64)


@dataclass(frozen=True, slots=True)
class CurveSet:
    """Every building class's curve for one family."""

    family: CurveFamily
    curves: dict[str, DamageCurve]
    default_class: str

    @property
    def verified(self) -> bool:
        """True only when every curve in the set has been checked against its source."""
        return all(curve.verified for curve in self.curves.values())

    def for_class(self, building_class: str | None) -> DamageCurve:
        """Return the curve for `building_class`, falling back to the default class."""
        if building_class is not None and building_class in self.curves:
            return self.curves[building_class]
        return self.curves[self.default_class]

    def lookup(
        self,
        *,
        config: Config | DamageConfig | None = None,
        sigma_by_class: dict[str, tuple[float, ...]] | None = None,
    ) -> CurveLookup:
        """Resample every curve onto one grid, for repeated evaluation.

        `sigma_by_class` carries the published standard deviation at each of the
        curve's own points; it is resampled onto the same grid so a draw can shift
        the curve without rebuilding the table.
        """
        damage = _resolve(config)
        lowest = min(curve.depths_m[0] for curve in self.curves.values())
        highest = damage.max_curve_depth_m
        if highest <= lowest:
            raise ValueError(
                f"max_curve_depth_m {highest:g} is at or below the lowest curve point "
                f"{lowest:g}, so the lookup grid would be empty"
            )
        breakpoints = {float(highest)}
        for curve in self.curves.values():
            breakpoints.update(float(d) for d in curve.depths_m if lowest <= d <= highest)
        grid = np.array(sorted(breakpoints), dtype=np.float64)
        n = grid.size
        if n < 2:
            raise ValueError(
                f"the curves share only {n} breakpoint(s) at or below max_curve_depth_m "
                f"{highest:g}, which is not enough to interpolate between"
            )
        names = sorted(self.curves)
        table = np.empty((len(names), n), dtype=np.float64)
        for row, name in enumerate(names):
            table[row] = self.curves[name].damage_fraction(grid, config=damage)
        index_of = {name: row for row, name in enumerate(names)}

        spread: npt.NDArray[np.float64] | None = None
        if sigma_by_class:
            spread = np.zeros_like(table)
            for row, name in enumerate(names):
                values = sigma_by_class.get(name)
                curve = self.curves[name]
                if values and len(values) == len(curve.depths_m):
                    spread[row] = np.interp(
                        grid,
                        np.asarray(curve.depths_m, dtype=np.float64),
                        np.asarray(values, dtype=np.float64),
                    )

        return CurveLookup(
            depths_m=grid,
            table=table,
            index_of=index_of,
            default_index=index_of[self.default_class],
            sigma=spread,
        )

    def damage_fraction(
        self,
        depth_m: npt.ArrayLike,
        building_class: npt.ArrayLike,
        *,
        config: Config | DamageConfig | None = None,
    ) -> npt.NDArray[np.float64]:
        """Apply the per-class curves to parallel arrays of depth and class."""
        depths = np.asarray(depth_m, dtype=np.float64)
        classes = np.asarray(building_class, dtype=object)
        if classes.shape != depths.shape:
            raise ValueError(f"class shape {classes.shape} does not match depth {depths.shape}")
        out = np.zeros(depths.shape, dtype=np.float64)
        # One interpolation per class present, rather than one per building.
        for name in {str(c) for c in classes.ravel()}:
            mask = classes == name
            out[mask] = self.for_class(name).damage_fraction(depths[mask], config=config)
        return out


def _resolve(config: Config | DamageConfig | None) -> DamageConfig:
    """Return the `DamageConfig` to use, defaulting when nothing is supplied."""
    if isinstance(config, Config):
        return config.damage
    return config if config is not None else DamageConfig()


# Families with constants in this module. CurveFamily.USACE is deliberately absent:
# it is the published library, loaded from file by `damage.usace`, not approximated
# here. Iterating CurveFamily and calling bundled_curves on each is therefore wrong,
# and iterating this instead is right.
BUNDLED_FAMILIES: tuple[CurveFamily, ...] = (
    CurveFamily.HAZUS,
    CurveFamily.JRC_OCEANIA,
    CurveFamily.JRC_GLOBAL,
)


def bundled_curves(
    family: CurveFamily,
    *,
    config: Config | DamageConfig | None = None,
) -> CurveSet:
    """Return the bundled curve set for `family`, every curve marked unverified."""
    damage = _resolve(config)
    if family not in _BUNDLED:
        raise ValueError(
            f"{family.value} has no bundled approximation - it is the published library. "
            "Load it with damage.usace.load_usace_curves instead."
        )
    table = _BUNDLED[family]
    return CurveSet(
        family=family,
        default_class=damage.default_class,
        curves={
            name: DamageCurve(
                family=family,
                building_class=name,
                depths_m=_DEPTHS,
                fractions=fractions,
                provenance=_UNVERIFIED,
                verified=False,
            )
            for name, fractions in table.items()
        },
    )


def load_curves(
    path: Path,
    *,
    config: Config | DamageConfig | None = None,
) -> dict[CurveFamily, CurveSet]:
    """Load curve sets from a JSON file, replacing the bundled constants.

    The file is a mapping of family name to `{"verified": bool, "provenance": str,
    "classes": {class: {"depths_m": [...], "fractions": [...]}}}`. This is the path
    for transcribed tables: set `verified` true only when the digits came from the
    source document, because that flag is what unlocks quoting absolute damages.
    """
    damage = _resolve(config)
    raw: dict[str, Any] = json.loads(path.read_text())
    out: dict[CurveFamily, CurveSet] = {}
    for family_name, spec in raw.items():
        family = CurveFamily(family_name)
        verified = bool(spec.get("verified", False))
        provenance = str(spec.get("provenance", f"loaded from {path.name}"))
        curves = {
            name: DamageCurve(
                family=family,
                building_class=name,
                depths_m=tuple(float(d) for d in points["depths_m"]),
                fractions=tuple(float(f) for f in points["fractions"]),
                provenance=provenance,
                verified=verified,
            )
            for name, points in spec["classes"].items()
        }
        if damage.default_class not in curves:
            raise ValueError(
                f"{path.name}: family {family_name!r} has no curve for the default class "
                f"{damage.default_class!r}, so unclassified buildings could not be priced"
            )
        out[family] = CurveSet(family=family, curves=curves, default_class=damage.default_class)
    return out
