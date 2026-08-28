from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
import rasterio
from typer.testing import CliRunner

from floodline import __version__
from floodline.cli import app
from floodline.io.raster import CrsError

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


@pytest.mark.parametrize("stage", ["report"])
def test_unimplemented_stages_exit_2(stage: str, tmp_path: Path) -> None:
    dummy = tmp_path / "in.tif"
    dummy.write_bytes(b"")
    args = {"report": [stage, str(tmp_path / "o.html")]}[stage]
    result = runner.invoke(app, args)
    assert result.exit_code == 2
    assert "not implemented" in result.stderr


def test_damage_refuses_a_table_that_is_not_an_exposure_table(tmp_path: Path) -> None:
    import geopandas as gpd
    from shapely.geometry import Point

    table = tmp_path / "wrong.parquet"
    gpd.GeoDataFrame({"id": [1]}, geometry=[Point(0, 0)], crs="EPSG:6587").to_parquet(table)
    result = runner.invoke(app, ["damage", str(table), str(tmp_path / "o.parquet")])
    assert result.exit_code == 1
    assert "floodline exposure" in result.stderr


def test_exposure_refuses_footprints_in_the_wrong_crs(tmp_path: Path) -> None:
    import geopandas as gpd
    from shapely.geometry import box

    raw = tmp_path / "raw.tif"
    assert runner.invoke(app, ["synth", str(raw), "--rows", "40", "--cols", "40"]).exit_code == 0
    footprints = tmp_path / "b.parquet"
    gpd.GeoDataFrame({"id": [1]}, geometry=[box(0, 0, 20, 20)], crs="EPSG:32615").to_parquet(
        footprints
    )

    # read_vector refuses the mismatch against the analysis CRS before the command's
    # own raster-vs-footprint check is reached; either way nothing is written.
    result = runner.invoke(
        app, ["exposure", str(raw), str(footprints), str(tmp_path / "o.parquet")]
    )
    assert result.exit_code != 0
    assert isinstance(result.exception, CrsError)
    assert not (tmp_path / "o.parquet").exists()


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


def _conditioned_watershed(tmp_path: Path) -> tuple[Path, Path, int, int]:
    """A conditioned synthetic DEM plus a gauge cell that sits on the network."""
    import numpy as np

    from floodline.io.raster import read_raster
    from floodline.terrain.route import route_terrain

    raw, filled = tmp_path / "raw.tif", tmp_path / "filled.tif"
    cfg = tmp_path / "c.toml"
    cfg.write_text("[floodline.terrain]\nstream_threshold_cells = 200\n")
    runner.invoke(app, ["synth", str(raw), "--rows", "120", "--cols", "90"])
    runner.invoke(app, ["condition", str(raw), str(filled), "--config", str(cfg)])

    raster = read_raster(filled)
    chain = route_terrain(raster.data, cellsize=raster.cellsize, stream_threshold=200)
    cells = np.argwhere(chain.streams)
    row, col = cells[len(cells) // 2]
    return filled, cfg, int(row), int(col)


def test_inundate_from_discharge_writes_a_depth_raster(tmp_path: Path) -> None:
    filled, cfg, row, col = _conditioned_watershed(tmp_path)
    out = tmp_path / "depth.tif"
    result = runner.invoke(
        app,
        [
            "inundate",
            str(filled),
            "150",
            str(out),
            "--gauge-row",
            str(row),
            "--gauge-col",
            str(col),
            "--config",
            str(cfg),
        ],
    )
    assert result.exit_code == 0, result.stdout
    assert "rating curves" in result.stdout
    assert "reach stage median" in result.stdout

    with rasterio.open(out) as src:
        depth = src.read(1, masked=True)
        assert src.dtypes[0] == "float32"
    assert depth.min() >= 0.0


def test_inundate_refuses_a_gauge_off_the_network(tmp_path: Path) -> None:
    """Without a contributing area there is nothing to scale discharge from."""
    import numpy as np

    from floodline.io.raster import read_raster
    from floodline.terrain.route import route_terrain

    filled, cfg, _, _ = _conditioned_watershed(tmp_path)
    raster = read_raster(filled)
    chain = route_terrain(raster.data, cellsize=raster.cellsize, stream_threshold=200)
    row, col = np.argwhere(~chain.streams)[0]

    result = runner.invoke(
        app,
        [
            "inundate",
            str(filled),
            "150",
            str(tmp_path / "d.tif"),
            "--gauge-row",
            str(int(row)),
            "--gauge-col",
            str(int(col)),
            "--config",
            str(cfg),
        ],
    )
    assert result.exit_code == 1
    assert "not on the stream network" in result.stderr


def test_more_discharge_floods_more(tmp_path: Path) -> None:
    """The monotonicity invariant, end to end through the rating curves."""
    filled, cfg, row, col = _conditioned_watershed(tmp_path)
    areas = []
    for q in ("50", "500"):
        out = tmp_path / f"d{q}.tif"
        result = runner.invoke(
            app,
            [
                "inundate",
                str(filled),
                q,
                str(out),
                "--gauge-row",
                str(row),
                "--gauge-col",
                str(col),
                "--config",
                str(cfg),
            ],
        )
        assert result.exit_code == 0, result.stdout
        with rasterio.open(out) as src:
            data = src.read(1, masked=True)
        areas.append(int((data.filled(0) > 0).sum()))
    assert areas[1] > areas[0]


def test_fetch_list_shows_every_source() -> None:
    result = runner.invoke(app, ["fetch", "--list"])
    assert result.exit_code == 0
    for name in ("usgs-dem", "usgs-gauge", "usgs-hwm", "fema-nfip-claims", "sentinel1-search"):
        assert name in result.stdout


def test_fetch_rejects_an_unknown_source(tmp_path: Path) -> None:
    result = runner.invoke(app, ["fetch", "nope", "--dest", str(tmp_path)])
    assert result.exit_code != 0


def test_fetch_writes_a_manifest_and_reports_failures(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The CLI must exit non-zero when a source fails, and still write the manifest."""
    import httpx

    from floodline.io import sources as src

    def fake_client(settings: object | None = None) -> httpx.Client:
        return httpx.Client(
            transport=httpx.MockTransport(lambda request: httpx.Response(503)),
            base_url="https://example.test",
        )

    monkeypatch.setattr(src, "make_client", fake_client)
    cfg = tmp_path / "fast.toml"
    cfg.write_text("[floodline.sources]\nmax_attempts = 1\nbackoff_seconds = 0.0\n")
    manifest = tmp_path / "MANIFEST.md"
    result = runner.invoke(
        app,
        [
            "fetch",
            "usgs-hwm",
            "--dest",
            str(tmp_path),
            "--manifest",
            str(manifest),
            "--config",
            str(cfg),
        ],
    )
    assert result.exit_code == 1
    assert "FAILED" in result.stderr
    assert manifest.exists()
    assert "Sources that did not fetch" in manifest.read_text()


def test_ingest_reprojects_a_geographic_tile(tmp_path: Path, geographic_tile_writer: Any) -> None:
    """The CLI half of the step that makes 3DEP tiles usable."""
    tiles = tmp_path / "tiles"
    tiles.mkdir()
    geographic_tile_writer(tiles / "USGS_1_x_20180510.tif", west=-95.6, south=29.7)
    geographic_tile_writer(tiles / "USGS_1_x_20260623.tif", west=-95.6, south=29.7)

    out = tmp_path / "dem.tif"
    result = runner.invoke(app, ["ingest", str(tiles), str(out), "--resolution", "60"])
    assert result.exit_code == 0, result.stdout
    assert "2 tiles -> 1 footprints" in result.stdout
    assert "EPSG:6587" in result.stdout

    with rasterio.open(out) as src:
        assert src.crs.to_epsg() == 6587
        assert src.res == (60.0, 60.0)


def test_ingest_errors_on_an_empty_directory(tmp_path: Path) -> None:
    empty = tmp_path / "empty"
    empty.mkdir()
    result = runner.invoke(app, ["ingest", str(empty), str(tmp_path / "o.tif")])
    assert result.exit_code == 1
    assert "no .tif files" in result.stderr


def test_watersheds_command_reports_missing_input(tmp_path: Path) -> None:
    result = runner.invoke(app, ["watersheds", "--path", str(tmp_path / "nope.geojson")])
    assert result.exit_code == 1
    assert "floodline fetch usgs-watersheds" in result.stderr


def test_ingest_huc_reports_an_unknown_code(tmp_path: Path, geographic_tile_writer: Any) -> None:
    import json

    tiles = tmp_path / "tiles"
    tiles.mkdir()
    geographic_tile_writer(tiles / "USGS_1_x_20180510.tif", west=-95.6, south=29.7)
    sheds = tmp_path / "w.geojson"
    sheds.write_text(
        json.dumps(
            {
                "type": "FeatureCollection",
                "features": [
                    {
                        "type": "Feature",
                        "properties": {"huc10": "1111111111", "name": "X", "areasqkm": 1.0},
                        "geometry": {
                            "type": "Polygon",
                            "coordinates": [
                                [
                                    [-95.6, 29.7],
                                    [-95.5, 29.7],
                                    [-95.5, 29.8],
                                    [-95.6, 29.8],
                                    [-95.6, 29.7],
                                ]
                            ],
                        },
                    }
                ],
            }
        )
    )
    result = runner.invoke(
        app,
        [
            "ingest",
            str(tiles),
            str(tmp_path / "o.tif"),
            "--huc",
            "0000000000",
            "--watersheds",
            str(sheds),
        ],
    )
    assert result.exit_code == 1
    assert "no watershed 0000000000" in result.stderr


def test_ingest_clips_to_a_named_watershed(tmp_path: Path, geographic_tile_writer: Any) -> None:
    import json

    tiles = tmp_path / "tiles"
    tiles.mkdir()
    geographic_tile_writer(
        tiles / "USGS_1_x_20180510.tif", west=-95.7, south=29.6, size=0.4, res=0.005
    )
    sheds = tmp_path / "w.geojson"
    sheds.write_text(
        json.dumps(
            {
                "type": "FeatureCollection",
                "features": [
                    {
                        "type": "Feature",
                        "properties": {"huc10": "1204010407", "name": "Buffalo", "areasqkm": 500.0},
                        "geometry": {
                            "type": "Polygon",
                            "coordinates": [
                                [
                                    [-95.60, 29.70],
                                    [-95.45, 29.70],
                                    [-95.45, 29.82],
                                    [-95.60, 29.82],
                                    [-95.60, 29.70],
                                ]
                            ],
                        },
                    }
                ],
            }
        )
    )
    out = tmp_path / "dem.tif"
    result = runner.invoke(
        app,
        [
            "ingest",
            str(tiles),
            str(out),
            "--resolution",
            "100",
            "--huc",
            "1204010407",
            "--watersheds",
            str(sheds),
        ],
    )
    assert result.exit_code == 0, result.stdout
    assert "clipping to HUC 1204010407 Buffalo" in result.stdout

    with rasterio.open(out) as src:
        data = src.read(1, masked=True)
    assert data.mask.any(), "cells outside the boundary must be nodata"
    assert (~data.mask).any(), "cells inside it must not be"
