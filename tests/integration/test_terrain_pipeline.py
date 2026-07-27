"""End-to-end on a tiny synthetic catchment: DEM on disk in, conditioned DEM out.

This grows a stage at a time as the pipeline is built. Today it covers everything
that exists: synthesise a catchment, write it as a COG, read it back through the
CRS-checking reader, fill it, and confirm the result is a surface every cell can
drain off.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import rasterio

from floodline.config import Config
from floodline.io.raster import read_raster, write_cog
from floodline.synthetic import make_synthetic_catchment
from floodline.terrain.fill import fill_depressions, undrained_mask
from floodline.terrain.flowdir import (
    D8_CODES,
    FLOW_NODATA,
    flow_direction,
    steps_to_outlet,
)


def test_synth_to_conditioned_dem(tmp_path: Path) -> None:
    cfg = Config()
    catchment = make_synthetic_catchment(rows=100, cols=80, n_pits=4, seed=12)

    raw_path = write_cog(tmp_path / "raw.tif", catchment.as_raster(), config=cfg)
    raw = read_raster(raw_path, config=cfg)

    assert raw.crs.to_epsg() == cfg.crs.analysis.to_epsg()
    assert undrained_mask(raw.data, nodata=raw.nodata).any(), "fixture should contain pits"

    filled, raised = fill_depressions(raw.data, config=cfg, nodata=raw.nodata, return_raised=True)
    assert raised > 0
    assert not undrained_mask(filled, nodata=raw.nodata).any()

    out_path = write_cog(tmp_path / "conditioned.tif", raw.with_data(filled), config=cfg)
    conditioned = read_raster(out_path, config=cfg)

    np.testing.assert_allclose(conditioned.data, filled, rtol=0, atol=1e-4)
    assert not undrained_mask(conditioned.data, nodata=conditioned.nodata).any()
    with rasterio.open(out_path) as src:
        assert src.nodata is not None
        assert src.profile["tiled"] is True


def test_conditioning_a_catchment_with_nodata(tmp_path: Path) -> None:
    cfg = Config()
    catchment = make_synthetic_catchment(rows=70, cols=60, n_pits=3, nodata_border_cells=4, seed=9)

    raw_path = write_cog(tmp_path / "raw.tif", catchment.as_raster(), config=cfg)
    raw = read_raster(raw_path, config=cfg)

    # read_raster masks nodata to NaN, so the border is out of the domain
    assert np.all(np.isnan(raw.data[catchment.nodata_mask]))

    filled = fill_depressions(raw.data, config=cfg)
    assert np.array_equal(np.isnan(filled), catchment.nodata_mask)
    assert not undrained_mask(filled).any()


def test_conditioned_dem_routes_end_to_end(tmp_path: Path) -> None:
    """DEM on disk -> fill -> flow direction, with every path reaching the edge."""
    cfg = Config.model_validate({"terrain": {"fill_epsilon": 1e-4}})
    catchment = make_synthetic_catchment(rows=120, cols=90, n_pits=5, seed=4)

    raw = read_raster(
        write_cog(tmp_path / "raw.tif", catchment.as_raster(), config=cfg), config=cfg
    )
    filled = fill_depressions(raw.data, config=cfg, nodata=raw.nodata)
    fdir = flow_direction(filled, config=cfg, nodata=raw.nodata, cellsize=raw.cellsize)

    # epsilon filling leaves no flats, so every cell is either routed or an outlet
    assert not (fdir == FLOW_NODATA).any()
    assert np.isin(fdir[fdir > 0], D8_CODES).all()

    steps = steps_to_outlet(fdir)  # raises on a cycle
    assert steps.max() < filled.size
    assert steps.max() > 10, "a real catchment should have paths longer than a few cells"


def test_flow_direction_survives_a_cog_roundtrip(tmp_path: Path) -> None:
    """Direction codes are int16 and must come back off disk bit-identical."""
    cfg = Config.model_validate({"terrain": {"fill_epsilon": 1e-4}})
    catchment = make_synthetic_catchment(rows=60, cols=50, n_pits=2, seed=6)

    filled = fill_depressions(catchment.dem.astype(np.float64), config=cfg)
    fdir = flow_direction(filled, config=cfg, cellsize=(catchment.cellsize,) * 2)

    raster = catchment.as_raster().with_data(fdir.astype(np.int16), nodata=FLOW_NODATA)
    path = write_cog(tmp_path / "flowdir.tif", raster, config=cfg)
    back = read_raster(path, config=cfg, masked=False)

    np.testing.assert_array_equal(back.data.astype(np.int16), fdir)
