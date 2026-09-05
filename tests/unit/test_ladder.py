from __future__ import annotations

import numpy as np
import pytest

from floodline.config import CurveFamily, DamageConfig
from floodline.damage.curves import CurveSet, DamageCurve
from floodline.damage.estimate import NO_WATER
from floodline.damage.ladder import damage_ladder

CURVE = CurveSet(
    family=CurveFamily.USACE,
    curves={
        "RES1-1SNB": DamageCurve(
            family=CurveFamily.USACE,
            building_class="RES1-1SNB",
            depths_m=(-0.61, 0.0, 1.0, 3.0),
            fractions=(0.0, 0.134, 0.30, 0.60),
            provenance="test",
            verified=True,
        )
    },
    default_class="RES1-1SNB",
)

# Three buildings: on the channel, a metre up, five metres up. One reach.
HAND = np.array([0.0, 1.0, 5.0])
REACH = np.array([0, 0, 0])
FOUND = np.array([0.3, 0.3, 0.3])
AREA = np.array([100.0, 100.0, 100.0])
CODES = np.array(["RES1-1SNB"] * 3, dtype=object)
STOREYS = np.array([1.0, 1.0, 1.0])
VALUE = np.array([300_000.0, 300_000.0, 300_000.0])
MULTS = np.array([0.0, 0.5, 1.0, 2.0, 3.0])
# Stage rising with discharge, one row because there is one reach.
STAGES = np.array([[0.0, 1.0, 2.0, 4.0, 6.0]])


def _ladder(**kwargs: object):
    base: dict[str, object] = {
        "stage_by_multiplier": STAGES,
        "multipliers": MULTS,
        "base_discharge_cms": 1000.0,
        "structure_value": VALUE,
        "curves": CURVE,
        "cap_storeys": False,
    }
    base.update(kwargs)
    return damage_ladder(HAND, REACH, FOUND, AREA, CODES, STOREYS, **base)  # type: ignore[arg-type]


def test_zero_discharge_costs_exactly_nothing() -> None:
    got = _ladder()
    assert got.damage[0] == 0.0
    assert got.inundated[0] == 0


def test_damage_rises_monotonically_with_discharge() -> None:
    got = _ladder()
    assert list(got.damage) == sorted(got.damage)
    assert list(got.inundated) == sorted(got.inundated)
    assert got.damage[-1] > got.damage[0]


def test_more_discharge_reaches_more_buildings() -> None:
    got = _ladder()
    # Stage 2 m clears the channel-side and the 1 m building, not the 5 m one.
    assert got.inundated[MULTS.tolist().index(1.0)] == 2
    assert got.inundated[-1] == 3


def test_discharge_is_the_multiplier_times_the_base() -> None:
    got = _ladder()
    assert got.discharge_cms == (0.0, 500.0, 1000.0, 2000.0, 3000.0)


def test_a_building_on_no_known_reach_stays_dry_at_every_discharge() -> None:
    got = damage_ladder(
        HAND,
        np.array([-1, -1, -1]),
        FOUND,
        AREA,
        CODES,
        STOREYS,
        stage_by_multiplier=STAGES,
        multipliers=MULTS,
        base_discharge_cms=1000.0,
        structure_value=VALUE,
        curves=CURVE,
        cap_storeys=False,
    )
    assert set(got.damage) == {0.0}
    assert set(got.inundated) == {0}


def test_residents_track_the_buildings_that_flood() -> None:
    got = _ladder(residents=np.array([2.0, 3.0, 4.0]))
    assert got.residents[0] == 0.0
    assert got.residents[MULTS.tolist().index(1.0)] == pytest.approx(5.0)
    assert got.residents[-1] == pytest.approx(9.0)


def test_interpolating_between_rungs_lands_between_them() -> None:
    got = _ladder()
    mid = got.at(0.75)
    lo = got.damage[MULTS.tolist().index(0.5)]
    hi = got.damage[MULTS.tolist().index(1.0)]
    assert lo <= mid["damage"] <= hi


def test_asking_beyond_the_ladder_clamps_rather_than_extrapolating() -> None:
    got = _ladder()
    assert got.at(99.0)["damage"] == pytest.approx(got.damage[-1])
    assert got.at(-5.0)["damage"] == pytest.approx(got.damage[0])


def test_a_mismatched_stage_table_is_refused() -> None:
    with pytest.raises(ValueError, match="multipliers but the stage table"):
        _ladder(multipliers=np.array([0.0, 1.0]))


def test_contents_are_priced_on_their_own_curve() -> None:
    contents = CurveSet(
        family=CurveFamily.USACE,
        curves={
            "RES1-1SNB": DamageCurve(
                family=CurveFamily.USACE,
                building_class="RES1-1SNB",
                depths_m=(-0.61, 0.0, 1.0, 3.0),
                fractions=(0.0, 0.08, 0.20, 0.45),
                provenance="test",
                verified=True,
            )
        },
        default_class="RES1-1SNB",
    )
    got = _ladder(contents_value=VALUE * 0.5, contents_curves=contents)
    assert got.contents[-1] > 0.0
    assert got.structure[-1] > 0.0
    for i in range(len(MULTS)):
        assert got.damage[i] == pytest.approx(got.structure[i] + got.contents[i])


def test_the_sentinel_is_the_one_the_estimator_understands() -> None:
    # A building with no reach is pushed to NO_WATER, which estimate_damage treats as
    # dry regardless of what the curve says below its first point.
    assert NO_WATER < -1000.0


def test_a_default_config_does_not_change_the_answer() -> None:
    assert _ladder().damage == _ladder(config=DamageConfig()).damage
