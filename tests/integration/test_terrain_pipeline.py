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
