"""Flat resolution (Barnes, Lehman & Mutlu 2014b).

A depression filled with `fill_epsilon = 0` becomes a perfectly flat surface, and
a cell in the middle of one has no strictly lower neighbour at all, so D8 has
nothing to point at. `flowdir` reports those cells as `FLOW_FLAT` rather than
inventing a direction, and everything upstream of them dead-ends: on the rough
synthetic catchment that stranded 92% of the grid.

This module gives each flat an artificial drainage gradient, so the flat cells get
real directions and the water gets out. The DEM itself is never modified — the
gradient lives in a separate integer field — so elevations, and therefore HAND and
every depth derived from it, stay exactly as filling left them.

The method
----------
Within each flat, two breadth-first distances are computed:

* `d_high` — steps away from the cells that touch *higher* ground. Water should
  run away from those.
* `d_low` — steps towards the cells that touch *lower* ground. Those are the
  flat's spill points, and water should run towards them.

They combine into a dimensionless surface::

    flat_mask = (flat_height - d_high) + 2 * d_low

where `flat_height` is the largest `d_high` in that flat. The factor of two is the
part that makes this work rather than merely look plausible: stepping towards a
spill point changes `2 * d_low` by exactly 2, while `(flat_height - d_high)` can
change by at most 1, so the sum strictly decreases along that step. Every flat
cell is therefore guaranteed a strictly lower neighbour on `flat_mask`, which is
what makes the resulting directions provably acyclic.

Directions on flat cells are then assigned by steepest descent on `flat_mask`,
with the same lowest-ESRI-code tie-break the elevation-based routing uses.

An alternative to all of this is filling with `fill_epsilon > 0`, which perturbs
the elevations instead. That is cheaper and still supported, but it makes the DEM
carry the artefact: HAND then inherits a few millimetres of fictional relief per
flat cell. Resolving flats separately keeps the artefact out of the elevations.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import numpy.typing as npt
from numba import njit

from floodline.core.terrain.flowdir import (
    _D8_DCOL,
    _D8_DROW,
    D8_CODES,
    FLOW_FLAT,
    _step_distances,
)

__all__ = ["FlatResolution", "resolve_flats"]


@dataclass(frozen=True, slots=True)
class FlatResolution:
    """Flow directions with flats resolved, and what it took."""

    flowdir: npt.NDArray[np.int16]
    """Direction codes, with `FLOW_FLAT` replaced wherever a route was found."""

    flat_mask: npt.NDArray[np.int32]
    """The artificial gradient, zero outside flats. Diagnostic; not an elevation."""

    n_flat_cells: int
    """Flat cells present before resolution."""

    n_resolved: int
    """Flat cells that came out with a real direction."""

    @property
    def n_unresolved(self) -> int:
        """Flat cells still without a direction: a flat with no spill point at all."""
        return self.n_flat_cells - self.n_resolved


@njit(cache=True, nogil=True)
def _build_flat_mask(
    dem: npt.NDArray[np.floating],
    flowdir: npt.NDArray[np.int16],
    valid: npt.NDArray[np.bool_],
    rows: int,
    cols: int,
    drow: npt.NDArray[np.int64],
    dcol: npt.NDArray[np.int64],
) -> npt.NDArray[np.int32]:
    """Return the combined away-from-higher / towards-lower gradient over all flats.

    Both traversals are breadth-first over cells at equal elevation, so a "flat"
    here is a connected run of equal-elevation cells — which is what the filled
    surface of a depression is.
    """
    n_cells = rows * cols
    n_off = drow.shape[0]

    is_flat = np.zeros(n_cells, dtype=np.bool_)
    for cell in range(n_cells):
        if valid[cell] and flowdir[cell] == FLOW_FLAT:
            is_flat[cell] = True

    d_high = np.zeros(n_cells, dtype=np.int32)
    d_low = np.zeros(n_cells, dtype=np.int32)
    seen = np.zeros(n_cells, dtype=np.bool_)
    queue = np.empty(n_cells, dtype=np.int64)

    # Pass 1: distance away from cells that touch higher ground.
    head = 0
    tail = 0
    for cell in range(n_cells):
        if not is_flat[cell]:
            continue
        row = cell // cols
        col = cell - row * cols
        for k in range(n_off):
            n_row = row + drow[k]
            n_col = col + dcol[k]
            if n_row < 0 or n_row >= rows or n_col < 0 or n_col >= cols:
                continue
            neighbour = n_row * cols + n_col
            if valid[neighbour] and dem[neighbour] > dem[cell]:
                seen[cell] = True
                d_high[cell] = 1
                queue[tail] = cell
                tail += 1
                break

    while head < tail:
        cell = queue[head]
        head += 1
        row = cell // cols
        col = cell - row * cols
        for k in range(n_off):
            n_row = row + drow[k]
            n_col = col + dcol[k]
            if n_row < 0 or n_row >= rows or n_col < 0 or n_col >= cols:
                continue
            neighbour = n_row * cols + n_col
            if seen[neighbour] or not is_flat[neighbour]:
                continue
            if dem[neighbour] != dem[cell]:
                continue
            seen[neighbour] = True
            d_high[neighbour] = d_high[cell] + 1
            queue[tail] = neighbour
            tail += 1

    flat_height = np.zeros(n_cells, dtype=np.int32)
    peak = np.int32(0)
    for cell in range(n_cells):
        if is_flat[cell] and d_high[cell] > peak:
            peak = d_high[cell]
    for cell in range(n_cells):
        if is_flat[cell]:
            flat_height[cell] = peak

    # Pass 2: distance towards cells that touch lower ground, i.e. the spill points.
    seen[:] = False
    head = 0
    tail = 0
    for cell in range(n_cells):
        if not is_flat[cell]:
            continue
        row = cell // cols
        col = cell - row * cols
        for k in range(n_off):
            n_row = row + drow[k]
            n_col = col + dcol[k]
            if n_row < 0 or n_row >= rows or n_col < 0 or n_col >= cols:
                continue
            neighbour = n_row * cols + n_col
            # A neighbour that is out of the flat and no higher is a way out:
            # either lower ground, or an equal-elevation cell that already has a
            # direction of its own.
            if valid[neighbour] and not is_flat[neighbour] and dem[neighbour] <= dem[cell]:
                seen[cell] = True
                d_low[cell] = 1
                queue[tail] = cell
                tail += 1
                break

    while head < tail:
        cell = queue[head]
        head += 1
        row = cell // cols
        col = cell - row * cols
        for k in range(n_off):
            n_row = row + drow[k]
            n_col = col + dcol[k]
            if n_row < 0 or n_row >= rows or n_col < 0 or n_col >= cols:
                continue
            neighbour = n_row * cols + n_col
            if seen[neighbour] or not is_flat[neighbour]:
                continue
            if dem[neighbour] != dem[cell]:
                continue
            seen[neighbour] = True
            d_low[neighbour] = d_low[cell] + 1
            queue[tail] = neighbour
            tail += 1

    # Combine. Cells nearer a spill point score lower; the factor of two makes the
    # towards-lower term dominate, which is what guarantees a strict descent.
    result = np.zeros(n_cells, dtype=np.int32)
    for cell in range(n_cells):
        if not is_flat[cell]:
            continue
        if d_low[cell] == 0:
            # Unreachable from any spill point: a flat with no way out at all.
            result[cell] = np.int32(0)
            continue
        result[cell] = (flat_height[cell] - d_high[cell]) + 2 * d_low[cell]
    return result


@njit(cache=True, nogil=True)
def _route_flats(
    dem: npt.NDArray[np.floating],
    flowdir: npt.NDArray[np.int16],
    flat_mask: npt.NDArray[np.int32],
    valid: npt.NDArray[np.bool_],
    rows: int,
    cols: int,
    codes: npt.NDArray[np.int16],
    drow: npt.NDArray[np.int64],
    dcol: npt.NDArray[np.int64],
    distances: npt.NDArray[np.float64],
) -> tuple[npt.NDArray[np.int16], int]:
    """Assign directions on flat cells by steepest descent on `flat_mask`."""
    out = flowdir.copy()
    resolved = 0

    for row in range(rows):
        for col in range(cols):
            cell = row * cols + col
            if not valid[cell] or flowdir[cell] != FLOW_FLAT or flat_mask[cell] == 0:
                continue

            best_slope = 0.0
            best_code = np.int16(0)
            for k in range(codes.shape[0]):
                n_row = row + drow[k]
                n_col = col + dcol[k]
                if n_row < 0 or n_row >= rows or n_col < 0 or n_col >= cols:
                    continue
                neighbour = n_row * cols + n_col
                if not valid[neighbour]:
                    continue

                if flat_mask[neighbour] != 0 and dem[neighbour] == dem[cell]:
                    # Same flat: compare artificial gradients.
                    other = flat_mask[neighbour]
                elif dem[neighbour] <= dem[cell]:
                    # Outside the flat and no higher: a genuine way out, which
                    # scores better than anything inside the flat.
                    other = np.int32(0)
                else:
                    # Higher ground. Routing into it would send water uphill, and
                    # since that cell's own direction points back downhill it would
                    # close a cycle -- which is exactly what happened before this
                    # guard existed.
                    continue

                drop = flat_mask[cell] - other
                if drop <= 0:
                    continue
                slope = drop / distances[k]
                if slope > best_slope:
                    best_slope = slope
                    best_code = codes[k]

            if best_code != 0:
                out[cell] = best_code
                resolved += 1

    return out, resolved


def resolve_flats(
    filled_dem: npt.NDArray[np.floating],
    flowdir: npt.NDArray[np.int16],
    *,
    nodata: float | None = None,
    cellsize: tuple[float, float] = (1.0, 1.0),
) -> FlatResolution:
    """Give flat cells a drainage direction without touching the elevations.

    Parameters
    ----------
    filled_dem
        The conditioned DEM the flow directions came from.
    flowdir
        D8 direction codes, with `FLOW_FLAT` where routing was undefined.
    nodata
        An additional sentinel treated as nodata, on top of NaN.
    cellsize
        (x, y) cell size, used so diagonal steps across a flat are ranked by true
        distance exactly as they are on real terrain.

    Returns
    -------
    FlatResolution
    """
    if filled_dem.shape != flowdir.shape:
        raise ValueError(
            f"filled_dem shape {filled_dem.shape} does not match flowdir {flowdir.shape}"
        )

    rows, cols = filled_dem.shape
    elevation = np.ascontiguousarray(filled_dem, dtype=np.float64).ravel()
    codes = np.ascontiguousarray(flowdir, dtype=np.int16).ravel()
    valid = np.isfinite(elevation)
    if nodata is not None and np.isfinite(nodata):
        valid &= elevation != nodata

    n_flat = int(((codes == FLOW_FLAT) & valid).sum())
    if n_flat == 0:
        return FlatResolution(
            flowdir=flowdir.copy(),
            flat_mask=np.zeros((rows, cols), dtype=np.int32),
            n_flat_cells=0,
            n_resolved=0,
        )

    flat_mask = _build_flat_mask(elevation, codes, valid, rows, cols, _D8_DROW, _D8_DCOL)
    routed, resolved = _route_flats(
        elevation,
        codes,
        flat_mask,
        valid,
        rows,
        cols,
        D8_CODES,
        _D8_DROW,
        _D8_DCOL,
        _step_distances(cellsize),
    )
    return FlatResolution(
        flowdir=routed.reshape(rows, cols),
        flat_mask=flat_mask.reshape(rows, cols),
        n_flat_cells=n_flat,
        n_resolved=resolved,
    )
