from __future__ import annotations

import json
from pathlib import Path

import pytest
from pyproj import CRS
from shapely.geometry import Polygon

from floodline.compute import marks_within
from floodline.config import Config
from floodline.io.ingest import Watershed


def houston_unit() -> tuple[Watershed, Config]:
    """A square watershed around Houston, in the UTM zone that suits it."""
    config = Config.model_validate({"crs": {"analysis": "EPSG:26915"}})
    from pyproj import Transformer

    project = Transformer.from_crs(CRS.from_epsg(4326), config.crs.analysis, always_xy=True)
    ring = [
        project.transform(x, y)
        for x, y in [(-95.5, 29.7), (-95.3, 29.7), (-95.3, 29.85), (-95.5, 29.85)]
    ]
    return Watershed(huc="1", name="Test", area_km2=100.0, geometry=Polygon(ring)), config


def write_marks(path: Path) -> Path:
    path.write_text(
        json.dumps(
            [
                {
                    "longitude_dd": -95.40,
                    "latitude_dd": 29.78,
                    "elev_ft": 40.0,
                    "eventName": "2017 Harvey",
                    "eventDate": "2017-08-24",
                    "hwm_quality_id": 1,
                    "height_above_gnd": 3.0,
                },
                {
                    "longitude_dd": -95.35,
                    "latitude_dd": 29.75,
                    "elev_ft": 30.0,
                    "eventName": "2017 Harvey",
                    "eventDate": "2017-08-24",
                    "hwm_quality_id": 2,
                    "height_above_gnd": None,
                },
                {
                    "longitude_dd": -99.00,
                    "latitude_dd": 31.00,
                    "elev_ft": 50.0,
                    "eventName": "Somewhere else",
                    "eventDate": "2011-08-01",
                    "hwm_quality_id": 1,
                    "height_above_gnd": 1.0,
                },
            ]
        )
    )
    return path


def test_only_marks_inside_the_watershed_are_returned(tmp_path: Path) -> None:
    unit, config = houston_unit()
    marks = marks_within(unit, config, write_marks(tmp_path / "m.json"))
    assert len(marks) == 2
    assert {m["event"] for m in marks} == {"2017 Harvey"}


def test_elevations_are_converted_to_metres(tmp_path: Path) -> None:
    """Feet to metres, rounded to a centimetre - finer than any survey, and it keeps
    the payload small enough to ship a whole watershed's marks inline."""
    unit, config = houston_unit()
    marks = marks_within(unit, config, write_marks(tmp_path / "m.json"))
    assert marks[0]["elev_m"] == pytest.approx(40.0 * 0.3048, abs=0.005)
    assert marks[0]["height_above_gnd_m"] == pytest.approx(3.0 * 0.3048, abs=0.005)


def test_a_missing_height_above_ground_is_none_not_zero(tmp_path: Path) -> None:
    """Zero would read as 'the water was at ground level', which is a claim."""
    unit, config = houston_unit()
    marks = marks_within(unit, config, write_marks(tmp_path / "m.json"))
    assert marks[1]["height_above_gnd_m"] is None


def test_the_event_name_and_date_survive(tmp_path: Path) -> None:
    """Matching a gauge peak to the right flood depends on the event date."""
    unit, config = houston_unit()
    marks = marks_within(unit, config, write_marks(tmp_path / "m.json"))
    assert marks[0]["event_date"] == "2017-08-24"


def test_an_unnamed_event_is_labelled_rather_than_blank(tmp_path: Path) -> None:
    path = tmp_path / "m.json"
    path.write_text(
        json.dumps(
            [
                {
                    "longitude_dd": -95.40,
                    "latitude_dd": 29.78,
                    "elev_ft": 40.0,
                    "eventName": "",
                    "hwm_quality_id": 1,
                }
            ]
        )
    )
    unit, config = houston_unit()
    assert marks_within(unit, config, path)[0]["event"] == "unnamed event"


def test_a_missing_cache_file_yields_nothing_rather_than_raising(tmp_path: Path) -> None:
    """A server without the national marks cached should still compute watersheds."""
    unit, config = houston_unit()
    assert marks_within(unit, config, tmp_path / "absent.json") == []
