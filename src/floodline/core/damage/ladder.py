"""Damage as a function of discharge, not a single answer at one flow.

The map's discharge slider always moved the water. It did not move the damage, which
made the most interesting question the model can answer — *what would another flood
cost* — the one thing it could not show. This computes the whole curve.

The trick is that nothing about a building changes with discharge. Its value, its
curve, its foundation height and its height above the nearest drainage are all fixed;
only the stage in its reach moves. So the expensive work — reading the raster, reducing
each footprint, matching occupancy codes — happens once, and each further point on the
ladder is a gather and a lookup:

    depth above floor at multiplier m  =  stage_m[reach of building] - HAND - foundation

Thirty-three multipliers over a quarter of a million structures costs a few seconds,
against several minutes if the raster were re-read for each.

The interval is not computed at every rung. A Monte Carlo at 300 draws is roughly a
hundred times the cost of one deterministic pass, so running it across the ladder would
take a quarter of an hour. It runs at the observed discharge, and the ladder carries
point estimates. A band drawn at every multiplier would look more informative and be
the same information stretched.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import numpy.typing as npt

from floodline.core.config import Config, DamageConfig
from floodline.core.damage.curves import CurveSet
from floodline.core.damage.estimate import NO_WATER, estimate_damage

__all__ = ["DamageLadder", "damage_ladder"]


@dataclass(frozen=True, slots=True)
class DamageLadder:
    """Point-estimate damage across a ladder of discharge multipliers."""

    multipliers: tuple[float, ...]
    discharge_cms: tuple[float, ...]
    damage: tuple[float, ...]
    structure: tuple[float, ...]
    contents: tuple[float, ...]
    inundated: tuple[int, ...]
    residents: tuple[float, ...]
    """Overnight residents in structures above finished floor at each multiplier."""

    per_building: dict[float, npt.NDArray[np.float64]] = field(default_factory=dict)
    """Damage per structure at a few reference multipliers, for the map layer. Kept
    only at the multipliers asked for: the full ladder over a quarter of a million
    structures would be 68 MB of float, to draw a picture 600 pixels wide."""

    def at(self, multiplier: float) -> dict[str, float]:
        """Interpolate the ladder, which is what a slider between rungs needs."""
        xs = np.asarray(self.multipliers, dtype=np.float64)
        clamped = float(np.clip(multiplier, xs[0], xs[-1]))
        return {
            "multiplier": clamped,
            "discharge_cms": float(np.interp(clamped, xs, self.discharge_cms)),
            "damage": float(np.interp(clamped, xs, self.damage)),
            "structure": float(np.interp(clamped, xs, self.structure)),
            "contents": float(np.interp(clamped, xs, self.contents)),
            "inundated": float(np.interp(clamped, xs, np.asarray(self.inundated, float))),
            "residents": float(np.interp(clamped, xs, self.residents)),
        }


def damage_ladder(
    hand_m: npt.NDArray[np.float64],
    reach_id: npt.NDArray[np.int64],
    foundation_m: npt.NDArray[np.float64],
    floor_area_m2: npt.NDArray[np.float64],
    building_class: npt.NDArray[np.object_],
    storeys: npt.NDArray[np.float64],
    *,
    stage_by_multiplier: npt.NDArray[np.float64],
    multipliers: npt.NDArray[np.float64],
    base_discharge_cms: float,
    structure_value: npt.NDArray[np.float64] | None = None,
    contents_value: npt.NDArray[np.float64] | None = None,
    curves: CurveSet | None = None,
    contents_curves: CurveSet | None = None,
    residents: npt.NDArray[np.float64] | None = None,
    reference_multipliers: tuple[float, ...] = (),
    config: Config | DamageConfig | None = None,
    cap_storeys: bool = True,
) -> DamageLadder:
    """Price every building at every multiplier on the ladder.

    Parameters
    ----------
    hand_m, reach_id, foundation_m
        Per building. `reach_id` of -1 means the building drains to no reach the model
        knows about, and it stays dry at every discharge rather than being given the
        stage of an arbitrary neighbour.
    stage_by_multiplier
        Stage per reach per multiplier, shape `(n_reaches, n_multipliers)`. The same
        table the map already uses to redraw depth when the slider moves, so the panel
        and the picture cannot disagree about what a multiplier means.
    reference_multipliers
        Multipliers at which to keep per-structure damage, for rasterising a map layer
        that responds to the slider. Anything not on the ladder is ignored.

    Returns
    -------
    DamageLadder
    """
    damage_config = config.damage if isinstance(config, Config) else (config or DamageConfig())
    n_reaches, n_steps = stage_by_multiplier.shape
    if len(multipliers) != n_steps:
        raise ValueError(
            f"{len(multipliers)} multipliers but the stage table has {n_steps} columns"
        )

    known = (reach_id >= 0) & (reach_id < n_reaches) & np.isfinite(hand_m)
    safe_reach = np.where(known, reach_id, 0)

    # Built once and reused at every rung; this is what makes the ladder cheap.
    lookup = curves.lookup(config=damage_config) if curves else None
    contents_lookup = contents_curves.lookup(config=damage_config) if contents_curves else None
    index = lookup.indices_for(building_class) if lookup else None
    contents_index = contents_lookup.indices_for(building_class) if contents_lookup else None

    wanted = {int(np.argmin(np.abs(multipliers - m))): float(m) for m in reference_multipliers}
    per_building: dict[float, npt.NDArray[np.float64]] = {}
    totals, structure, contents, counts, people = [], [], [], [], []
    for step in range(n_steps):
        stage = stage_by_multiplier[safe_reach, step]
        # The curves are defined below floor level because water can sit in a
        # crawlspace without reaching the boards. That only means anything when there
        # is water: at the ground, `stage - HAND` has to be positive first. Without
        # this gate a channel-side building was charged 2.7% of its value at zero
        # discharge, on an empty channel.
        on_the_ground = stage - hand_m
        above_floor = np.where(
            known & (on_the_ground > 0.0), on_the_ground - foundation_m, NO_WATER
        )

        result = estimate_damage(
            above_floor,
            floor_area_m2,
            building_class,
            storeys=storeys,
            config=damage_config,
            curves=curves,
            structure_value=structure_value,
            contents_value=contents_value,
            contents_curves=contents_curves,
            cap_storeys=cap_storeys,
            lookup=lookup,
            contents_lookup=contents_lookup,
            class_index=index,
            contents_index=contents_index,
        )
        if step in wanted:
            per_building[wanted[step]] = result.per_building.copy()
        wet = above_floor > 0.0
        totals.append(result.total)
        contents.append(result.contents_total)
        structure.append(result.total - result.contents_total)
        counts.append(int(wet.sum()))
        people.append(float(residents[wet].sum()) if residents is not None else 0.0)

    return DamageLadder(
        multipliers=tuple(float(m) for m in multipliers),
        discharge_cms=tuple(float(m) * base_discharge_cms for m in multipliers),
        damage=tuple(totals),
        structure=tuple(structure),
        contents=tuple(contents),
        inundated=tuple(counts),
        residents=tuple(people),
        per_building=per_building,
    )
