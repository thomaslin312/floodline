"""Water-surface elevation interpolated between every gauge in a basin.

The model's stage has always come from one gauge: take its peak discharge, transfer it
to every reach by drainage-area ratio, and convert to a stage through a synthetic
rating curve. Two modelled steps sit between an observation and the water level, and
the uncertainty decomposition put stage at 100% of the damage interval - the largest
single term by a factor of six.

A basin usually has more than one gauge, and each of them measured a *stage*, not only
a discharge. `gage_ht` on the annual peak, added to the gauge datum altitude, is an
observed water-surface elevation at a known point on the network. Where two gauges
bracket a reach, the water level between them can be interpolated directly, with no
rating curve and no area ratio in the way.

Distance is measured **along the flow path**, not between coordinates. Two points a
kilometre apart across a meander bend can be ten kilometres apart down the channel, and
the water surface falls with channel distance, not with separation.

Outside the gauged span the interpolation stops and depth is held constant instead:
water-surface elevation is carried up or down from the nearest gauge parallel to the
bed. That is HAND's own assumption applied locally, which is honest about being an
extrapolation rather than pretending the last gauge speaks for the headwaters.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

import numpy as np
import numpy.typing as npt

__all__ = [
    "GaugeStage",
    "InterpolatedStage",
    "interpolate_stage",
    "reach_graph",
    "select_gauges",
]

FEET_TO_M = 0.3048


@dataclass(frozen=True, slots=True)
class GaugeStage:
    """One gauge's observed peak water-surface elevation, placed on the network."""

    site: str
    reach: int
    wse_m: float
    """Gauge datum altitude plus peak gage height, in metres."""

    bed_m: float
    """Model bed elevation at the gauge's reach, for extrapolating depth outward."""

    datum: str
    """Vertical datum of the gauge altitude. NAVD88 and NGVD29 differ by a few tens of
    centimetres in Texas, which is the same size as the error being chased."""

    date: str
    area_km2: float


@dataclass(frozen=True, slots=True)
class InterpolatedStage:
    """A per-reach water level built from observed gauge stages."""

    by_reach: dict[int, float]
    """Stage above the local bed, in metres, ready to compare against HAND."""

    wse_by_reach: dict[int, float]
    """The water-surface elevation each stage came from."""

    interpolated: set[int] = field(default_factory=set)
    """Reaches that sit between two gauges, where the level is genuinely interpolated."""

    extrapolated: set[int] = field(default_factory=set)
    """Reaches outside the gauged span, where depth was carried from the nearest gauge."""

    gauged: set[int] = field(default_factory=set)
    """Reaches holding a gauge, where the level is the observation itself."""

    n_gauges: int = 0
    mixed_datums: bool = False
    """True when the gauges do not agree on a vertical datum, so the interpolation is
    crossing a datum shift it cannot correct."""


def reach_graph(
    reach_of_cell: npt.NDArray[np.int64],
    downstream: npt.NDArray[np.int64],
    cellsize_m: float,
) -> tuple[dict[int, int], dict[int, float]]:
    """Return each reach's downstream neighbour, and its own along-channel length.

    Built from the D8 receiver array rather than from geometry, so the distances are
    the ones water actually travels. A reach whose receiver leaves the raster, or
    flows to itself, has no downstream entry and terminates the walk.
    """
    flat_reach = reach_of_cell.ravel()
    down: dict[int, int] = {}
    length: dict[int, float] = {}
    diagonal = cellsize_m * math.sqrt(2.0)
    cols = reach_of_cell.shape[1]

    for index in np.flatnonzero(flat_reach >= 0):
        here = int(flat_reach[index])
        target = int(downstream[index])
        if target < 0 or target == index:
            continue
        step = diagonal if (abs(target - index) not in (1, cols)) else cellsize_m
        there = int(flat_reach[target])
        if there == here:
            length[here] = length.get(here, 0.0) + step
        elif there >= 0:
            # First crossing out of this reach defines its receiver. A D8 tree gives
            # each reach one outflow, so a later crossing would be the same edge.
            down.setdefault(here, there)
            length[here] = length.get(here, 0.0) + step
    return down, length


def _walk_down(
    start: int, down: dict[int, int], length: dict[int, float]
) -> list[tuple[int, float]]:
    """Reaches downstream of `start`, with along-channel distance to each."""
    out: list[tuple[int, float]] = []
    seen = {start}
    here, travelled = start, 0.0
    while here in down:
        travelled += length.get(here, 0.0)
        here = down[here]
        if here in seen:
            break  # a cycle should not exist in a D8 tree, but never loop forever
        seen.add(here)
        out.append((here, travelled))
    return out


def select_gauges(
    gauges: list[GaugeStage], *, max_depth_m: float = 20.0
) -> tuple[list[GaugeStage], list[str]]:
    """Drop gauges whose stage cannot be reconciled with the model's own bed.

    Two failure modes, both seen in the wild and both catastrophic rather than noisy,
    because one bad level propagates along the whole interpolated span.

    A gauge whose implied depth - its water-surface elevation minus the model bed at
    its reach - is negative or absurd is not measuring the channel this model built.
    Either the published datum altitude is wrong, the station snapped to the wrong
    reach, or the DEM is out by more than the flood. One basin came back with a
    1,232 m residual from a single such record.

    Mixed vertical datums are the second. NAVD88 and NGVD29 differ by a few tens of
    centimetres in the United States, which is the same size as the error being
    chased, and interpolating between two gauges on different datums bakes the offset
    into every reach between them. The majority datum wins and the rest are dropped:
    converting between them properly needs a geoid model this project does not carry,
    and silently mixing them is worse than using fewer gauges.

    Returns the surviving gauges and a line per rejection, for the caller to report.
    """
    notes: list[str] = []
    plausible = []
    for gauge in gauges:
        depth = gauge.wse_m - gauge.bed_m
        if not math.isfinite(depth) or depth < 0.0 or depth > max_depth_m:
            notes.append(
                f"gauge {gauge.site} implies {depth:,.1f} m of water over the model bed, "
                f"outside 0 to {max_depth_m:g} m, so its datum or its snap is wrong"
            )
            continue
        plausible.append(gauge)

    datums = [g.datum for g in plausible if g.datum]
    if len(set(datums)) > 1:
        majority = max(set(datums), key=datums.count)
        dropped = [g.site for g in plausible if g.datum and g.datum != majority]
        plausible = [g for g in plausible if not g.datum or g.datum == majority]
        notes.append(
            f"gauges {', '.join(dropped)} report a different vertical datum from the "
            f"majority ({majority}); mixing datums bakes their offset into every reach "
            "between them, so they are dropped rather than converted"
        )
    return plausible, notes


def interpolate_stage(
    gauges: list[GaugeStage],
    reach_of_cell: npt.NDArray[np.int64],
    downstream: npt.NDArray[np.int64],
    bed_by_reach: dict[int, float],
    *,
    cellsize_m: float,
) -> InterpolatedStage:
    """Build a per-reach water level from observed gauge stages.

    Every reach is classified into one of three cases, and the classification is
    returned so a caller can score them separately. A model that is accurate at its
    gauges and wrong halfway between them is a different problem from one that is
    uniformly wrong, and the only way to tell is to keep the two apart.
    """
    if not gauges:
        return InterpolatedStage(by_reach={}, wse_by_reach={})

    down, length = reach_graph(reach_of_cell, downstream, cellsize_m)
    at_reach = {g.reach: g for g in gauges}

    # For every reach, the nearest gauge downstream along the flow path, and the
    # nearest upstream. Upstream is found by inverting the downstream walks rather
    # than by traversing a reversed tree, which would need the whole branching set.
    nearest_down: dict[int, tuple[float, GaugeStage]] = {}
    nearest_up: dict[int, tuple[float, GaugeStage]] = {}
    reaches = {int(r) for r in np.unique(reach_of_cell) if r >= 0}

    for reach in reaches:
        for other, distance in _walk_down(reach, down, length):
            gauge = at_reach.get(other)
            if gauge is not None:
                nearest_down[reach] = (distance, gauge)
                break
    for gauge in gauges:
        for other, distance in _walk_down(gauge.reach, down, length):
            current = nearest_up.get(other)
            if current is None or distance < current[0]:
                nearest_up[other] = (distance, gauge)

    by_reach: dict[int, float] = {}
    wse_by_reach: dict[int, float] = {}
    interpolated: set[int] = set()
    extrapolated: set[int] = set()
    gauged: set[int] = set()

    for reach in reaches:
        bed = bed_by_reach.get(reach)
        if bed is None:
            continue
        here = at_reach.get(reach)
        if here is not None:
            wse = here.wse_m
            gauged.add(reach)
        else:
            up, low = nearest_up.get(reach), nearest_down.get(reach)
            if up is not None and low is not None:
                # Interpolate *depth*, then add the local bed - not water-surface
                # elevation directly. Elevation between two gauges is dominated by the
                # bed profile, which is not linear in channel distance, so a straight
                # line between two levels floats above the bed in steep reaches and
                # cuts below it in flat ones. On a mountain basin that produced a 37 m
                # residual from two individually plausible gauges. Depth is the smooth
                # quantity: it varies over metres where elevation varies over hundreds.
                d_up, g_up = up
                d_down, g_down = low
                span = d_up + d_down
                weight = d_up / span if span > 0 else 0.0
                depth_up = g_up.wse_m - g_up.bed_m
                depth_down = g_down.wse_m - g_down.bed_m
                wse = bed + depth_up + (depth_down - depth_up) * weight
                interpolated.add(reach)
            elif low is not None or up is not None:
                _, gauge = low if low is not None else up  # type: ignore[misc]
                # Carry depth, not level: the water surface follows the bed outside
                # the gauged span rather than sitting flat across a hillside.
                wse = bed + (gauge.wse_m - gauge.bed_m)
                extrapolated.add(reach)
            else:
                continue
        stage = wse - bed
        if stage <= 0.0:
            stage = 0.0
        by_reach[reach] = stage
        wse_by_reach[reach] = wse

    datums = {g.datum for g in gauges if g.datum}
    return InterpolatedStage(
        by_reach=by_reach,
        wse_by_reach=wse_by_reach,
        interpolated=interpolated,
        extrapolated=extrapolated,
        gauged=gauged,
        n_gauges=len(gauges),
        mixed_datums=len(datums) > 1,
    )


def peak_water_surface(
    site_info: dict[str, Any], peak_row: dict[str, Any]
) -> tuple[float, str] | None:
    """Water-surface elevation in metres from a gauge datum and a peak gage height.

    Returns None when either half is missing, which is common: plenty of peaks carry a
    discharge and no stage, and plenty of sites carry no datum altitude. A partial
    answer here would be a fabricated elevation.
    """
    altitude = (site_info.get("alt_va") or "").strip()
    height = (peak_row.get("gage_ht") or "").strip()
    if not altitude or not height:
        return None
    try:
        wse_ft = float(altitude) + float(height)
    except ValueError:
        return None
    return wse_ft * FEET_TO_M, (site_info.get("alt_datum_cd") or "").strip()
