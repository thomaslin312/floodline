from __future__ import annotations

import numpy as np
import pytest

from floodline.config import Config, HydraulicsConfig
from floodline.hydraulics.stage import (
    GaugeStage,
    constant_stage,
    gauge_reading_to_ahd,
    resolve_gauge,
    slope_stage,
    stage_field,
)
from floodline.synthetic import SyntheticCatchment
from floodline.terrain.route import route_terrain

DATUM = {"hydraulics": {"gauge_datum_offset_m": 10.0}}


# --- the datum, which is the whole point of this module ---------------------------


def test_unset_datum_refuses_to_run() -> None:
    with pytest.raises(ValueError, match="gauge_datum_offset_m is not set"):
        HydraulicsConfig().require_gauge_datum()


def test_unset_datum_blocks_conversion() -> None:
    with pytest.raises(ValueError, match="gauge_datum_offset_m is not set"):
        gauge_reading_to_ahd(14.4)


def test_zero_is_a_legitimate_but_deliberate_datum() -> None:
    cfg = Config.model_validate({"hydraulics": {"gauge_datum_offset_m": 0.0}})
    assert cfg.hydraulics.require_gauge_datum() == 0.0
    assert gauge_reading_to_ahd(14.4, config=cfg) == pytest.approx(14.4)


def test_datum_is_applied() -> None:
    cfg = Config.model_validate(DATUM)
    assert gauge_reading_to_ahd(14.4, config=cfg) == pytest.approx(24.4)


def test_negative_datum_is_allowed() -> None:
    cfg = Config.model_validate({"hydraulics": {"gauge_datum_offset_m": -3.25}})
    assert gauge_reading_to_ahd(10.0, config=cfg) == pytest.approx(6.75)


# --- resolving a gauge against a channel cell --------------------------------------


def test_resolve_gauge_gives_the_channel_depth() -> None:
    """A 14.4 m reading on a gauge whose zero sits at 10 m AHD, over a 21 m bed."""
    dem = np.full((5, 5), 100.0)
    dem[2, 2] = 21.0
    cfg = Config.model_validate(DATUM)  # offset 10.0

    gauge = resolve_gauge(14.4, dem, (2, 2), config=cfg)

    assert isinstance(gauge, GaugeStage)
    assert gauge.reading_m == pytest.approx(14.4)
    assert gauge.ahd_m == pytest.approx(24.4)
    assert gauge.bed_elevation_m == pytest.approx(21.0)
    assert gauge.depth_m == pytest.approx(3.4)
    assert gauge.cell == (2, 2)


def test_reading_below_the_bed_is_refused() -> None:
    """The commonest symptom of a wrong datum offset, so it must not pass silently."""
    dem = np.full((5, 5), 100.0)
    cfg = Config.model_validate(DATUM)
    with pytest.raises(ValueError, match="below the channel bed"):
        resolve_gauge(5.0, dem, (2, 2), config=cfg)


def test_resolve_gauge_depth_arithmetic() -> None:
    dem = np.full((5, 5), 10.0)
    dem[2, 2] = 4.0
    cfg = Config.model_validate({"hydraulics": {"gauge_datum_offset_m": 2.0}})
    gauge = resolve_gauge(5.0, dem, (2, 2), config=cfg)
    assert gauge.ahd_m == pytest.approx(7.0)
    assert gauge.depth_m == pytest.approx(3.0)


def test_gauge_cell_out_of_bounds() -> None:
    cfg = Config.model_validate(DATUM)
    with pytest.raises(ValueError, match="outside the raster"):
        resolve_gauge(5.0, np.zeros((4, 4)), (9, 9), config=cfg)


def test_gauge_cell_on_nodata() -> None:
    dem = np.full((5, 5), 10.0)
    dem[2, 2] = np.nan
    cfg = Config.model_validate(DATUM)
    with pytest.raises(ValueError, match="nodata"):
        resolve_gauge(5.0, dem, (2, 2), config=cfg)


# --- stage fields -------------------------------------------------------------------


def test_constant_stage_is_uniform() -> None:
    field = constant_stage((4, 6), 2.5)
    assert field.shape == (4, 6)
    assert np.all(field == 2.5)


def test_constant_stage_rejects_negative() -> None:
    with pytest.raises(ValueError, match="non-negative"):
        constant_stage((3, 3), -0.1)


def test_slope_stage_falls_upstream_and_rises_downstream(
    catchment: SyntheticCatchment,
) -> None:
    cfg = Config.model_validate(
        {"hydraulics": {"gauge_datum_offset_m": 0.0, "water_surface_slope": 0.001}}
    )
    chain = route_terrain(
        catchment.dem.astype(np.float64),
        cellsize=(catchment.cellsize,) * 2,
        stream_threshold=200,
    )
    # put the gauge on a mid-network stream cell
    stream_cells = np.argwhere(chain.streams)
    row, col = stream_cells[len(stream_cells) // 2]
    gauge = resolve_gauge(
        float(chain.filled[row, col]) + 3.0, chain.filled, (int(row), int(col)), config=cfg
    )

    field = slope_stage(
        chain.filled, chain.flowdir, gauge, config=cfg, cellsize=(catchment.cellsize,) * 2
    )
    assert field[int(row), int(col)] == pytest.approx(gauge.depth_m)
    assert np.all(field >= 0.0)
    # somewhere upstream is shallower, somewhere downstream deeper
    assert field.min() < gauge.depth_m
    assert field.max() > gauge.depth_m


def test_stage_field_dispatches_on_config(catchment: SyntheticCatchment) -> None:
    chain = route_terrain(catchment.dem.astype(np.float64), cellsize=(catchment.cellsize,) * 2)
    stream_cells = np.argwhere(chain.streams)
    row, col = stream_cells[len(stream_cells) // 2]

    base = {"gauge_datum_offset_m": 0.0}
    const_cfg = Config.model_validate({"hydraulics": {**base, "stage_method": "constant"}})
    slope_cfg = Config.model_validate({"hydraulics": {**base, "stage_method": "slope"}})
    gauge = resolve_gauge(
        float(chain.filled[row, col]) + 3.0,
        chain.filled,
        (int(row), int(col)),
        config=const_cfg,
    )

    flat = stage_field(chain.filled, chain.flowdir, gauge, config=const_cfg)
    assert len(np.unique(flat)) == 1

    sloped = stage_field(
        chain.filled,
        chain.flowdir,
        gauge,
        config=slope_cfg,
        cellsize=(catchment.cellsize,) * 2,
    )
    assert len(np.unique(sloped)) > 1


def test_slope_stage_is_never_negative(catchment: SyntheticCatchment) -> None:
    """Far enough upstream the adjustment would go negative; it must clip at zero."""
    cfg = Config.model_validate(
        {"hydraulics": {"gauge_datum_offset_m": 0.0, "water_surface_slope": 0.5}}
    )
    chain = route_terrain(catchment.dem.astype(np.float64), cellsize=(catchment.cellsize,) * 2)
    stream_cells = np.argwhere(chain.streams)
    row, col = stream_cells[len(stream_cells) // 2]
    gauge = resolve_gauge(
        float(chain.filled[row, col]) + 1.0, chain.filled, (int(row), int(col)), config=cfg
    )
    field = slope_stage(
        chain.filled, chain.flowdir, gauge, config=cfg, cellsize=(catchment.cellsize,) * 2
    )
    assert np.all(field >= 0.0)
    assert (field == 0.0).any()
