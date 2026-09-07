"""A local-inertial 2D solver, to find out what HAND's assumption costs.

HAND says the water surface parallels the drainage line. It has no backwater, no
floodplain storage, and no routing: water appears at the level its reach implies and
stays there. The residual against surveyed marks has a floor around 1.5 m that the
stage decomposition attributes to HAND and the DEM rather than to stage, and this
module exists to find out how much of that floor is the assumption itself.

The scheme is the local-inertial form of the shallow-water equations - the momentum
equation with the advection term dropped, which is what makes an explicit update stable
at flood-plausible timesteps. Flux between two cells is

    q = (q_prev - g * h_flow * dt * dSurface/dx)
        / (1 + g * dt * n^2 * |q_prev| / h_flow^(7/3))

with `h_flow` the depth over the higher of the two bed elevations, which is what stops
water climbing a wall it cannot reach. Depth then updates by continuity. This is the
standard formulation for floodplain inundation at these scales and it is not novel; the
point of having it here is comparison, not capability.

**This is a diagnostic, not a replacement.** It is explicit and therefore timestep
bound, it has no wetting-and-drying refinements beyond a depth floor, and it is not
wired into the pipeline. Read its output as "what would a real solver say about this
basin", not as a second product.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import numpy.typing as npt
from numba import njit

__all__ = ["InertialResult", "simulate_inertial"]

GRAVITY = 9.80665


@dataclass(frozen=True, slots=True)
class InertialResult:
    """Final depths from a local-inertial run, and what it cost to get them."""

    depth_m: npt.NDArray[np.float64]
    steps: int
    simulated_s: float
    max_depth_m: float
    wet_cells: int
    mass_error_frac: float
    """Relative departure from the volume that was put in. An explicit scheme leaks a
    little; a large number here means the timestep was too long to trust."""


@njit(cache=True, nogil=True)
def _advance(
    bed: npt.NDArray[np.float64],
    depth: npt.NDArray[np.float64],
    qx: npt.NDArray[np.float64],
    qy: npt.NDArray[np.float64],
    inflow: npt.NDArray[np.float64],
    dt: float,
    dx: float,
    manning: float,
    depth_floor: float,
) -> None:
    """One explicit step, in place.

    Fluxes are held on cell faces: `qx[i, j]` is the flux from `(i, j)` to `(i, j+1)`.
    Keeping them between steps is what makes this local-inertial rather than diffusive -
    the previous flux carries the momentum the advection term would otherwise supply.
    """
    rows, cols = bed.shape

    for i in range(rows):
        for j in range(cols - 1):
            left = bed[i, j] + depth[i, j]
            right = bed[i, j + 1] + depth[i, j + 1]
            higher_bed = bed[i, j] if bed[i, j] > bed[i, j + 1] else bed[i, j + 1]
            higher_surface = left if left > right else right
            h_flow = higher_surface - higher_bed
            if h_flow <= depth_floor:
                qx[i, j] = 0.0
                continue
            slope = (right - left) / dx
            previous = qx[i, j]
            numerator = previous - GRAVITY * h_flow * dt * slope
            friction = GRAVITY * dt * manning * manning * abs(previous) / (h_flow ** (7.0 / 3.0))
            qx[i, j] = numerator / (1.0 + friction)

    for i in range(rows - 1):
        for j in range(cols):
            up = bed[i, j] + depth[i, j]
            down = bed[i + 1, j] + depth[i + 1, j]
            higher_bed = bed[i, j] if bed[i, j] > bed[i + 1, j] else bed[i + 1, j]
            higher_surface = up if up > down else down
            h_flow = higher_surface - higher_bed
            if h_flow <= depth_floor:
                qy[i, j] = 0.0
                continue
            slope = (down - up) / dx
            previous = qy[i, j]
            numerator = previous - GRAVITY * h_flow * dt * slope
            friction = GRAVITY * dt * manning * manning * abs(previous) / (h_flow ** (7.0 / 3.0))
            qy[i, j] = numerator / (1.0 + friction)

    # Continuity. Flux is per unit width, so a face carries q * dx and a cell holds
    # depth * dx * dx; the dx cancels to leave dt / dx on the divergence.
    scale = dt / dx
    for i in range(rows):
        for j in range(cols):
            net = inflow[i, j] * dt
            if j > 0:
                net += qx[i, j - 1] * scale
            if j < cols - 1:
                net -= qx[i, j] * scale
            if i > 0:
                net += qy[i - 1, j] * scale
            if i < rows - 1:
                net -= qy[i, j] * scale
            updated = depth[i, j] + net
            # A negative depth is the scheme overshooting a drying cell. Clamp rather
            # than let it propagate; the mass error reports how often this bites.
            depth[i, j] = updated if updated > 0.0 else 0.0


def simulate_inertial(
    bed: npt.NDArray[np.floating],
    inflow_m_per_s: npt.NDArray[np.floating],
    *,
    cellsize_m: float,
    manning_n: float = 0.035,
    duration_s: float = 6 * 3600.0,
    dt_s: float | None = None,
    depth_floor_m: float = 0.005,
    initial_depth: npt.NDArray[np.floating] | None = None,
) -> InertialResult:
    """Route `inflow` over `bed` until `duration_s`, and return the depths it leaves.

    Parameters
    ----------
    bed
        Ground elevation in metres. NaN is treated as a wall: no flux crosses it.
    inflow_m_per_s
        Water added per cell per second, as a depth rate. A discharge spread over the
        channel cells is the usual way to drive this.
    cellsize_m
        Square cells are assumed, which every analysis grid here already is.
    dt_s
        Timestep. Defaults to a Courant-limited estimate from the cell size and the
        deepest water expected; too long and the mass error will say so.

    Returns
    -------
    InertialResult
    """
    surface = np.array(bed, dtype=np.float64)
    wall = ~np.isfinite(surface)
    # A wall has to be high rather than absent: the flux kernel compares elevations, and
    # a NaN would poison every neighbour it touches.
    surface[wall] = np.nanmax(surface) + 1000.0 if np.isfinite(surface).any() else 0.0

    depth = (
        np.zeros_like(surface)
        if initial_depth is None
        else np.array(initial_depth, dtype=np.float64)
    )
    source = np.array(inflow_m_per_s, dtype=np.float64)
    source[wall] = 0.0

    if dt_s is None:
        # Courant condition for the local-inertial scheme, with a safety factor. The
        # celerity uses a nominal deep-water depth so the step is set before any water
        # has arrived to measure.
        dt_s = 0.4 * cellsize_m / np.sqrt(GRAVITY * 5.0)

    qx = np.zeros_like(surface)
    qy = np.zeros_like(surface)
    steps = int(duration_s / dt_s)
    for _ in range(steps):
        _advance(surface, depth, qx, qy, source, dt_s, cellsize_m, manning_n, depth_floor_m)
        depth[wall] = 0.0

    added = float(source.sum() * dt_s * steps)
    held = float(depth.sum())
    return InertialResult(
        depth_m=depth,
        steps=steps,
        simulated_s=dt_s * steps,
        max_depth_m=float(depth.max()) if depth.size else 0.0,
        wet_cells=int((depth > depth_floor_m).sum()),
        mass_error_frac=abs(held - added) / added if added > 0 else 0.0,
    )
