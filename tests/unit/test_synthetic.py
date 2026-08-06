from __future__ import annotations

import numpy as np
import pytest

from floodline.synthetic import make_synthetic_catchment


def test_deterministic_for_a_seed() -> None:
    a = make_synthetic_catchment(rows=50, cols=40, seed=42)
    b = make_synthetic_catchment(rows=50, cols=40, seed=42)
    np.testing.assert_array_equal(a.dem, b.dem)
    assert a.pits == b.pits


def test_different_seeds_move_the_pits() -> None:
    a = make_synthetic_catchment(rows=80, cols=60, seed=1)
    b = make_synthetic_catchment(rows=80, cols=60, seed=2)
    assert a.pits != b.pits


def test_requested_pit_count_is_placed() -> None:
    catchment = make_synthetic_catchment(rows=140, cols=110, n_pits=6, seed=11)
    assert len(catchment.pits) == 6


def test_pit_sinks_are_strict_local_minima() -> None:
    catchment = make_synthetic_catchment(rows=140, cols=110, n_pits=5, seed=0)
    dem = catchment.dem.astype(np.float64)
    for row, col in catchment.sinks:
        centre = dem[row, col]
        ring = dem[row - 1 : row + 2, col - 1 : col + 2]
        assert np.count_nonzero(ring <= centre) == 1  # only the sink itself
        assert centre < ring.max()


def test_pits_are_closed_depressions() -> None:
    """Every pit sits below the surface ringing it: water in it has nowhere to go."""
    catchment = make_synthetic_catchment(rows=140, cols=110, n_pits=5, seed=0)
    dem = catchment.dem.astype(np.float64)
    for pit in catchment.pits:
        reach = int(np.ceil(2 * pit.radius_cells))
        block = dem[
            pit.row - reach : pit.row + reach + 1,
            pit.col - reach : pit.col + reach + 1,
        ]
        edge = np.concatenate([block[0], block[-1], block[1:-1, 0], block[1:-1, -1]])
        assert dem[pit.sink] < edge.min()


def test_shallow_pits_on_a_steep_plane_are_still_pits() -> None:
    """The sampled depth is not trusted; the generator deepens until it closes."""
    catchment = make_synthetic_catchment(
        rows=120, cols=90, slope=0.05, n_pits=3, pit_depth_m=(0.05, 0.1), seed=5
    )
    dem = catchment.dem.astype(np.float64)
    assert all(pit.depth_m > 0.1 for pit in catchment.pits)
    for row, col in catchment.sinks:
        ring = dem[row - 1 : row + 2, col - 1 : col + 2]
        assert np.count_nonzero(ring <= dem[row, col]) == 1


def test_valley_is_the_lowest_thing_in_its_row() -> None:
    catchment = make_synthetic_catchment(rows=100, cols=80, n_pits=0, seed=0)
    dem = catchment.dem
    for row in range(0, 100, 7):
        assert int(np.argmin(dem[row])) == pytest.approx(catchment.channel_cols[row], abs=1)


def test_surface_drains_downhill_along_the_thalweg() -> None:
    catchment = make_synthetic_catchment(rows=100, cols=80, n_pits=0, seed=0)
    thalweg = catchment.dem[np.arange(100), catchment.channel_cols]
    assert np.all(np.diff(thalweg) < 0)


def test_nodata_border() -> None:
    catchment = make_synthetic_catchment(rows=40, cols=40, nodata_border_cells=2, seed=0)
    assert np.all(catchment.dem[:2, :] == catchment.nodata)
    assert np.all(catchment.dem[:, -2:] == catchment.nodata)
    assert catchment.nodata_mask.sum() == 40 * 40 - 36 * 36
    assert catchment.valid_mask.sum() == 36 * 36


def test_channel_mask_widening() -> None:
    catchment = make_synthetic_catchment(rows=30, cols=30, n_pits=0, seed=0)
    assert catchment.channel_mask(0).sum() == 30
    assert catchment.channel_mask(1).sum() == 30 * 3


def test_georeferencing_is_projected_metres() -> None:
    catchment = make_synthetic_catchment(rows=20, cols=20, cellsize=2.5)
    assert catchment.crs.to_epsg() == 6587
    assert not catchment.crs.is_geographic
    assert catchment.as_raster().cellsize == pytest.approx((2.5, 2.5))


@pytest.mark.parametrize(
    "kwargs",
    [
        {"rows": 2},
        {"cellsize": 0.0},
        {"rows": 10, "cols": 10, "nodata_border_cells": 5},
        {"pit_depth_m": (4.0, 1.0)},
    ],
)
def test_bad_parameters_rejected(kwargs: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        make_synthetic_catchment(**kwargs)  # type: ignore[arg-type]


def test_dtype_is_float32_contiguous() -> None:
    catchment = make_synthetic_catchment(rows=20, cols=20)
    assert catchment.dem.dtype == np.float32
    assert catchment.dem.flags["C_CONTIGUOUS"]
