"""Pure parts of the population reader: URLs, and how a grid is cleaned.

The network read itself is not covered here — it needs a live WorldPop or JRC
endpoint, and a mocked one would only test rasterio.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest
from rasterio.crs import CRS
from rasterio.transform import Affine

from floodline.io.population import (
    PopulationNotLocalError,
    PopulationProduct,
    bounds_wgs84,
    ensure_population_raster,
    population_url,
)
from floodline.io.raster import Raster


def test_worldpop_constrained_url_is_per_country_and_year() -> None:
    url = population_url(PopulationProduct.WORLDPOP_CONSTRAINED, iso3="USA", year=2020)
    assert url.endswith("USA/usa_ppp_2020_UNadj_constrained.tif")
    assert "Global_2000_2020_Constrained" in url


def test_worldpop_unconstrained_uses_the_other_tree() -> None:
    url = population_url(PopulationProduct.WORLDPOP_UNCONSTRAINED, iso3="USA", year=2020)
    assert url.endswith("USA/usa_ppp_2020_UNadj.tif")
    assert "Constrained" not in url


def test_ghs_pop_is_global_and_ignores_the_country() -> None:
    a = population_url(PopulationProduct.GHS_POP, iso3="USA", year=2020)
    b = population_url(PopulationProduct.GHS_POP, iso3="AUS", year=2020)
    assert a == b
    assert "GHS_POP_E2020" in a


def test_country_code_is_lowercased_in_the_filename_not_the_directory() -> None:
    url = population_url(PopulationProduct.WORLDPOP_CONSTRAINED, iso3="AUS", year=2020)
    assert "/AUS/aus_ppp_" in url


def _raster() -> Raster:
    # 100 m cells in UTM 15N over Houston.
    transform = Affine.translation(260000.0, 3300000.0) * Affine.scale(100.0, -100.0)
    return Raster(
        data=np.zeros((10, 10), dtype=np.float64),
        transform=transform,
        crs=CRS.from_epsg(26915),
        nodata=-9999.0,
    )


def test_raster_bounds_are_west_south_east_north() -> None:
    west, south, east, north = _raster().bounds
    assert west == pytest.approx(260000.0)
    assert north == pytest.approx(3300000.0)
    assert east == pytest.approx(261000.0)
    assert south == pytest.approx(3299000.0)


def test_bounds_wgs84_lands_in_texas() -> None:
    west, south, east, north = bounds_wgs84(_raster())
    assert -96.0 < west < -95.0
    assert 29.0 < south < 30.5
    assert west < east and south < north


def test_a_missing_local_raster_names_the_command_rather_than_downloading(
    tmp_path: Path,
) -> None:
    # WorldPop cannot be windowed over HTTP, so the fallback is a 494 MB download.
    # Doing that implicitly would make any script that calls this unsafe to run.
    with pytest.raises(PopulationNotLocalError, match="fetch-population"):
        ensure_population_raster(
            PopulationProduct.WORLDPOP_CONSTRAINED, cache_dir=tmp_path, download=False
        )


def test_an_already_cached_raster_is_returned_without_a_request(tmp_path: Path) -> None:
    target = tmp_path / "worldpop_constrained-usa-2020.tif"
    target.write_bytes(b"not really a tiff, but non-empty")
    got = ensure_population_raster(
        PopulationProduct.WORLDPOP_CONSTRAINED, cache_dir=tmp_path, download=False
    )
    assert got == target


def test_an_empty_cache_file_is_not_mistaken_for_a_download(tmp_path: Path) -> None:
    (tmp_path / "worldpop_constrained-usa-2020.tif").touch()
    with pytest.raises(PopulationNotLocalError):
        ensure_population_raster(
            PopulationProduct.WORLDPOP_CONSTRAINED, cache_dir=tmp_path, download=False
        )
