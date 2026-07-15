from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest
from rasterio.transform import from_origin

from floodline.report.figures import block_reduce, render_layers


def test_block_reduce_means_continuous_values() -> None:
    array = np.array([[1.0, 3.0], [5.0, 7.0]])
    assert block_reduce(array, 2)[0, 0] == pytest.approx(4.0)


def test_block_reduce_ignores_nan() -> None:
    array = np.array([[1.0, np.nan], [3.0, np.nan]])
    assert block_reduce(array, 2)[0, 0] == pytest.approx(2.0)


def test_block_reduce_returns_nan_for_an_empty_block() -> None:
    """A watershed does not fill its bounding box, so all-NaN blocks are normal."""
    assert np.isnan(block_reduce(np.full((2, 2), np.nan), 2)[0, 0])


def test_max_reduction_preserves_a_one_cell_feature() -> None:
    """A stream is one cell wide; a mean would average it away to nothing."""
    array = np.zeros((4, 4))
    array[:, 1] = 1.0  # a one-cell-wide channel
    assert block_reduce(array, 2, how="max").max() == pytest.approx(1.0)
    assert block_reduce(array, 2, how="mean").max() == pytest.approx(0.5)


def test_block_reduce_of_one_is_identity() -> None:
    array = np.arange(9.0).reshape(3, 3)
    np.testing.assert_array_equal(block_reduce(array, 1), array)


@pytest.mark.parametrize(("factor", "how"), [(0, "mean"), (1, "median")])
def test_block_reduce_rejects_bad_arguments(factor: int, how: str) -> None:
    with pytest.raises(ValueError):
        block_reduce(np.zeros((4, 4)), factor, how=how)


def test_render_layers_writes_aligned_pngs(tmp_path: Path) -> None:
    surface = np.random.default_rng(0).random((200, 300)) * 10
    mask = np.zeros((200, 300), dtype=bool)
    mask[100, :] = True

    result = render_layers(
        tmp_path,
        transform=from_origin(3_000_000.0, 13_800_000.0, 10.0, 10.0),
        crs="EPSG:6587",
        continuous={"hand": (surface, "HAND", "m", "viridis")},
        masks={"streams": (mask, "Streams", "spring")},
        target_width=100,
    )
    assert result.reduction == 3
    assert (tmp_path / "hand.png").exists()
    assert (tmp_path / "streams.png").exists()
    assert result.width == 100
    assert [layer.name for layer in result.layers] == ["hand", "streams"]
    assert result.layers[0].kind == "continuous"
    assert result.layers[1].kind == "mask"


def test_every_layer_shares_one_reduction(tmp_path: Path) -> None:
    """Pixel alignment is what lets a point placed on one layer be placed on all."""
    a = np.ones((120, 240))
    b = np.zeros((120, 240), dtype=bool)
    result = render_layers(
        tmp_path,
        transform=from_origin(0.0, 0.0, 10.0, 10.0),
        crs="EPSG:6587",
        continuous={"a": (a, "A", "m", "viridis")},
        masks={"b": (b, "B", "cool")},
        target_width=80,
    )
    assert result.width == 80
    assert result.height == 40


def test_bounds_come_from_the_source_transform(tmp_path: Path) -> None:
    result = render_layers(
        tmp_path,
        transform=from_origin(1000.0, 5000.0, 10.0, 10.0),
        crs="EPSG:6587",
        continuous={"a": (np.ones((100, 200)), "A", "m", "viridis")},
        masks={},
        target_width=50,
    )
    assert result.bounds == (1000.0, 4000.0, 3000.0, 5000.0)


def test_manifest_round_trips(tmp_path: Path) -> None:
    result = render_layers(
        tmp_path,
        transform=from_origin(0.0, 0.0, 10.0, 10.0),
        crs="EPSG:6587",
        continuous={"a": (np.ones((40, 40)), "A", "m", "viridis")},
        masks={},
        target_width=20,
    )
    result.points["marks"] = [{"px": 1.5, "py": 2.5}]
    payload = json.loads(result.to_json(tmp_path / "layers.json").read_text())
    assert payload["crs"] == "EPSG:6587"
    assert payload["layers"][0]["label"] == "A"
    assert payload["points"]["marks"][0]["px"] == 1.5


def test_nothing_to_render_is_an_error(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="nothing to render"):
        render_layers(
            tmp_path,
            transform=from_origin(0.0, 0.0, 1.0, 1.0),
            crs="EPSG:6587",
            continuous={},
            masks={},
        )
