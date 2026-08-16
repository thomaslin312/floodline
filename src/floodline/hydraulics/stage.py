"""Gauge readings to a per-cell stage, in metres above the local drainage.

Three conversions, in order, each of which has bitten someone:

1. **Raw reading to metres on the vertical datum.** Two things the reading cannot
   tell you: its unit and its datum. USGS NWIS reports gauge height in *feet* -- a
   41.9 ft Harvey peak is 12.8 m, and treating it as metres would put three times
   the water over Houston. And the reading is relative to that gauge's own zero,
   which is not recoverable from the reading either. Both
   `require_gauge_reading_unit` and `require_gauge_datum` refuse to guess: a run
   without them stops, rather than placing the whole flood at the wrong elevation.
2. **Datum elevation to depth above the channel bed.** The HAND model floods a cell when its
   height above the nearest drainage is below the water depth *in the channel*, not
   below an absolute elevation. So the gauge's AHD stage is turned into a depth by
   subtracting the conditioned elevation of the channel cell the gauge sits on.
3. **One gauge to every reach.** The default is `constant`: the same depth applies
   everywhere, which is the plain HAND assumption — a water surface parallel to the
   drainage line. `slope` adjusts it along the network by
   `water_surface_slope`, which is a screening approximation, not a backwater
   calculation.

The limits of step 3 are the method's headline limitation and belong in the
write-up: one gauge is not one stage for a whole reach, and neither option here
represents a levee, a culvert, or a genuine backwater.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import numpy.typing as npt
from numba import njit

from floodline.config import Config, HydraulicsConfig, StageMethod
from floodline.hydraulics.rating import RatingCurve
from floodline.terrain.flowdir import D8_CODES, downstream_index

__all__ = [
    "GaugeStage",
    "ReachStages",
    "constant_stage",
    "gauge_reading_to_datum",
    "resolve_gauge",
    "slope_stage",
    "stage_field",
    "stage_field_from_discharge",
]


def _resolve(config: Config | HydraulicsConfig | None) -> HydraulicsConfig:
    """Return the `HydraulicsConfig` to use, defaulting when nothing is supplied."""
    if isinstance(config, Config):
        return config.hydraulics
    return config if config is not None else HydraulicsConfig()


def gauge_reading_to_datum(
    reading: float, *, config: Config | HydraulicsConfig | None = None
) -> float:
    """Convert a raw gauge reading to an elevation in metres on the vertical datum.

    Two conversions, both of which need a value the reading itself cannot supply:
    the unit (NWIS reports feet) and the datum offset (gauge zero to NAVD88 or AHD).
    Both refuse to guess.

    Raises
    ------
    ValueError
        If `gauge_reading_unit` or `gauge_datum_offset_m` has not been set.
    """
    hydraulics = _resolve(config)
    unit = hydraulics.require_gauge_reading_unit()
    return reading * unit.metres + hydraulics.require_gauge_datum()


@dataclass(frozen=True, slots=True)
class GaugeStage:
    """A gauge reading resolved against a specific channel cell."""

    reading: float
    """The raw gauge reading, relative to gauge zero, in its own unit."""

    datum_elevation_m: float
    """The same reading in metres on the vertical datum (NAVD88 here, AHD in AU)."""

    bed_elevation_m: float
    """Conditioned elevation of the channel cell the gauge sits on."""

    depth_m: float
    """Water depth in the channel: the stage the HAND model actually thresholds on."""

    cell: tuple[int, int]
    """(row, col) of the gauge's channel cell."""


def resolve_gauge(
    reading: float,
    filled_dem: npt.NDArray[np.floating],
    gauge_cell: tuple[int, int],
    *,
    config: Config | HydraulicsConfig | None = None,
) -> GaugeStage:
    """Turn a gauge reading into a channel depth at `gauge_cell`.

    Parameters
    ----------
    reading
        Gauge reading, relative to gauge zero, in `hydraulics.gauge_reading_unit`.
    filled_dem
        The conditioned DEM. Using anything else here mixes datums.
    gauge_cell
        (row, col) of the channel cell the gauge sits on. It should be a stream
        cell; a gauge snapped to a hillslope produces a meaningless bed elevation.

    Returns
    -------
    GaugeStage

    Raises
    ------
    ValueError
        If the datum offset is unset, the cell is out of bounds or nodata, or the
        reading is below the channel bed.
    """
    hydraulics = _resolve(config)
    elevation = gauge_reading_to_datum(reading, config=hydraulics)

    row, col = gauge_cell
    rows, cols = filled_dem.shape
    if not (0 <= row < rows and 0 <= col < cols):
        raise ValueError(f"gauge cell {gauge_cell} is outside the raster {filled_dem.shape}")

    bed = float(filled_dem[row, col])
    if not np.isfinite(bed):
        raise ValueError(f"gauge cell {gauge_cell} is nodata in the DEM")

    depth = elevation - bed
    if depth < 0:
        unit = hydraulics.require_gauge_reading_unit().value
        raise ValueError(
            f"gauge reading {reading} {unit} converts to {elevation:.3f} m on the "
            f"vertical datum, which is below the channel bed at {gauge_cell} "
            f"({bed:.3f} m). Check the reading unit, the datum offset, and that the "
            "gauge is snapped to the right cell."
        )
    return GaugeStage(
        reading=reading,
        datum_elevation_m=elevation,
        bed_elevation_m=bed,
        depth_m=depth,
        cell=(row, col),
    )


def constant_stage(shape: tuple[int, int], depth_m: float) -> npt.NDArray[np.float64]:
    """Return a uniform stage field: the plain HAND assumption.

    Every reach gets the gauge's own channel depth. This is what "water surface
    parallel to the drainage line" means in practice.
    """
    if depth_m < 0:
        raise ValueError(f"stage depth must be non-negative, got {depth_m}")
    return np.full(shape, float(depth_m), dtype=np.float64)


@njit(cache=True, nogil=True)
def _network_distance(
    receiver: npt.NDArray[np.int64],
    step_length: npt.NDArray[np.float64],
    codes: npt.NDArray[np.int16],
    flowdir: npt.NDArray[np.int16],
    gauge: int,
) -> npt.NDArray[np.float64]:
    """Signed along-flow distance from every cell to `gauge`.

    Positive upstream of the gauge, negative downstream of it, NaN for cells whose
    flow path never passes through the gauge and which the gauge's path never
    reaches. Memoised: each cell's walk stops as soon as it meets a known answer.
    """
    n_cells = receiver.shape[0]
    distance = np.full(n_cells, np.nan, dtype=np.float64)
    state = np.zeros(n_cells, dtype=np.uint8)  # 0 unknown, 1 on this walk, 2 done
    path = np.empty(n_cells, dtype=np.int64)
    steps = np.empty(n_cells, dtype=np.float64)

    # Downstream of the gauge: follow its own flow path, accumulating negatively.
    distance[gauge] = 0.0
    state[gauge] = 2
    walker = gauge
    running = 0.0
    while True:
        code = flowdir[walker]
        target = receiver[walker]
        if target < 0:
            break
        length = 0.0
        for k in range(codes.shape[0]):
            if codes[k] == code:
                length = step_length[k]
                break
        running -= length
        if state[target] == 2:
            break
        distance[target] = running
        state[target] = 2
        walker = target

    # Upstream: walk each cell down until it meets something already known.
    for start in range(n_cells):
        if state[start] != 0:
            continue
        depth = 0
        cell = start
        base = np.nan
        while True:
            if cell < 0:
                break
            if state[cell] == 2:
                base = distance[cell]
                break
            if state[cell] == 1:
                break
            state[cell] = 1
            path[depth] = cell
            code = flowdir[cell]
            length = 0.0
            for k in range(codes.shape[0]):
                if codes[k] == code:
                    length = step_length[k]
                    break
            steps[depth] = length
            depth += 1
            cell = receiver[cell]

        running = base
        for i in range(depth - 1, -1, -1):
            running = running + steps[i]
            distance[path[i]] = running
            state[path[i]] = 2

    return distance


def slope_stage(
    filled_dem: npt.NDArray[np.floating],
    flowdir: npt.NDArray[np.int16],
    gauge: GaugeStage,
    *,
    config: Config | HydraulicsConfig | None = None,
    cellsize: tuple[float, float] = (1.0, 1.0),
) -> npt.NDArray[np.float64]:
    """Return a stage field adjusted along the network by the water-surface slope.

    The channel depth falls by `water_surface_slope` per metre of along-network
    distance upstream of the gauge, and rises by the same going downstream. It is a
    screening approximation: a real water surface is set by the backwater profile,
    which this does not solve. Cells the gauge's network never touches keep the
    gauge depth unchanged, since there is no distance to adjust by.

    Depths are clipped at zero: far enough upstream the adjustment would go
    negative, which would mean a negative water depth.
    """
    hydraulics = _resolve(config)
    rows, cols = filled_dem.shape
    receiver = np.ascontiguousarray(downstream_index(flowdir)).ravel()

    x_size, y_size = cellsize
    diagonal = float(np.hypot(x_size, y_size))
    step_length = np.array(
        [x_size, diagonal, y_size, diagonal, x_size, diagonal, y_size, diagonal],
        dtype=np.float64,
    )

    distance = _network_distance(
        receiver,
        step_length,
        D8_CODES,
        np.ascontiguousarray(flowdir, dtype=np.int16).ravel(),
        gauge.cell[0] * cols + gauge.cell[1],
    ).reshape(rows, cols)

    adjustment = np.where(np.isfinite(distance), -hydraulics.water_surface_slope * distance, 0.0)
    return np.clip(gauge.depth_m + adjustment, 0.0, None)


def stage_field(
    filled_dem: npt.NDArray[np.floating],
    flowdir: npt.NDArray[np.int16],
    gauge: GaugeStage,
    *,
    config: Config | HydraulicsConfig | None = None,
    cellsize: tuple[float, float] = (1.0, 1.0),
) -> npt.NDArray[np.float64]:
    """Return the per-cell stage field selected by `hydraulics.stage_method`."""
    hydraulics = _resolve(config)
    if hydraulics.stage_method is StageMethod.CONSTANT:
        return constant_stage(filled_dem.shape, gauge.depth_m)
    return slope_stage(filled_dem, flowdir, gauge, config=hydraulics, cellsize=cellsize)


@dataclass(frozen=True, slots=True)
class ReachStages:
    """Per-reach stages from a discharge field, and what could not be resolved."""

    stage_m: npt.NDArray[np.float64]
    """Per-cell stage above local drainage, ready for `inundate`."""

    by_reach: dict[int, float]
    """Stage in metres, keyed by reach index."""

    reaches_without_a_curve: int
    """Reaches with no rating curve - too short, or no catchment."""

    reaches_off_the_curve: int
    """Reaches whose discharge exceeded the tabulated maximum, so stage was capped."""


def stage_field_from_discharge(
    reach_of_cell: npt.NDArray[np.int64],
    curves: dict[int, RatingCurve],
    discharge_cms: dict[int, float],
) -> ReachStages:
    """Turn per-reach discharge into a per-cell stage field via the rating curves.

    This is what replaces a single gauge stage applied everywhere. Each reach gets
    the stage its own geometry says carries its own discharge.

    Cells draining to a reach with no discharge, or no curve, get stage zero: the
    model has nothing to say about them, and zero floods nothing, which is the
    honest default. `reaches_without_a_curve` reports how much of the network that
    covers, so silence is visible.
    """
    stage = np.zeros(reach_of_cell.shape, dtype=np.float64)
    by_reach: dict[int, float] = {}
    missing = 0
    capped = 0

    for reach, flow in discharge_cms.items():
        curve = curves.get(reach)
        if curve is None:
            missing += 1
            continue
        if curve.exceeds_curve(flow):
            capped += 1
        by_reach[reach] = curve.stage_for_discharge(flow)

    if by_reach:
        lookup = np.zeros(max(by_reach) + 1, dtype=np.float64)
        for reach, value in by_reach.items():
            lookup[reach] = value
        within = (reach_of_cell >= 0) & (reach_of_cell < lookup.size)
        stage[within] = lookup[reach_of_cell[within]]

    return ReachStages(
        stage_m=stage,
        by_reach=by_reach,
        reaches_without_a_curve=missing,
        reaches_off_the_curve=capped,
    )
