from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest

from floodline.config import CurveFamily, DamageConfig
from floodline.damage.curves import DamageCurve, bundled_curves, load_curves


def test_every_bundled_family_covers_every_priced_class() -> None:
    priced = set(DamageConfig().replacement_cost_per_m2)
    for family in CurveFamily:
        assert set(bundled_curves(family).curves) == priced


def test_bundled_curves_are_marked_unverified() -> None:
    # The flag is what stops a currency figure being quoted off digits nobody checked.
    for family in CurveFamily:
        assert bundled_curves(family).verified is False


def test_dry_and_below_floor_is_zero_damage() -> None:
    curve = bundled_curves(CurveFamily.HAZUS).for_class("residential")
    np.testing.assert_array_equal(curve.damage_fraction([-1.0, -0.01, 0.0]), [0.0, 0.0, 0.0])


def test_damage_is_non_decreasing_in_depth() -> None:
    depths = np.linspace(0.0, 10.0, 200)
    for family in CurveFamily:
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
    for family in CurveFamily:
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
