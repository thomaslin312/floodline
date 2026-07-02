from __future__ import annotations

import json
from pathlib import Path

import pytest
import rasterio
from typer.testing import CliRunner

from floodline import __version__
from floodline.cli import app

runner = CliRunner()


def test_help_lists_every_stage() -> None:
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    for stage in ("condition", "hand", "inundate", "exposure", "damage", "validate", "report"):
        assert stage in result.stdout


def test_version() -> None:
    result = runner.invoke(app, ["--version"])
    assert result.exit_code == 0
    assert result.stdout.strip() == __version__


def test_config_command_emits_json() -> None:
    result = runner.invoke(app, ["config"])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["terrain"]["stream_threshold_cells"] == 1000


def test_config_command_reads_a_file(tmp_path: Path) -> None:
    path = tmp_path / "c.toml"
    path.write_text("[floodline.terrain]\nstream_threshold_cells = 42\n")
    result = runner.invoke(app, ["config", "--config", str(path)])
    assert result.exit_code == 0
    assert json.loads(result.stdout)["terrain"]["stream_threshold_cells"] == 42


def test_synth_writes_a_cog(tmp_path: Path) -> None:
    out = tmp_path / "synth.tif"
    result = runner.invoke(app, ["synth", str(out), "--rows", "60", "--cols", "50"])
    assert result.exit_code == 0, result.stdout
    assert out.exists()
    with rasterio.open(out) as src:
        assert src.shape == (60, 50)
        assert src.crs.to_epsg() == 7856
        assert src.nodata is not None


@pytest.mark.parametrize("stage", ["hand", "inundate", "exposure", "damage"])
def test_unimplemented_stages_exit_2(stage: str, tmp_path: Path) -> None:
    dummy = tmp_path / "in.tif"
    dummy.write_bytes(b"")
    args = {
        "hand": [stage, str(dummy), str(tmp_path / "o.tif")],
        "inundate": [stage, str(dummy), "10.5", str(tmp_path / "o.tif")],
        "exposure": [stage, str(dummy), str(dummy), str(tmp_path / "o.parquet")],
        "damage": [stage, str(dummy), str(tmp_path / "o.parquet")],
    }[stage]
    result = runner.invoke(app, args)
    assert result.exit_code == 2
    assert "not implemented" in result.stderr


def test_condition_fills_a_synthetic_dem(tmp_path: Path) -> None:
    raw = tmp_path / "raw.tif"
    filled = tmp_path / "filled.tif"
    assert runner.invoke(app, ["synth", str(raw), "--rows", "80", "--cols", "60"]).exit_code == 0

    result = runner.invoke(app, ["condition", str(raw), str(filled)])
    assert result.exit_code == 0, result.stdout
    assert "cells raised" in result.stdout
    assert filled.exists()


def test_condition_refuses_stream_burning(tmp_path: Path) -> None:
    raw = tmp_path / "raw.tif"
    runner.invoke(app, ["synth", str(raw), "--rows", "40", "--cols", "40"])
    result = runner.invoke(
        app, ["condition", str(raw), str(tmp_path / "o.tif"), "--streams", str(raw)]
    )
    assert result.exit_code == 2
    assert "not implemented" in result.stderr


def test_condition_refuses_a_geographic_dem(tmp_path: Path) -> None:
    import numpy as np
    from rasterio.crs import CRS as RioCRS
    from rasterio.transform import from_origin

    path = tmp_path / "wgs84.tif"
    with rasterio.open(
        path,
        "w",
        driver="GTiff",
        height=8,
        width=8,
        count=1,
        dtype="float32",
        crs=RioCRS.from_epsg(4326),
        transform=from_origin(153.0, -28.8, 0.001, 0.001),
        nodata=-9999.0,
    ) as dst:
        dst.write(np.zeros((8, 8), np.float32), 1)

    result = runner.invoke(app, ["condition", str(path), str(tmp_path / "o.tif")])
    assert result.exit_code != 0
    assert isinstance(result.exception, Exception)


def test_streams_writes_a_geoparquet(tmp_path: Path) -> None:
    import geopandas as gpd

    raw, filled = tmp_path / "raw.tif", tmp_path / "filled.tif"
    net, mask = tmp_path / "net.parquet", tmp_path / "mask.tif"
    cfg = tmp_path / "c.toml"
    cfg.write_text("[floodline.terrain]\nfill_epsilon = 1e-4\nstream_threshold_cells = 200\n")

    assert runner.invoke(app, ["synth", str(raw), "--rows", "120", "--cols", "90"]).exit_code == 0
    assert (
        runner.invoke(app, ["condition", str(raw), str(filled), "--config", str(cfg)]).exit_code
        == 0
    )
    result = runner.invoke(
        app,
        ["streams", str(filled), str(net), "--raster-out", str(mask), "--config", str(cfg)],
    )
    assert result.exit_code == 0, result.stdout
    assert "links" in result.stdout

    frame = gpd.read_parquet(net)
    assert frame.crs.to_epsg() == 7856
    assert len(frame) > 0
    assert {"link_id", "strahler", "acc_outflow", "length_m"} <= set(frame.columns)
    with rasterio.open(mask) as src:
        assert src.dtypes[0] == "uint8"


def test_streams_warns_when_water_drains_into_flats(tmp_path: Path) -> None:
    """An epsilon-free fill strands water in flats; the CLI must say so."""
    raw, filled = tmp_path / "raw.tif", tmp_path / "filled.tif"
    runner.invoke(app, ["synth", str(raw), "--rows", "80", "--cols", "60"])
    runner.invoke(app, ["condition", str(raw), str(filled)])
    result = runner.invoke(app, ["streams", str(filled), str(tmp_path / "n.parquet")])
    assert result.exit_code == 0, result.stdout
    assert "drain into flats" in result.stderr
