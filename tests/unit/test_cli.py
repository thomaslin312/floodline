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
        assert src.crs.to_epsg() == 6587
        assert src.nodata is not None


@pytest.mark.parametrize("stage", ["exposure", "damage"])
def test_unimplemented_stages_exit_2(stage: str, tmp_path: Path) -> None:
    dummy = tmp_path / "in.tif"
    dummy.write_bytes(b"")
    args = {
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
    assert frame.crs.to_epsg() == 6587
    assert len(frame) > 0
    assert {"link_id", "strahler", "acc_outflow", "length_m"} <= set(frame.columns)
    with rasterio.open(mask) as src:
        assert src.dtypes[0] == "uint8"


def test_streams_warns_when_water_drains_into_flats(tmp_path: Path) -> None:
    """With both escapes disabled, water strands in flats and the CLI must say so."""
    raw, filled = tmp_path / "raw.tif", tmp_path / "filled.tif"
    cfg = tmp_path / "stranded.toml"
    cfg.write_text("[floodline.terrain]\nfill_epsilon = 0.0\nresolve_flats = false\n")

    runner.invoke(app, ["synth", str(raw), "--rows", "80", "--cols", "60"])
    runner.invoke(app, ["condition", str(raw), str(filled), "--config", str(cfg)])
    result = runner.invoke(
        app, ["streams", str(filled), str(tmp_path / "n.parquet"), "--config", str(cfg)]
    )
    assert result.exit_code == 0, result.stdout
    assert "drain into flats" in result.stderr


def test_streams_strands_nothing_by_default(tmp_path: Path) -> None:
    """The default config resolves flats, so no warning and nothing is stranded."""
    raw, filled = tmp_path / "raw.tif", tmp_path / "filled.tif"
    runner.invoke(app, ["synth", str(raw), "--rows", "80", "--cols", "60"])
    runner.invoke(app, ["condition", str(raw), str(filled)])
    result = runner.invoke(app, ["streams", str(filled), str(tmp_path / "n.parquet")])
    assert result.exit_code == 0, result.stdout
    assert "drain into flats" not in result.stderr


def test_hand_writes_a_raster(tmp_path: Path) -> None:

    raw, filled, out = tmp_path / "raw.tif", tmp_path / "f.tif", tmp_path / "hand.tif"
    cfg = tmp_path / "c.toml"
    cfg.write_text("[floodline.terrain]\nfill_epsilon = 1e-4\nstream_threshold_cells = 200\n")

    runner.invoke(app, ["synth", str(raw), "--rows", "120", "--cols", "90"])
    runner.invoke(app, ["condition", str(raw), str(filled), "--config", str(cfg)])
    result = runner.invoke(app, ["hand", str(filled), str(out), "--config", str(cfg)])
    assert result.exit_code == 0, result.stdout
    assert "stream cells" in result.stdout

    with rasterio.open(out) as src:
        assert src.dtypes[0] == "float32"
        assert src.crs.to_epsg() == 6587
        values = src.read(1, masked=True)
    assert values.min() >= 0.0, "HAND must never be negative"


def _prepare_conditioned(tmp_path: Path, cfg_text: str) -> tuple[Path, Path]:
    raw, filled = tmp_path / "raw.tif", tmp_path / "filled.tif"
    cfg = tmp_path / "c.toml"
    cfg.write_text(cfg_text)
    runner.invoke(app, ["synth", str(raw), "--rows", "120", "--cols", "90"])
    assert (
        runner.invoke(app, ["condition", str(raw), str(filled), "--config", str(cfg)]).exit_code
        == 0
    )
    return filled, cfg


def test_inundate_refuses_without_a_gauge_datum(tmp_path: Path) -> None:
    """The headline safeguard: no datum, no run."""
    filled, cfg = _prepare_conditioned(
        tmp_path, "[floodline.terrain]\nstream_threshold_cells = 200\n"
    )
    result = runner.invoke(
        app,
        [
            "inundate",
            str(filled),
            "3.0",
            str(tmp_path / "d.tif"),
            "--gauge-row",
            "60",
            "--gauge-col",
            "45",
            "--config",
            str(cfg),
        ],
    )
    assert result.exit_code != 0
    assert isinstance(result.exception, ValueError)
    assert "gauge_datum_offset_m is not set" in str(result.exception)


def test_inundate_writes_a_depth_raster(tmp_path: Path) -> None:
    import numpy as np

    filled, cfg = _prepare_conditioned(
        tmp_path,
        "[floodline.terrain]\nstream_threshold_cells = 200\n"
        "[floodline.hydraulics]\ngauge_datum_offset_m = 0.0\n",
    )
    # find a stream cell to put the gauge on
    from floodline.io.raster import read_raster
    from floodline.terrain.route import route_terrain

    raster = read_raster(filled)
    chain = route_terrain(raster.data, cellsize=raster.cellsize, stream_threshold=200)
    row, col = np.argwhere(chain.streams)[len(np.argwhere(chain.streams)) // 2]
    reading = float(chain.filled[row, col]) + 4.0

    out = tmp_path / "depth.tif"
    result = runner.invoke(
        app,
        [
            "inundate",
            str(filled),
            str(reading),
            str(out),
            "--gauge-row",
            str(int(row)),
            "--gauge-col",
            str(int(col)),
            "--config",
            str(cfg),
        ],
    )
    assert result.exit_code == 0, result.stdout
    assert "cells wet" in result.stdout
    assert "m AHD" in result.stdout

    with rasterio.open(out) as src:
        depth = src.read(1, masked=True)
        assert src.dtypes[0] == "float32"
    assert depth.min() >= 0.0
    assert depth.max() <= 4.0 + 1e-3


def test_inundate_warns_when_the_gauge_is_off_the_network(tmp_path: Path) -> None:
    import numpy as np

    filled, cfg = _prepare_conditioned(
        tmp_path,
        "[floodline.terrain]\nstream_threshold_cells = 200\n"
        "[floodline.hydraulics]\ngauge_datum_offset_m = 0.0\n",
    )
    from floodline.io.raster import read_raster
    from floodline.terrain.route import route_terrain

    raster = read_raster(filled)
    chain = route_terrain(raster.data, cellsize=raster.cellsize, stream_threshold=200)
    row, col = np.argwhere(~chain.streams)[0]
    reading = float(chain.filled[row, col]) + 2.0

    result = runner.invoke(
        app,
        [
            "inundate",
            str(filled),
            str(reading),
            str(tmp_path / "d.tif"),
            "--gauge-row",
            str(int(row)),
            "--gauge-col",
            str(int(col)),
            "--config",
            str(cfg),
        ],
    )
    assert result.exit_code == 0, result.stdout
    assert "not a stream cell" in result.stderr
