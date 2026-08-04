from __future__ import annotations

import numpy as np
import pytest

from floodline.config import Config, Connectivity
from floodline.hydraulics.inundate import inundate
from floodline.synthetic import SyntheticCatchment
from floodline.terrain.route import route_terrain

NO_FILTER = {"require_connectivity": False, "min_depth": 0.0}


def test_depth_is_stage_minus_hand() -> None:
    hand = np.array([[0.0, 0.5, 1.0], [1.5, 2.0, 3.0], [4.0, 5.0, 6.0]])
    result = inundate(hand, 2.0, **NO_FILTER)
    expected = np.maximum(2.0 - hand, 0.0)
    np.testing.assert_allclose(result.depth, expected)


def test_dry_cells_are_zero_not_negative() -> None:
    hand = np.array([[0.0, 5.0], [10.0, 20.0]])
    result = inundate(hand, 1.0, **NO_FILTER)
    assert np.all(result.depth >= 0.0)
    assert result.depth[1, 1] == 0.0


def test_max_depth_never_exceeds_the_stage() -> None:
    hand = np.zeros((4, 4))
    result = inundate(hand, 3.0, **NO_FILTER)
    assert result.max_depth_m == pytest.approx(3.0)
    assert result.max_depth_m <= 3.0


def test_stream_cells_flood_to_the_full_stage() -> None:
    hand = np.array([[0.0, 1.0], [2.0, 3.0]])
    result = inundate(hand, 2.5, **NO_FILTER)
    assert result.depth[0, 0] == pytest.approx(2.5)


def test_nan_hand_is_never_wet() -> None:
    hand = np.array([[0.0, np.nan], [1.0, 2.0]])
    result = inundate(hand, 5.0, **NO_FILTER)
    assert not result.wet[0, 1]
    assert np.isnan(result.depth[0, 1])


def test_min_depth_threshold() -> None:
    hand = np.array([[0.0, 0.90, 0.99]])  # depths 1.00, 0.10, 0.01
    result = inundate(hand, 1.0, min_depth=0.05, require_connectivity=False)
    assert result.wet[0, 0]
    assert result.wet[0, 1]
    assert not result.wet[0, 2], "1 cm of water is inside the DEM's own error"
    assert result.n_wet == 2
    assert result.depth[0, 2] == 0.0, "sub-threshold cells report zero, not their depth"


def test_min_depth_comes_from_config() -> None:
    hand = np.array([[0.0, 0.8]])
    cfg = Config.model_validate({"hydraulics": {"min_depth_m": 0.5}})
    result = inundate(hand, 1.0, config=cfg, require_connectivity=False)
    assert result.n_wet == 1


def test_zero_stage_floods_nothing() -> None:
    hand = np.array([[0.0, 1.0], [2.0, 3.0]])
    result = inundate(hand, 0.0, **NO_FILTER)
    assert result.n_wet == 0
    assert result.max_depth_m == 0.0


def test_negative_stage_rejected() -> None:
    with pytest.raises(ValueError, match="non-negative"):
        inundate(np.zeros((3, 3)), -1.0, require_connectivity=False)


def test_stage_field_shape_checked() -> None:
    with pytest.raises(ValueError, match="does not match"):
        inundate(np.zeros((3, 3)), np.zeros((4, 4)), require_connectivity=False)


def test_a_stage_field_is_applied_per_cell() -> None:
    hand = np.zeros((2, 2))
    stage = np.array([[1.0, 2.0], [3.0, 4.0]])
    result = inundate(hand, stage, **NO_FILTER)
    np.testing.assert_allclose(result.depth, stage)


# --- the connectivity filter --------------------------------------------------------


def test_connectivity_removes_a_detached_hollow() -> None:
    """A low hollow behind higher ground has small HAND but no path to the river."""
    hand = np.full((7, 7), 10.0)
    hand[3, 0:3] = 0.0  # the channel and its floodplain
    hand[3, 5:7] = 0.0  # a detached hollow on the far side of high ground
    streams = np.zeros((7, 7), dtype=bool)
    streams[3, 0] = True

    unfiltered = inundate(hand, 1.0, streams=streams, **NO_FILTER)
    filtered = inundate(hand, 1.0, streams=streams, min_depth=0.0, require_connectivity=True)

    assert unfiltered.n_wet == 5
    assert filtered.n_wet == 3
    assert filtered.n_removed_by_connectivity == 2
    assert not filtered.wet[3, 5:7].any()


def test_connectivity_keeps_water_joined_to_the_channel() -> None:
    hand = np.full((5, 5), 10.0)
    hand[2, :] = 0.0
    streams = np.zeros((5, 5), dtype=bool)
    streams[2, 0] = True
    result = inundate(hand, 1.0, streams=streams, min_depth=0.0, require_connectivity=True)
    assert result.n_wet == 5
    assert result.n_removed_by_connectivity == 0


def test_connectivity_needs_a_stream_mask() -> None:
    with pytest.raises(ValueError, match="no stream mask"):
        inundate(np.zeros((3, 3)), 1.0, require_connectivity=True)


def test_stream_mask_shape_checked() -> None:
    with pytest.raises(ValueError, match="does not match"):
        inundate(
            np.zeros((3, 3)),
            1.0,
            streams=np.zeros((4, 4), dtype=bool),
            require_connectivity=True,
        )


def test_d4_connectivity_is_stricter_than_d8() -> None:
    """Water joined only across a diagonal survives D8 but not D4."""
    hand = np.full((5, 5), 10.0)
    hand[1, 1] = 0.0
    hand[2, 2] = 0.0  # touches (1, 1) only diagonally
    streams = np.zeros((5, 5), dtype=bool)
    streams[1, 1] = True

    d8 = inundate(
        hand,
        1.0,
        streams=streams,
        min_depth=0.0,
        require_connectivity=True,
        connectivity=Connectivity.EIGHT,
    )
    d4 = inundate(
        hand,
        1.0,
        streams=streams,
        min_depth=0.0,
        require_connectivity=True,
        connectivity=Connectivity.FOUR,
    )
    assert d8.n_wet == 2
    assert d4.n_wet == 1


def test_area_uses_the_cell_size() -> None:
    hand = np.zeros((3, 3))
    result = inundate(hand, 1.0, cell_area_m2=25.0, **NO_FILTER)
    assert result.n_wet == 9
    assert result.area_m2 == pytest.approx(225.0)


# --- on the synthetic catchment ------------------------------------------------------


def test_catchment_floods_around_the_channel(catchment: SyntheticCatchment) -> None:
    chain = route_terrain(
        catchment.dem.astype(np.float64),
        cellsize=(catchment.cellsize,) * 2,
        stream_threshold=200,
    )
    result = inundate(
        chain.hand.hand,
        3.0,
        streams=chain.streams,
        cell_area_m2=catchment.cellsize**2,
        require_connectivity=True,
    )
    assert result.n_wet > 0
    assert np.all(result.wet[chain.streams])
    assert result.max_depth_m <= 3.0
    assert result.area_m2 == pytest.approx(result.n_wet * catchment.cellsize**2)
