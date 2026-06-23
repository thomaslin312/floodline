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


def test_defaults_are_lismore_mga56() -> None:
    cfg = Config()
    assert cfg.crs.analysis.to_epsg() == 7856
    assert cfg.crs.analysis.is_projected
    assert not cfg.crs.allow_reprojection


@pytest.mark.parametrize("crs", ["EPSG:7856", 7856, 28356, "EPSG:32756", CRS.from_epsg(7856)])
def test_projected_metre_crs_accepted(crs: object) -> None:
    assert validate_projected_crs(crs).is_projected  # type: ignore[arg-type]


@pytest.mark.parametrize("crs", ["EPSG:4326", 4283, "EPSG:7844"])
def test_geographic_crs_refused(crs: object) -> None:
    with pytest.raises(ValueError, match="geographic CRS"):
        validate_projected_crs(crs)  # type: ignore[arg-type]


def test_non_metre_projected_crs_refused() -> None:
    # NAD83 / Texas Central (ftUS) — projected, but the axes are survey feet.
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
