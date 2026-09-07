"""The local-inertial solver, checked against things water is known to do."""

from __future__ import annotations

import numpy as np

from floodline.core.hydro.inertial import simulate_inertial


def test_water_conserves_mass() -> None:
    """An explicit scheme leaks when the timestep is too long; this reports it."""
    bed = np.full((30, 30), 10.0)
    bed[14:17, :] = 8.0
    inflow = np.zeros((30, 30))
    inflow[15, 3] = 0.02
    out = simulate_inertial(bed, inflow, cellsize_m=30.0, duration_s=1800.0)
    assert out.mass_error_frac < 0.01, "more than a per cent lost is a bad timestep"
    assert out.wet_cells > 0
    assert out.steps > 0


def test_water_does_not_climb_the_bank() -> None:
    """h_flow over the higher bed is what stops water crossing a wall it cannot reach."""
    bed = np.full((30, 30), 20.0)
    bed[14:17, :] = 8.0  # a channel twelve metres below the plain
    inflow = np.zeros((30, 30))
    inflow[15, 3] = 0.01
    out = simulate_inertial(bed, inflow, cellsize_m=30.0, duration_s=1800.0)
    assert out.max_depth_m < 12.0, "the channel is not full, so nothing should overtop"
    assert out.depth_m[:14].max() < 1e-6, "the plain above the bank stays dry"
    assert out.depth_m[17:].max() < 1e-6


def test_water_runs_downhill() -> None:
    bed = np.tile(np.linspace(20.0, 10.0, 40), (12, 1))
    inflow = np.zeros((12, 40))
    inflow[6, 1] = 0.02
    out = simulate_inertial(bed, inflow, cellsize_m=30.0, duration_s=3600.0)
    upstream = out.depth_m[:, :10].sum()
    downstream = out.depth_m[:, 30:].sum()
    assert downstream > upstream, "a slope should move water down it"


def test_nodata_is_a_wall_not_a_hole() -> None:
    """A NaN neighbour would poison every elevation comparison it touched."""
    bed = np.full((20, 20), 10.0)
    bed[:, 10] = np.nan
    inflow = np.zeros((20, 20))
    inflow[10, 2] = 0.02
    out = simulate_inertial(bed, inflow, cellsize_m=30.0, duration_s=1800.0)
    assert np.isfinite(out.depth_m).all(), "no NaN may escape into the depths"
    assert out.depth_m[:, 10].max() == 0.0, "nodata holds no water"
    assert out.depth_m[:, 11:].max() < 1e-6, "and nothing crosses it"


def test_no_inflow_leaves_the_ground_dry() -> None:
    bed = np.full((15, 15), 5.0)
    out = simulate_inertial(bed, np.zeros((15, 15)), cellsize_m=30.0, duration_s=600.0)
    assert out.wet_cells == 0
    assert out.max_depth_m == 0.0
    assert out.mass_error_frac == 0.0


def test_standing_water_stays_put_on_a_flat_bed() -> None:
    """A flat surface has no gradient, so a still pond must not drift or grow."""
    bed = np.full((20, 20), 5.0)
    start = np.full((20, 20), 0.5)
    out = simulate_inertial(
        bed, np.zeros((20, 20)), cellsize_m=30.0, duration_s=600.0, initial_depth=start
    )
    assert np.allclose(out.depth_m, 0.5, atol=1e-6), "flat water should not move"
