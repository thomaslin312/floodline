from __future__ import annotations

from pathlib import Path
from typing import Any

import geopandas as gpd
import httpx
import pytest
import shapely

from floodline.io.nsi import fetch_nsi_structures, structure_footprints

BOX = shapely.box(-95.46, 29.79, -95.44, 29.81)


def _feature(**overrides: Any) -> dict[str, Any]:
    properties = {
        "fd_id": 1,
        "occtype": "RES1-1SNB",
        "st_damcat": "RES",
        "val_struct": 300000.0,
        "val_cont": 150000.0,
        "val_vehic": 20000.0,
        "sqft": 2000.0,
        "ftprntsqft": 1200.0,
        "num_story": 1.0,
        "found_ht": 1.0,
        "found_type": "S",
        "ground_elv": 60.0,
        "med_yr_blt": 1984,
        "pop2amu65": 2,
        "pop2amo65": 1,
        "pop2pmu65": 0,
        "pop2pmo65": 1,
    }
    properties.update(overrides)
    return {
        "type": "Feature",
        "geometry": {"type": "Point", "coordinates": [-95.45, 29.80]},
        "properties": properties,
    }


def _client(features: list[dict[str, Any]]) -> httpx.Client:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"type": "FeatureCollection", "features": features})

    return httpx.Client(transport=httpx.MockTransport(handler))


def test_units_convert_to_si(tmp_path: Path) -> None:
    got = fetch_nsi_structures(
        BOX, cache_dir=tmp_path, client=_client([_feature()]), use_cache=False
    )
    row = got.structures.iloc[0]
    assert row["footprint_m2"] == pytest.approx(1200 * 0.092903)
    assert row["floor_area_m2"] == pytest.approx(2000 * 0.092903)
    assert row["found_ht_m"] == pytest.approx(0.3048)


def test_day_and_night_population_are_summed_across_age_bands(tmp_path: Path) -> None:
    got = fetch_nsi_structures(
        BOX, cache_dir=tmp_path, client=_client([_feature()]), use_cache=False
    )
    assert got.night_population == pytest.approx(3.0)
    assert got.day_population == pytest.approx(1.0)


def test_an_empty_result_still_has_the_expected_columns(tmp_path: Path) -> None:
    got = fetch_nsi_structures(BOX, cache_dir=tmp_path, client=_client([]), use_cache=False)
    assert len(got.structures) == 0
    for column in ("footprint_m2", "floor_area_m2", "found_ht_m", "pop_night", "pop_day"):
        assert column in got.structures.columns


def test_storeys_never_drop_below_one(tmp_path: Path) -> None:
    got = fetch_nsi_structures(
        BOX, cache_dir=tmp_path, client=_client([_feature(num_story=0)]), use_cache=False
    )
    assert got.structures.iloc[0]["num_story"] == 1.0


def test_results_are_cached_and_reused(tmp_path: Path) -> None:
    first = fetch_nsi_structures(
        BOX, cache_dir=tmp_path, cache_key="test", client=_client([_feature()])
    )
    assert first.from_cache is False

    def refuse(request: httpx.Request) -> httpx.Response:
        raise AssertionError("cached result should not hit the network")

    second = fetch_nsi_structures(
        BOX,
        cache_dir=tmp_path,
        cache_key="test",
        client=httpx.Client(transport=httpx.MockTransport(refuse)),
    )
    assert second.from_cache is True
    assert len(second.structures) == 1


def test_footprints_square_the_recorded_area(tmp_path: Path) -> None:
    got = fetch_nsi_structures(
        BOX, cache_dir=tmp_path, client=_client([_feature()]), use_cache=False
    )
    boxes = structure_footprints(got.structures.to_crs("EPSG:26915"))
    area = boxes.geometry.iloc[0].area
    assert area == pytest.approx(1200 * 0.092903, rel=1e-6)


def test_a_structure_with_no_footprint_area_still_gets_sampled(tmp_path: Path) -> None:
    got = fetch_nsi_structures(
        BOX, cache_dir=tmp_path, client=_client([_feature(ftprntsqft=0)]), use_cache=False
    )
    boxes = structure_footprints(got.structures.to_crs("EPSG:26915"), min_side_m=4.0)
    assert boxes.geometry.iloc[0].area == pytest.approx(16.0)


def test_squaring_a_geographic_frame_is_refused(tmp_path: Path) -> None:
    got = fetch_nsi_structures(
        BOX, cache_dir=tmp_path, client=_client([_feature()]), use_cache=False
    )
    # Boxing degrees by metres would silently produce footprints the size of a county.
    with pytest.raises(ValueError, match="projected CRS"):
        structure_footprints(got.structures)


def test_missing_values_become_zero_not_nan(tmp_path: Path) -> None:
    got = fetch_nsi_structures(
        BOX, cache_dir=tmp_path, client=_client([_feature(val_cont=None)]), use_cache=False
    )
    assert got.structures.iloc[0]["val_cont"] == 0.0


def test_geometry_is_posted_as_geojson(tmp_path: Path) -> None:
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        import json as _json

        seen.update(_json.loads(request.content))
        return httpx.Response(200, json={"type": "FeatureCollection", "features": []})

    fetch_nsi_structures(
        BOX,
        cache_dir=tmp_path,
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        use_cache=False,
    )
    assert seen["geometry"]["type"] == "Polygon"
    assert isinstance(
        gpd.GeoSeries([shapely.from_geojson(__import__("json").dumps(seen["geometry"]))]).iloc[0],
        shapely.Polygon,
    )
