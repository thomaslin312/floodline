"""Monte Carlo over the four errors that actually move the damage total.

Each draw perturbs the inputs and re-runs the deterministic estimate:

Measured on Whiteoak Bayou, each term sampled alone, as a share of the point
estimate: **stage 100%, cost 84%, curve and family together 16%, DEM 8%**, against
123% for all of them at once. Stage and cost dominate because they change how many
buildings are wet; the curve only changes what each wet building costs, and the DEM
error largely averages out. Worth stating because this file previously asserted that
curve family was the largest term, which was reasoning rather than measurement.

* **Stage** (`stage_sigma_m`) - the gauge reading, and everything the rating curve
  did with it. Shifts every building's depth together, so it moves the total far
  more than its size suggests: it is a systematic error, not a per-building one.
* **DEM** (`dem_sigma_m`) - vertical error in the terrain, and so in HAND. Applied
  per building and independently, because lidar error decorrelates over tens of
  metres, so it largely averages out across a basin and mostly widens the tails.
* **The curve** - two terms, both live. `curve_family_weights` asks which published
  family is right, and it is the largest single term at depth, because families
  disagree about where a curve saturates far more than a gauge disagrees with itself.
  Loading the USACE library used to switch this off, on the reasoning that one library
  is not an ensemble - but the disagreement does not stop existing because only one
  opinion was consulted, and the band came out *tighter* for having more specific
  curves, which is backwards. The loaded library now leads at
  `supplied_family_weight` and the bundled families take the rest. Alongside it, each
  curve's own published standard deviation is sampled - one draw for the whole curve,
  since the spread is uncertainty about where the curve sits and drawing it per
  building would average away across a quarter of a million of them.

  Contents curves are the exception: only the USACE library publishes them here, so a
  draw that prices structure against JRC still prices contents against USACE. That
  term is therefore still missing, and the interval remains a lower bound.
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

from floodline.core.config import Config, CurveFamily, DamageConfig, MonteCarloConfig
from floodline.core.damage.curves import CurveLookup, CurveSet, bundled_curves
from floodline.core.damage.estimate import estimate_damage

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

    expected_per_building: npt.NDArray[np.float64]
    """Mean damage per building across the draws, rather than damage at the mean depth.

    These are not the same number and the gap is not small. A depth-damage curve is
    strongly non-linear, so `E[f(depth)]` and `f(E[depth])` diverge exactly where the
    uncertainty straddles the finished floor - which, with a water-surface residual
    around 1.5 m against floor heights a third of that, is most of the buildings that
    matter. The point estimate reads the curve once at the modelled depth and so
    commits to a coin flip per building; this reads it under the whole distribution
    and lets a half-likely building carry half its loss.

    Sum this for an expected-loss total. `point` remains the deterministic answer, so
    the two can be compared rather than one silently replacing the other."""

    curves_verified: bool
    """Whether the curves behind the *point estimate* are transcribed from a source.

    Not `all(...)` over the sampled families. Once a loaded library is sampled against
    the bundled approximations, that conjunction is always False, and it reported a
    verified USACE point estimate as unquotable currency. The point estimate is priced
    against one library and this describes that library; `all_families_verified` covers
    the spread around it.
    """

    all_families_verified: bool
    """Whether every family that contributed a draw is transcribed.

    False whenever bundled approximations widen the interval, which is the normal case.
    It qualifies the width of the band, not the number in the middle.
    """

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
    curve_sigma: dict[str, tuple[float, ...]] | None = None,
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
    curve_sigma
        Published per-depth standard deviation of each curve, keyed by class. This is
        the within-library term, separate from the between-library one; without it
        that half is simply absent.
    curves
        A library loaded explicitly, which leads the family sampling at
        `supplied_family_weight` rather than replacing it. The point estimate is
        priced against this set alone; the interval around it carries the other
        families too. Set `sample_across_families` False to price against this one
        library only, knowing the interval then understates itself.
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
    if curves is not None and not mc.sample_across_families:
        # Priced against one library on purpose. The interval then carries stage, DEM,
        # cost and the library's own published spread, and nothing for the choice of
        # library, which is the term that dominates at depth.
        families = [curves.family]
        weights = np.asarray([1.0], dtype=np.float64)
        sets = {curves.family: curves}
    elif curves is not None:
        # A loaded library leads, but the bundled families still get a share, because
        # "which published family is right" is a real question and answering it with
        # silence understates the interval. Families differ by about 2.3x at one metre;
        # loading USACE used to remove that entirely, so the band looked tighter for
        # having more specific curves, which is backwards.
        others = [f for f in mc.curve_family_weights if f != curves.family]
        lead = float(mc.supplied_family_weight)
        families = [curves.family, *others]
        rest = np.asarray([float(mc.curve_family_weights[f]) for f in others])
        rest = rest / rest.sum() * (1.0 - lead) if rest.size and rest.sum() > 0 else rest
        weights = np.concatenate([[lead], rest]) if rest.size else np.asarray([1.0])
        weights = weights / weights.sum()
        sets = {curves.family: curves}
        for family in others:
            supplied = curve_sets.get(family) if curve_sets else None
            sets[family] = supplied or bundled_curves(family, config=damage_config)
    else:
        for family in families:
            supplied = curve_sets.get(family) if curve_sets else None
            sets[family] = supplied or bundled_curves(family, config=damage_config)

    # Curves and classes are fixed across draws, so the per-class grouping and the
    # interpolation grid are built once rather than 500 times.
    lookups: dict[CurveFamily, CurveLookup] = {
        family: curve_set.lookup(config=damage_config, sigma_by_class=curve_sigma)
        for family, curve_set in sets.items()
    }
    contents_lookup = (
        contents_curves.lookup(config=damage_config, sigma_by_class=curve_sigma)
        if contents_curves
        else None
    )
    # One index array per curve set: rows are numbered by sorted class name, and the
    # sets do not always hold the same classes.
    indices = {family: table.indices_for(classes) for family, table in lookups.items()}
    contents_index = contents_lookup.indices_for(classes) if contents_lookup else None

    rng = np.random.default_rng(mc.seed)
    picks = rng.choice(len(families), size=mc.n_samples, p=weights)
    # One stage shift per draw (systematic), one DEM error per building per draw.
    stage_shift = rng.normal(0.0, mc.stage_sigma_m, size=mc.n_samples)
    dem_error = rng.normal(0.0, mc.dem_sigma_m, size=(mc.n_samples, depths.size))
    # Log-normal keeps cost positive; sigma is set so the multiplier's spread matches
    # cost_sigma_frac for the small fractions this is used with.
    cost_scale = rng.lognormal(0.0, mc.cost_sigma_frac, size=mc.n_samples)
    # One curve draw per sample, applied to every building in it. Zero where the
    # source publishes no spread, which is every bundled family.
    curve_z = rng.normal(0.0, 1.0, size=mc.n_samples)

    totals = np.empty(mc.n_samples, dtype=np.float64)
    inundated = np.empty(mc.n_samples, dtype=np.int64)
    sampled: dict[CurveFamily, int] = dict.fromkeys(families, 0)
    # Running sum of per-building damage across draws, for the expected-damage
    # estimator. One accumulator rather than keeping every draw's vector: a quarter of
    # a million buildings by a thousand draws is two gigabytes, and only the mean is
    # wanted.
    expected_sum = np.zeros(depths.size, dtype=np.float64)

    for i in range(mc.n_samples):
        family = families[int(picks[i])]
        sampled[family] += 1
        # A deeper stage and a lower DEM both mean deeper water at the building.
        # -inf margins stay dry under any perturbation, which is the point.
        #
        # Not clamped at zero. Clamping here made every dry building look like water
        # was touching its slab, and the USACE curves are 13.4% at that point - the
        # same defect as passing a clamped depth to the point estimate, which is what
        # `damage_below_floor` now refuses. The curves are defined below zero; let
        # them answer for below zero.
        drawn = margins + stage_shift[i] + dem_error[i]
        result = estimate_damage(
            drawn,
            areas,
            classes,
            # The per-class breakdown costs 119 ms a call here and nothing reads it:
            # only the point estimate below is ever asked for one.
            with_by_class=False,
            storeys=counts,
            config=damage_config,
            curves=sets[family],
            structure_value=structure_value,
            contents_value=contents_value,
            contents_curves=contents_curves,
            cost_scale=float(cost_scale[i]),
            curve_sigma_z=float(curve_z[i]),
            cap_storeys=cap_storeys,
            lookup=lookups[family],
            contents_lookup=contents_lookup,
            class_index=indices[family],
            contents_index=contents_index,
        )
        totals[i] = result.total
        inundated[i] = int((drawn > 0).sum())
        expected_sum += result.per_building

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
        expected_per_building=expected_sum / mc.n_samples,
        median=float(np.median(totals)),
        lower=float(np.quantile(totals, lo)),
        upper=float(np.quantile(totals, hi)),
        quantiles=(float(lo), float(hi)),
        samples=totals,
        building_counts=inundated,
        n_samples=mc.n_samples,
        curves_verified=(
            curves.verified
            if curves is not None
            else sets[damage_config.curve_family].verified
            if damage_config.curve_family in sets
            else all(s.verified for s in sets.values())
        ),
        all_families_verified=all(s.verified for s in sets.values()),
        families_sampled=sampled,
        count_interval_conditional=conditional,
    )
