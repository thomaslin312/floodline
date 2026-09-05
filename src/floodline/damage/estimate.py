"""Per-building damage: depth to fraction to currency, then aggregated.

    damage = curve(depth above floor) x replacement value x storey exposure

Everything upstream of this module produces the depth; everything downstream turns
one estimate into an interval. This is the single deterministic pass that the Monte
Carlo runs a thousand times.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import numpy.typing as npt

from floodline.config import Config, CurveFamily, DamageConfig
from floodline.damage.costs import exposed_value, storey_exposure
from floodline.damage.curves import CurveLookup, CurveSet, bundled_curves

__all__ = ["NO_WATER", "DamageEstimate", "estimate_damage"]

NO_WATER = -1_000_000.0
"""Depth standing for "no water reached this building at all".

Distinct from a depth merely below a curve's first point, which is a real state a real
curve answers for: four USACE with-basement types start at 1.7% at -2.44 m, because a
basement eight feet down does take water. `np.interp` holds a curve's first value below
its first point, so without this distinction every basement in the watershed was
charged 1.7% at zero discharge. Anything below `NO_WATER_BELOW` is dry, full stop."""

NO_WATER_BELOW = -1_000.0
"""Depths under this are the sentinel, not a measurement. A thousand metres below a
building's floor is not a flood state any curve has an opinion about, and it leaves
room for the Monte Carlo to add noise to the sentinel without lifting it."""


@dataclass(frozen=True, slots=True)
class DamageEstimate:
    """Direct damage over a set of buildings."""

    total: float
    """Summed damage, in the currency of `replacement_cost_per_m2`."""

    by_class: dict[str, float]
    per_building: npt.NDArray[np.float64]
    exposed_value_total: float
    """Replacement value of every building considered, damaged or not."""

    contents_total: float
    """Damage to contents, when a contents curve and a contents value were supplied.
    Zero otherwise, which is an omission rather than a finding: contents are worth
    roughly as much again as the structure."""

    n_buildings: int
    n_damaged: int
    family: CurveFamily
    curves_verified: bool
    """False when any curve used carries unverified constants. When this is false the
    ratios and counts stand but the currency totals must not be quoted."""

    notes: tuple[str, ...] = field(default=())

    @property
    def loss_ratio(self) -> float:
        """Damage as a fraction of the exposed replacement value."""
        return self.total / self.exposed_value_total if self.exposed_value_total else 0.0


def _resolve(config: Config | DamageConfig | None) -> DamageConfig:
    """Return the `DamageConfig` to use, defaulting when nothing is supplied."""
    if isinstance(config, Config):
        return config.damage
    return config if config is not None else DamageConfig()


def estimate_damage(
    floor_depth_m: npt.ArrayLike,
    floor_area_m2: npt.ArrayLike,
    building_class: npt.ArrayLike,
    *,
    storeys: npt.ArrayLike | None = None,
    structure_value: npt.ArrayLike | None = None,
    contents_value: npt.ArrayLike | None = None,
    contents_curves: CurveSet | None = None,
    config: Config | DamageConfig | None = None,
    curves: CurveSet | None = None,
    family: CurveFamily | None = None,
    cost_scale: float = 1.0,
    cap_storeys: bool = True,
    damage_below_floor: bool | None = None,
    lookup: CurveLookup | None = None,
    contents_lookup: CurveLookup | None = None,
    class_index: npt.NDArray[np.int64] | None = None,
    contents_index: npt.NDArray[np.int64] | None = None,
    curve_sigma_z: float = 0.0,
) -> DamageEstimate:
    """Estimate direct damage for a set of buildings.

    Parameters
    ----------
    floor_depth_m
        Distance from the water surface to the finished floor, **signed**: negative
        where the water stopped below it. Pass `floor_margin_m`, not `floor_depth_m`.

        This is not a preference. A clamped depth cannot tell a building the water
        missed by five metres from one it reached exactly, and the USACE curves are
        non-zero at zero - RES1-1SNB is already at 13.4% when water touches the slab.
        Feeding them a clamped depth charged 205,754 dry Houston buildings 13.4% of
        their value each. The curves are indexed from -0.61 m precisely so the
        below-floor part is theirs to answer, and `damage_below_floor` refuses the
        combination that caused it.
    floor_area_m2
        Footprint area times storeys.
    building_class
        Class per building, used to pick both the curve and the cost rate. With the
        USACE library this holds HAZUS occupancy codes rather than the four generic
        classes, and the same array keys both curve sets.
    structure_value
        Replacement value per building, overriding `floor_area x rate`. Supply it
        whenever a real inventory is available: area times a flat rate per class
        overstated a Houston sample by 1.4x and was within 30% for only 64% of
        buildings.
    contents_value, contents_curves
        Contents priced on their own curve. Both or neither; contents damage is
        reported separately and included in `total`.
    storeys
        Storey count per building. Required when `cap_storeys` is on, which is what
        stops a metre of water being priced against every floor of a tower block.
    curves, family
        The curve set to use, or the family to take bundled curves from. Defaults to
        `config.curve_family`.
    cost_scale
        Multiplier on replacement cost; the Monte Carlo samples it.
    cap_storeys
        Apply `costs.storey_exposure` so damage reaches only the storeys the water
        can get to.
    lookup, contents_lookup, class_index, contents_index
        Precomputed curve tables and per-building rows, from `CurveSet.lookup` and
        `CurveLookup.indices_for`. Structure and contents need their own index arrays:
        a `CurveLookup` numbers its rows by sorted class name, and the two sets do not
        always hold the same classes, so one index array is not valid for both.

        Purely an optimisation: the Monte Carlo builds these once rather than
        re-grouping by class on every draw, which took draw cost over a quarter of a
        million buildings from 285 ms to about 20 ms. Its grid is the union of the
        curves' own breakpoints, so interpolating between columns reproduces the
        direct path exactly rather than approximately.

    Returns
    -------
    DamageEstimate
    """
    damage_config = _resolve(config)
    exposure_config = config.exposure if isinstance(config, Config) else None
    chosen = family if family is not None else damage_config.curve_family
    curve_set = curves if curves is not None else bundled_curves(chosen, config=damage_config)

    depths = np.asarray(floor_depth_m, dtype=np.float64)
    areas = np.asarray(floor_area_m2, dtype=np.float64)
    classes = np.asarray(building_class, dtype=object)
    if not (depths.shape == areas.shape == classes.shape):
        raise ValueError(
            f"depth {depths.shape}, area {areas.shape} and class {classes.shape} must match"
        )

    # Does this curve set charge a building the water never reached? Only a curve
    # defined below zero can answer that, and only a signed input can ask it.
    below_floor = (
        damage_below_floor
        if damage_below_floor is not None
        else min(curve.depths_m[0] for curve in curve_set.curves.values()) < 0.0
    )
    # The signature of a clamped array is a pile of values at exactly 0.0 and nothing
    # below it. A genuine signed margin is continuous, so exact zeros are vanishingly
    # rare; a clamped one had 213,880 of them on Whiteoak Bayou. Pass
    # damage_below_floor=False to override, for curves that really do start at zero.
    if below_floor and np.any(depths == 0.0) and not np.any(depths < 0.0):
        raise ValueError(
            f"these curves are defined below floor level, but {int(np.sum(depths == 0.0)):,} "
            "of the depths given are exactly 0.0 and none are negative, which is what a "
            "clamped depth looks like. Pass the signed floor_margin_m instead: with a "
            "clamped depth every dry building is charged the curve's value at zero, "
            "which for USACE residential is 13.4% of the structure."
        )

    if lookup is not None and class_index is not None:
        fraction = lookup.fraction(depths, class_index, sigma_z=curve_sigma_z)
    else:
        fraction = curve_set.damage_fraction(depths, classes, config=damage_config)
    # No water is not the same as water below the curve's range.
    dry = depths <= NO_WATER_BELOW
    fraction = np.where(dry, 0.0, fraction)
    if structure_value is None:
        value = exposed_value(areas, classes, config=damage_config, cost_scale=cost_scale)
    else:
        value = np.asarray(structure_value, dtype=np.float64) * cost_scale
        if value.shape != depths.shape:
            raise ValueError(
                f"structure_value shape {value.shape} does not match depth {depths.shape}"
            )

    # Computed once and reused for contents, so the two components cannot disagree
    # about how many storeys the water reached.
    reach = np.ones_like(depths)
    if cap_storeys:
        if storeys is None:
            raise ValueError("cap_storeys is on but no storey count was given")
        reach = storey_exposure(depths, storeys, config=exposure_config)
    fraction = fraction * reach

    per_building = fraction * value

    contents_damage = np.zeros_like(per_building)
    if contents_value is not None and contents_curves is not None:
        contents = np.asarray(contents_value, dtype=np.float64) * cost_scale
        if contents.shape != depths.shape:
            raise ValueError(
                f"contents_value shape {contents.shape} does not match depth {depths.shape}"
            )
        if contents_lookup is not None and contents_index is not None:
            contents_fraction = (
                contents_lookup.fraction(depths, contents_index, sigma_z=curve_sigma_z) * reach
            )
        else:
            contents_fraction = (
                contents_curves.damage_fraction(depths, classes, config=damage_config) * reach
            )
        contents_damage = np.where(dry, 0.0, contents_fraction) * contents
        per_building = per_building + contents_damage
    elif (contents_value is None) != (contents_curves is None):
        raise ValueError("contents_value and contents_curves must be given together")

    by_class: dict[str, float] = {}
    for name in {str(c) for c in classes.ravel()}:
        by_class[name] = float(per_building[classes == name].sum())

    notes: list[str] = []
    if contents_value is None:
        notes.append(
            "Contents damage is not included; contents are typically worth about as "
            "much again as the structure, so this total is low by roughly that much."
        )
    if not curve_set.verified:
        notes.append(
            "Curve constants are unverified; ratios and counts stand, currency totals do not."
        )

    return DamageEstimate(
        total=float(per_building.sum()),
        by_class=dict(sorted(by_class.items())),
        per_building=per_building,
        contents_total=float(contents_damage.sum()),
        exposed_value_total=float(value.sum()),
        n_buildings=int(depths.size),
        n_damaged=int((per_building > 0).sum()),
        family=curve_set.family,
        curves_verified=curve_set.verified,
        notes=tuple(notes),
    )
