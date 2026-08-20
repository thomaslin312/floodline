"""Monte Carlo over the four errors that actually move the damage total.

Each draw perturbs the inputs and re-runs the deterministic estimate:

* **Stage** (`stage_sigma_m`) - the gauge reading, and everything the rating curve
  did with it. Shifts every building's depth together, so it moves the total far
  more than its size suggests: it is a systematic error, not a per-building one.
* **DEM** (`dem_sigma_m`) - vertical error in the terrain, and so in HAND. Applied
  per building and independently, because lidar error decorrelates over tens of
  metres, so it largely averages out across a basin and mostly widens the tails.
* **Curve family** (`curve_family_weights`) - which published family is right. This
  is the largest single contributor at depth, because the families disagree about
  where a curve saturates far more than a gauge disagrees with itself.
* **Cost** (`cost_sigma_frac`) - the replacement rate. Log-normal, so a draw cannot
  make rebuilding free, and applied as one scalar per draw because construction
  costs move together across a region.

Perturbation is applied to `floor_margin_m` - the *signed* distance from the water
surface to the finished floor - not to the clamped depth. The distinction is not
pedantic. A depth raster records every dry building as exactly zero, so adding
symmetric noise to it can only push buildings upward into the flood: on the synthetic
catchment that turned a deterministic 57 inundated buildings into a count interval of
67-219, with the point estimate below its own lower bound. With the margin, a
building the water missed by five metres stays dry through every draw and one it
missed by a centimetre is correctly uncertain. When no margin is supplied the draws
fall back to perturbing only the buildings already wet, and `count_interval_conditional`
is set to say the count interval is conditional on the deterministic wet set.

What is *not* sampled, and therefore is not in the interval: storey counts and floor
area, the finished-floor freeboard, building class assignment, the footprint
database's own completeness, and HAND's structural assumption that the water surface
parallels the drainage. The interval is a lower bound on the real uncertainty. It is
an honest account of four known errors, not of everything that could be wrong.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import numpy.typing as npt

from floodline.config import Config, CurveFamily, DamageConfig, MonteCarloConfig
from floodline.damage.curves import CurveSet, bundled_curves
from floodline.damage.estimate import estimate_damage

__all__ = ["DamageInterval", "monte_carlo_damage"]


@dataclass(frozen=True, slots=True)
class DamageInterval:
    """A damage total with a credible interval and the draws behind it."""

    point: float
    """The deterministic estimate, with no perturbation applied."""

    median: float
    lower: float
    upper: float
    quantiles: tuple[float, float]
    samples: npt.NDArray[np.float64]
    building_counts: npt.NDArray[np.int64]
    """Inundated building count per draw, so the count has an interval too."""

    n_samples: int
    curves_verified: bool
    families_sampled: dict[CurveFamily, int]
    count_interval_conditional: bool
    """True when no signed margin was supplied, so only already-wet buildings were
    perturbed and the count interval cannot widen downward-to-dry properly."""

    @property
    def count_interval(self) -> tuple[int, int]:
        """Credible interval on the number of buildings inundated."""
        lo, hi = self.quantiles
        return (
            int(np.quantile(self.building_counts, lo)),
            int(np.quantile(self.building_counts, hi)),
        )


def _resolve_mc(config: Config | MonteCarloConfig | None) -> MonteCarloConfig:
    """Return the `MonteCarloConfig` to use, defaulting when nothing is supplied."""
    if isinstance(config, Config):
        return config.monte_carlo
    return config if config is not None else MonteCarloConfig()


def _resolve_damage(config: Config | DamageConfig | None) -> DamageConfig:
    """Return the `DamageConfig` to use, defaulting when nothing is supplied."""
    if isinstance(config, Config):
        return config.damage
    return config if config is not None else DamageConfig()


def monte_carlo_damage(
    floor_depth_m: npt.ArrayLike,
    floor_area_m2: npt.ArrayLike,
    building_class: npt.ArrayLike,
    *,
    storeys: npt.ArrayLike | None = None,
    floor_margin_m: npt.ArrayLike | None = None,
    structure_value: npt.ArrayLike | None = None,
    contents_value: npt.ArrayLike | None = None,
    contents_curves: CurveSet | None = None,
    curves: CurveSet | None = None,
    config: Config | None = None,
    monte_carlo: MonteCarloConfig | None = None,
    damage: DamageConfig | None = None,
    curve_sets: dict[CurveFamily, CurveSet] | None = None,
    cap_storeys: bool = True,
) -> DamageInterval:
    """Run the damage estimate `n_samples` times with perturbed inputs.

    Parameters
    ----------
    floor_depth_m, floor_area_m2, building_class, storeys
        As for `estimate_damage`.
    floor_margin_m
        Signed distance from the water surface to the finished floor, from
        `exposure.building_depths` when it was given an unclamped depth field.
        Strongly preferred: without it, dry buildings cannot be perturbed at all,
        because a depth of zero does not say how far below the floor the water was.
    config
        Supplies both sub-configs when given; `monte_carlo` and `damage` override it.
    curve_sets
        Curve sets by family, for when transcribed tables have been loaded. Missing
        families fall back to the bundled constants.
    curves
        A single curve set used for every draw, which turns family sampling off. That
        is the right mode for the published USACE library: there is one library, not
        an ensemble of competing approximations, so the disagreement term family
        sampling stands in for does not apply.
    structure_value, contents_value, contents_curves
        Passed to `estimate_damage`. Real per-structure values replace the
        area-times-rate proxy, which makes the cost sigma perturb a valuation rather
        than a guess.

    Returns
    -------
    DamageInterval
    """
    mc = monte_carlo if monte_carlo is not None else _resolve_mc(config)
    damage_config = damage if damage is not None else _resolve_damage(config)

    depths = np.asarray(floor_depth_m, dtype=np.float64)
    areas = np.asarray(floor_area_m2, dtype=np.float64)
    classes = np.asarray(building_class, dtype=object)
    counts = None if storeys is None else np.asarray(storeys, dtype=np.float64)

    if floor_margin_m is None:
        # No signed margin: perturb only what is already wet. Noise on a value
        # clamped at zero would manufacture flooding out of dry ground.
        margins = np.where(depths > 0.0, depths, -np.inf)
        conditional = True
    else:
        margins = np.asarray(floor_margin_m, dtype=np.float64)
        if margins.shape != depths.shape:
            raise ValueError(
                f"floor_margin_m shape {margins.shape} does not match depth {depths.shape}"
            )
        conditional = False

    families = list(mc.curve_family_weights)
    # MonteCarloConfig already guarantees these sum to something positive.
    weights = np.asarray([mc.curve_family_weights[f] for f in families], dtype=np.float64)
    weights = weights / weights.sum()

    sets: dict[CurveFamily, CurveSet] = {}
    single = curves is not None
    if curves is not None:
        families = [curves.family]
        weights = np.asarray([1.0], dtype=np.float64)
        sets = {curves.family: curves}
    else:
        for family in families:
            supplied = curve_sets.get(family) if curve_sets else None
            sets[family] = supplied or bundled_curves(family, config=damage_config)

    rng = np.random.default_rng(mc.seed)
    picks = rng.choice(len(families), size=mc.n_samples, p=weights)
    # One stage shift per draw (systematic), one DEM error per building per draw.
    stage_shift = rng.normal(0.0, mc.stage_sigma_m, size=mc.n_samples)
    dem_error = rng.normal(0.0, mc.dem_sigma_m, size=(mc.n_samples, depths.size))
    # Log-normal keeps cost positive; sigma is set so the multiplier's spread matches
    # cost_sigma_frac for the small fractions this is used with.
    cost_scale = rng.lognormal(0.0, mc.cost_sigma_frac, size=mc.n_samples)

    totals = np.empty(mc.n_samples, dtype=np.float64)
    inundated = np.empty(mc.n_samples, dtype=np.int64)
    sampled: dict[CurveFamily, int] = dict.fromkeys(families, 0)

    for i in range(mc.n_samples):
        family = families[int(picks[i])]
        sampled[family] += 1
        # A deeper stage and a lower DEM both mean deeper water at the building.
        # -inf margins stay dry under any perturbation, which is the point.
        drawn = np.maximum(margins + stage_shift[i] + dem_error[i], 0.0)
        result = estimate_damage(
            drawn,
            areas,
            classes,
            storeys=counts,
            config=damage_config,
            curves=sets[family],
            structure_value=structure_value,
            contents_value=contents_value,
            contents_curves=contents_curves,
            cost_scale=float(cost_scale[i]),
            cap_storeys=cap_storeys,
        )
        totals[i] = result.total
        inundated[i] = int((drawn > 0).sum())

    point = estimate_damage(
        depths,
        areas,
        classes,
        storeys=counts,
        config=damage_config,
        curves=(
            curves
            if single
            else sets.get(damage_config.curve_family)
            or bundled_curves(damage_config.curve_family, config=damage_config)
        ),
        structure_value=structure_value,
        contents_value=contents_value,
        contents_curves=contents_curves,
        cap_storeys=cap_storeys,
    )

    lo, hi = mc.interval
    return DamageInterval(
        point=point.total,
        median=float(np.median(totals)),
        lower=float(np.quantile(totals, lo)),
        upper=float(np.quantile(totals, hi)),
        quantiles=(float(lo), float(hi)),
        samples=totals,
        building_counts=inundated,
        n_samples=mc.n_samples,
        curves_verified=all(s.verified for s in sets.values()),
        families_sampled=sampled,
        count_interval_conditional=conditional,
    )
