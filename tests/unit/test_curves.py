from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest

from floodline.config import CurveFamily, DamageConfig
from floodline.damage.curves import (
    BUNDLED_FAMILIES,
    CurveSet,
    DamageCurve,
    bundled_curves,
    load_curves,
)


def test_every_bundled_family_covers_every_priced_class() -> None:
    priced = set(DamageConfig().replacement_cost_per_m2)
    for family in BUNDLED_FAMILIES:
        assert set(bundled_curves(family).curves) == priced


def test_bundled_curves_are_marked_unverified() -> None:
    # The flag is what stops a currency figure being quoted off digits nobody checked.
    for family in BUNDLED_FAMILIES:
        assert bundled_curves(family).verified is False


def test_dry_and_below_floor_is_zero_damage() -> None:
    curve = bundled_curves(CurveFamily.HAZUS).for_class("residential")
    np.testing.assert_array_equal(curve.damage_fraction([-1.0, -0.01, 0.0]), [0.0, 0.0, 0.0])


def test_damage_is_non_decreasing_in_depth() -> None:
    depths = np.linspace(0.0, 10.0, 200)
    for family in BUNDLED_FAMILIES:
        for curve in bundled_curves(family).curves.values():
            fractions = curve.damage_fraction(depths)
            assert np.all(np.diff(fractions) >= -1e-12), f"{family}/{curve.building_class}"


def test_curve_is_held_beyond_max_depth_not_extrapolated() -> None:
    config = DamageConfig(max_curve_depth_m=4.0)
    curve = bundled_curves(CurveFamily.JRC_GLOBAL).for_class("residential")
    at_cap = curve.damage_fraction([4.0], config=config)
    beyond = curve.damage_fraction([40.0], config=config)
    np.testing.assert_allclose(at_cap, beyond)


def test_fractions_stay_within_unit_interval() -> None:
    depths = np.linspace(0.0, 50.0, 500)
    for family in BUNDLED_FAMILIES:
        for curve in bundled_curves(family).curves.values():
            fractions = curve.damage_fraction(depths)
            assert fractions.min() >= 0.0
            assert fractions.max() <= 1.0


def test_interpolation_is_linear_between_points() -> None:
    curve = DamageCurve(
        family=CurveFamily.HAZUS,
        building_class="residential",
        depths_m=(0.0, 2.0),
        fractions=(0.0, 1.0),
        provenance="test",
        verified=True,
    )
    np.testing.assert_allclose(curve.damage_fraction([0.5, 1.0, 1.5]), [0.25, 0.5, 0.75])


def test_unknown_class_falls_back_to_default() -> None:
    curves = bundled_curves(CurveFamily.HAZUS)
    fallback = curves.for_class("lighthouse")
    assert fallback.building_class == DamageConfig().default_class


def test_vectorised_per_class_matches_per_curve() -> None:
    curves = bundled_curves(CurveFamily.JRC_OCEANIA)
    depths = np.array([0.4, 1.2, 3.0, 0.9])
    classes = np.array(["residential", "commercial", "industrial", "residential"], dtype=object)
    got = curves.damage_fraction(depths, classes)
    for i, name in enumerate(classes):
        expected = curves.for_class(str(name)).damage_fraction([depths[i]])
        np.testing.assert_allclose(got[i], expected[0])


@pytest.mark.parametrize(
    ("depths", "fractions", "message"),
    [
        ((0.0, 1.0), (0.0,), "fractions"),
        ((0.0,), (0.0,), "at least two"),
        ((1.0, 0.0), (0.0, 1.0), "ascend"),
        ((0.0, 1.0), (0.5, 0.2), "must not decrease"),
        ((0.0, 1.0), (0.0, 1.5), r"\[0, 1\]"),
    ],
)
def test_malformed_curves_are_refused(
    depths: tuple[float, ...], fractions: tuple[float, ...], message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        DamageCurve(
            family=CurveFamily.HAZUS,
            building_class="residential",
            depths_m=depths,
            fractions=fractions,
            provenance="test",
            verified=True,
        )


def test_loaded_curves_can_be_marked_verified(tmp_path: Path) -> None:
    path = tmp_path / "curves.json"
    path.write_text(
        json.dumps(
            {
                "hazus": {
                    "verified": True,
                    "provenance": "transcribed from the technical manual",
                    "classes": {
                        "residential": {"depths_m": [0.0, 3.0], "fractions": [0.0, 0.9]},
                    },
                }
            }
        )
    )
    loaded = load_curves(path)
    assert loaded[CurveFamily.HAZUS].verified is True
    np.testing.assert_allclose(
        loaded[CurveFamily.HAZUS].for_class("residential").damage_fraction([1.5]), [0.45]
    )


def test_loaded_curves_must_price_the_default_class(tmp_path: Path) -> None:
    path = tmp_path / "curves.json"
    path.write_text(
        json.dumps(
            {"hazus": {"classes": {"pumphouse": {"depths_m": [0.0, 1.0], "fractions": [0.0, 1.0]}}}}
        )
    )
    with pytest.raises(ValueError, match="default class"):
        load_curves(path)


# --- the precomputed lookup: an optimisation that must not change the answer ---


def test_lookup_matches_direct_interpolation() -> None:
    curves = bundled_curves(CurveFamily.JRC_OCEANIA)
    table = curves.lookup()
    depths = np.linspace(-1.0, 9.0, 400)
    classes = np.array(["residential", "commercial", "industrial", "other"] * 100, dtype=object)
    direct = curves.damage_fraction(depths, classes)
    fast = table.fraction(depths, table.indices_for(classes))
    # The grid is the curves' own breakpoints, so this is equality, not closeness.
    np.testing.assert_allclose(direct, fast, atol=1e-12)


def test_lookup_is_exact_for_breakpoints_off_a_round_grid() -> None:
    """Curves whose kinks fall between round depths are the case a uniform grid got wrong.

    The bundled curves break at half metres, which any sane uniform grid lands on
    exactly, so they cannot detect a grid that cuts corners. The published USACE
    curves break at whole feet, and against a 5 mm grid that silently cost 6.5e-8 of
    a study-area total. Breakpoints here are deliberately irrational-ish.
    """
    step = 0.3048  # one foot, the spacing the real USACE tables use
    depths = np.arange(-1, 6, dtype=np.float64) * step
    curves = CurveSet(
        family=CurveFamily.USACE,
        curves={
            "residential": DamageCurve(
                family=CurveFamily.USACE,
                building_class="residential",
                depths_m=tuple(float(d) for d in depths),
                # Sharp kinks: a corner-cutting grid shows up as a shortfall at each.
                fractions=(0.0, 0.0, 0.6, 0.65, 0.67, 0.9, 0.95),
                provenance="test",
                verified=False,
            )
        },
        default_class="residential",
    )
    table = curves.lookup()
    # Sample densely and off-grid, so any straddled kink is hit from both sides.
    probes = np.linspace(depths[0] - 0.5, depths[-1] + 0.5, 5_000)
    classes = np.full(probes.size, "residential", dtype=object)
    direct = curves.damage_fraction(probes, classes)
    fast = table.fraction(probes, table.indices_for(classes))
    np.testing.assert_allclose(direct, fast, atol=1e-12)
    # And the breakpoints themselves are columns, not values the grid had to land near.
    assert np.isin(depths, table.depths_m).all()


def test_lookup_sends_unknown_classes_to_the_default_row() -> None:
    curves = bundled_curves(CurveFamily.HAZUS)
    table = curves.lookup()
    rows = table.indices_for(np.array(["lighthouse", "residential"], dtype=object))
    assert rows[0] == table.default_index
    assert rows[1] == table.index_of["residential"]


def test_lookup_holds_the_curve_beyond_both_ends() -> None:
    curves = bundled_curves(CurveFamily.HAZUS)
    table = curves.lookup()
    rows = table.indices_for(np.array(["residential"] * 3, dtype=object))
    got = table.fraction([-99.0, 0.0, 99.0], rows)
    assert got[0] == 0.0
    assert got[2] == pytest.approx(
        curves.for_class("residential").damage_fraction([DamageConfig().max_curve_depth_m])[0]
    )


def test_lookup_covers_curves_that_start_below_zero() -> None:
    # USACE curves start at -0.61 m, so a grid anchored at zero would clip them.
    curve = DamageCurve(
        family=CurveFamily.USACE,
        building_class="RES1-1SNB",
        depths_m=(-0.61, 0.0, 1.0),
        fractions=(0.0, 0.134, 0.30),
        provenance="test",
        verified=True,
    )
    curves = CurveSet(
        family=CurveFamily.USACE,
        curves={"RES1-1SNB": curve},
        default_class="RES1-1SNB",
    )
    table = curves.lookup()
    assert table.depths_m[0] == pytest.approx(-0.61)
    rows = table.indices_for(np.array(["RES1-1SNB"], dtype=object))
    assert table.fraction([0.0], rows)[0] == pytest.approx(0.134)


def test_an_impossible_grid_is_refused() -> None:
    curve = DamageCurve(
        family=CurveFamily.HAZUS,
        building_class="residential",
        depths_m=(10.0, 20.0),
        fractions=(0.0, 1.0),
        provenance="test",
        verified=True,
    )
    curves = CurveSet(
        family=CurveFamily.HAZUS,
        curves={"residential": curve},
        default_class="residential",
    )
    with pytest.raises(ValueError, match="lookup grid would be empty"):
        curves.lookup(config=DamageConfig(max_curve_depth_m=5.0))


def test_generic_class_joins_the_two_curve_vocabularies() -> None:
    """NSI and USACE speak HAZUS occupancy codes; the international libraries do not.

    Without this join every one of the 42 occupancy types lands on JRC's default row,
    and sampling across families compares a detailed library against a single curve
    while looking like it worked.
    """
    from floodline.damage.curves import generic_class

    assert generic_class("RES1-2SWB") == "residential"
    assert generic_class("COM4") == "commercial"
    assert generic_class("IND2") == "industrial"
    for code in ("AGR1", "GOV1", "EDU2", "REL1"):
        assert generic_class(code) == "other", code
    # Already-generic names pass through, and an unknown one is left alone so the
    # caller's own default handling applies rather than a wrong guess.
    assert generic_class("residential") == "residential"
    assert generic_class("wharf") == "wharf"


def test_a_hazus_occtype_finds_a_generic_family_row() -> None:
    curves = bundled_curves(CurveFamily.JRC_GLOBAL)
    table = curves.lookup()
    classes = np.array(["RES1-2SWB", "COM4", "IND2", "AGR1", "residential"], dtype=object)
    rows = table.indices_for(classes)
    assert rows[0] == table.index_of["residential"]
    assert rows[1] == table.index_of["commercial"]
    assert rows[2] == table.index_of["industrial"]
    assert rows[3] == table.index_of["other"]
    assert rows[4] == table.index_of["residential"]
    # An unrecognisable name still falls back rather than raising.
    assert table.indices_for(np.array(["nonsense"], dtype=object))[0] == table.default_index
