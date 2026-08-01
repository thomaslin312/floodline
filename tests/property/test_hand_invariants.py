"""Hypothesis property tests for HAND.

The three invariants the spec names:

* ``HAND >= 0`` everywhere;
* ``HAND == 0`` on stream cells;
* ``HAND == filled[cell] - filled[drainage(cell)]``.

The first only holds because the DEM was filled: elevation is non-increasing
along a flow path on the conditioned surface and nowhere else, so these all run
on a filled DEM and one test pins what happens if you ignore that.
"""

from __future__ import annotations

import numpy as np
import numpy.typing as npt
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from hypothesis.extra import numpy as hnp

from floodline.terrain.fill import fill_depressions
from floodline.terrain.flowacc import flow_accumulation
from floodline.terrain.flowdir import downstream_index, flow_direction
from floodline.terrain.hand import hand
from floodline.terrain.streams import stream_mask

EPSILON = 1e-3

SETTINGS = settings(
    max_examples=120,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)

Chain = tuple[npt.NDArray[np.float64], npt.NDArray[np.int16], npt.NDArray[np.bool_]]


@st.composite
def chains(draw: st.DrawFn) -> Chain:
    """Draw a DEM and return (filled, flowdir, stream_mask) for it."""
    shape = draw(st.tuples(st.integers(4, 14), st.integers(4, 14)))
    dem = draw(
        hnp.arrays(
            dtype=np.float64,
            shape=shape,
            elements=st.floats(-100.0, 900.0, allow_nan=False, allow_infinity=False, width=32),
        )
    )
    if draw(st.booleans()):
        holes = draw(hnp.arrays(dtype=np.bool_, shape=shape, elements=st.booleans()))
        if holes.any() and not holes.all():
            dem = dem.copy()
            dem[holes] = np.nan
    filled = fill_depressions(dem, epsilon=EPSILON)
    fdir = flow_direction(filled)
    acc = flow_accumulation(fdir).accumulation
    threshold = draw(st.integers(1, 20))
    return filled, fdir, stream_mask(acc, fdir, threshold=threshold)


@SETTINGS
@given(chain=chains())
def test_hand_is_never_negative(chain: Chain) -> None:
    filled, fdir, streams = chain
    heights = hand(filled, fdir, streams).hand
    finite = np.isfinite(heights)
    assert np.all(heights[finite] >= 0.0)


@SETTINGS
@given(chain=chains())
def test_hand_is_zero_on_stream_cells(chain: Chain) -> None:
    filled, fdir, streams = chain
    heights = hand(filled, fdir, streams).hand
    assert np.all(heights[streams] == 0.0)


@SETTINGS
@given(chain=chains())
def test_hand_is_the_drop_to_the_reported_drainage_cell(chain: Chain) -> None:
    """The spec's defining identity, checked against the index HAND itself reports."""
    filled, fdir, streams = chain
    result = hand(filled, fdir, streams)
    cols = filled.shape[1]
    for row, col in np.argwhere(result.drainage_index >= 0):
        outlet = divmod(int(result.drainage_index[row, col]), cols)
        assert result.hand[row, col] == filled[row, col] - filled[outlet]


@SETTINGS
@given(chain=chains())
def test_the_drainage_cell_is_the_first_stream_cell_on_the_path(chain: Chain) -> None:
    """Nearest means first-reached, so no stream cell may be skipped over."""
    filled, fdir, streams = chain
    result = hand(filled, fdir, streams)
    receiver = downstream_index(fdir).ravel()
    cols = filled.shape[1]

    for row, col in np.argwhere(np.isfinite(filled)):
        cell = row * cols + col
        expected = -1
        walked = 0
        while cell >= 0 and walked <= filled.size:
            if streams.ravel()[cell]:
                expected = cell
                break
            cell = int(receiver[cell])
            walked += 1
        assert int(result.drainage_index[row, col]) == expected


@SETTINGS
@given(chain=chains())
def test_cells_without_drainage_are_nan_and_counted(chain: Chain) -> None:
    filled, fdir, streams = chain
    result = hand(filled, fdir, streams)
    missing = np.isnan(result.hand) & np.isfinite(filled)
    assert int(missing.sum()) == result.cells_without_drainage
    np.testing.assert_array_equal(missing, np.isfinite(filled) & (result.drainage_index < 0))


@SETTINGS
@given(chain=chains())
def test_hand_never_exceeds_the_relief(chain: Chain) -> None:
    """A drop to the channel cannot be larger than the whole grid's relief."""
    filled, fdir, streams = chain
    heights = hand(filled, fdir, streams).hand
    finite = np.isfinite(heights)
    if finite.any() and np.isfinite(filled).any():
        assert heights[finite].max() <= np.ptp(filled[np.isfinite(filled)]) + 1e-9


@SETTINGS
@given(chain=chains())
def test_hand_decreases_downstream(chain: Chain) -> None:
    """Moving downstream cannot take you further above your own drainage."""
    filled, fdir, streams = chain
    result = hand(filled, fdir, streams)
    receiver = downstream_index(fdir)
    cols = filled.shape[1]
    for row, col in np.argwhere(fdir > 0):
        target = divmod(int(receiver[row, col]), cols)
        here, below = result.hand[row, col], result.hand[target]
        # Same drainage cell means HAND must fall; a different one means the path
        # crossed into another reach and the two are not comparable.
        if (
            np.isfinite(here)
            and np.isfinite(below)
            and result.drainage_index[row, col] == result.drainage_index[target]
        ):
            assert below <= here + 1e-12


@SETTINGS
@given(chain=chains())
def test_a_denser_network_cannot_raise_hand(chain: Chain) -> None:
    """Adding channels can only bring the nearest drainage closer, never further."""
    filled, fdir, streams = chain
    denser = streams.copy()
    denser[np.isfinite(filled)] |= (
        np.random.default_rng(0).random(streams.shape)[np.isfinite(filled)] < 0.1
    )
    base = hand(filled, fdir, streams).hand
    dense = hand(filled, fdir, denser).hand
    both = np.isfinite(base) & np.isfinite(dense)
    assert np.all(dense[both] <= base[both] + 1e-12)
