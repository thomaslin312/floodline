"""Tests for the data-acquisition layer.

Every test runs against an `httpx.MockTransport`, so the real fetch code paths -
streaming, hashing, the `.part` rename, paging, the budget check - are exercised
without touching the network. A test suite that needs the internet is a test suite
that fails for reasons unrelated to the code.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

import httpx
import pytest

from floodline.core.config import Config
from floodline.io import sources as src
from floodline.io.sources import (
    Artifact,
    CredentialError,
    FetchContext,
    Source,
    SourceError,
    download,
    fetch,
    list_sources,
    sha256_file,
    write_manifest,
)

FAST = {"sources": {"backoff_seconds": 0.0, "max_attempts": 3}}
"""Retries still happen in tests, just without the real 2/4/8-second sleeps."""


def fast_config(**overrides: Any) -> Config:
    return Config.model_validate({**FAST, **overrides})


def client_returning(handler: Any) -> httpx.Client:
    """Return a client whose every request is served by `handler`."""
    return httpx.Client(transport=httpx.MockTransport(handler), base_url="https://example.test")


def static_client(body: bytes = b"hello", status: int = 200) -> httpx.Client:
    return client_returning(lambda request: httpx.Response(status, content=body))


def make_context(tmp_path: Path, client: httpx.Client, **kwargs: Any) -> FetchContext:
    return FetchContext(config=fast_config(), dest=tmp_path, client=client, **kwargs)


# --- hashing and downloading --------------------------------------------------------


def test_sha256_file_matches_hashlib(tmp_path: Path) -> None:
    import hashlib

    path = tmp_path / "f.bin"
    payload = b"floodline" * 1000
    path.write_bytes(payload)
    assert sha256_file(path) == hashlib.sha256(payload).hexdigest()


def test_download_writes_and_hashes(tmp_path: Path) -> None:
    context = make_context(tmp_path, static_client(b"abc"))
    artifact = download(context, "https://example.test/a.tif", tmp_path / "a.tif", name="a")
    assert artifact.path.read_bytes() == b"abc"
    assert artifact.size_bytes == 3
    assert artifact.sha256 == sha256_file(artifact.path)


def test_download_reuses_an_existing_file(tmp_path: Path) -> None:
    """An already-present file is hashed, not re-fetched."""
    dest = tmp_path / "a.tif"
    dest.write_bytes(b"already here")

    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url)
        return httpx.Response(200, content=b"different")

    context = make_context(tmp_path, client_returning(handler))
    artifact = download(context, "https://example.test/a.tif", dest, name="a")

    assert not calls, "an existing file must not be re-downloaded"
    assert artifact.path.read_bytes() == b"already here"
    assert "reused" in artifact.note


def test_download_leaves_no_part_file_on_failure(tmp_path: Path) -> None:
    """An interrupted download must not leave a truncated file that looks finished."""
    context = make_context(tmp_path, static_client(status=500))
    with pytest.raises(SourceError, match="attempts"):
        download(context, "https://example.test/a.tif", tmp_path / "a.tif", name="a")
    assert not (tmp_path / "a.tif").exists()
    assert not list(tmp_path.glob("*.part"))


def test_download_cleans_up_on_interrupt(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise KeyboardInterrupt

    context = make_context(tmp_path, client_returning(handler))
    with pytest.raises(KeyboardInterrupt):
        download(context, "https://example.test/a.tif", tmp_path / "a.tif", name="a")
    assert not list(tmp_path.glob("*.part"))


# --- credentials --------------------------------------------------------------------


def test_missing_credential_is_a_clear_error(monkeypatch: pytest.MonkeyPatch) -> None:
    source = Source(
        name="needs-key", description="", fetch=lambda ctx: [], credential_env="FLOODLINE_TEST_KEY"
    )
    monkeypatch.delenv("FLOODLINE_TEST_KEY", raising=False)
    with pytest.raises(CredentialError, match="FLOODLINE_TEST_KEY"):
        source.credential()


def test_credential_is_read_from_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    source = Source(
        name="needs-key", description="", fetch=lambda ctx: [], credential_env="FLOODLINE_TEST_KEY"
    )
    monkeypatch.setenv("FLOODLINE_TEST_KEY", "s3cret")
    assert source.credential() == "s3cret"
    assert not source.is_automatable


def test_a_source_with_no_credential_is_automatable() -> None:
    assert all(s.is_automatable for s in list_sources())


# --- the DEM source -------------------------------------------------------------------


def _tnm_handler(n_tiles: int, size_each: int) -> Any:
    def handler(request: httpx.Request) -> httpx.Response:
        if "tnmaccess" in str(request.url):
            items = [
                {
                    "title": f"tile {i}",
                    "downloadURL": f"https://example.test/tiles/t{i}.tif",
                    "sizeInBytes": size_each,
                    "format": "GeoTIFF",
                }
                for i in range(n_tiles)
            ]
            return httpx.Response(200, json={"total": n_tiles, "items": items})
        return httpx.Response(200, content=b"x" * 16)

    return handler


def test_dem_source_downloads_tiles(tmp_path: Path) -> None:
    config = fast_config(case={"dem_resolutions_m": [30]})
    context = FetchContext(
        config=config, dest=tmp_path, client=client_returning(_tnm_handler(3, 1000))
    )
    artifacts = src.fetch_usgs_dem(context)
    assert len(artifacts) == 3
    assert all(a.path.exists() for a in artifacts)
    assert all(a.name.startswith("3dep-30m/") for a in artifacts)


def test_dem_source_respects_the_download_budget(tmp_path: Path) -> None:
    """A 57 GB accident is exactly what this guard is for."""
    config = Config.model_validate({"case": {"dem_resolutions_m": [1], "dem_max_download_gb": 1.0}})
    context = FetchContext(
        config=config, dest=tmp_path, client=client_returning(_tnm_handler(20, 500_000_000))
    )
    with pytest.raises(SourceError, match="over the 1 GB budget"):
        src.fetch_usgs_dem(context)


def test_dry_run_reports_size_without_downloading(tmp_path: Path) -> None:
    config = Config.model_validate({"case": {"dem_resolutions_m": [1], "dem_max_download_gb": 1.0}})
    context = FetchContext(
        config=config,
        dest=tmp_path,
        client=client_returning(_tnm_handler(20, 500_000_000)),
        dry_run=True,
    )
    artifacts = src.fetch_usgs_dem(context)
    assert len(artifacts) == 20
    assert sum(a.size_bytes for a in artifacts) == 10_000_000_000
    assert all(a.sha256 == "" for a in artifacts)
    assert not any(a.path.exists() for a in artifacts), "dry run must not write files"


def test_dem_source_errors_when_a_resolution_has_no_coverage(tmp_path: Path) -> None:
    """1/9 arc-second really does return nothing over Houston; say so, don't pass."""
    config = fast_config(case={"dem_resolutions_m": [3]})
    context = FetchContext(
        config=config,
        dest=tmp_path,
        client=client_returning(lambda r: httpx.Response(200, json={"total": 0, "items": []})),
    )
    with pytest.raises(SourceError, match="no 3 m tiles"):
        src.fetch_usgs_dem(context)


def test_dem_source_rejects_an_unmapped_resolution(tmp_path: Path) -> None:
    config = fast_config(case={"dem_resolutions_m": [7]})
    context = FetchContext(config=config, dest=tmp_path, client=static_client())
    with pytest.raises(SourceError, match="no 3DEP product mapped"):
        src.fetch_usgs_dem(context)


def test_limit_caps_the_tile_count(tmp_path: Path) -> None:
    config = fast_config(case={"dem_resolutions_m": [30]})
    context = FetchContext(
        config=config, dest=tmp_path, client=client_returning(_tnm_handler(10, 100)), limit=2
    )
    assert len(src.fetch_usgs_dem(context)) == 2


# --- high-water marks -------------------------------------------------------------------


def test_hwm_source_filters_to_the_aoi(tmp_path: Path) -> None:
    marks = [
        {"longitude_dd": -95.4, "latitude_dd": 29.8, "hwm_quality_id": 1},  # inside
        {"longitude_dd": -95.4, "latitude_dd": 29.9, "hwm_quality_id": 3},  # inside
        {"longitude_dd": -99.0, "latitude_dd": 31.0, "hwm_quality_id": 1},  # outside
        {"longitude_dd": None, "latitude_dd": None},  # unusable
    ]
    context = make_context(tmp_path, client_returning(lambda r: httpx.Response(200, json=marks)))
    artifacts = src.fetch_usgs_high_water_marks(context)
    saved = json.loads(artifacts[0].path.read_text())
    assert len(saved) == 2
    assert "2 marks in AOI of 4" in artifacts[0].note
    assert "1 at quality 1-2" in artifacts[0].note


def test_hwm_source_errors_when_nothing_is_in_the_aoi(tmp_path: Path) -> None:
    marks = [{"longitude_dd": -99.0, "latitude_dd": 31.0}]
    context = make_context(tmp_path, client_returning(lambda r: httpx.Response(200, json=marks)))
    with pytest.raises(SourceError, match="no high-water marks"):
        src.fetch_usgs_high_water_marks(context)


# --- FEMA paging ---------------------------------------------------------------------


def test_fema_source_pages_past_the_ten_thousand_cap(tmp_path: Path) -> None:
    """The live API caps a page at 10,000; taking one page silently loses 89% of it."""
    total = 25_000
    page = 10_000

    def handler(request: httpx.Request) -> httpx.Response:
        skip = int(request.url.params.get("$skip", 0))
        remaining = max(0, total - skip)
        count = min(page, remaining)
        return httpx.Response(
            200, json={"FimaNfipClaims": [{"id": skip + i} for i in range(count)]}
        )

    context = make_context(tmp_path, client_returning(handler))
    artifacts = src.fetch_fema_claims(context)
    saved = json.loads(artifacts[0].path.read_text())
    assert len(saved) == total
    assert "25,000 claims, paged" in artifacts[0].note


def test_fema_source_errors_on_an_empty_result(tmp_path: Path) -> None:
    context = make_context(
        tmp_path,
        client_returning(lambda r: httpx.Response(200, json={"FimaNfipClaims": []})),
    )
    with pytest.raises(SourceError, match="no NFIP claims"):
        src.fetch_fema_claims(context)


# --- Sentinel-1 search -----------------------------------------------------------------


def test_sentinel1_search_pins_the_scenes(tmp_path: Path) -> None:
    scenes = {"features": [{"id": "S1A_x"}, {"id": "S1A_y"}]}
    context = make_context(tmp_path, client_returning(lambda r: httpx.Response(200, json=scenes)))
    artifacts = src.fetch_sentinel1_search(context)
    assert "2 scenes" in artifacts[0].note
    assert json.loads(artifacts[0].path.read_text())["features"][0]["id"] == "S1A_x"


def test_sentinel1_search_errors_on_an_empty_window(tmp_path: Path) -> None:
    context = make_context(
        tmp_path, client_returning(lambda r: httpx.Response(200, json={"features": []}))
    )
    with pytest.raises(SourceError, match="no sentinel-1-rtc scenes"):
        src.fetch_sentinel1_search(context)


# --- orchestration and the manifest -------------------------------------------------------


def test_a_transient_server_error_is_retried(tmp_path: Path) -> None:
    """TNM 500'd on a real run minutes after serving the same query. One attempt is
    not a fair test of whether a dataset is reachable."""
    attempts = []

    def handler(request: httpx.Request) -> httpx.Response:
        attempts.append(1)
        if len(attempts) < 3:
            return httpx.Response(503)
        return httpx.Response(200, content=b"finally")

    context = make_context(tmp_path, client_returning(handler))
    artifact = download(context, "https://example.test/a.tif", tmp_path / "a.tif", name="a")
    assert len(attempts) == 3
    assert artifact.path.read_bytes() == b"finally"


def test_a_permanent_error_is_not_retried(tmp_path: Path) -> None:
    """A 404 will not improve on a second attempt; retrying it just wastes time."""
    attempts = []

    def handler(request: httpx.Request) -> httpx.Response:
        attempts.append(1)
        return httpx.Response(404)

    import httpx as _httpx

    context = make_context(tmp_path, client_returning(handler))
    with pytest.raises(_httpx.HTTPStatusError):
        download(context, "https://example.test/a.tif", tmp_path / "a.tif", name="a")
    assert len(attempts) == 1
    assert not list(tmp_path.glob("*.part"))


def test_retry_gives_up_and_says_how_many_times(tmp_path: Path) -> None:
    context = make_context(tmp_path, static_client(status=503))
    with pytest.raises(SourceError, match="after 3 attempts"):
        download(context, "https://example.test/a.tif", tmp_path / "a.tif", name="a")
    assert not list(tmp_path.glob("*.part"))


def test_fetch_reports_a_failure_without_stopping_the_others(tmp_path: Path) -> None:
    """A source that fails is written down, not worked around."""

    def handler(request: httpx.Request) -> httpx.Response:
        if "stn.wim" in str(request.url):
            return httpx.Response(500)
        return httpx.Response(200, json={"features": [{"id": "s1"}]})

    results = fetch(
        ["usgs-hwm", "sentinel1-search"],
        config=fast_config(),
        dest=tmp_path,
        client=client_returning(handler),
    )
    by_name = {r.source: r for r in results}
    assert by_name["usgs-hwm"].note.startswith("FAILED")
    assert by_name["usgs-hwm"].artifacts == ()
    assert by_name["sentinel1-search"].artifacts, "the other source must still run"


def test_fetch_rejects_an_unknown_source(tmp_path: Path) -> None:
    with pytest.raises(KeyError, match="unknown source"):
        fetch(["not-a-source"], dest=tmp_path, client=static_client())


def test_manifest_records_every_artifact(tmp_path: Path) -> None:
    from datetime import UTC, datetime

    results = [
        src.FetchResult(
            source="demo",
            artifacts=(
                Artifact(
                    name="thing",
                    path=Path("data/raw/thing.tif"),
                    url="https://example.test/thing.tif",
                    sha256="a" * 64,
                    size_bytes=1234,
                    retrieved_utc=datetime(2026, 9, 3, tzinfo=UTC),
                    note="a note",
                ),
            ),
        )
    ]
    path = write_manifest(tmp_path / "MANIFEST.md", results)
    text = path.read_text()
    assert "thing" in text
    assert "1,234" in text
    assert "aaaaaaaaaaaaaaaa" in text
    assert "https://example.test/thing.tif" in text
    assert "2026-09-03" in text


def test_manifest_records_failures_rather_than_omitting_them(tmp_path: Path) -> None:
    """A manifest that silently omits what did not arrive reads as a complete record."""
    results = [src.FetchResult(source="broken", note="FAILED: the server said no")]
    text = write_manifest(tmp_path / "MANIFEST.md", results).read_text()
    assert "Sources that did not fetch" in text
    assert "broken" in text
    assert "the server said no" in text


def test_manifest_handles_an_empty_run(tmp_path: Path) -> None:
    text = write_manifest(tmp_path / "MANIFEST.md", []).read_text()
    assert "nothing retrieved yet" in text


def test_a_slow_url_gives_up_on_the_budget_rather_than_on_the_attempt_count(
    tmp_path: Path,
) -> None:
    """The wall-clock budget has to bind before `max_attempts` does.

    The per-attempt timeouts bound an attempt, not a request. At the shipped defaults -
    four attempts, a 300 s read, 2 s doubling backoff - one URL can hold a slot for
    20 minutes, and the service allows two computations in flight, so a single slow
    agency could take the whole deployment down for that long. `request_budget_s` is
    what stops that, and this asserts it stops it early: three attempts are allowed
    here and the budget must cut in before they are spent.
    """
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        time.sleep(0.05)
        return httpx.Response(503)

    context = FetchContext(
        config=fast_config(
            sources={"backoff_seconds": 0.0, "max_attempts": 3, "request_budget_s": 0.04}
        ),
        dest=tmp_path,
        client=client_returning(handler),
    )
    with pytest.raises(SourceError, match="gave up after"):
        context.request("GET", "https://example.test/slow")
    # One attempt made, then the budget refused the second - not all three.
    assert calls == 1


def test_the_budget_does_not_interfere_with_a_request_that_succeeds(tmp_path: Path) -> None:
    """A generous budget must leave the retry behaviour exactly as it was."""
    seen = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal seen
        seen += 1
        return httpx.Response(503 if seen == 1 else 200, content=b"ok")

    context = FetchContext(
        config=fast_config(
            sources={"backoff_seconds": 0.0, "max_attempts": 3, "request_budget_s": 60.0}
        ),
        dest=tmp_path,
        client=client_returning(handler),
    )
    assert context.request("GET", "https://example.test/flaky").content == b"ok"
    assert seen == 2
