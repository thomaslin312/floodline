"""Hypothesis property tests for flat resolution.

The point of the module is that after it runs, an epsilon-free fill routes as
completely as an epsilon fill does — without the epsilon fill's fictional relief
in the elevations. These check that claim from both ends.
"""

from __future__ import annotations

import numpy as np
import numpy.typing as npt
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from hypothesis.extra import numpy as hnp

from floodline.core.terrain.fill import fill_depressions
from floodline.core.terrain.flats import resolve_flats
from floodline.core.terrain.flowacc import flow_accumulation
from floodline.core.terrain.flowdir import (
    D8_CODES,
    FLOW_FLAT,
    downstream_index,
    flow_direction,
    steps_to_outlet,
)

SETTINGS = settings(
    max_examples=150,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)


@st.composite
def flat_filled_dems(draw: st.DrawFn) -> npt.NDArray[np.float64]:
    """Draw a DEM filled with epsilon = 0, so flats are present to resolve."""
    shape = draw(st.tuples(st.integers(4, 14), st.integers(4, 14)))
    dem = draw(
        hnp.arrays(
            dtype=np.float64,
            shape=shape,
            # A coarse elevation grid makes ties, and therefore flats, common.
            elements=st.integers(0, 12).map(float),
        )
    )
    if draw(st.booleans()):
        holes = draw(hnp.arrays(dtype=np.bool_, shape=shape, elements=st.booleans()))
        if holes.any() and not holes.all():
            dem = dem.copy()
            dem[holes] = np.nan
    return fill_depressions(dem, epsilon=0.0)


@SETTINGS
@given(dem=flat_filled_dems())
def test_every_interior_flat_is_resolved(dem: npt.NDArray[np.float64]) -> None:
    """A flat on a filled DEM always has a spill point, so it always resolves."""
    result = resolve_flats(dem, flow_direction(dem))
    assert result.n_unresolved == 0


@SETTINGS
@given(dem=flat_filled_dems())
def test_resolution_removes_flat_drainage_entirely(dem: npt.NDArray[np.float64]) -> None:
    """Step 4's goal, as a property: the flat-drainage count reaches zero."""
    result = resolve_flats(dem, flow_direction(dem))
    after = flow_accumulation(result.flowdir)
    assert after.cells_draining_to_flats == 0
    assert after.cells_draining_to_outlets == after.n_valid


@SETTINGS
@given(dem=flat_filled_dems())
def test_no_cycles_are_introduced(dem: npt.NDArray[np.float64]) -> None:
    result = resolve_flats(dem, flow_direction(dem))
    steps = steps_to_outlet(result.flowdir)  # raises on a cycle
    assert np.all(steps >= 0)
    assert np.all(steps <= dem.size)


@SETTINGS
@given(dem=flat_filled_dems())
def test_only_flat_cells_change(dem: npt.NDArray[np.float64]) -> None:
    fdir = flow_direction(dem)
    result = resolve_flats(dem, fdir)
    unchanged = fdir != FLOW_FLAT
    np.testing.assert_array_equal(result.flowdir[unchanged], fdir[unchanged])


@SETTINGS
@given(dem=flat_filled_dems())
def test_resolved_directions_never_point_uphill(dem: npt.NDArray[np.float64]) -> None:
    """Across a flat the step is level; it must never be a step up."""
    fdir = flow_direction(dem)
    result = resolve_flats(dem, fdir)
    receiver = downstream_index(result.flowdir)
    cols = dem.shape[1]
    for row, col in np.argwhere(fdir == FLOW_FLAT):
        target = int(receiver[row, col])
        assert target >= 0
        assert dem[divmod(target, cols)] <= dem[row, col]


@SETTINGS
@given(dem=flat_filled_dems())
def test_output_alphabet_is_unchanged(dem: npt.NDArray[np.float64]) -> None:
    result = resolve_flats(dem, flow_direction(dem))
    alphabet = np.concatenate([D8_CODES.astype(np.int64), [0, -1, -2]])
    assert np.all(np.isin(result.flowdir, alphabet))


@SETTINGS
@given(dem=flat_filled_dems())
def test_elevations_are_untouched(dem: npt.NDArray[np.float64]) -> None:
    before = dem.copy()
    resolve_flats(dem, flow_direction(dem))
    np.testing.assert_array_equal(dem, before)


@SETTINGS
@given(dem=flat_filled_dems())
def test_resolution_is_deterministic(dem: npt.NDArray[np.float64]) -> None:
    fdir = flow_direction(dem)
    np.testing.assert_array_equal(
        resolve_flats(dem, fdir).flowdir, resolve_flats(dem, fdir).flowdir
    )
