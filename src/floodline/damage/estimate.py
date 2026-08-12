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
from floodline.damage.curves import CurveSet, bundled_curves

__all__ = ["DamageEstimate", "estimate_damage"]


@dataclass(frozen=True, slots=True)
class DamageEstimate:
    """Direct damage over a set of buildings."""

    total: float
    """Summed damage, in the currency of `replacement_cost_per_m2`."""

    by_class: dict[str, float]
    per_building: npt.NDArray[np.float64]
    exposed_value_total: float
    """Replacement value of every building considered, damaged or not."""

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
    config: Config | DamageConfig | None = None,
    curves: CurveSet | None = None,
    family: CurveFamily | None = None,
    cost_scale: float = 1.0,
    cap_storeys: bool = True,
) -> DamageEstimate:
    """Estimate direct damage for a set of buildings.

    Parameters
    ----------
    floor_depth_m
        Water depth above finished floor level, from `exposure.building_depths`.
        Depths at or below zero produce zero damage.
    floor_area_m2
        Footprint area times storeys.
    building_class
        Class per building, used to pick both the curve and the cost rate.
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

    Returns
    -------
    DamageEstimate
    """
    damage_config = _resolve(config)
    chosen = family if family is not None else damage_config.curve_family
    curve_set = curves if curves is not None else bundled_curves(chosen, config=damage_config)

    depths = np.asarray(floor_depth_m, dtype=np.float64)
    areas = np.asarray(floor_area_m2, dtype=np.float64)
    classes = np.asarray(building_class, dtype=object)
    if not (depths.shape == areas.shape == classes.shape):
        raise ValueError(
            f"depth {depths.shape}, area {areas.shape} and class {classes.shape} must match"
        )

    fraction = curve_set.damage_fraction(depths, classes, config=damage_config)
    value = exposed_value(areas, classes, config=damage_config, cost_scale=cost_scale)

    if cap_storeys:
        if storeys is None:
            raise ValueError("cap_storeys is on but no storey count was given")
        fraction = fraction * storey_exposure(depths, storeys)

    per_building = fraction * value

    by_class: dict[str, float] = {}
    for name in {str(c) for c in classes.ravel()}:
        by_class[name] = float(per_building[classes == name].sum())

    notes: list[str] = []
    if not curve_set.verified:
        notes.append(
            "Curve constants are unverified; ratios and counts stand, currency totals do not."
        )

    return DamageEstimate(
        total=float(per_building.sum()),
        by_class=dict(sorted(by_class.items())),
        per_building=per_building,
        exposed_value_total=float(value.sum()),
        n_buildings=int(depths.size),
        n_damaged=int((per_building > 0).sum()),
        family=curve_set.family,
        curves_verified=curve_set.verified,
        notes=tuple(notes),
    )
