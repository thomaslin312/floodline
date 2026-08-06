from __future__ import annotations

import tomllib
from pathlib import Path

import pytest
from pydantic import ValidationError
from pyproj import CRS

from floodline.config import (
    Config,
    CurveFamily,
    MonteCarloConfig,
    validate_projected_crs,
)


def test_defaults_are_houston_texas_south_central() -> None:
    cfg = Config()
    assert cfg.crs.analysis.to_epsg() == 6587
    assert cfg.crs.analysis.is_projected
    assert not cfg.crs.allow_reprojection


@pytest.mark.parametrize("crs", ["EPSG:6587", 6587, 26915, "EPSG:32615", 7856, CRS.from_epsg(6587)])
def test_projected_metre_crs_accepted(crs: object) -> None:
    assert validate_projected_crs(crs).is_projected  # type: ignore[arg-type]


@pytest.mark.parametrize("crs", ["EPSG:4326", 4283, "EPSG:7844"])
def test_geographic_crs_refused(crs: object) -> None:
    with pytest.raises(ValueError, match="geographic CRS"):
        validate_projected_crs(crs)  # type: ignore[arg-type]


@pytest.mark.parametrize("crs", [3857, "EPSG:3857", 900913, 3785, 3395])
def test_whole_world_mercator_refused(crs: object) -> None:
    """Pseudo-Mercator's axis unit is the metre, but not a ground metre.

    Its scale factor is 1/cos(latitude), so at Houston a "metre" is about 15% too
    long and areas are out by 30%. Both the USGS 3DEP and NSW elevation image
    services serve 3857 natively, so this is the CRS a fetched raster is most
    likely to arrive in - which is why accepting it would be the easiest way to
    get quietly wrong slopes and areas.
    """
    with pytest.raises(ValueError, match="whole-world Mercator"):
        validate_projected_crs(crs)  # type: ignore[arg-type]


@pytest.mark.parametrize("crs", [26915, 32615, 7856, 28356])
def test_transverse_mercator_is_not_caught_by_the_mercator_rule(crs: int) -> None:
    """UTM and MGA are Transverse Mercator: scale is referenced to a meridian."""
    assert validate_projected_crs(crs).is_projected


def test_lambert_conformal_conic_is_accepted() -> None:
    assert validate_projected_crs(6587).name.startswith("NAD83(2011) / Texas")


def test_non_metre_projected_crs_refused() -> None:
    # NAD83(2011) / Texas South Central (ftUS) — the survey-foot twin of the
    # analysis CRS, which is exactly the mistake most likely to be made here.
    with pytest.raises(ValueError, match="metres"):
        validate_projected_crs("EPSG:6588")
    with pytest.raises(ValueError, match="metres"):
        validate_projected_crs("EPSG:2277")


def test_config_rejects_geographic_analysis_crs() -> None:
    with pytest.raises(ValidationError):
        Config.model_validate({"crs": {"analysis": "EPSG:4326"}})


def test_config_is_frozen() -> None:
    cfg = Config()
    with pytest.raises(ValidationError):
        cfg.terrain.stream_threshold_cells = 5  # type: ignore[misc]


def test_unknown_keys_are_an_error() -> None:
    with pytest.raises(ValidationError):
        Config.model_validate({"terrain": {"stream_thresold_cells": 500}})


def test_bad_numeric_bounds_rejected() -> None:
    with pytest.raises(ValidationError):
        Config.model_validate({"terrain": {"stream_threshold_cells": 0}})
    with pytest.raises(ValidationError):
        Config.model_validate({"terrain": {"fill_epsilon": -1e-6}})
    with pytest.raises(ValidationError):
        Config.model_validate({"monte_carlo": {"n_samples": 0}})


def test_monte_carlo_interval_must_increase() -> None:
    with pytest.raises(ValidationError):
        MonteCarloConfig(interval=(0.95, 0.05))


def test_default_class_must_be_priced() -> None:
    with pytest.raises(ValidationError):
        Config.model_validate({"damage": {"default_class": "castle"}})


def test_from_file_roundtrip(tmp_path: Path) -> None:
    path = tmp_path / "floodline.toml"
    path.write_text(
        "[floodline.terrain]\n"
        "stream_threshold_cells = 250\n"
        "\n"
        "[floodline.damage]\n"
        'curve_family = "hazus"\n'
        "\n"
        "[floodline.crs]\n"
        'analysis = "EPSG:28356"\n'
    )
    cfg = Config.from_file(path)
    assert cfg.terrain.stream_threshold_cells == 250
    assert cfg.damage.curve_family is CurveFamily.HAZUS
    assert cfg.crs.analysis.to_epsg() == 28356
    # sanity: the file we wrote is valid TOML
    with path.open("rb") as handle:
        assert "floodline" in tomllib.load(handle)


def test_from_file_rejects_geographic(tmp_path: Path) -> None:
    path = tmp_path / "bad.toml"
    path.write_text('[floodline.crs]\nanalysis = "EPSG:4326"\n')
    with pytest.raises(ValidationError):
        Config.from_file(path)


def test_no_magic_numbers_escape_config() -> None:
    """Every threshold the pipeline needs is reachable from the top-level Config."""
    cfg = Config()
    assert cfg.hydraulics.min_depth_m > 0
    assert cfg.exposure.population_depth_threshold_m > 0
    assert cfg.damage.replacement_cost_per_m2[cfg.damage.default_class] > 0
    assert cfg.monte_carlo.n_samples > 0
    assert cfg.validation.otsu_bins > 1
