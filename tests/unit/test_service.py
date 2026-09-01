from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from floodline.config import Config
from floodline.service import create_app


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
    (tmp_path / "120401040305_10m.json").write_text(
        json.dumps({"huc": "120401040305", "hand": "x"})
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
