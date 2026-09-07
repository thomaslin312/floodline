from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from floodline.compute import VSICURL_ENV, watershed_by_huc, watershed_for_point
from floodline.core.config import Config
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
    unit, _ = watershed_by_huc("1204010403", client=wbd_client([feature()]))
    assert unit.huc == "1204010403"
    assert unit.area_km2 == pytest.approx(491.0)
    west, _, east, _ = unit.bounds
    assert abs(east - west) > 1000, "bounds must be projected metres, not degrees"


def test_the_huc_digit_count_selects_the_level() -> None:
    """A 12-digit code is a subwatershed; the caller should not have to say so."""
    unit, _ = watershed_by_huc("120401040302", client=wbd_client([feature("120401040302", 12)]))
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
    unit, _ = watershed_for_point(-95.5, 29.8, client=wbd_client([far, feature()]))
    assert unit.huc == "1204010403"


def test_point_with_no_watershed_is_an_error() -> None:
    with pytest.raises(SourceError, match="no HUC-10 watershed contains"):
        watershed_for_point(0.0, 0.0, client=wbd_client([]))


def test_an_oversized_watershed_is_refused_before_any_network_work() -> None:
    """Depression filling is global, so the whole grid must fit in memory at once."""
    from floodline.compute import compute_watershed

    unit, local = watershed_by_huc("1204010403", client=wbd_client([feature()]))
    with pytest.raises(ValueError, match=r"over the .* limit"):
        compute_watershed(
            unit, resolution_m=1.0, max_cells=1_000_000, config=local, client=wbd_client([])
        )


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


def test_the_analysis_crs_follows_the_watershed() -> None:
    """A fixed CRS only works for a fixed study area.

    EPSG:6587 is Texas South Central: right for Houston, meaningless in Oregon. For a
    tool that takes any watershed in the country the projection has to follow it.
    """
    from floodline.compute import utm_crs_for

    _, texas = watershed_by_huc("1204010403", client=wbd_client([feature()]))
    assert texas.crs.analysis.to_epsg() == 26915, "Houston is UTM zone 15N"

    assert utm_crs_for(-122.7, 45.5).to_epsg() == 26910, "Portland is zone 10N"
    assert utm_crs_for(-80.2, 25.8).to_epsg() == 26917, "Miami is zone 17N"
    assert utm_crs_for(-95.4, 29.8).name.endswith("15N")


def test_every_continental_utm_zone_is_a_valid_analysis_crs() -> None:
    from floodline.compute import utm_crs_for
    from floodline.core.config import validate_projected_crs

    for lon in range(-124, -66, 6):
        crs = utm_crs_for(float(lon), 40.0)
        assert validate_projected_crs(crs).is_projected


def test_southern_latitudes_are_refused_rather_than_silently_wrong() -> None:
    from floodline.compute import utm_crs_for

    with pytest.raises(ValueError, match="southern hemisphere"):
        utm_crs_for(151.2, -33.9)


def test_event_matching_picks_the_flood_the_marks_came_from() -> None:
    """A residual against the wrong flood measures the gap between two events.

    This is why the map and the assessment must share one definition: for months
    only the map did it, and the reference watershed was the one place it made no
    difference, because Harvey is Whiteoak Bayou's peak of record.
    """
    from floodline.compute import event_matched_gauge

    gauge = {
        "site": "05464500",
        "discharge_cms": 2400.0,  # peak of record, 1993
        "series": [
            {"date": "1993-07-09", "cms": 2400.0},
            {"date": "2008-06-12", "cms": 1600.0},
            {"date": "2019-05-30", "cms": 900.0},
        ],
    }
    marks = [{"event_date": "2008-06-14"}] * 7 + [{"event_date": "1993-07-10"}]
    matched = event_matched_gauge(gauge, marks)
    assert matched is not None
    assert matched["event_discharge_cms"] == 1600.0, "the dominant year wins, not the largest"
    assert matched["event_year"] == "2008"
    assert matched["matched_marks"] == 7
    # The peak of record is preserved: it is still what "in the record" is measured against.
    assert matched["discharge_cms"] == 2400.0


def test_event_matching_leaves_the_gauge_alone_when_it_cannot_match() -> None:
    from floodline.compute import event_matched_gauge

    gauge = {"discharge_cms": 500.0, "series": [{"date": "1993-07-09", "cms": 500.0}]}
    assert event_matched_gauge(gauge, []) is gauge, "no marks, nothing to match"
    assert event_matched_gauge(None, [{"event_date": "1993-01-01"}]) is None
    # A year the gauge never recorded is not a match, and must not invent one.
    unmatched = event_matched_gauge(gauge, [{"event_date": "2011-08-28"}])
    assert unmatched is not None and "event_discharge_cms" not in unmatched
    # Marks with no date at all.
    assert event_matched_gauge(gauge, [{"event_date": ""}]) is gauge


def test_coastal_marks_are_excluded_from_scoring() -> None:
    """HAND has no surge term, so a coastal mark is the wrong physics, not a hard case.

    Counting them as misses made Monterey Bay look like a 2.1 m error when what it
    actually shows is that the method does not model the mechanism that flooded it.
    """
    from floodline.compute import scorable_marks

    marks = [
        {"quality": 1, "environment": "Riverine"},
        {"quality": 2, "environment": "Coastal"},
        {"quality": 1, "environment": "coastal"},  # case is not guaranteed
        {"quality": 1, "environment": ""},  # unlabelled stays in
        {"quality": 3, "environment": "Riverine"},  # rough survey, dropped by grade
    ]
    usable, n_coastal = scorable_marks(marks)
    assert len(usable) == 2
    assert n_coastal == 2
    assert all((m.get("environment") or "").lower() != "coastal" for m in usable)

    # Ungraded scoring keeps grade 3 but still drops coastal.
    usable_all, n_coastal_all = scorable_marks(marks, graded_only=False)
    assert len(usable_all) == 3
    assert n_coastal_all == 2
