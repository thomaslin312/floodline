"""Synthetic rating curves from HAND geometry (Zheng et al. 2018).

Why this exists: applying one gauge's stage as a single HAND threshold across a
whole watershed does not work, and measurement said so. Validated against 16
surveyed high-water marks in the Whiteoak Bayou-Buffalo Bayou watershed, the
threshold that would place the water correctly ranges from -0.19 m to 12.32 m
across those 16 points. The best possible *constant* still misses every one of
them by more than a metre. The water surface is simply not a fixed height above
local drainage across a watershed with many independent tributaries.

The fix is to give every reach its own stage, derived from its own geometry:

1. Each reach owns the cells whose flow path reaches the network at that reach.
2. For a trial stage `h`, the cells of that catchment with `HAND < h` are wet, and
   their geometry gives the channel cross-section::

       volume     V(h) = sum over wet cells of (h - HAND) x cell_area
       bed area  SA(h) = sum over wet cells of cell_area
       area       A(h) = V(h)  / reach_length     cross-section
       perimeter  P(h) = SA(h) / reach_length     wetted perimeter
       radius     R(h) = A(h) / P(h)

3. Manning's equation turns that into a discharge::

       Q(h) = (1/n) x A x R^(2/3) x sqrt(S)

   with `S` the reach's bed slope and `n` from config.

Tabulating `h` gives a stage-discharge curve per reach. Inverting it turns an
observed discharge into a reach-specific stage, which is the per-reach threshold
the constant-stage model was missing.

What this is not: a hydraulic model. Manning's equation assumes steady uniform
flow, the cross-section comes from a DEM rather than a survey, and `n` is a guess
for a whole reach. It is a screening rating curve, and its errors are systematic in
`n` and in slope, both of which the Monte Carlo should sample over.
"""

from __future__ import annotations

from dataclasses import dataclass
from itertools import pairwise

import numpy as np
import numpy.typing as npt

from floodline.core.config import Config, HydraulicsConfig

__all__ = [
    "RatingCurve",
    "ReachGeometry",
    "build_rating_curves",
    "discharge_by_area_ratio",
    "reach_catchments",
]


def _resolve(config: Config | HydraulicsConfig | None) -> HydraulicsConfig:
    """Return the `HydraulicsConfig` to use, defaulting when nothing is supplied."""
    if isinstance(config, Config):
        return config.hydraulics
    return config if config is not None else HydraulicsConfig()


@dataclass(frozen=True, slots=True)
class ReachGeometry:
    """The measurable properties of one reach, before any hydraulics is applied."""

    link_id: int
    n_cells: int
    length_m: float
    slope: float
    """Bed slope, floored at `min_reach_slope`."""
    raw_slope: float
    """Slope before the floor, kept so a reach that needed flooring is identifiable."""
    catchment_cells: int
    """Cells whose flow path first reaches the network at this reach."""


@dataclass(frozen=True, slots=True)
class RatingCurve:
    """A stage-discharge relationship for one reach, tabulated from its own geometry."""

    geometry: ReachGeometry
    stage_m: npt.NDArray[np.float64]
    discharge_cms: npt.NDArray[np.float64]
    area_m2: npt.NDArray[np.float64]
    hydraulic_radius_m: npt.NDArray[np.float64]

    @property
    def max_discharge_cms(self) -> float:
        """Discharge at the top of the tabulated curve."""
        return float(self.discharge_cms[-1])

    def stage_for_discharge(self, discharge_cms: float) -> float:
        """Return the stage that carries `discharge_cms`, by linear interpolation.

        Beyond the top of the curve the stage is held at the tabulated maximum
        rather than extrapolated: Manning's equation on a cross-section the DEM
        never saw is not a prediction. `exceeds_curve` reports when that happened.
        """
        if discharge_cms < 0:
            raise ValueError(f"discharge must be non-negative, got {discharge_cms}")
        return float(np.interp(discharge_cms, self.discharge_cms, self.stage_m))

    def exceeds_curve(self, discharge_cms: float) -> bool:
        """Report whether `discharge_cms` is off the top of the tabulated curve."""
        return discharge_cms > self.max_discharge_cms


def reach_catchments(
    drainage_index: npt.NDArray[np.int64], link_ids: npt.NDArray[np.int64]
) -> npt.NDArray[np.int64]:
    """Map every cell to the reach its flow path first reaches.

    `drainage_index` comes from `terrain.hand` and already answers "which stream
    cell does this cell drain to"; this composes that with "which reach is that
    stream cell part of". Cells with no drainage stay -1.
    """
    if drainage_index.shape != link_ids.shape:
        raise ValueError(
            f"drainage_index shape {drainage_index.shape} does not match link_ids {link_ids.shape}"
        )
    flat_links = link_ids.ravel()
    flat_drain = drainage_index.ravel()
    out = np.full(flat_drain.shape, -1, dtype=np.int64)
    found = flat_drain >= 0
    out[found] = flat_links[flat_drain[found]]
    return out.reshape(drainage_index.shape)


def _reach_length_and_slope(
    cells: list[int],
    filled: npt.NDArray[np.float64],
    cols: int,
    cellsize: tuple[float, float],
) -> tuple[float, float]:
    """Return (length in metres, raw bed slope) for a run of connected cells."""
    x_size, y_size = cellsize
    diagonal = float(np.hypot(x_size, y_size))
    length = 0.0
    for first, second in pairwise(cells):
        row_a, col_a = divmod(first, cols)
        row_b, col_b = divmod(second, cols)
        if row_a != row_b and col_a != col_b:
            length += diagonal
        elif row_a != row_b:
            length += y_size
        else:
            length += x_size
    if length <= 0.0:
        return 0.0, 0.0
    drop = float(filled[divmod(cells[0], cols)] - filled[divmod(cells[-1], cols)])
    return length, drop / length


def build_rating_curves(
    hand: npt.NDArray[np.floating],
    filled_dem: npt.NDArray[np.floating],
    links: list[list[int]],
    reach_of_cell: npt.NDArray[np.int64],
    *,
    config: Config | HydraulicsConfig | None = None,
    cellsize: tuple[float, float] = (1.0, 1.0),
) -> dict[int, RatingCurve]:
    """Build a synthetic rating curve for every reach long enough to have geometry.

    Parameters
    ----------
    hand
        Height above nearest drainage, from `terrain.hand`.
    filled_dem
        The conditioned DEM the reaches were derived from, for bed slope.
    links
        Reach partition from `terrain.streams.link_raster`.
    reach_of_cell
        Per-cell reach index from `reach_catchments`.
    config
        Supplies Manning's n, the stage range and step, and the slope and length floors.
    cellsize
        (x, y) cell size in metres.

    Returns
    -------
    dict
        Keyed by link index. Reaches shorter than `min_reach_length_m`, or with no
        catchment, are absent rather than present with a meaningless curve.
    """
    hydraulics = _resolve(config)
    if hand.shape != filled_dem.shape or hand.shape != reach_of_cell.shape:
        raise ValueError("hand, filled_dem and reach_of_cell must have the same shape")

    cols = hand.shape[1]
    cell_area = float(cellsize[0] * cellsize[1])
    elevation = np.ascontiguousarray(filled_dem, dtype=np.float64)
    # The ladder starts at zero. Without a (Q=0, stage=0) point, inverting the curve
    # below its first tabulated discharge clamps to the first *stage* instead, so a
    # reach carrying no water would still report a quarter-metre of it.
    stages = np.arange(
        0.0,
        hydraulics.rating_max_stage_m + hydraulics.rating_stage_step_m,
        hydraulics.rating_stage_step_m,
        dtype=np.float64,
    )

    heights = np.asarray(hand, dtype=np.float64)
    reaches = reach_of_cell.ravel()
    flat_hand = heights.ravel()
    order = np.argsort(reaches, kind="stable")
    sorted_reaches = reaches[order]
    boundaries = np.searchsorted(sorted_reaches, np.arange(len(links) + 1))

    curves: dict[int, RatingCurve] = {}
    for index, cells in enumerate(links):
        length, raw_slope = _reach_length_and_slope(cells, elevation, cols, cellsize)
        if length < hydraulics.min_reach_length_m:
            continue

        members = order[boundaries[index] : boundaries[index + 1]]
        catchment_hand = flat_hand[members]
        catchment_hand = catchment_hand[np.isfinite(catchment_hand)]
        if catchment_hand.size == 0:
            continue

        slope = max(raw_slope, hydraulics.min_reach_slope)
        depths = np.maximum(stages[:, None] - catchment_hand[None, :], 0.0)
        wet = depths > 0.0

        volume = depths.sum(axis=1) * cell_area
        bed_area = wet.sum(axis=1) * cell_area
        area = volume / length
        perimeter = np.where(bed_area > 0.0, bed_area / length, np.nan)
        radius = np.where(perimeter > 0.0, area / perimeter, 0.0)
        discharge = np.where(
            radius > 0.0,
            (1.0 / hydraulics.manning_n) * area * np.power(radius, 2.0 / 3.0) * np.sqrt(slope),
            0.0,
        )
        # Manning's Q is monotone in stage in principle; enforce it so the curve can
        # be inverted by interpolation even where the discrete geometry wobbles.
        discharge = np.maximum.accumulate(np.nan_to_num(discharge))

        curves[index] = RatingCurve(
            geometry=ReachGeometry(
                link_id=index,
                n_cells=len(cells),
                length_m=length,
                slope=slope,
                raw_slope=raw_slope,
                catchment_cells=int(catchment_hand.size),
            ),
            stage_m=stages,
            discharge_cms=discharge,
            area_m2=area,
            hydraulic_radius_m=np.nan_to_num(radius),
        )
    return curves


def discharge_by_area_ratio(
    gauge_discharge_cms: float,
    gauge_area_cells: float,
    links: list[list[int]],
    accumulation: npt.NDArray[np.floating],
    *,
    config: Config | HydraulicsConfig | None = None,
) -> dict[int, float]:
    """Carry a gauged discharge to every reach by drainage-area ratio.

    One gauge measures one point. Every other reach needs a discharge from
    somewhere, and the standard screening assumption is that flow scales with
    contributing area::

        Q_reach = Q_gauge x (A_reach / A_gauge) ** k

    `k = 1` is plain proportionality; regional regressions usually put it between
    0.7 and 1.0, smaller meaning small catchments yield more per unit area. It is a
    real assumption with a real error, `hydraulics.discharge_area_exponent` holds
    it, and the Monte Carlo should sample over it.

    This is still one gauge's information spread over a watershed, so it does not
    represent a storm that hit one tributary and missed another. It does at least
    give each reach a discharge appropriate to its own size, which a single stage
    threshold does not.

    Parameters
    ----------
    gauge_discharge_cms
        Observed discharge at the gauge, in cubic metres per second.
    gauge_area_cells
        Flow accumulation at the gauge's cell, in cells.
    links
        Reach partition from `terrain.streams.link_raster`.
    accumulation
        Flow accumulation raster, in cells.

    Returns
    -------
    dict
        Discharge in cumecs, keyed by reach index.
    """
    hydraulics = _resolve(config)
    if gauge_discharge_cms < 0:
        raise ValueError(f"discharge must be non-negative, got {gauge_discharge_cms}")
    if gauge_area_cells <= 0:
        raise ValueError(
            f"the gauge's contributing area must be positive, got {gauge_area_cells}. "
            "A gauge snapped off the stream network has no catchment."
        )

    flat = np.asarray(accumulation, dtype=np.float64).ravel()
    exponent = hydraulics.discharge_area_exponent

    out: dict[int, float] = {}
    for index, cells in enumerate(links):
        # The reach's own contributing area is the accumulation at its last cell,
        # which is where everything it drains has arrived.
        area = float(flat[cells[-1]])
        if area <= 0:
            continue
        out[index] = gauge_discharge_cms * (area / gauge_area_cells) ** exponent
    return out
