from __future__ import annotations

import base64
import io

import numpy as np
import pytest
from PIL import Image

from floodline.core.hydro.rating import build_rating_curves
from floodline.report.bundle import (
    HAND_NODATA,
    HAND_SCALE,
    REACH_NODATA,
    encode_hand,
    encode_reach_ids,
    encode_stage_table,
    to_data_uri,
)


def decode(payload: bytes) -> np.ndarray:
    return np.asarray(Image.open(io.BytesIO(payload)))


def test_hand_round_trips_to_a_decimetre() -> None:
    hand = np.array([[0.0, 1.25, 9.99], [12.5, 25.3, np.nan]])
    back = decode(encode_hand(hand)).astype(np.float64)
    np.testing.assert_allclose(back[0] / HAND_SCALE, [0.0, 1.2, 9.9], atol=0.1)
    assert back[1, 2] == HAND_NODATA


def test_hand_above_the_range_clamps_and_only_ever_dries() -> None:
    """Clamping caps HAND high, which makes a cell drier - it cannot invent water."""
    back = decode(encode_hand(np.array([[40.0]]))).astype(np.float64)
    assert back[0, 0] == HAND_NODATA - 1
    assert back[0, 0] / HAND_SCALE == pytest.approx(25.4)


def test_reach_ids_round_trip_beyond_a_byte() -> None:
    ids = np.array([[0, 255, 256], [1000, 65534, -1]], dtype=np.int64)
    rgb = decode(encode_reach_ids(ids)).astype(np.int64)
    recovered = (rgb[..., 0] << 8) | rgb[..., 1]
    np.testing.assert_array_equal(recovered[0], [0, 255, 256])
    np.testing.assert_array_equal(recovered[1], [1000, 65534, REACH_NODATA])


def test_reach_blue_channel_is_unused() -> None:
    rgb = decode(encode_reach_ids(np.array([[7]], dtype=np.int64)))
    assert rgb[0, 0, 2] == 0


def _one_reach_curves() -> tuple[dict, dict]:
    rows, cols, middle = 9, 21, 4
    hand = np.abs(np.arange(rows) - middle).astype(np.float64)[:, None] * np.ones(cols)
    filled = hand + (np.arange(cols)[::-1] * 0.001 * 10.0)[None, :]
    links = [[middle * cols + c for c in range(cols)]]
    reach_of = np.zeros((rows, cols), dtype=np.int64)
    curves = build_rating_curves(hand, filled, links, reach_of, cellsize=(10.0, 10.0))
    return curves, {0: float(curves[0].discharge_cms[8])}


def test_stage_table_is_one_row_per_reach() -> None:
    curves, flows = _one_reach_curves()
    table = decode(encode_stage_table(1, curves, flows, np.linspace(0, 3, 33)))
    assert table.shape == (1, 33)


def test_stage_table_rises_with_the_multiplier() -> None:
    curves, flows = _one_reach_curves()
    row = decode(encode_stage_table(1, curves, flows, np.linspace(0, 3, 33)))[0]
    assert row[0] == 0, "no discharge, no stage"
    assert np.all(np.diff(row.astype(int)) >= 0)
    assert row[-1] > row[8]


def test_the_multiplier_of_one_reproduces_the_curve() -> None:
    curves, flows = _one_reach_curves()
    mults = np.linspace(0, 3, 33)
    row = decode(encode_stage_table(1, curves, flows, mults))[0]
    index = int(np.argmin(np.abs(mults - 1.0)))
    expected = curves[0].stage_for_discharge(flows[0])
    assert row[index] / HAND_SCALE == pytest.approx(expected, abs=0.1)


def test_a_reach_without_a_curve_stays_dry() -> None:
    """Zero floods nothing, the same honest default the stage field uses."""
    table = decode(encode_stage_table(3, {}, {0: 100.0}, np.linspace(0, 3, 9)))
    assert table.shape == (3, 9)
    assert not table.any()


def test_data_uri_is_decodable() -> None:
    uri = to_data_uri(b"floodline")
    assert uri.startswith("data:image/png;base64,")
    assert base64.b64decode(uri.split(",", 1)[1]) == b"floodline"


def test_a_watershed_bundle_stays_small() -> None:
    """The whole point: a browser can carry twenty of these and still recompute any."""
    rng = np.random.default_rng(0)
    hand = np.abs(rng.normal(4, 3, (520, 740)))
    reaches = (np.arange(520 * 740) // 900).reshape(520, 740) % 600
    total = len(encode_hand(hand)) + len(encode_reach_ids(reaches))
    assert total < 900_000, f"{total} bytes is too much to inline twenty times"
