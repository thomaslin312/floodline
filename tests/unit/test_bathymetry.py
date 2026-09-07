"""The estimated channel bed, and what it does to the surface HAND is measured from."""

from __future__ import annotations

import numpy as np

from floodline.config import BathymetryConfig
from floodline.terrain.bathymetry import burn_channel, channel_depth_m, channel_width_m


def _on(**over: object) -> BathymetryConfig:
    return BathymetryConfig(enabled=True, **over)  # type: ignore[arg-type]


def test_depth_grows_with_drainage_area_and_stops_at_the_cap() -> None:
    cfg = _on(depth_coefficient_m=0.4, depth_exponent=0.25, max_depth_m=3.0)
    d = channel_depth_m([1.0, 100.0, 10_000.0, 1e9], config=cfg)
    assert d[0] < d[1] < d[2], "a bigger catchment carries a deeper channel"
    assert d[3] == 3.0, "a power law has no upper bound; the cap has to supply one"
    # 0.4 * 100^0.25 = 0.4 * 3.1623
    assert np.isclose(d[1], 0.4 * 100.0**0.25)


def test_no_upstream_area_is_not_a_channel() -> None:
    """Callers pass whole rasters, hillslope included, so zero must not raise."""
    cfg = _on()
    assert channel_depth_m([0.0, -1.0], config=cfg).tolist() == [0.0, 0.0]
    assert channel_width_m([0.0, -1.0], config=cfg).tolist() == [0.0, 0.0]


def test_burning_lowers_only_the_channel() -> None:
    dem = np.full((5, 5), 10.0)
    acc = np.zeros((5, 5))
    streams = np.zeros((5, 5), dtype=bool)
    streams[2, :] = True
    acc[2, :] = 1000.0  # 1000 cells of 900 m2 is 0.9 km2

    out = burn_channel(dem, acc, streams, cell_area_m2=900.0, cellsize_m=30.0, config=_on())
    assert out.n_cells == 5
    assert (out.dem[2, :] < 10.0).all(), "the channel is cut"
    assert (out.dem[[0, 1, 3, 4], :] == 10.0).all(), "the hillslope is untouched"
    assert out.max_depth_m > 0 and out.mean_depth_m > 0
    # The input is not modified in place; a caller may still need the unburned surface.
    assert (dem == 10.0).all()


def test_disabled_returns_the_surface_untouched() -> None:
    """Callers always run this and let config decide, so off must be a true no-op."""
    dem = np.full((4, 4), 5.0)
    acc = np.full((4, 4), 500.0)
    streams = np.ones((4, 4), dtype=bool)
    out = burn_channel(
        dem,
        acc,
        streams,
        cell_area_m2=900.0,
        cellsize_m=30.0,
        config=BathymetryConfig(enabled=False),
    )
    assert out.dem is dem
    assert out.n_cells == 0
    assert not out.depth_m.any()


def test_nodata_survives_the_burn() -> None:
    dem = np.full((3, 3), 10.0)
    dem[1, 1] = np.nan
    acc = np.full((3, 3), 800.0)
    streams = np.ones((3, 3), dtype=bool)
    out = burn_channel(dem, acc, streams, cell_area_m2=900.0, cellsize_m=30.0, config=_on())
    assert np.isnan(out.dem[1, 1]), "a hole must not become a real elevation"
    assert np.isfinite(out.dem[0, 0])


def test_a_wide_river_burns_wider_than_one_cell() -> None:
    """A one-cell burn on a river far wider than a cell cuts a slot, not a channel."""
    dem = np.full((7, 7), 20.0)
    acc = np.zeros((7, 7))
    streams = np.zeros((7, 7), dtype=bool)
    streams[3, :] = True
    acc[3, :] = 5_000_000.0  # a very large catchment, so width far exceeds the cell
    narrow = burn_channel(
        dem,
        acc,
        streams,
        cell_area_m2=900.0,
        cellsize_m=30.0,
        config=_on(width_coefficient_m=0.01, width_exponent=0.1),
    )
    wide = burn_channel(
        dem,
        acc,
        streams,
        cell_area_m2=900.0,
        cellsize_m=30.0,
        config=_on(width_coefficient_m=10.0, width_exponent=0.5),
    )
    assert narrow.n_cells == 7, "a sub-cell channel stays one cell across"
    assert wide.n_cells > 7, "a wide river spreads across neighbouring cells"
