"""Synthetic catchment generator used by the test fixtures and `floodline synth`.

The surface is a tilted plane draining to the bottom edge, with a meandering valley
carved into it and a handful of Gaussian pits punched in. That is enough structure to
exercise every terrain invariant: the plane guarantees a drainage direction, the
valley gives flow accumulation somewhere to concentrate, and the pits give the
depression filler something to actually fill.

Everything is deterministic given `seed`, so a failing property test reproduces.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import numpy.typing as npt
from pyproj import CRS
from rasterio.transform import Affine, from_origin

from floodline.io.raster import Raster

__all__ = ["Pit", "SyntheticCatchment", "make_synthetic_catchment"]

DEFAULT_CRS_EPSG = 7856
"""GDA2020 / MGA zone 56 — the analysis CRS for the Lismore case."""


@dataclass(frozen=True, slots=True)
class Pit:
    """A Gaussian depression punched into the synthetic surface.

    `row`/`col` are the centre of the Gaussian. `sink_row`/`sink_col` are the cell
    that actually ends up lowest, which the background slope pulls a cell or two
    downhill of the centre. Terrain tests want the sink; the generator wants the
    centre. Both are recorded rather than conflated.
    """

    row: int
    col: int
    depth_m: float
    radius_cells: float
    sink_row: int = -1
    sink_col: int = -1

    @property
    def sink(self) -> tuple[int, int]:
        """Return the (row, col) of the lowest cell in this depression."""
        if self.sink_row < 0:
            raise ValueError("pit sink has not been located yet")
        return self.sink_row, self.sink_col


@dataclass(frozen=True, slots=True)
class SyntheticCatchment:
    """A synthetic DEM plus the ground truth used to write assertions against."""

    dem: npt.NDArray[np.float32]
    transform: Affine
    crs: CRS
    nodata: float
    cellsize: float
    pits: tuple[Pit, ...]
    channel_cols: npt.NDArray[np.int64]
    """For each row, the column index of the valley thalweg."""
    nodata_mask: npt.NDArray[np.bool_] = field(repr=False)

    @property
    def shape(self) -> tuple[int, int]:
        """Return (rows, cols)."""
        rows, cols = self.dem.shape
        return int(rows), int(cols)

    @property
    def sinks(self) -> tuple[tuple[int, int], ...]:
        """Return the (row, col) of the lowest cell of every pit."""
        return tuple(pit.sink for pit in self.pits)

    @property
    def valid_mask(self) -> npt.NDArray[np.bool_]:
        """Return True where the DEM holds real elevations."""
        return ~self.nodata_mask

    def as_raster(self) -> Raster:
        """Return the DEM wrapped as a `Raster` ready for `write_cog`."""
        return Raster(data=self.dem, transform=self.transform, crs=self.crs, nodata=self.nodata)

    def channel_mask(self, half_width_cells: int = 0) -> npt.NDArray[np.bool_]:
        """Return a boolean mask of the thalweg, optionally widened either side."""
        rows, cols = self.shape
        mask = np.zeros((rows, cols), dtype=np.bool_)
        for row in range(rows):
            lo = max(0, int(self.channel_cols[row]) - half_width_cells)
            hi = min(cols, int(self.channel_cols[row]) + half_width_cells + 1)
            mask[row, lo:hi] = True
        return mask & self.valid_mask


def make_synthetic_catchment(
    *,
    rows: int = 120,
    cols: int = 90,
    cellsize: float = 5.0,
    base_elevation_m: float = 100.0,
    slope: float = 0.02,
    valley_depth_m: float = 12.0,
    valley_width_cells: float = 6.0,
    meander_amplitude_cells: float = 12.0,
    meander_wavelength_rows: float = 60.0,
    n_pits: int = 5,
    pit_depth_m: tuple[float, float] = (1.0, 4.0),
    pit_radius_cells: tuple[float, float] = (2.5, 5.0),
    pit_min_relief_m: float = 0.25,
    roughness_m: float = 0.0,
    nodata_border_cells: int = 0,
    nodata: float = -9999.0,
    origin_xy: tuple[float, float] = (500_000.0, 6_800_000.0),
    epsg: int = DEFAULT_CRS_EPSG,
    seed: int = 0,
) -> SyntheticCatchment:
    """Build a synthetic catchment: tilted plane, carved meandering valley, a few pits.

    Parameters
    ----------
    rows, cols
        Grid size in cells.
    cellsize
        Cell size in metres, used for both axes.
    base_elevation_m
        Elevation of the plane at the outlet (bottom) edge, before the valley is cut.
    slope
        Downslope gradient (rise/run) of the plane towards the bottom edge.
    valley_depth_m
        Depth of the carved valley at the thalweg.
    valley_width_cells
        Gaussian half-width of the valley cross-section, in cells.
    meander_amplitude_cells, meander_wavelength_rows
        Sinusoidal wander of the thalweg about the grid centre.
    n_pits
        Number of Gaussian depressions to punch in, away from the thalweg.
    pit_depth_m, pit_radius_cells
        Uniform ranges the pit depth and radius are drawn from.
    pit_min_relief_m
        Each pit is deepened, if the sampled depth is not enough, until its centre
        sits at least this far below the surface ringing it. Without this a shallow
        pit on a steep plane is not a closed depression at all, and the fixture
        would not exercise the depression filler.
    roughness_m
        Standard deviation of optional white noise added to the surface. Zero by
        default so the pit count is exactly `n_pits`.
    nodata_border_cells
        Width of a nodata frame around the grid. Zero by default.
    nodata
        Value written into nodata cells.
    origin_xy
        Upper-left corner of the grid in CRS coordinates.
    epsg
        Projected CRS. Must be in metres; the raster writer refuses anything else.
    seed
        Seed for pit placement and roughness.

    Returns
    -------
    SyntheticCatchment
    """
    if rows < 3 or cols < 3:
        raise ValueError(f"grid must be at least 3x3, got {rows}x{cols}")
    if cellsize <= 0:
        raise ValueError(f"cellsize must be positive, got {cellsize}")
    if nodata_border_cells * 2 >= min(rows, cols):
        raise ValueError("nodata border consumes the whole grid")
    if pit_depth_m[0] > pit_depth_m[1] or pit_radius_cells[0] > pit_radius_cells[1]:
        raise ValueError("range parameters must be (low, high)")

    rng = np.random.default_rng(seed)
    row_idx, col_idx = np.mgrid[0:rows, 0:cols].astype(np.float64)

    # Tilted plane: highest at row 0, draining to the bottom edge.
    dem = base_elevation_m + slope * cellsize * (rows - 1 - row_idx)

    # Meandering thalweg, and a Gaussian valley cut around it.
    centre = (cols - 1) / 2.0
    thalweg = centre + meander_amplitude_cells * np.sin(
        2.0 * np.pi * np.arange(rows, dtype=np.float64) / meander_wavelength_rows
    )
    thalweg = np.clip(thalweg, 1.0, cols - 2.0)
    across = col_idx - thalweg[:, None]
    dem -= valley_depth_m * np.exp(-0.5 * (across / valley_width_cells) ** 2)

    # Pits, placed clear of the valley so they are genuine closed depressions.
    pits = _place_pits(
        rng=rng,
        rows=rows,
        cols=cols,
        thalweg=thalweg,
        n_pits=n_pits,
        pit_depth_m=pit_depth_m,
        pit_radius_cells=pit_radius_cells,
        clearance=2.5 * valley_width_cells,
    )
    if pits:
        pits = _deepen_until_closed(
            surface=dem,
            row_idx=row_idx,
            col_idx=col_idx,
            pits=pits,
            min_relief_m=pit_min_relief_m,
        )
        dem = dem - _pit_field(row_idx, col_idx, pits)
        pits = _locate_sinks(dem, pits)

    if roughness_m > 0:
        dem += rng.normal(0.0, roughness_m, size=dem.shape)

    nodata_mask = np.zeros((rows, cols), dtype=np.bool_)
    if nodata_border_cells > 0:
        band = nodata_border_cells
        nodata_mask[:band, :] = True
        nodata_mask[-band:, :] = True
        nodata_mask[:, :band] = True
        nodata_mask[:, -band:] = True
        dem[nodata_mask] = nodata

    x0, y0 = origin_xy
    return SyntheticCatchment(
        dem=np.ascontiguousarray(dem, dtype=np.float32),
        transform=from_origin(x0, y0, cellsize, cellsize),
        crs=CRS.from_epsg(epsg),
        nodata=nodata,
        cellsize=cellsize,
        pits=tuple(pits),
        channel_cols=np.rint(thalweg).astype(np.int64),
        nodata_mask=nodata_mask,
    )


def _pit_field(
    row_idx: npt.NDArray[np.float64],
    col_idx: npt.NDArray[np.float64],
    pits: tuple[Pit, ...],
) -> npt.NDArray[np.float64]:
    """Return the summed Gaussian depression field for `pits`."""
    field_ = np.zeros(row_idx.shape, dtype=np.float64)
    for pit in pits:
        dist2 = (row_idx - pit.row) ** 2 + (col_idx - pit.col) ** 2
        field_ += pit.depth_m * np.exp(-0.5 * dist2 / pit.radius_cells**2)
    return field_


def _place_pits(
    *,
    rng: np.random.Generator,
    rows: int,
    cols: int,
    thalweg: npt.NDArray[np.float64],
    n_pits: int,
    pit_depth_m: tuple[float, float],
    pit_radius_cells: tuple[float, float],
    clearance: float,
) -> tuple[Pit, ...]:
    """Sample `n_pits` non-overlapping pit centres clear of the thalweg and the edges."""
    if n_pits <= 0:
        return ()
    margin = int(np.ceil(pit_radius_cells[1] * 2.0)) + 1
    margin = min(margin, (rows - 1) // 2 - 1, (cols - 1) // 2 - 1)
    if margin < 2:
        raise ValueError(
            f"grid {rows}x{cols} is too small to hold pits of radius up to "
            f"{pit_radius_cells[1]} cells; enlarge the grid or pass n_pits=0"
        )

    pits: list[Pit] = []
    for _ in range(200 * n_pits):
        if len(pits) == n_pits:
            break
        pit_row = int(rng.integers(margin, rows - margin))
        pit_col = int(rng.integers(margin, cols - margin))
        if abs(pit_col - thalweg[pit_row]) < clearance:
            continue
        if any(
            abs(pit_row - p.row) < 3 * p.radius_cells and abs(pit_col - p.col) < 3 * p.radius_cells
            for p in pits
        ):
            continue
        pits.append(
            Pit(
                row=pit_row,
                col=pit_col,
                depth_m=float(rng.uniform(*pit_depth_m)),
                radius_cells=float(rng.uniform(*pit_radius_cells)),
            )
        )
    return tuple(pits)


def _deepen_until_closed(
    *,
    surface: npt.NDArray[np.float64],
    row_idx: npt.NDArray[np.float64],
    col_idx: npt.NDArray[np.float64],
    pits: tuple[Pit, ...],
    min_relief_m: float,
    max_passes: int = 12,
    tol_m: float = 1e-3,
) -> tuple[Pit, ...]:
    """Return `pits` with depths raised until each is a closed depression.

    A Gaussian of the sampled depth is not necessarily a pit: on a plane steep
    enough, the downslope side of the bowl still drains away. Each pass measures
    the lowest cell on the ring around a pit and deepens the pit by whatever it
    falls short of sitting `min_relief_m` below that ring.
    """
    rows, cols = surface.shape
    current = list(pits)
    for _ in range(max_passes):
        dem = surface - _pit_field(row_idx, col_idx, tuple(current))
        deficits: list[float] = []
        for pit in current:
            ring_r = max(2, int(np.ceil(2.0 * pit.radius_cells)))
            r0, r1 = max(0, pit.row - ring_r), min(rows, pit.row + ring_r + 1)
            c0, c1 = max(0, pit.col - ring_r), min(cols, pit.col + ring_r + 1)
            block = dem[r0:r1, c0:c1]
            edge = np.concatenate([block[0], block[-1], block[1:-1, 0], block[1:-1, -1]])
            deficits.append(float(dem[pit.row, pit.col] - (edge.min() - min_relief_m)))
        if max(deficits) <= tol_m:
            return tuple(current)
        current = [
            Pit(p.row, p.col, p.depth_m + max(0.0, d + tol_m), p.radius_cells)
            for p, d in zip(current, deficits, strict=True)
        ]
    raise RuntimeError(  # pragma: no cover - guard against a non-converging surface
        "pit deepening did not converge; check slope and pit radius parameters"
    )


def _locate_sinks(dem: npt.NDArray[np.float64], pits: tuple[Pit, ...]) -> tuple[Pit, ...]:
    """Return `pits` with `sink_row`/`sink_col` set to each depression's lowest cell."""
    rows, cols = dem.shape
    located: list[Pit] = []
    for pit in pits:
        reach = max(2, int(np.ceil(pit.radius_cells)))
        r0, r1 = max(0, pit.row - reach), min(rows, pit.row + reach + 1)
        c0, c1 = max(0, pit.col - reach), min(cols, pit.col + reach + 1)
        block = dem[r0:r1, c0:c1]
        local = np.unravel_index(int(np.argmin(block)), block.shape)
        located.append(
            Pit(
                row=pit.row,
                col=pit.col,
                depth_m=pit.depth_m,
                radius_cells=pit.radius_cells,
                sink_row=r0 + int(local[0]),
                sink_col=c0 + int(local[1]),
            )
        )
    return tuple(located)
