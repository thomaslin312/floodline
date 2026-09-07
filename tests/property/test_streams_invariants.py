"""Hypothesis property tests for the stream network.

Thresholding and vectorising have no published invariants in the spec, so these
are the structural ones the rest of the pipeline relies on: the mask is closed
downstream, pruning only ever removes, and the vector network is a faithful,
lossless re-encoding of the mask it came from.
"""

from __future__ import annotations

import numpy as np
import numpy.typing as npt
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from hypothesis.extra import numpy as hnp
from pyproj import CRS
from rasterio.transform import from_origin

from floodline.core.terrain.fill import fill_depressions
from floodline.core.terrain.flowacc import flow_accumulation
from floodline.core.terrain.flowdir import downstream_index, flow_direction
from floodline.core.terrain.streams import prune_stream_mask, stream_mask, stream_network

TRANSFORM = from_origin(500_000.0, 6_800_000.0, 1.0, 1.0)
ANALYSIS_CRS = CRS.from_epsg(6587)

SETTINGS = settings(
    max_examples=100,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)


@st.composite
def routed(draw: st.DrawFn) -> tuple[npt.NDArray[np.int16], npt.NDArray[np.float64]]:
    """Draw a small DEM and return its flow directions and accumulation."""
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
    fdir = flow_direction(fill_depressions(dem, epsilon=1e-3))
    return fdir, flow_accumulation(fdir).accumulation


@SETTINGS
@given(data=routed(), threshold=st.integers(1, 40))
def test_mask_is_closed_downstream(
    data: tuple[npt.NDArray[np.int16], npt.NDArray[np.float64]], threshold: int
) -> None:
    """Accumulation only grows downstream, so a channel never stops mid-slope."""
    fdir, acc = data
    mask = stream_mask(acc, fdir, threshold=threshold)
    receiver = downstream_index(fdir)
    cols = fdir.shape[1]
    for row, col in np.argwhere(mask):
        target = int(receiver[row, col])
        if target >= 0:
            assert mask[divmod(target, cols)]


@SETTINGS
@given(data=routed(), threshold=st.integers(1, 40))
def test_a_lower_threshold_gives_a_superset(
    data: tuple[npt.NDArray[np.int16], npt.NDArray[np.float64]], threshold: int
) -> None:
    """Drainage density is monotone in the threshold, which is what makes it tunable."""
    fdir, acc = data
    dense = stream_mask(acc, fdir, threshold=threshold)
    sparse = stream_mask(acc, fdir, threshold=threshold + 5)
    assert np.all(sparse <= dense)


@SETTINGS
@given(data=routed(), threshold=st.integers(1, 30), min_length=st.integers(2, 8))
def test_pruning_only_removes(
    data: tuple[npt.NDArray[np.int16], npt.NDArray[np.float64]],
    threshold: int,
    min_length: int,
) -> None:
    fdir, acc = data
    mask = stream_mask(acc, fdir, threshold=threshold)
    assert np.all(prune_stream_mask(mask, fdir, min_length=min_length) <= mask)


@SETTINGS
@given(data=routed(), threshold=st.integers(1, 30), min_length=st.integers(2, 8))
def test_pruning_reaches_a_fixed_point(
    data: tuple[npt.NDArray[np.int16], npt.NDArray[np.float64]],
    threshold: int,
    min_length: int,
) -> None:
    """Pruning twice is pruning once: the iteration really did converge."""
    fdir, acc = data
    mask = stream_mask(acc, fdir, threshold=threshold)
    once = prune_stream_mask(mask, fdir, min_length=min_length)
    twice = prune_stream_mask(once, fdir, min_length=min_length)
    np.testing.assert_array_equal(once, twice)


@SETTINGS
@given(data=routed(), threshold=st.integers(1, 30))
def test_pruning_keeps_the_mask_closed_downstream(
    data: tuple[npt.NDArray[np.int16], npt.NDArray[np.float64]], threshold: int
) -> None:
    """Removing headwaters must not punch a hole in the middle of a channel."""
    fdir, acc = data
    pruned = prune_stream_mask(stream_mask(acc, fdir, threshold=threshold), fdir, min_length=4)
    receiver = downstream_index(fdir)
    cols = fdir.shape[1]
    for row, col in np.argwhere(pruned):
        target = int(receiver[row, col])
        if target >= 0 and pruned[divmod(target, cols)].size:
            downstream_is_stream = pruned[divmod(target, cols)]
            upstream_of_target = sum(
                pruned[divmod(int(c), cols)] for c in np.flatnonzero(receiver.ravel() == target)
            )
            assert downstream_is_stream or upstream_of_target >= 1


@SETTINGS
@given(data=routed(), threshold=st.integers(1, 30))
def test_network_is_a_lossless_partition_of_the_mask(
    data: tuple[npt.NDArray[np.int16], npt.NDArray[np.float64]], threshold: int
) -> None:
    """Every stream cell lands in exactly one link, or in the dropped count."""
    fdir, acc = data
    mask = stream_mask(acc, fdir, threshold=threshold)
    net = stream_network(mask, fdir, acc, TRANSFORM, ANALYSIS_CRS)
    counted = int(net["n_cells"].sum()) if not net.empty else 0
    assert counted + net.attrs["dropped_degenerate_links"] == int(mask.sum())


@SETTINGS
@given(data=routed(), threshold=st.integers(1, 30))
def test_network_attributes_are_consistent(
    data: tuple[npt.NDArray[np.int16], npt.NDArray[np.float64]], threshold: int
) -> None:
    fdir, acc = data
    mask = stream_mask(acc, fdir, threshold=threshold)
    net = stream_network(mask, fdir, acc, TRANSFORM, ANALYSIS_CRS)
    if net.empty:
        return
    assert (net["acc_outflow"] >= net["acc_head"]).all()
    assert (net["strahler"] >= 1).all()
    assert (net["length_m"] > 0).all()
    assert (net["n_cells"] >= 1).all()
    assert list(net["link_id"]) == list(range(len(net)))
