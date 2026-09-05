from __future__ import annotations

import numpy as np
import pytest

from floodline.config import Config, CurveFamily, DamageConfig, MonteCarloConfig
from floodline.damage.costs import exposed_value, storey_exposure
from floodline.damage.curves import BUNDLED_FAMILIES, CurveSet, DamageCurve, bundled_curves
from floodline.damage.estimate import estimate_damage
from floodline.damage.uncertainty import monte_carlo_damage

CLASSES = np.array(["residential", "commercial", "residential"], dtype=object)
AREAS = np.array([100.0, 200.0, 150.0])
STOREYS = np.array([1.0, 1.0, 1.0])


def test_exposed_value_prices_by_class() -> None:
    config = DamageConfig(replacement_cost_per_m2={"residential": 10.0, "commercial": 20.0})
    value = exposed_value(AREAS, CLASSES, config=config)
    np.testing.assert_allclose(value, [1000.0, 4000.0, 1500.0])


def test_unpriced_class_falls_back_to_the_default_rate() -> None:
    config = DamageConfig(
        replacement_cost_per_m2={"residential": 10.0}, default_class="residential"
    )
    value = exposed_value([100.0], np.array(["lighthouse"], dtype=object), config=config)
    np.testing.assert_allclose(value, [1000.0])


def test_default_class_must_be_priced() -> None:
    with pytest.raises(ValueError, match="replacement_cost_per_m2"):
        DamageConfig(replacement_cost_per_m2={"commercial": 10.0}, default_class="residential")


def test_storey_exposure_limits_damage_to_the_storeys_water_reaches() -> None:
    # 0.8 m of water in a two-storey building reaches one of its two floors.
    np.testing.assert_allclose(storey_exposure([0.8], [2.0]), [0.5])
    # 4 m reaches both.
    np.testing.assert_allclose(storey_exposure([4.0], [2.0]), [1.0])
    # A single-storey building is always fully exposed.
    np.testing.assert_allclose(storey_exposure([0.1], [1.0]), [1.0])


def test_dry_buildings_take_no_damage() -> None:
    result = estimate_damage(np.zeros(3), AREAS, CLASSES, storeys=STOREYS)
    assert result.total == 0.0
    assert result.n_damaged == 0


def test_damage_rises_with_depth() -> None:
    shallow = estimate_damage(np.full(3, 0.3), AREAS, CLASSES, storeys=STOREYS)
    deep = estimate_damage(np.full(3, 3.0), AREAS, CLASSES, storeys=STOREYS)
    assert deep.total > shallow.total


def test_damage_never_exceeds_exposed_value() -> None:
    result = estimate_damage(np.full(3, 100.0), AREAS, CLASSES, storeys=STOREYS)
    assert result.total <= result.exposed_value_total
    assert 0.0 <= result.loss_ratio <= 1.0


def test_by_class_sums_to_the_total() -> None:
    result = estimate_damage(np.full(3, 1.5), AREAS, CLASSES, storeys=STOREYS)
    assert sum(result.by_class.values()) == pytest.approx(result.total)
    assert set(result.by_class) == {"residential", "commercial"}


def test_unverified_curves_propagate_to_the_estimate() -> None:
    result = estimate_damage(np.full(3, 1.0), AREAS, CLASSES, storeys=STOREYS)
    assert result.curves_verified is False
    assert any("unverified" in note for note in result.notes)


def test_curve_families_disagree_on_the_same_buildings() -> None:
    # The whole reason the Monte Carlo samples across families.
    totals = {
        family: estimate_damage(
            np.full(3, 1.0), AREAS, CLASSES, storeys=STOREYS, family=family
        ).total
        for family in BUNDLED_FAMILIES
    }
    assert len(set(totals.values())) == len(BUNDLED_FAMILIES)


def test_mismatched_input_shapes_are_refused() -> None:
    with pytest.raises(ValueError, match="must match"):
        estimate_damage(np.zeros(3), np.zeros(2), CLASSES, storeys=STOREYS)


def test_capping_storeys_requires_a_storey_count() -> None:
    with pytest.raises(ValueError, match="no storey count"):
        estimate_damage(np.full(3, 1.0), AREAS, CLASSES, cap_storeys=True)


def test_capping_storeys_lowers_damage_in_tall_buildings() -> None:
    tall = np.array([4.0, 4.0, 4.0])
    capped = estimate_damage(np.full(3, 1.0), AREAS, CLASSES, storeys=tall, cap_storeys=True)
    uncapped = estimate_damage(np.full(3, 1.0), AREAS, CLASSES, storeys=tall, cap_storeys=False)
    assert capped.total < uncapped.total


def test_supplied_curves_override_the_bundled_ones() -> None:
    curves = bundled_curves(CurveFamily.JRC_OCEANIA)
    result = estimate_damage(
        np.full(3, 1.0), AREAS, CLASSES, storeys=STOREYS, curves=curves, family=CurveFamily.HAZUS
    )
    assert result.family is CurveFamily.JRC_OCEANIA


MC = MonteCarloConfig(n_samples=200, seed=7)


def test_interval_brackets_the_median_and_is_ordered() -> None:
    result = monte_carlo_damage(np.full(3, 1.0), AREAS, CLASSES, storeys=STOREYS, monte_carlo=MC)
    assert result.lower <= result.median <= result.upper
    assert result.n_samples == 200
    assert result.samples.shape == (200,)


def test_monte_carlo_is_reproducible_from_its_seed() -> None:
    args = (np.full(3, 1.0), AREAS, CLASSES)
    first = monte_carlo_damage(*args, storeys=STOREYS, monte_carlo=MC)
    second = monte_carlo_damage(*args, storeys=STOREYS, monte_carlo=MC)
    np.testing.assert_array_equal(first.samples, second.samples)


def test_a_different_seed_gives_different_draws() -> None:
    args = (np.full(3, 1.0), AREAS, CLASSES)
    first = monte_carlo_damage(*args, storeys=STOREYS, monte_carlo=MC)
    other = monte_carlo_damage(
        *args, storeys=STOREYS, monte_carlo=MonteCarloConfig(n_samples=200, seed=8)
    )
    assert not np.array_equal(first.samples, other.samples)


def test_zero_uncertainty_collapses_the_interval_onto_the_point() -> None:
    quiet = MonteCarloConfig(
        n_samples=50,
        stage_sigma_m=0.0,
        dem_sigma_m=0.0,
        cost_sigma_frac=1e-12,
        curve_family_weights={CurveFamily.HAZUS: 1.0},
    )
    result = monte_carlo_damage(
        np.full(3, 1.0),
        AREAS,
        CLASSES,
        storeys=STOREYS,
        monte_carlo=quiet,
        damage=DamageConfig(curve_family=CurveFamily.HAZUS),
    )
    assert result.lower == pytest.approx(result.point, rel=1e-6)
    assert result.upper == pytest.approx(result.point, rel=1e-6)


def test_wider_stage_error_widens_the_interval() -> None:
    args = (np.full(3, 1.0), AREAS, CLASSES)
    narrow = monte_carlo_damage(
        *args, storeys=STOREYS, monte_carlo=MonteCarloConfig(n_samples=300, stage_sigma_m=0.05)
    )
    wide = monte_carlo_damage(
        *args, storeys=STOREYS, monte_carlo=MonteCarloConfig(n_samples=300, stage_sigma_m=0.60)
    )
    assert (wide.upper - wide.lower) > (narrow.upper - narrow.lower)


def test_families_are_sampled_in_proportion_to_their_weights() -> None:
    weighted = MonteCarloConfig(
        n_samples=2000,
        curve_family_weights={CurveFamily.HAZUS: 0.8, CurveFamily.JRC_GLOBAL: 0.2},
    )
    result = monte_carlo_damage(
        np.full(3, 1.0), AREAS, CLASSES, storeys=STOREYS, monte_carlo=weighted
    )
    share = result.families_sampled[CurveFamily.HAZUS] / result.n_samples
    assert share == pytest.approx(0.8, abs=0.03)


def test_zero_weights_are_refused_by_the_config() -> None:
    # Caught at construction, so monte_carlo_damage never sees an unusable weighting.
    with pytest.raises(ValueError, match="sum to a positive number"):
        MonteCarloConfig(curve_family_weights={CurveFamily.HAZUS: 0.0})


def test_building_count_has_an_interval_too() -> None:
    result = monte_carlo_damage(np.full(3, 0.05), AREAS, CLASSES, storeys=STOREYS, monte_carlo=MC)
    low, high = result.count_interval
    assert 0 <= low <= high <= 3


def test_a_whole_config_supplies_both_sub_configs() -> None:
    result = monte_carlo_damage(
        np.full(3, 1.0),
        AREAS,
        CLASSES,
        storeys=STOREYS,
        config=Config(monte_carlo=MonteCarloConfig(n_samples=25)),
    )
    assert result.n_samples == 25


# --- the clamped-depth bug: noise on a value floored at zero can only invent flooding ---

DRY_AND_WET = np.array([0.0, 0.0, 1.0])
MARGINS_FAR = np.array([-5.0, -4.0, 1.0])
MARGINS_NEAR = np.array([-0.02, -0.03, 1.0])


def test_point_estimate_lies_inside_its_own_count_interval() -> None:
    result = monte_carlo_damage(
        DRY_AND_WET,
        AREAS,
        CLASSES,
        storeys=STOREYS,
        floor_margin_m=MARGINS_FAR,
        monte_carlo=MonteCarloConfig(n_samples=400, seed=1),
    )
    low, high = result.count_interval
    assert low <= 1 <= high


def test_buildings_far_below_the_floor_stay_dry_through_every_draw() -> None:
    result = monte_carlo_damage(
        DRY_AND_WET,
        AREAS,
        CLASSES,
        storeys=STOREYS,
        floor_margin_m=MARGINS_FAR,
        monte_carlo=MonteCarloConfig(n_samples=500, seed=2, stage_sigma_m=0.3),
    )
    assert result.building_counts.max() == 1
    assert result.count_interval_conditional is False


def test_buildings_just_below_the_floor_are_correctly_uncertain() -> None:
    result = monte_carlo_damage(
        DRY_AND_WET,
        AREAS,
        CLASSES,
        storeys=STOREYS,
        floor_margin_m=MARGINS_NEAR,
        monte_carlo=MonteCarloConfig(n_samples=500, seed=2, stage_sigma_m=0.3),
    )
    # A couple of centimetres short is a coin flip against a 30 cm stage error.
    assert result.building_counts.max() == 3
    assert result.building_counts.min() == 1


def test_without_a_margin_dry_buildings_are_held_dry_and_flagged() -> None:
    result = monte_carlo_damage(
        DRY_AND_WET,
        AREAS,
        CLASSES,
        storeys=STOREYS,
        monte_carlo=MonteCarloConfig(n_samples=300, seed=4, stage_sigma_m=0.5),
    )
    assert result.count_interval_conditional is True
    assert result.building_counts.max() == 1


def test_margin_shape_must_match() -> None:
    with pytest.raises(ValueError, match="floor_margin_m shape"):
        monte_carlo_damage(DRY_AND_WET, AREAS, CLASSES, storeys=STOREYS, floor_margin_m=np.zeros(2))


# --- the published curve spread, which the interval used to omit entirely ---

SIGMA = {
    "residential": (0.0, 0.05, 0.05, 0.05, 0.05, 0.05, 0.05, 0.05, 0.05),
    "commercial": (0.0, 0.05, 0.05, 0.05, 0.05, 0.05, 0.05, 0.05, 0.05),
}


def test_a_sigma_draw_shifts_the_curve() -> None:
    curves = bundled_curves(CurveFamily.HAZUS)
    table = curves.lookup(sigma_by_class=SIGMA)
    rows = table.indices_for(np.array(["residential"], dtype=object))
    base = table.fraction([1.0], rows)[0]
    up = table.fraction([1.0], rows, sigma_z=1.0)[0]
    down = table.fraction([1.0], rows, sigma_z=-1.0)[0]
    assert up > base > down
    assert up - base == pytest.approx(0.05, abs=1e-6)


def test_a_sigma_draw_cannot_push_damage_outside_the_unit_interval() -> None:
    curves = bundled_curves(CurveFamily.JRC_OCEANIA)
    table = curves.lookup(sigma_by_class={"residential": (0.9,) * 9})
    rows = table.indices_for(np.array(["residential"], dtype=object))
    assert table.fraction([6.0], rows, sigma_z=5.0)[0] <= 1.0
    assert table.fraction([0.6], rows, sigma_z=-5.0)[0] >= 0.0


def test_without_sigma_the_draw_does_nothing() -> None:
    table = bundled_curves(CurveFamily.HAZUS).lookup()
    rows = table.indices_for(np.array(["residential"], dtype=object))
    assert table.sigma is None
    assert table.fraction([1.0], rows, sigma_z=3.0)[0] == table.fraction([1.0], rows)[0]


def test_curve_spread_widens_the_interval() -> None:
    """With one library there is no family term, so this is the only curve
    uncertainty there is. Omitting it made the band narrower than it should be."""
    args = (np.full(3, 1.0), AREAS, CLASSES)
    single = {"curves": bundled_curves(CurveFamily.HAZUS)}
    quiet = MonteCarloConfig(
        n_samples=400, seed=5, stage_sigma_m=0.01, dem_sigma_m=0.01, cost_sigma_frac=1e-9
    )
    without = monte_carlo_damage(*args, storeys=STOREYS, monte_carlo=quiet, **single)
    with_spread = monte_carlo_damage(
        *args, storeys=STOREYS, monte_carlo=quiet, curve_sigma=SIGMA, **single
    )
    assert (with_spread.upper - with_spread.lower) > (without.upper - without.lower)


# --- the clamped-depth-into-a-below-floor-curve bug ---

BELOW_FLOOR = CurveSet(
    family=CurveFamily.USACE,
    curves={
        "RES1-1SNB": DamageCurve(
            family=CurveFamily.USACE,
            building_class="RES1-1SNB",
            depths_m=(-0.61, -0.30, 0.0, 0.30),
            fractions=(0.0, 0.027, 0.134, 0.231),
            provenance="test",
            verified=True,
        )
    },
    default_class="RES1-1SNB",
)
CODES = np.array(["RES1-1SNB"] * 3, dtype=object)


def test_a_dry_building_takes_no_damage_from_a_below_floor_curve() -> None:
    """The bug: USACE curves are 13.4% at zero, and a clamped depth makes every dry
    building look like water is touching its slab."""
    margins = np.array([-5.0, -1.0, 0.5])
    got = estimate_damage(
        margins, AREAS, CODES, storeys=STOREYS, curves=BELOW_FLOOR, cap_storeys=False
    )
    assert got.per_building[0] == 0.0
    assert got.per_building[1] == 0.0
    assert got.per_building[2] > 0.0
    assert got.n_damaged == 1


def test_a_clamped_depth_against_a_below_floor_curve_is_refused() -> None:
    with pytest.raises(ValueError, match="what a clamped depth looks like"):
        estimate_damage(
            np.zeros(3), AREAS, CODES, storeys=STOREYS, curves=BELOW_FLOOR, cap_storeys=False
        )


def test_an_all_wet_batch_is_not_mistaken_for_a_clamped_one() -> None:
    """No negatives is not enough to call it clamped: a small all-wet batch has none
    either. The tell is a pile of values at exactly zero."""
    got = estimate_damage(
        np.array([2.0, 1.5, 0.8]),
        AREAS,
        CODES,
        storeys=STOREYS,
        curves=BELOW_FLOOR,
        cap_storeys=False,
    )
    assert got.n_damaged == 3


def test_the_guard_can_be_overridden_for_curves_that_start_at_zero() -> None:
    got = estimate_damage(
        np.zeros(3),
        AREAS,
        CODES,
        storeys=STOREYS,
        curves=BELOW_FLOOR,
        cap_storeys=False,
        damage_below_floor=False,
    )
    assert got.total > 0.0


def test_water_exactly_at_the_floor_still_takes_the_curve_value() -> None:
    """The opposite error: refusing at-floor damage would throw away what the curve
    actually says happens when water reaches the slab."""
    got = estimate_damage(
        np.array([-1.0, 0.0, 0.3]),
        AREAS,
        CODES,
        storeys=STOREYS,
        curves=BELOW_FLOOR,
        cap_storeys=False,
    )
    assert got.per_building[1] > 0.0
    assert got.per_building[1] < got.per_building[2]


def test_curves_that_start_at_zero_still_accept_clamped_depths() -> None:
    """The bundled families are zero at zero, so a clamped depth is fine for them."""
    got = estimate_damage(np.zeros(3), AREAS, CLASSES, storeys=STOREYS)
    assert got.total == 0.0


def test_the_monte_carlo_does_not_clamp_dry_buildings_into_the_flood() -> None:
    """Second home of the same bug: the draw loop clamped the perturbed margin at
    zero, so on every draw a dry building looked like water was touching its slab."""
    margins = np.array([-40.0, -30.0, 2.0])
    result = monte_carlo_damage(
        margins,
        AREAS,
        CODES,
        storeys=STOREYS,
        floor_margin_m=margins,
        curves=BELOW_FLOOR,
        cap_storeys=False,
        monte_carlo=MonteCarloConfig(n_samples=120, seed=9, stage_sigma_m=0.3),
    )
    # Only the third building is wet, so the point estimate must equal that one
    # priced alone: the dry pair contribute exactly nothing.
    only_wet = estimate_damage(
        margins[2:],
        AREAS[2:],
        CODES[2:],
        storeys=STOREYS[2:],
        curves=BELOW_FLOOR,
        cap_storeys=False,
    )
    assert result.point == pytest.approx(only_wet.total)
    # And no draw drags the dry pair 30 m uphill into the flood.
    assert result.building_counts.max() == 1
