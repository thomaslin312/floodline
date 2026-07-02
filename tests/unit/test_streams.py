from __future__ import annotations

import numpy as np
import pytest

from floodline.config import Config
from floodline.synthetic import SyntheticCatchment
from floodline.terrain.fill import fill_depressions
from floodline.terrain.flowacc import flow_accumulation
from floodline.terrain.flowdir import flow_direction
from floodline.terrain.streams import prune_stream_mask, stream_mask, stream_network


def _routed(dem: np.ndarray, cellsize: float = 1.0) -> tuple[np.ndarray, np.ndarray]:
    filled = fill_depressions(dem, epsilon=1e-4)
    fdir = flow_direction(filled, cellsize=(cellsize, cellsize))
    return fdir, flow_accumulation(fdir).accumulation


def test_threshold_selects_the_high_accumulation_cells() -> None:
    dem = np.tile(np.arange(10.0, 0.0, -1.0).reshape(-1, 1), (1, 5))
    fdir, acc = _routed(dem)
    mask = stream_mask(acc, fdir, threshold=5)
    np.testing.assert_array_equal(mask, acc >= 5)
    assert mask[9, :].all()
    assert not mask[0, :].any()


def test_threshold_comes_from_config() -> None:
    dem = np.tile(np.arange(10.0, 0.0, -1.0).reshape(-1, 1), (1, 5))
    fdir, acc = _routed(dem)
    cfg = Config.model_validate({"terrain": {"stream_threshold_cells": 7}})
    np.testing.assert_array_equal(
        stream_mask(acc, fdir, config=cfg), stream_mask(acc, fdir, threshold=7)
    )


def test_nodata_is_never_stream() -> None:
    dem = np.tile(np.arange(8.0, 0.0, -1.0).reshape(-1, 1), (1, 4))
    dem[7, 1] = np.nan
    fdir, acc = _routed(dem)
    assert not stream_mask(acc, fdir, threshold=1)[7, 1]


def test_shape_mismatch_rejected() -> None:
    dem = np.zeros((5, 5))
    fdir, _ = _routed(dem)
    with pytest.raises(ValueError, match="does not match"):
        stream_mask(np.zeros((4, 4)), fdir)


def test_a_stream_mask_is_downstream_closed() -> None:
    """Accumulation only grows downstream, so a channel never stops mid-slope."""
    dem = np.tile(np.arange(12.0, 0.0, -1.0).reshape(-1, 1), (1, 6))
    fdir, acc = _routed(dem)
    mask = stream_mask(acc, fdir, threshold=20)
    from floodline.terrain.flowdir import downstream_index

    receiver = downstream_index(fdir)
    cols = dem.shape[1]
    for row, col in np.argwhere(mask):
        target = int(receiver[row, col])
        if target >= 0:
            assert mask[divmod(target, cols)]


# --- pruning ---------------------------------------------------------------------


def test_pruning_removes_every_short_first_order_branch() -> None:
    """Both branches above a junction are first-order; neither stem is privileged.

    Column 4 runs the length of the grid and a three-cell stub joins it at (2, 4).
    The stub goes, and so do the two cells of column 4 *above* the junction: they
    are a two-cell first-order branch too. That is the intent — the topmost cells
    of any channel are the ones the accumulation threshold is least sure about —
    but it does mean pruning clips headwaters, not just side stubs.
    """
    mask = np.zeros((9, 9), dtype=bool)
    mask[:, 4] = True  # a channel running down column 4
    fdir = np.zeros((9, 9), dtype=np.int16)
    fdir[:, 4] = 4  # south
    mask[2, 1:4] = True  # a stub joining at (2, 4)
    fdir[2, 1:4] = 1  # east

    pruned = prune_stream_mask(mask, fdir, min_length=5)
    assert not pruned[2, 1:4].any(), "the stub should go"
    assert not pruned[0:2, 4].any(), "so should the two-cell headwater above the junction"
    assert pruned[2:, 4].all(), "the seven cells below the junction are long enough"


def test_pruning_keeps_a_long_branch() -> None:
    mask = np.zeros((9, 9), dtype=bool)
    mask[:, 8] = True
    fdir = np.zeros((9, 9), dtype=np.int16)
    fdir[:, 8] = 4
    mask[2, 1:8] = True  # a seven-cell branch
    fdir[2, 1:8] = 1

    pruned = prune_stream_mask(mask, fdir, min_length=5)
    assert pruned[2, 1:8].all()


def test_pruning_is_a_no_op_below_two() -> None:
    mask = np.zeros((5, 5), dtype=bool)
    mask[2, :] = True
    fdir = np.zeros((5, 5), dtype=np.int16)
    fdir[2, :] = 1
    np.testing.assert_array_equal(prune_stream_mask(mask, fdir, min_length=1), mask)
    np.testing.assert_array_equal(prune_stream_mask(mask, fdir, min_length=0), mask)


def test_pruning_never_adds_cells(catchment: SyntheticCatchment) -> None:
    fdir, acc = _routed(catchment.dem.astype(np.float64), catchment.cellsize)
    mask = stream_mask(acc, fdir, threshold=200)
    pruned = prune_stream_mask(mask, fdir, min_length=5)
    assert np.all(pruned <= mask)
    assert pruned.sum() < mask.sum()


# --- vectorisation ----------------------------------------------------------------


def test_network_partitions_every_stream_cell(catchment: SyntheticCatchment) -> None:
    """Each stream cell belongs to exactly one link, dropped ones accounted for."""
    fdir, acc = _routed(catchment.dem.astype(np.float64), catchment.cellsize)
    mask = prune_stream_mask(stream_mask(acc, fdir, threshold=200), fdir, min_length=5)
    net = stream_network(mask, fdir, acc, catchment.transform, catchment.crs)

    assert not net.empty
    assert int(net["n_cells"].sum()) + net.attrs["dropped_degenerate_links"] == int(mask.sum())
    assert list(net["link_id"]) == list(range(len(net)))


def test_network_carries_the_analysis_crs(catchment: SyntheticCatchment) -> None:
    fdir, acc = _routed(catchment.dem.astype(np.float64), catchment.cellsize)
    mask = prune_stream_mask(stream_mask(acc, fdir, threshold=200), fdir, min_length=5)
    net = stream_network(mask, fdir, acc, catchment.transform, catchment.crs)
    assert net.crs.to_epsg() == 7856
    assert (net.geometry.geom_type == "LineString").all()


def test_outflow_excludes_a_sibling_tributary(catchment: SyntheticCatchment) -> None:
    """acc_outflow is this reach's own water, so it never exceeds its own head+cells."""
    fdir, acc = _routed(catchment.dem.astype(np.float64), catchment.cellsize)
    mask = prune_stream_mask(stream_mask(acc, fdir, threshold=200), fdir, min_length=5)
    net = stream_network(mask, fdir, acc, catchment.transform, catchment.crs)
    assert (net["acc_outflow"] >= net["acc_head"]).all()


def test_exactly_one_link_terminates(catchment: SyntheticCatchment) -> None:
    """The synthetic catchment drains off one edge, so the network has one outlet."""
    fdir, acc = _routed(catchment.dem.astype(np.float64), catchment.cellsize)
    mask = prune_stream_mask(stream_mask(acc, fdir, threshold=200), fdir, min_length=5)
    net = stream_network(mask, fdir, acc, catchment.transform, catchment.crs)
    # the terminus here is the dropped single-cell junction on the raster edge
    assert int(net["terminates"].sum()) + net.attrs["dropped_degenerate_links"] >= 1


def test_strahler_starts_at_one_and_grows_downstream(
    catchment: SyntheticCatchment,
) -> None:
    fdir, acc = _routed(catchment.dem.astype(np.float64), catchment.cellsize)
    mask = prune_stream_mask(stream_mask(acc, fdir, threshold=200), fdir, min_length=5)
    net = stream_network(mask, fdir, acc, catchment.transform, catchment.crs)
    assert net["strahler"].min() == 1
    assert net["strahler"].max() >= 2
    # the highest-accumulation link must not be a first-order headwater
    assert net.loc[net["acc_outflow"].idxmax(), "strahler"] > 1


def test_length_matches_the_cell_geometry(catchment: SyntheticCatchment) -> None:
    """A link of n cells spans between n-1 and (n)*sqrt(2) cell-widths."""
    fdir, acc = _routed(catchment.dem.astype(np.float64), catchment.cellsize)
    mask = prune_stream_mask(stream_mask(acc, fdir, threshold=200), fdir, min_length=5)
    net = stream_network(mask, fdir, acc, catchment.transform, catchment.crs)
    cs = catchment.cellsize
    for _, link in net.iterrows():
        segments = link["n_cells"] if not link["terminates"] else link["n_cells"] - 1
        assert segments * cs <= link["length_m"] + 1e-9
        assert link["length_m"] <= segments * cs * np.sqrt(2) + 1e-9


def test_empty_mask_gives_an_empty_network(catchment: SyntheticCatchment) -> None:
    fdir, acc = _routed(catchment.dem.astype(np.float64), catchment.cellsize)
    net = stream_network(
        np.zeros_like(fdir, dtype=bool), fdir, acc, catchment.transform, catchment.crs
    )
    assert net.empty


def test_network_shape_mismatch_rejected(catchment: SyntheticCatchment) -> None:
    fdir, acc = _routed(catchment.dem.astype(np.float64), catchment.cellsize)
    with pytest.raises(ValueError, match="same shape"):
        stream_network(np.zeros((3, 3), dtype=bool), fdir, acc, catchment.transform, catchment.crs)
