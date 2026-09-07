"""The terrain chain as one call: DEM in, HAND out.

`fill` → `flowdir` → `flats` → `flowacc` → `streams` → `hand`, wired together with
one `Config` and one set of diagnostics. The individual modules stay independent
and separately testable; this is the composition the CLI, the integration test and
the benchmark all need, in one place so they cannot drift apart.

It also lives here rather than in `flowdir` because flat resolution imports from
`flowdir`: putting the composition in either module would make the import circular.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import numpy.typing as npt

from floodline.config import Config, TerrainConfig
from floodline.terrain.bathymetry import BurnedChannel, burn_channel
from floodline.terrain.fill import fill_depressions
from floodline.terrain.flats import resolve_flats
from floodline.terrain.flowacc import FlowAccumulation, flow_accumulation
from floodline.terrain.flowdir import FLOW_FLAT, flow_direction
from floodline.terrain.hand import HandResult, hand
from floodline.terrain.streams import prune_stream_mask, stream_mask

__all__ = ["TerrainChain", "route_terrain"]


@dataclass(frozen=True, slots=True)
class TerrainChain:
    """Every intermediate product of the terrain pipeline, plus its diagnostics."""

    filled: npt.NDArray[np.floating]
    flowdir: npt.NDArray[np.int16]
    accumulation: FlowAccumulation
    streams: npt.NDArray[np.bool_]
    hand: HandResult

    cells_raised_by_fill: int
    flat_cells_before: int
    flat_cells_after: int

    channel_burn: BurnedChannel | None = None
    """What bathymetry removed, or None when it was off. `filled` is already burned."""

    @property
    def drains_completely(self) -> bool:
        """True when every valid cell's water reaches the edge of the data."""
        return self.accumulation.cells_draining_to_flats == 0


def route_terrain(
    dem: npt.NDArray[np.floating],
    *,
    config: Config | TerrainConfig | None = None,
    nodata: float | None = None,
    cellsize: tuple[float, float] = (1.0, 1.0),
    stream_threshold: float | None = None,
) -> TerrainChain:
    """Run the whole terrain chain on `dem`.

    Parameters
    ----------
    dem
        Raw 2-D elevation array. NaN is nodata.
    config
        Supplies `fill_epsilon`, `fill_connectivity`, `resolve_flats`,
        `stream_threshold_cells` and `min_stream_length_cells`.
    nodata
        An additional sentinel treated as nodata, on top of NaN.
    cellsize
        (x, y) cell size in CRS units. Pass `Raster.cellsize`.
    stream_threshold
        Overrides the configured accumulation threshold.

    Returns
    -------
    TerrainChain
    """
    terrain = config.terrain if isinstance(config, Config) else (config or TerrainConfig())
    bathymetry = config.bathymetry if isinstance(config, Config) else None

    filled, raised = fill_depressions(dem, config=terrain, nodata=nodata, return_raised=True)
    directions = flow_direction(filled, config=terrain, nodata=nodata, cellsize=cellsize)
    flat_before = int((directions == FLOW_FLAT).sum())

    if terrain.resolve_flats and flat_before:
        directions = resolve_flats(filled, directions, nodata=nodata, cellsize=cellsize).flowdir
    flat_after = int((directions == FLOW_FLAT).sum())

    accumulated = flow_accumulation(directions)
    channels = prune_stream_mask(
        stream_mask(
            accumulated.accumulation, directions, config=terrain, threshold=stream_threshold
        ),
        directions,
        config=terrain,
    )
    # Burn the channel before HAND, not after. HAND is measured to the drainage cell,
    # so lowering the bed afterwards would leave every height referenced to a bed that
    # no longer exists. Off unless configured, in which case this is a no-op.
    burned = burn_channel(
        filled,
        accumulated.accumulation,
        channels,
        cell_area_m2=abs(cellsize[0] * cellsize[1]),
        cellsize_m=float(min(abs(cellsize[0]), abs(cellsize[1]))),
        config=bathymetry,
    )
    filled = burned.dem

    heights = hand(filled, directions, channels, nodata=nodata)

    return TerrainChain(
        filled=filled,
        flowdir=directions,
        accumulation=accumulated,
        streams=channels,
        hand=heights,
        cells_raised_by_fill=raised,
        flat_cells_before=flat_before,
        flat_cells_after=flat_after,
        channel_burn=burned if burned.n_cells else None,
    )
