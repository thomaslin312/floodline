from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from floodline.core.config import Config
from floodline.service import create_app, evict_cache


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    """An app whose every outbound call is served by a mock transport."""

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "tigerweb" in url:
            return httpx.Response(
                200,
                json={
                    "features": [
                        {
                            "attributes": {
                                "ZCTA5": "77007",
                                "CENTLAT": "+29.77",
                                "CENTLON": "-095.41",
                            }
                        }
                    ]
                },
            )
        if "geocoding.geo.census.gov" in url:
            return httpx.Response(
                200,
                json={
                    "result": {
                        "addressMatches": [
                            {
                                "matchedAddress": "1 Main St, Houston, TX",
                                "coordinates": {"x": -95.36, "y": 29.76},
                            }
                        ]
                    }
                },
            )
        if "wbd" in url:
            return httpx.Response(
                200,
                json={
                    "type": "FeatureCollection",
                    "features": [
                        {
                            "type": "Feature",
                            "properties": {
                                "huc12": "120401040305",
                                "name": "Test Bayou",
                                "areasqkm": 53.0,
                            },
                            "geometry": {
                                "type": "Polygon",
                                "coordinates": [
                                    [
                                        [-95.5, 29.7],
                                        [-95.4, 29.7],
                                        [-95.4, 29.8],
                                        [-95.5, 29.8],
                                        [-95.5, 29.7],
                                    ]
                                ],
                            },
                        }
                    ],
                },
            )
        return httpx.Response(404)

    import floodline.service as svc

    monkeypatch.setattr(
        svc,
        "make_client",
        lambda settings=None: httpx.Client(
            transport=httpx.MockTransport(handler), base_url="https://example.test"
        ),
    )
    return TestClient(create_app(config=Config(), cache_dir=tmp_path))


def test_health(client: TestClient) -> None:
    body = client.get("/api/health").json()
    assert body["ok"] is True
    assert body["cached_watersheds"] == 0


def test_index_is_served(client: TestClient) -> None:
    response = client.get("/")
    assert response.status_code == 200
    assert "floodline" in response.text
    assert "leaflet" in response.text.lower()


def test_zip_code_geocodes_via_the_census_zcta_layer(client: TestClient) -> None:
    body = client.get("/api/geocode", params={"q": "77007"}).json()
    assert body["lat"] == pytest.approx(29.77)
    assert body["lon"] == pytest.approx(-95.41)
    assert "77007" in body["label"]


def test_an_address_geocodes_via_the_census_address_service(client: TestClient) -> None:
    body = client.get("/api/geocode", params={"q": "1 Main St, Houston TX"}).json()
    assert body["lon"] == pytest.approx(-95.36)
    assert "Houston" in body["label"]


def test_a_short_query_is_rejected(client: TestClient) -> None:
    assert client.get("/api/geocode", params={"q": "x"}).status_code == 422


def test_point_lookup_returns_an_outline_in_degrees(client: TestClient) -> None:
    body = client.get("/api/watershed", params={"lon": -95.45, "lat": 29.75}).json()
    assert body["huc"] == "120401040305"
    assert body["analysis_crs"] == "EPSG:26915", "the CRS must follow the location"
    ring = body["geometry"]["coordinates"][0]
    assert all(-180 <= x <= 180 and -90 <= y <= 90 for x, y in ring), "map wants degrees"


def test_watershed_reports_whether_it_is_too_big_for_ten_metres(client: TestClient) -> None:
    body = client.get("/api/watershed", params={"lon": -95.45, "lat": 29.75}).json()
    assert "too_big_at_10m" in body
    assert body["cells_at"]["10"] > body["cells_at"]["30"]


def test_a_malformed_huc_is_rejected_before_any_network_call(client: TestClient) -> None:
    for bad in ("abc", "123", "1" * 18):
        response = client.get(f"/api/watershed/{bad}")
        assert response.status_code == 400, bad


def test_out_of_range_coordinates_are_rejected(client: TestClient) -> None:
    assert client.get("/api/watershed", params={"lon": 999, "lat": 0}).status_code == 422


def test_a_cached_bundle_is_served_without_recomputing(client: TestClient, tmp_path: Path) -> None:
    """The second person to ask about a watershed should wait for a file read."""
    from floodline.service import CACHE_SCHEMA

    (tmp_path / "120401040305_10m.json").write_text(
        json.dumps({"huc": "120401040305", "hand": "x", "schema": CACHE_SCHEMA})
    )
    body = client.get("/api/compute/120401040305", params={"resolution": 10}).json()
    assert body["cached"] is True
    assert body["huc"] == "120401040305"


def test_resolution_is_bounded(client: TestClient) -> None:
    assert client.get("/api/compute/120401040305", params={"resolution": 0.1}).status_code == 422
    assert client.get("/api/compute/120401040305", params={"resolution": 500}).status_code == 422


def test_a_missing_watershed_is_404_on_every_route(monkeypatch: pytest.MonkeyPatch) -> None:
    """Not-found and upstream-broken are different failures and different codes.

    Reporting "that HUC does not exist" as a 502 sends whoever is debugging looking
    for an outage; reporting a real outage as a 404 sends them looking for a typo.
    """
    from floodline.compute import WatershedNotFoundError

    def missing(*args: object, **kwargs: object) -> None:
        raise WatershedNotFoundError("no HUC-12 watershed with code '9'")

    monkeypatch.setattr("floodline.service.watershed_by_huc", missing)
    client = TestClient(create_app())
    for route in ("/api/watershed/99", "/api/compute/99", "/api/exposure/99"):
        assert client.get(route).status_code == 404, route


def test_a_real_upstream_failure_is_502_not_404(monkeypatch: pytest.MonkeyPatch) -> None:
    from floodline.io.sources import SourceError

    def broken(*args: object, **kwargs: object) -> None:
        raise SourceError("GET https://hydro.nationalmap.gov/... failed after 4 attempts")

    monkeypatch.setattr("floodline.service.watershed_by_huc", broken)
    client = TestClient(create_app())
    for route in ("/api/watershed/99", "/api/compute/99", "/api/exposure/99"):
        response = client.get(route)
        assert response.status_code == 502, route
        assert "upstream" in response.json()["detail"]


def test_a_cache_from_an_older_schema_is_not_served(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A payload that gained fields must not be served to code that expects them.

    The exposure payload gained a damage ladder and four reference multipliers, and
    caches written before that kept being served: the new decoder read the old image's
    channels as something they were not and drew a damage layer covering most of a
    watershed for a flood that reached 6% of it.
    """
    from floodline.service import CACHE_SCHEMA, _fresh

    assert _fresh({"schema": CACHE_SCHEMA}) is True
    assert _fresh({"schema": CACHE_SCHEMA - 1}) is False
    assert _fresh({}) is False, "a payload from before versioning existed is stale"


def test_a_written_cache_carries_the_schema(tmp_path: Path) -> None:
    from floodline.service import CACHE_SCHEMA, _fresh

    payload = {"huc": "1", "cached": False, "schema": CACHE_SCHEMA}
    (tmp_path / "c.json").write_text(json.dumps(payload))
    assert _fresh(json.loads((tmp_path / "c.json").read_text())) is True


def test_the_exposure_cache_is_keyed_on_the_sample_count(tmp_path: Path) -> None:
    """A 400-sample interval and a 5000-sample interval are different answers.

    `samples` is a query parameter anywhere from 50 to 5000 and it sets the width of
    the reported interval directly. The key left it out, so whichever count the first
    caller asked for was served to everyone after, with no sign that the number of
    draws behind the interval was not the one requested.
    """
    from floodline.service import _exposure_cache_path

    paths = {_exposure_cache_path(tmp_path, "1204010403", 30.0, n) for n in (400, 1000)}
    assert len(paths) == 2, "two sample counts must not collide on one cache file"

    # Resolution and HUC still separate, and the same request still hits the same file.
    same = _exposure_cache_path(tmp_path, "1204010403", 30.0, 400)
    assert same == _exposure_cache_path(tmp_path, "1204010403", 30.0, 400)
    assert same != _exposure_cache_path(tmp_path, "1204010403", 10.0, 400)
    assert same != _exposure_cache_path(tmp_path, "1204010404", 30.0, 400)


def test_a_watershed_reports_gauges_in_its_bounding_box(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Zero gauges in the box is the one answer knowable before the model runs.

    The box contains the polygon, so an empty bbox query proves there is no gauge in
    the watershed and the page can say so at click time rather than after two minutes
    of routing terrain. A non-zero count proves nothing - a site still has to snap to
    our stream network and carry a peak record - so only the zero is acted on.
    """
    monkeypatch.setattr("floodline.service.find_gauges", lambda *a, **k: [])
    body = client.get("/api/watershed/120401040305").json()
    assert body["gauges_in_bbox"] == 0

    monkeypatch.setattr("floodline.service.find_gauges", lambda *a, **k: [{"site": "08074500"}])
    body = client.get("/api/watershed/120401040305").json()
    assert body["gauges_in_bbox"] == 1


def test_a_failed_gauge_lookup_is_not_reported_as_no_gauge(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A broken NWIS call is not evidence that the basin is ungauged.

    Reporting 0 here would tell the reader their watershed has no gauge and withhold
    exposure on the strength of a network error. None says "could not ask", and the
    page warns on 0 only.
    """
    from floodline.io.sources import SourceError

    def broken(*args: object, **kwargs: object) -> list[dict[str, object]]:
        raise SourceError("NWIS site service failed after 4 attempts")

    monkeypatch.setattr("floodline.service.find_gauges", broken)
    body = client.get("/api/watershed/120401040305").json()
    assert body["gauges_in_bbox"] is None
    # The rest of the description still arrives; the gauge count is a hint, not a gate.
    assert body["huc"] == "120401040305"
    assert body["geometry"]["type"] in {"Polygon", "MultiPolygon"}


def test_exposure_on_an_ungauged_basin_is_refused_not_estimated(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The map disables its exposure button on this contract, so pin the contract.

    compute_watershed stands in a severe-flood scenario when there is no gauge, which
    is right for a picture of where water goes. Multiplying that assumption through a
    structure inventory is not: "33,279 buildings, USD 7.95 bn" reads as a measurement
    whatever the caption says. The server refuses, and the page must be able to tell
    that refusal from a real outage - 422, not 502.
    """
    from floodline.assess import NoDischargeError

    def ungauged(*args: object, **kwargs: object) -> None:
        raise NoDischargeError("no USGS gauge inside Ox Spring Wash (160600121003)")

    monkeypatch.setattr("floodline.service.assess_watershed", ungauged)
    response = client.get("/api/exposure/160600121003")
    assert response.status_code == 422
    assert "no USGS gauge" in response.json()["detail"]


def test_the_cache_is_evicted_to_its_budget(tmp_path: Path) -> None:
    """Unbounded growth fills the volume and fails in a way that looks unrelated."""
    import os
    import time

    from floodline.service import evict_cache

    for i in range(6):
        f = tmp_path / f"{i}_30m.json"
        f.write_text("x" * 400)
        # Distinct modification times, so least-recently-used has a defined order.
        stamp = time.time() - (10 - i)
        os.utime(f, (stamp, stamp))

    # 2.4 kB of files against a 1 kB budget: the four oldest go.
    removed = evict_cache(tmp_path, budget_mb=1024 / (1024 * 1024))
    assert removed == 4
    left = sorted(f.name for f in tmp_path.glob("*.json"))
    assert left == ["4_30m.json", "5_30m.json"], "the newest survive"


def test_eviction_leaves_a_cache_inside_its_budget_alone(tmp_path: Path) -> None:
    (tmp_path / "a_30m.json").write_text("x" * 10)
    assert evict_cache(tmp_path, budget_mb=1.0) == 0
    assert (tmp_path / "a_30m.json").exists()
