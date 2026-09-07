"""The service: validation at the boundary, and the cache-before-fetch ordering.

The ordering test is the important one. It is not checking that the cache is faster -
that is obvious - but that a hit reaches the network *zero* times. The measurement that
motivated the whole split was that terrain routing costs tenths of a second and
fetching the elevation it runs on costs tens of seconds, so a cache consulted after the
fetch would save nothing. An ordering constraint that is only documented is one that
gets reordered by the next person to touch the handler.
"""

from __future__ import annotations

import numpy as np
import pytest
from fastapi.testclient import TestClient

from floodline.api.app import create_api
from floodline.api.scenario import serve_scenario
from floodline.core.config import Config
from floodline.pipeline import compute_terrain, params_hash, terrain_params
from floodline.storage import LocalTerrainStore


class Tripwire:
    """A client that fails the test if anything asks it for a request."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def get(self, url: str, *args: object, **kwargs: object) -> None:
        self.calls.append(url)
        raise AssertionError(f"a cache hit reached the network: GET {url}")

    def request(self, method: str, url: str, *args: object, **kwargs: object) -> None:
        self.calls.append(f"{method} {url}")
        raise AssertionError(f"a cache hit reached the network: {method} {url}")

    def close(self) -> None:
        pass


class FakeUnit:
    """The attributes `serve_scenario` reads from a watershed, and no more.

    Bounds are in the analysis CRS, as a real unit's are, so the tile query on the
    miss path gets as far as asking the client rather than failing before it.
    """

    huc = "1204010403"
    name = "Test Basin"
    area_km2 = 42.0
    bounds = (500000.0, 3200000.0, 502400.0, 3202400.0)


def _valley(rows: int = 80, cols: int = 80) -> np.ndarray:
    y, x = np.mgrid[0:rows, 0:cols]
    rng = np.random.default_rng(0)
    return 40.0 - 0.02 * y + 0.004 * np.abs(x - cols / 2) ** 1.6 + rng.normal(0, 0.02, (rows, cols))


def test_a_cache_hit_makes_no_network_calls(tmp_path) -> None:
    """The constraint the whole split rests on: a hit fetches nothing."""
    config = Config()
    store = LocalTerrainStore(root=tmp_path)
    transform = (30.0, 0.0, 500000.0, 0.0, -30.0, 3300000.0)

    # Warm the store directly, without going through the service.
    terrain = compute_terrain(_valley(), config=config, cellsize=(30.0, 30.0), transform=transform)
    key = params_hash(terrain_params(config, 30.0))
    store.put_hand(FakeUnit.huc, key, terrain.to_artifact())
    assert store.exists(FakeUnit.huc, key)

    tripwire = Tripwire()
    outcome = serve_scenario(
        FakeUnit(),  # type: ignore[arg-type]
        config,
        discharge_cms=100.0,
        resolution_m=30.0,
        store=store,
        client=tripwire,  # type: ignore[arg-type]
    )
    assert outcome.cached is True
    assert tripwire.calls == [], f"a cache hit issued {len(tripwire.calls)} request(s)"
    assert outcome.scenario.depth_m.shape == terrain.hand.shape


def test_a_cache_miss_is_what_reaches_upstream(tmp_path) -> None:
    """The other half of the same contract: a miss must actually try to fetch.

    Without this, a store that always reported a hit would pass the test above while
    serving nothing.
    """
    from floodline.api.scenario import UpstreamUnavailableError

    store = LocalTerrainStore(root=tmp_path)
    tripwire = Tripwire()
    with pytest.raises((AssertionError, UpstreamUnavailableError)):
        serve_scenario(
            FakeUnit(),  # type: ignore[arg-type]
            Config(),
            discharge_cms=100.0,
            resolution_m=30.0,
            store=store,
            client=tripwire,  # type: ignore[arg-type]
        )
    assert tripwire.calls, "a miss must reach upstream; it is the only path that should"


@pytest.fixture
def client(tmp_path) -> TestClient:
    return TestClient(create_api(store=LocalTerrainStore(root=tmp_path)))


def test_health_touches_no_dependency(client: TestClient) -> None:
    """A liveness probe that checks the database restarts healthy containers."""
    response = client.get("/health")
    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True and body["service"] == "floodline"


def test_ready_reports_each_dependency_by_name(client: TestClient) -> None:
    response = client.get("/ready")
    assert response.status_code in (200, 503)
    checks = response.json()["checks"]
    assert set(checks) == {"database", "terrain_store"}
    for name, check in checks.items():
        assert "ok" in check and check["detail"], name
    # Not-ready must be a 503, or an orchestrator reading the status sends traffic
    # to a container that cannot serve it.
    assert (response.status_code == 200) == response.json()["ready"]


@pytest.mark.parametrize(
    ("payload", "why"),
    [
        ({"huc": "1204010403", "discharge_cms": -5.0}, "negative discharge"),
        ({"huc": "1204010403", "discharge_cms": 0.0}, "zero is a missing value"),
        ({"huc": "1204010403", "discharge_cms": 5e9}, "past anything a river does"),
        ({"huc": "120401040", "discharge_cms": 100.0}, "odd-length hydrologic code"),
        ({"huc": "abc", "discharge_cms": 100.0}, "not digits"),
        ({"huc": "1204010403"}, "no discharge at all"),
        ({"huc": "1204010403", "discharge_cms": 100.0, "resolution_m": 0.5}, "sub-metre"),
        ({"huc": "1204010403", "discharge_cms": 100.0, "extra": 1}, "unknown field"),
    ],
)
def test_the_schema_rejects_what_cannot_be_a_scenario(
    client: TestClient, payload: dict[str, object], why: str
) -> None:
    """Rejected before any work: no DEM fetch, no upstream call, no stack trace."""
    response = client.post("/api/scenario", json=payload)
    assert response.status_code == 422, f"{why}: {response.status_code}"
    assert response.json()["detail"], why


def test_a_valid_request_passes_the_schema(client: TestClient) -> None:
    """The bounds must not be so tight they reject a real flood.

    Harvey's peak at Whiteoak Bayou was 1,433 m3/s. A schema that rejected it would be
    worse than no schema.
    """
    from floodline.api.schemas import ScenarioRequest

    request = ScenarioRequest(huc="1204010403", discharge_cms=1433.0)
    assert request.resolution_m == 30.0
    assert ScenarioRequest(huc="12", discharge_cms=0.001).huc == "12"


def test_the_deployed_app_serves_the_map_and_the_service_together() -> None:
    """The container runs one application, and this is the shape of it.

    The image used to run the service factory alone, so `GET /` answered 404 in the
    container while working under `floodline serve` - the deployed artefact was missing
    the only page anyone opens, and nothing failed to say so. This asserts the merge:
    the map at the root, the service beside it, and the probes where an orchestrator
    looks for them.
    """
    from floodline.api.asgi import app

    paths = {getattr(route, "path", "") for route in app.routes}
    assert {"/", "/methodology"} <= paths, "the map must be served by the deployed app"
    assert {"/health", "/ready"} <= paths, "probes stay at the root, above the routing"
    assert "/api/scenario" in paths, "the service must be reachable under /api"
