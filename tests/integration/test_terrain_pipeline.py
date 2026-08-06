"""End-to-end on a tiny synthetic catchment: DEM on disk in, conditioned DEM out.

This grows a stage at a time as the pipeline is built. Today it covers everything
that exists: synthesise a catchment, write it as a COG, read it back through the
CRS-checking reader, fill it, and confirm the result is a surface every cell can
drain off.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest
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


def test_dem_to_hand_to_three_nested_extents(tmp_path: Path) -> None:
    """The whole model on the synthetic catchment: DEM -> HAND -> extent at 3 stages.

    The stages are nested by construction — a higher water level floods a superset
    of the cells a lower one does — and that is asserted cell by cell, not just as
    growing areas, because two extents can both grow while swapping cells.
    """
    from floodline.hydraulics.inundate import inundate
    from floodline.hydraulics.stage import resolve_gauge, stage_field
    from floodline.io.vector import read_vector, write_vector
    from floodline.terrain.route import route_terrain
    from floodline.terrain.streams import stream_network

    cfg = Config.model_validate(
        {
            "terrain": {"stream_threshold_cells": 200, "min_stream_length_cells": 5},
            "hydraulics": {
                "gauge_datum_offset_m": 0.0,
                "gauge_reading_unit": "m",
                "min_depth_m": 0.05,
            },
        }
    )
    catchment = make_synthetic_catchment(rows=140, cols=110, n_pits=6, seed=21)

    # DEM on disk, read back through the CRS-checking reader
    dem_path = write_cog(tmp_path / "dem.tif", catchment.as_raster(), config=cfg)
    raster = read_raster(dem_path, config=cfg)

    chain = route_terrain(raster.data, config=cfg, nodata=raster.nodata, cellsize=raster.cellsize)
    assert chain.drains_completely, "the defaults must not strand water"
    assert chain.flat_cells_after == 0
    assert not undrained_mask(chain.filled, nodata=raster.nodata).any()

    # the vector network round-trips through GeoParquet
    network = stream_network(
        chain.streams,
        chain.flowdir,
        chain.accumulation.accumulation,
        raster.transform,
        raster.crs,
    )
    net_path = write_vector(tmp_path / "streams.parquet", network, config=cfg)
    assert len(read_vector(net_path, config=cfg)) == len(network)

    # HAND is well formed
    heights = chain.hand.hand
    assert np.all(heights[chain.streams] == 0.0)
    assert np.all(heights[np.isfinite(heights)] >= 0.0)

    # a gauge on the highest-accumulation stream cell
    accumulation = np.where(chain.streams, chain.accumulation.accumulation, -np.inf)
    row, col = np.unravel_index(int(np.argmax(accumulation)), accumulation.shape)
    bed = float(chain.filled[row, col])

    extents = []
    for rise in (1.0, 3.0, 6.0):
        gauge = resolve_gauge(bed + rise, chain.filled, (int(row), int(col)), config=cfg)
        assert gauge.depth_m == pytest.approx(rise)
        stage = stage_field(
            chain.filled, chain.flowdir, gauge, config=cfg, cellsize=raster.cellsize
        )
        flood = inundate(
            heights,
            stage,
            streams=chain.streams,
            config=cfg,
            cell_area_m2=raster.cell_area_m2,
        )
        extents.append(flood)
        write_cog(tmp_path / f"depth_{rise:g}.tif", raster.with_data(flood.depth), config=cfg)

    low, mid, high = extents
    assert 0 < low.n_wet < mid.n_wet < high.n_wet
    assert np.all(mid.wet >= low.wet), "extents must be nested, not merely larger"
    assert np.all(high.wet >= mid.wet)
    assert low.max_depth_m <= 1.0 + 1e-9
    assert mid.max_depth_m <= 3.0 + 1e-9
    assert high.max_depth_m <= 6.0 + 1e-9
    assert high.area_m2 == pytest.approx(high.n_wet * raster.cell_area_m2)

    # every wet cell touches the channel, at every stage
    for flood in extents:
        assert np.all(flood.wet[chain.streams])


def test_depth_rasters_round_trip_through_cog(tmp_path: Path) -> None:
    from floodline.hydraulics.inundate import inundate
    from floodline.terrain.route import route_terrain

    cfg = Config.model_validate({"terrain": {"stream_threshold_cells": 150}})
    catchment = make_synthetic_catchment(rows=90, cols=70, n_pits=3, seed=13)
    raster = read_raster(
        write_cog(tmp_path / "dem.tif", catchment.as_raster(), config=cfg), config=cfg
    )
    chain = route_terrain(raster.data, config=cfg, nodata=raster.nodata, cellsize=raster.cellsize)
    flood = inundate(
        chain.hand.hand,
        2.0,
        streams=chain.streams,
        config=cfg,
        cell_area_m2=raster.cell_area_m2,
    )
    path = write_cog(
        tmp_path / "depth.tif", raster.with_data(flood.depth), config=cfg, dtype="float32"
    )
    back = read_raster(path, config=cfg)
    wet_again = np.isfinite(back.data) & (back.data >= cfg.hydraulics.min_depth_m)
    np.testing.assert_array_equal(wet_again, flood.wet)
