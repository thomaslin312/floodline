from __future__ import annotations

import numpy as np
import pytest

from floodline.synthetic import SyntheticCatchment, make_synthetic_catchment
from floodline.terrain.fill import fill_depressions
from floodline.terrain.flats import resolve_flats
from floodline.terrain.flowacc import flow_accumulation
from floodline.terrain.flowdir import FLOW_FLAT, flow_direction, steps_to_outlet


def _flat_dem() -> np.ndarray:
    """A bowl with one rim notch, filled flat: the canonical flat problem."""
    dem = np.full((13, 13), 30.0)
    dem[3:10, 3:10] = 10.0
    dem[0:3, 6] = 20.0
    return fill_depressions(dem, epsilon=0.0)


def test_flats_are_resolved() -> None:
    filled = _flat_dem()
    fdir = flow_direction(filled)
    assert (fdir == FLOW_FLAT).any()

    result = resolve_flats(filled, fdir)
    assert result.n_flat_cells > 0
    assert result.n_resolved == result.n_flat_cells
    assert result.n_unresolved == 0
    assert not (result.flowdir == FLOW_FLAT).any()


def test_resolution_introduces_no_cycles() -> None:
    filled = _flat_dem()
    result = resolve_flats(filled, flow_direction(filled))
    steps = steps_to_outlet(result.flowdir)  # raises on a cycle
    assert steps.max() < filled.size


def test_water_leaves_the_flat() -> None:
    filled = _flat_dem()
    fdir = flow_direction(filled)
    before = flow_accumulation(fdir)
    after = flow_accumulation(resolve_flats(filled, fdir).flowdir)

    assert before.cells_draining_to_flats > 0
    assert after.cells_draining_to_flats == 0
    assert after.cells_draining_to_outlets == after.n_valid


def test_elevations_are_never_touched() -> None:
    """The gradient lives in flat_mask, so HAND does not inherit fictional relief."""
    filled = _flat_dem()
    before = filled.copy()
    resolve_flats(filled, flow_direction(filled))
    np.testing.assert_array_equal(filled, before)


def test_non_flat_directions_are_left_alone() -> None:
    filled = _flat_dem()
    fdir = flow_direction(filled)
    result = resolve_flats(filled, fdir)
    unchanged = fdir != FLOW_FLAT
    np.testing.assert_array_equal(result.flowdir[unchanged], fdir[unchanged])


def test_flat_mask_is_zero_outside_flats() -> None:
    filled = _flat_dem()
    fdir = flow_direction(filled)
    result = resolve_flats(filled, fdir)
    assert np.all(result.flat_mask[fdir != FLOW_FLAT] == 0)
    assert np.all(result.flat_mask[fdir == FLOW_FLAT] > 0)


def test_flat_mask_strictly_descends_towards_the_spill() -> None:
    """The factor of two in the gradient is what guarantees this; pin it."""
    filled = _flat_dem()
    fdir = flow_direction(filled)
    result = resolve_flats(filled, fdir)
    for row, col in np.argwhere(fdir == FLOW_FLAT):
        here = result.flat_mask[row, col]
        neighbours = result.flat_mask[max(0, row - 1) : row + 2, max(0, col - 1) : col + 2]
        lower_inside = (neighbours > 0) & (neighbours < here)
        touches_exit = (
            result.flat_mask[max(0, row - 1) : row + 2, max(0, col - 1) : col + 2] == 0
        ).any()
        assert lower_inside.any() or touches_exit


def test_no_flats_is_a_cheap_no_op() -> None:
    dem = np.tile(np.arange(8.0, 0.0, -1.0).reshape(-1, 1), (1, 5))
    fdir = flow_direction(dem)
    result = resolve_flats(dem, fdir)
    assert result.n_flat_cells == 0
    assert result.n_resolved == 0
    np.testing.assert_array_equal(result.flowdir, fdir)
    assert np.all(result.flat_mask == 0)


def test_shape_mismatch_rejected() -> None:
    with pytest.raises(ValueError, match="does not match"):
        resolve_flats(np.zeros((4, 4)), np.zeros((3, 3), dtype=np.int16))


def test_nodata_is_respected() -> None:
    dem = np.full((11, 11), 30.0)
    dem[3:8, 3:8] = 10.0
    dem[0:3, 5] = 20.0
    dem[10, :] = np.nan
    filled = fill_depressions(dem, epsilon=0.0)
    fdir = flow_direction(filled)
    result = resolve_flats(filled, fdir)
    assert np.all(result.flat_mask[10, :] == 0)
    assert result.n_unresolved == 0


@pytest.mark.parametrize(
    "kwargs",
    [
        {"rows": 120, "cols": 90, "n_pits": 5, "seed": 0},
        {"rows": 90, "cols": 70, "n_pits": 4, "roughness_m": 0.3, "seed": 2},
        {"rows": 150, "cols": 110, "n_pits": 7, "roughness_m": 0.5, "seed": 9},
    ],
)
def test_synthetic_catchments_drain_completely(kwargs: dict[str, object]) -> None:
    """Step 4's acceptance test: the flat-drainage count must reach zero."""
    catchment = make_synthetic_catchment(**kwargs)  # type: ignore[arg-type]
    cellsize = (catchment.cellsize,) * 2
    filled = fill_depressions(catchment.dem.astype(np.float64), epsilon=0.0)
    fdir = flow_direction(filled, cellsize=cellsize)

    before = flow_accumulation(fdir)
    assert before.cells_draining_to_flats > 0, "fixture should exhibit the flat problem"

    result = resolve_flats(filled, fdir, cellsize=cellsize)
    after = flow_accumulation(result.flowdir)

    assert result.n_unresolved == 0
    assert after.cells_draining_to_flats == 0
    assert after.cells_draining_to_outlets == after.n_valid
    steps_to_outlet(result.flowdir)


def test_resolution_beats_epsilon_on_elevation_fidelity(
    catchment: SyntheticCatchment,
) -> None:
    """Both drain fully, but only flat resolution leaves the DEM untouched."""
    raw = catchment.dem.astype(np.float64)
    cellsize = (catchment.cellsize,) * 2

    flat_filled = fill_depressions(raw, epsilon=0.0)
    resolved = resolve_flats(
        flat_filled, flow_direction(flat_filled, cellsize=cellsize), cellsize=cellsize
    )
    eps_filled = fill_depressions(raw, epsilon=1e-4)

    assert flow_accumulation(resolved.flowdir).cells_draining_to_flats == 0
    assert (
        flow_accumulation(flow_direction(eps_filled, cellsize=cellsize)).cells_draining_to_flats
        == 0
    )

    # the epsilon surface carries relief the epsilon-free one does not
    assert np.any(eps_filled > flat_filled)
    assert np.array_equal(flat_filled, fill_depressions(raw, epsilon=0.0))
