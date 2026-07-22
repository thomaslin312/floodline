from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from floodline.compute import VSICURL_ENV, watershed_by_huc, watershed_for_point
from floodline.config import Config
from floodline.io.sources import SourceError

SQUARE = {
    "type": "Polygon",
    "coordinates": [[[-95.6, 29.7], [-95.4, 29.7], [-95.4, 29.86], [-95.6, 29.86], [-95.6, 29.7]]],
}


def wbd_client(features: list[dict[str, Any]]) -> httpx.Client:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"type": "FeatureCollection", "features": features})

    return httpx.Client(transport=httpx.MockTransport(handler), base_url="https://example.test")


def feature(huc: str = "1204010403", level: int = 10) -> dict[str, Any]:
    return {
        "type": "Feature",
        "properties": {f"huc{level}": huc, "name": "Test Bayou", "areasqkm": 491.0},
        "geometry": SQUARE,
    }


def test_lookup_by_huc_returns_an_analysis_crs_watershed() -> None:
    unit = watershed_by_huc("1204010403", client=wbd_client([feature()]))
    assert unit.huc == "1204010403"
    assert unit.area_km2 == pytest.approx(491.0)
    west, _, east, _ = unit.bounds
    assert abs(east - west) > 1000, "bounds must be projected metres, not degrees"


def test_the_huc_digit_count_selects_the_level() -> None:
    """A 12-digit code is a subwatershed; the caller should not have to say so."""
    unit = watershed_by_huc("120401040302", client=wbd_client([feature("120401040302", 12)]))
    assert unit.huc == "120401040302"


def test_an_unknown_huc_is_a_clear_error() -> None:
    with pytest.raises(SourceError, match="no HUC-10 watershed"):
        watershed_by_huc("0000000000", client=wbd_client([]))


def test_point_lookup_picks_the_containing_polygon() -> None:
    """WBD returns everything the query envelope touches; only one contains the point."""
    far = {
        "type": "Feature",
        "properties": {"huc10": "9999999999", "name": "Elsewhere", "areasqkm": 10.0},
        "geometry": {
            "type": "Polygon",
            "coordinates": [
                [[-96.9, 29.0], [-96.8, 29.0], [-96.8, 29.1], [-96.9, 29.1], [-96.9, 29.0]]
            ],
        },
    }
    unit = watershed_for_point(-95.5, 29.8, client=wbd_client([far, feature()]))
    assert unit.huc == "1204010403"


def test_point_with_no_watershed_is_an_error() -> None:
    with pytest.raises(SourceError, match="no HUC-10 watershed contains"):
        watershed_for_point(0.0, 0.0, client=wbd_client([]))


def test_an_oversized_watershed_is_refused_before_any_network_work() -> None:
    """Depression filling is global, so the whole grid must fit in memory at once."""
    from floodline.compute import compute_watershed

    unit = watershed_by_huc("1204010403", client=wbd_client([feature()]))
    with pytest.raises(ValueError, match=r"over the .* limit"):
        compute_watershed(unit, resolution_m=1.0, max_cells=1_000_000, client=wbd_client([]))


def test_gdal_retry_settings_are_present_and_numeric() -> None:
    """A truncated range read is normal over HTTP, and GDAL does not retry unless told.

    Without this, one short read fails a whole watershed several minutes into the job -
    which is exactly what happened before these were set.
    """
    assert VSICURL_ENV["GDAL_HTTP_MAX_RETRY"] == 5
    assert isinstance(VSICURL_ENV["GDAL_HTTP_RETRY_DELAY"], int)
    assert VSICURL_ENV["GDAL_DISABLE_READDIR_ON_OPEN"] == "EMPTY_DIR"


def test_config_round_trips_through_json() -> None:
    """A service handler receives config as JSON, so it has to survive the trip."""
    cfg = Config.model_validate({"terrain": {"stream_threshold_cells": 5000}})
    again = Config.model_validate(json.loads(cfg.model_dump_json()))
    assert again.terrain.stream_threshold_cells == 5000
    assert again.crs.analysis.to_epsg() == 6587
