from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest

from floodline.config import CurveFamily, DamageConfig
from floodline.damage.curves import bundled_curves
from floodline.damage.usace import ensure_usace_curves, load_usace_curves

# Two real occupancy types, in the file's own units: feet and percent.
FIXTURE = {
    "occupancytypes": {
        "RES1-1SNB": {
            "name": "RES1-1SNB",
            "componentdamagefunctions": {
                "structure": {
                    "damagefunctions": {
                        "depth": {
                            "source": "EGM damage functions",
                            "damagedriver": "depth",
                            "damagefunction": {
                                "xvalues": [-2, -1, 0, 1, 2],
                                "ydistributions": [
                                    {
                                        "type": "DeterministicDistribution",
                                        "parameters": {"value": 0},
                                    },
                                    {
                                        "type": "NormalDistribution",
                                        "parameters": {"mean": 2.5, "standarddeviation": 0.3},
                                    },
                                    {
                                        "type": "NormalDistribution",
                                        "parameters": {"mean": 13.4, "standarddeviation": 1.2},
                                    },
                                    {
                                        "type": "NormalDistribution",
                                        "parameters": {"mean": 23.3, "standarddeviation": 1.6},
                                    },
                                    {
                                        "type": "NormalDistribution",
                                        "parameters": {"mean": 32.1, "standarddeviation": 1.6},
                                    },
                                ],
                            },
                        }
                    }
                },
                "contents": {
                    "damagefunctions": {
                        "depth": {
                            "source": "EGM damage functions",
                            "damagefunction": {
                                "xvalues": [-2, -1, 0, 1, 2],
                                "ydistributions": [
                                    {
                                        "type": "DeterministicDistribution",
                                        "parameters": {"value": 0},
                                    },
                                    {
                                        "type": "NormalDistribution",
                                        "parameters": {"mean": 2.4, "standarddeviation": 0.2},
                                    },
                                    {
                                        "type": "NormalDistribution",
                                        "parameters": {"mean": 8.1, "standarddeviation": 0.5},
                                    },
                                    {
                                        "type": "NormalDistribution",
                                        "parameters": {"mean": 13.3, "standarddeviation": 0.8},
                                    },
                                    {
                                        "type": "NormalDistribution",
                                        "parameters": {"mean": 17.9, "standarddeviation": 1.0},
                                    },
                                ],
                            },
                        }
                    }
                },
            },
        },
        # A code with no depth-indexed curve, like the generic COM/IND aggregates.
        "COM": {"name": "COM", "componentdamagefunctions": {"structure": {"damagefunctions": {}}}},
    }
}


@pytest.fixture
def curve_file(tmp_path: Path) -> Path:
    path = tmp_path / "occtypes.json"
    path.write_text(json.dumps(FIXTURE))
    return path


def test_feet_convert_to_metres(curve_file: Path) -> None:
    curves = load_usace_curves(curve_file)
    depths = curves.structure.for_class("RES1-1SNB").depths_m
    assert depths[0] == pytest.approx(-2 * 0.3048)
    assert depths[2] == pytest.approx(0.0)


def test_percent_converts_to_fraction(curve_file: Path) -> None:
    curves = load_usace_curves(curve_file)
    at_floor = curves.structure.for_class("RES1-1SNB").damage_fraction([0.0])
    assert at_floor[0] == pytest.approx(0.134)


def test_damage_at_floor_level_is_not_zeroed_out(curve_file: Path) -> None:
    """The bundled curves are zero at zero; these are not, and that must survive.

    A blanket "no damage at or below the floor" rule would throw away 13.4% of a
    house, which is what USACE says water lapping the slab actually costs.
    """
    curves = load_usace_curves(curve_file)
    assert curves.structure.for_class("RES1-1SNB").damage_fraction([0.0])[0] > 0.13
    # Still zero well below the floor, where the curve itself says zero.
    assert curves.structure.for_class("RES1-1SNB").damage_fraction([-1.0])[0] == 0.0


def test_deterministic_and_normal_distributions_both_parse(curve_file: Path) -> None:
    curves = load_usace_curves(curve_file)
    fractions = curves.structure.for_class("RES1-1SNB").fractions
    assert fractions[0] == 0.0  # DeterministicDistribution {"value": 0}
    assert fractions[1] == pytest.approx(0.025)  # NormalDistribution mean 2.5


def test_standard_deviations_are_kept_as_fractions(curve_file: Path) -> None:
    curves = load_usace_curves(curve_file)
    sigma = curves.sigma["RES1-1SNB"]
    assert sigma[2] == pytest.approx(0.012)
    assert len(sigma) == 5


def test_contents_curves_load_separately(curve_file: Path) -> None:
    curves = load_usace_curves(curve_file)
    structure = curves.structure.for_class("RES1-1SNB").damage_fraction([0.3048])[0]
    contents = curves.contents.for_class("RES1-1SNB").damage_fraction([0.3048])[0]
    assert structure == pytest.approx(0.233)
    assert contents == pytest.approx(0.133)


def test_codes_without_a_depth_curve_fall_back_rather_than_break(curve_file: Path) -> None:
    curves = load_usace_curves(curve_file)
    assert curves.has("COM") is False
    assert curves.for_default().building_class == "RES1-1SNB"


def test_loaded_curves_are_verified(curve_file: Path) -> None:
    curves = load_usace_curves(curve_file)
    assert curves.structure.verified is True
    assert "go-consequences" in curves.structure.for_class("RES1-1SNB").provenance


def test_usace_has_no_bundled_approximation() -> None:
    with pytest.raises(ValueError, match="published library"):
        bundled_curves(CurveFamily.USACE)


def test_a_missing_default_occupancy_is_refused(curve_file: Path) -> None:
    config = DamageConfig(usace_default_occupancy="MANSION")
    with pytest.raises(ValueError, match="default occupancy"):
        load_usace_curves(curve_file, config=config)


def test_a_missing_cache_names_the_command(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError, match="fetch-curves"):
        ensure_usace_curves(cache_dir=tmp_path, download=False)


def test_usace_and_bundled_hazus_disagree(curve_file: Path) -> None:
    """The bundled constants were a guess; this is the measure of how far off."""
    usace = load_usace_curves(curve_file).structure.for_class("RES1-1SNB")
    guess = bundled_curves(CurveFamily.HAZUS).for_class("residential")
    at_one_foot = 0.3048
    assert usace.damage_fraction([at_one_foot])[0] == pytest.approx(0.233)
    assert guess.damage_fraction([at_one_foot])[0] < 0.16
    assert not np.isclose(
        usace.damage_fraction([at_one_foot])[0], guess.damage_fraction([at_one_foot])[0]
    )
