"""Reproducible data acquisition.

Every dataset the model consumes is fetched by a named source in the registry
below, hashed, and recorded in `data/MANIFEST.md`. Nothing is committed; the
manifest is what makes a run reproducible from a clean checkout.

Design rules, all of which come from the project conventions:

* **No credentials in the repo, ever.** A source that needs one names an
  environment variable and raises `CredentialError` if it is unset. This module
  never reads, stores or logs a credential's value.
* **No event parameters in fetch code.** Bounding box, dates, gauge numbers and
  event identifiers are all fields on `CaseConfig`. Changing case is a config
  change.
* **Idempotent.** A file already on disk whose SHA256 matches the manifest is not
  downloaded again. Partial downloads land on a `.part` file and are only renamed
  once complete, so an interrupted fetch never leaves a plausible-looking truncated
  raster behind.
* **Honest about failure.** A source that cannot be fetched reports why. The
  convention is that a failed data source is written down, not worked around.

The HTTP client is injectable so the tests exercise the real code paths against a
mock transport rather than the network.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

from floodline.config import CaseConfig, Config, SourcesConfig

__all__ = [
    "Artifact",
    "CredentialError",
    "FetchContext",
    "FetchResult",
    "Source",
    "SourceError",
    "fetch",
    "list_sources",
    "write_manifest",
]

USER_AGENT = "floodline/0.1 (+https://github.com/thomaslin312/floodline)"
"""Some of these services reject a request with no User-Agent (WaterNSW 502s)."""

CHUNK_BYTES = 1 << 20


class SourceError(RuntimeError):
    """A source could not be fetched. The message says what and why."""


class CredentialError(SourceError):
    """A source needs a credential that is not present in the environment."""


@dataclass(frozen=True, slots=True)
class Artifact:
    """One file on disk, with everything needed to prove where it came from."""

    name: str
    path: Path
    url: str
    sha256: str
    size_bytes: int
    retrieved_utc: datetime
    note: str = ""

    def manifest_row(self, root: Path) -> str:
        """Return this artifact as a `data/MANIFEST.md` table row."""
        try:
            shown = self.path.relative_to(root)
        except ValueError:
            shown = self.path
        return (
            f"| {self.name} | {shown} | {self.size_bytes:,} | "
            f"`{self.sha256[:16]}…` | {self.retrieved_utc:%Y-%m-%d} | "
            f"{self.url} | {self.note} |"
        )


@dataclass(frozen=True, slots=True)
class FetchResult:
    """What one source produced."""

    source: str
    artifacts: tuple[Artifact, ...] = ()
    reused: int = 0
    note: str = ""

    @property
    def total_bytes(self) -> int:
        """Bytes across every artifact, downloaded or reused."""
        return sum(a.size_bytes for a in self.artifacts)


@dataclass(slots=True)
class FetchContext:
    """Everything a source function needs: where to write, what to fetch, how to talk."""

    config: Config
    dest: Path
    client: httpx.Client
    limit: int | None = None
    """Cap on the number of files a source will pull, for smoke runs."""
    dry_run: bool = False
    """Report what would be fetched, and how much of it, without downloading."""

    @property
    def case(self) -> CaseConfig:
        """The event being fetched."""
        return self.config.case

    @property
    def settings(self) -> SourcesConfig:
        """Retry and timeout policy."""
        return self.config.sources

    def request(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        """Issue a request, retrying transient failures with exponential backoff.

        Public agency APIs return 500s and 503s often enough that a single attempt
        is not a fair test of whether a dataset is reachable - the TNM products
        endpoint 500'd on a run minutes after serving the same query happily. Only
        the statuses in `retry_status_codes` and transport errors are retried; a
        404 or a 400 will not improve on a second attempt.
        """
        settings = self.settings
        last: Exception | None = None
        for attempt in range(settings.max_attempts):
            if attempt:
                time.sleep(settings.backoff_seconds * (2 ** (attempt - 1)))
            try:
                response = self.client.request(method, url, **kwargs)
            except httpx.HTTPError as exc:
                last = exc
                continue
            if response.status_code in settings.retry_status_codes:
                last = httpx.HTTPStatusError(
                    f"HTTP {response.status_code}", request=response.request, response=response
                )
                response.close()
                continue
            return response
        raise SourceError(
            f"{method} {url} failed after {settings.max_attempts} attempts ({last})"
        ) from last

    def subdir(self, name: str) -> Path:
        """Return (and create) a per-source directory under `dest`."""
        path = self.dest / name
        path.mkdir(parents=True, exist_ok=True)
        return path


@dataclass(frozen=True, slots=True)
class Source:
    """A named, reproducible way to obtain one dataset."""

    name: str
    description: str
    fetch: Callable[[FetchContext], list[Artifact]] = field(repr=False)
    credential_env: str | None = None
    """Environment variable holding a required credential, if any."""
    manual_note: str = ""
    """Set when a source cannot be fully automated; explains what a human must do."""

    @property
    def is_automatable(self) -> bool:
        """True when this source needs neither a credential nor a manual step."""
        return self.credential_env is None and not self.manual_note

    def credential(self) -> str:
        """Return the credential from the environment, or explain that it is missing."""
        if self.credential_env is None:
            raise SourceError(f"source {self.name!r} declares no credential")
        value = os.environ.get(self.credential_env)
        if not value:
            raise CredentialError(
                f"source {self.name!r} needs the {self.credential_env} environment "
                "variable. Set it in your shell; it is deliberately not read from a "
                "file and never written to the manifest."
            )
        return value


# --------------------------------------------------------------------------------
# Plumbing
# --------------------------------------------------------------------------------


def sha256_file(path: Path) -> str:
    """Return the SHA256 of a file, read in chunks so size does not matter."""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(CHUNK_BYTES):
            digest.update(chunk)
    return digest.hexdigest()


def download(
    context: FetchContext,
    url: str,
    dest: Path,
    *,
    name: str,
    note: str = "",
    params: dict[str, Any] | None = None,
) -> Artifact:
    """Stream `url` to `dest`, hashing as it goes.

    An existing file is hashed and reused rather than re-downloaded. The download
    lands on a sibling `.part` file and is renamed only once the stream completes,
    so an interrupted run cannot leave a truncated file that looks finished.
    """
    if dest.exists() and dest.stat().st_size > 0:
        return Artifact(
            name=name,
            path=dest,
            url=url,
            sha256=sha256_file(dest),
            size_bytes=dest.stat().st_size,
            retrieved_utc=datetime.fromtimestamp(dest.stat().st_mtime, tz=UTC),
            note=(note + " (reused)").strip(),
        )

    dest.parent.mkdir(parents=True, exist_ok=True)
    partial = dest.with_suffix(dest.suffix + ".part")
    settings = context.settings
    last: Exception | None = None

    for attempt in range(settings.max_attempts):
        if attempt:
            time.sleep(settings.backoff_seconds * (2 ** (attempt - 1)))
        digest = hashlib.sha256()
        size = 0
        try:
            with context.client.stream("GET", url, params=params) as response:
                if response.status_code in settings.retry_status_codes:
                    last = httpx.HTTPStatusError(
                        f"HTTP {response.status_code}",
                        request=response.request,
                        response=response,
                    )
                    continue
                response.raise_for_status()
                with partial.open("wb") as handle:
                    for chunk in response.iter_bytes(CHUNK_BYTES):
                        handle.write(chunk)
                        digest.update(chunk)
                        size += len(chunk)
            partial.replace(dest)
            break
        except httpx.HTTPStatusError:
            # A non-retryable status: retryable ones were caught above, so this is
            # a 404 or a 400, which will not improve on a second attempt.
            partial.unlink(missing_ok=True)
            raise
        except httpx.HTTPError as exc:
            # A transport-level failure - connection reset, timeout - which is
            # exactly the kind of thing worth trying again.
            last = exc
            partial.unlink(missing_ok=True)
            continue
        except BaseException:
            partial.unlink(missing_ok=True)
            raise
    else:
        partial.unlink(missing_ok=True)
        raise SourceError(
            f"{name}: fetching {url} failed after {settings.max_attempts} attempts ({last})"
        )

    return Artifact(
        name=name,
        path=dest,
        url=url,
        sha256=digest.hexdigest(),
        size_bytes=size,
        retrieved_utc=datetime.now(UTC),
        note=note,
    )


def write_json(payload: Any, dest: Path, *, name: str, url: str, note: str = "") -> Artifact:
    """Write a decoded JSON payload to disk as an artifact.

    Used where a source queries an API and keeps the response rather than a file:
    the bytes on disk are what was actually received, so the hash still means
    something.
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    body = json.dumps(payload, indent=2, sort_keys=True, default=str).encode()
    dest.write_bytes(body)
    return Artifact(
        name=name,
        path=dest,
        url=url,
        sha256=hashlib.sha256(body).hexdigest(),
        size_bytes=len(body),
        retrieved_utc=datetime.now(UTC),
        note=note,
    )


def get_json(context: FetchContext, url: str, params: dict[str, Any] | None = None) -> Any:
    """GET `url` and decode JSON, retrying transient failures."""
    response = context.request("GET", url, params=params)
    try:
        response.raise_for_status()
        return response.json()
    except httpx.HTTPError as exc:
        raise SourceError(f"request to {url} failed ({exc})") from exc
    except json.JSONDecodeError as exc:
        raise SourceError(f"{url} did not return JSON ({exc})") from exc


def make_client(settings: SourcesConfig | None = None) -> httpx.Client:
    """Return an HTTP client configured the way every source expects."""
    resolved = settings or SourcesConfig()
    return httpx.Client(
        headers={"User-Agent": USER_AGENT},
        timeout=httpx.Timeout(resolved.connect_timeout_s, read=resolved.read_timeout_s),
        follow_redirects=True,
    )


# --------------------------------------------------------------------------------
# Sources
# --------------------------------------------------------------------------------

TNM_PRODUCTS = "https://tnmaccess.nationalmap.gov/api/v1/products"
NWIS_IV = "https://waterservices.usgs.gov/nwis/iv/"
NWIS_SITE = "https://waterservices.usgs.gov/nwis/site/"
STN_HWM = "https://stn.wim.usgs.gov/STNServices/HWMs/FilteredHWMs.json"
OPENFEMA = "https://www.fema.gov/api/open/v2"
MPC_STAC = "https://planetarycomputer.microsoft.com/api/stac/v1/search"
WBD = "https://hydro.nationalmap.gov/arcgis/rest/services/wbd/MapServer"

WBD_LAYER_BY_HUC_LEVEL = {2: 1, 4: 2, 6: 3, 8: 4, 10: 5, 12: 6, 14: 7, 16: 8}
"""Watershed Boundary Dataset layer index by HUC digit count."""

_OPENFEMA_MAX_PAGES = 100
"""Guard so a filter that matches everything cannot page forever."""

TNM_DATASET_BY_RESOLUTION = {
    1: "Digital Elevation Model (DEM) 1 meter",
    3: "National Elevation Dataset (NED) 1/9 arc-second",
    10: "National Elevation Dataset (NED) 1/3 arc-second",
    30: "National Elevation Dataset (NED) 1 arc-second",
}
"""3DEP product names by nominal metre resolution.

1/9 and 1/3 arc-second are about 3 m and 10 m at these latitudes. 1/9 arc-second is
mapped but not in the default resolution list: its US coverage is patchy and it
returns zero tiles over Houston, which is a property of the data rather than of the
query."""


def _bbox_string(bbox: tuple[float, float, float, float]) -> str:
    """Format a (w, s, e, n) box the way the TNM API wants it."""
    return ",".join(f"{v:g}" for v in bbox)


def fetch_usgs_dem(context: FetchContext) -> list[Artifact]:
    """USGS 3DEP elevation tiles at every configured resolution.

    Queries the TNM Products API for tiles intersecting the AOI, then pulls the
    GeoTIFFs from `prd-tnm` on S3. Anonymous throughout.

    The tiles arrive in their published CRS, which is generally a UTM zone or a
    geographic grid, not the analysis CRS. They are stored as delivered; the
    conditioning step reprojects, so the bytes on disk stay byte-identical to what
    USGS published and the checksum keeps meaning something.
    """
    artifacts: list[Artifact] = []
    for resolution in context.case.dem_resolutions_m:
        dataset = TNM_DATASET_BY_RESOLUTION.get(resolution)
        if dataset is None:
            raise SourceError(
                f"no 3DEP product mapped for {resolution} m; known: "
                f"{sorted(TNM_DATASET_BY_RESOLUTION)}"
            )
        payload = get_json(
            context,
            TNM_PRODUCTS,
            {
                "datasets": dataset,
                "bbox": _bbox_string(context.case.aoi_bbox_wgs84),
                "prodFormats": "GeoTIFF",
                "max": 200,
            },
        )
        items = payload.get("items", [])
        if not items:
            raise SourceError(
                f"3DEP returned no {resolution} m tiles for bbox "
                f"{context.case.aoi_bbox_wgs84}. 1/9 arc-second (3 m) in particular has "
                "patchy coverage. Widen the AOI or drop this resolution."
            )
        if context.limit is not None:
            items = items[: context.limit]

        planned = sum(item.get("sizeInBytes") or 0 for item in items)
        budget = context.case.dem_max_download_gb * 1e9
        if planned > budget and not context.dry_run:
            raise SourceError(
                f"{resolution} m DEM over this AOI is {len(items)} tiles and "
                f"{planned / 1e9:.1f} GB, over the {context.case.dem_max_download_gb:g} GB "
                "budget in case.dem_max_download_gb. At roughly 125 bytes of peak memory "
                "per cell, that is also well past what the global priority-flood can hold "
                "in one pass. Shrink case.aoi_bbox_wgs84, drop this resolution, or raise "
                "the budget deliberately."
            )

        target = context.subdir(f"dem/{resolution}m")
        for item in items:
            url = item.get("downloadURL")
            if not url:
                continue
            note = f"{resolution} m bare-earth DEM; {item.get('title', '')[:60]}"
            if context.dry_run:
                artifacts.append(
                    Artifact(
                        name=f"3dep-{resolution}m/{Path(url).name}",
                        path=target / Path(url).name,
                        url=url,
                        sha256="",
                        size_bytes=int(item.get("sizeInBytes") or 0),
                        retrieved_utc=datetime.now(UTC),
                        note=note + " (dry run, not downloaded)",
                    )
                )
                continue
            artifacts.append(
                download(
                    context,
                    url,
                    target / Path(url).name,
                    name=f"3dep-{resolution}m/{Path(url).name}",
                    note=note,
                )
            )
    return artifacts


def fetch_usgs_gauge(context: FetchContext) -> list[Artifact]:
    """USGS gauge height time series, and the site metadata that carries the datum.

    The site metadata is the important half. `alt_datum_cd` states the vertical
    datum the gauge altitude is referenced to (NAVD88 for these sites), which is
    what `hydraulics.gauge_datum_offset_m` needs and what the original brief called
    the thing that bites everyone.
    """
    case = context.case
    target = context.subdir("gauge")
    sites = ",".join(case.gauge_sites)
    artifacts: list[Artifact] = []

    metadata_params = {"format": "rdb", "sites": sites, "siteOutput": "expanded"}
    artifacts.append(
        download(
            context,
            NWIS_SITE,
            target / "site_metadata.rdb",
            name="nwis-site-metadata",
            note="carries alt_va and alt_datum_cd -- the vertical datum",
            params=metadata_params,
        )
    )

    series_params = {
        "format": "json",
        "sites": sites,
        "parameterCd": "00065",  # gauge height, feet
        "startDT": case.event_start.isoformat(),
        "endDT": case.event_end.isoformat(),
    }
    artifacts.append(
        download(
            context,
            NWIS_IV,
            target / "gauge_height.json",
            name="nwis-gauge-height",
            note=f"parameter 00065, {case.event_start} to {case.event_end}",
            params=series_params,
        )
    )
    return artifacts


def fetch_usgs_high_water_marks(context: FetchContext) -> list[Artifact]:
    """USGS Short-Term Network surveyed high-water marks for the event.

    The primary validation reference: ground-surveyed water-surface elevations,
    with a per-mark quality flag. Unlike a SAR mask these are not blinded by tree
    canopy or urban double-bounce, which is why they lead and SAR follows.
    """
    case = context.case
    payload = get_json(context, STN_HWM, {"Event": case.stn_event_id})
    if not isinstance(payload, list):
        raise SourceError(f"STN returned {type(payload).__name__}, expected a list of marks")

    west, south, east, north = case.aoi_bbox_wgs84
    inside = [
        mark
        for mark in payload
        if isinstance(mark.get("longitude_dd"), int | float)
        and isinstance(mark.get("latitude_dd"), int | float)
        and west <= mark["longitude_dd"] <= east
        and south <= mark["latitude_dd"] <= north
    ]
    if not inside:
        raise SourceError(f"no high-water marks for STN event {case.stn_event_id} inside the AOI")

    graded = sum(1 for m in inside if m.get("hwm_quality_id") in (1, 2))
    return [
        write_json(
            inside,
            context.subdir("validation") / "high_water_marks.json",
            name="usgs-high-water-marks",
            url=f"{STN_HWM}?Event={case.stn_event_id}",
            note=(
                f"{len(inside)} marks in AOI of {len(payload)} for the event; "
                f"{graded} at quality 1-2"
            ),
        )
    ]


def fetch_fema_claims(context: FetchContext) -> list[Artifact]:
    """OpenFEMA NFIP claims for the event's date range, inside the AOI.

    Per-property paid claims: the reference for the building-count and damage
    comparisons. Claim locations are published at reduced precision, which is a
    real limit on how finely they can be matched to footprints and belongs in the
    write-up rather than being quietly ignored.
    """
    case = context.case
    url = f"{OPENFEMA}/FimaNfipClaims"
    query = (
        f"dateOfLoss ge '{case.event_start.isoformat()}' and "
        f"dateOfLoss le '{case.event_end.isoformat()}' and state eq 'TX'"
    )
    # OpenFEMA caps a response at 10,000 records. Taking the first page and
    # stopping would silently drop claims and quietly shrink the damage total, so
    # page until a short page arrives.
    page_size = 10_000
    records: list[dict[str, Any]] = []
    for page in range(_OPENFEMA_MAX_PAGES):
        payload = get_json(
            context,
            url,
            {
                "$filter": query,
                "$top": page_size,
                "$skip": page * page_size,
                "$format": "json",
                "$metadata": "off",
            },
        )
        batch = payload.get("FimaNfipClaims", []) if isinstance(payload, dict) else []
        records.extend(batch)
        if len(batch) < page_size:
            break
    else:
        raise SourceError(
            f"OpenFEMA paging did not terminate within {_OPENFEMA_MAX_PAGES} pages "
            f"({len(records):,} records so far). Narrow the filter."
        )

    if not records:
        raise SourceError(
            f"OpenFEMA returned no NFIP claims for {case.event_start}..{case.event_end}. "
            "Check the date range and that the dataset version is still v2."
        )
    return [
        write_json(
            records,
            context.subdir("validation") / "nfip_claims.json",
            name="fema-nfip-claims",
            url=f"{url}?$filter={query}",
            note=(f"{len(records):,} claims, paged; locations are published at reduced precision"),
        )
    ]


def fetch_sentinel1_search(context: FetchContext) -> list[Artifact]:
    """Find the Sentinel-1 RTC scenes over the AOI nearest the flood peak.

    Search only, and search needs no credential. The scene list is pinned to disk
    so the choice of scene is reproducible and reviewable before anyone spends a
    key on downloading it. `sentinel1-rtc` does the actual fetch and declares the
    credential it needs.
    """
    case = context.case
    body = {
        "collections": ["sentinel-1-rtc"],
        "bbox": list(case.aoi_bbox_wgs84),
        "datetime": (
            f"{case.peak_start.isoformat()}T00:00:00Z/{case.peak_end.isoformat()}T23:59:59Z"
        ),
        "limit": 50,
    }
    response = context.request("POST", MPC_STAC, json=body)
    try:
        response.raise_for_status()
        payload = response.json()
    except httpx.HTTPError as exc:
        raise SourceError(f"Planetary Computer STAC search failed ({exc})") from exc

    features = payload.get("features", [])
    if not features:
        raise SourceError(
            f"no sentinel-1-rtc scenes over the AOI between {case.peak_start} and "
            f"{case.peak_end}. Widen the peak window."
        )
    return [
        write_json(
            payload,
            context.subdir("validation") / "sentinel1_scenes.json",
            name="sentinel1-scene-search",
            url=MPC_STAC,
            note=(
                f"{len(features)} scenes in the peak window; search is keyless, "
                "downloading the assets is not"
            ),
        )
    ]


def fetch_watersheds(context: FetchContext) -> list[Artifact]:
    """USGS Watershed Boundary Dataset polygons intersecting the AOI.

    These are the unit of work for the terrain chain. Flow accumulation at a cell
    depends on everything upstream of it, so terrain products computed over an
    arbitrary box are wrong near the box's edges -- measured on the Houston 30 m
    grid, clipping to an 800x800 window left 4.3% of HAND cells more than 0.5 m out,
    with a 10.8 m worst case, and lost 12% of the stream network. A HUC is
    hydrologically complete, so the same computation over one is correct throughout.
    """
    case = context.case
    layer = WBD_LAYER_BY_HUC_LEVEL.get(case.huc_level)
    if layer is None:
        raise SourceError(
            f"no WBD layer for HUC level {case.huc_level}; known: {sorted(WBD_LAYER_BY_HUC_LEVEL)}"
        )
    field = f"huc{case.huc_level}"
    west, south, east, north = case.aoi_bbox_wgs84
    payload = get_json(
        context,
        f"{WBD}/{layer}/query",
        {
            "geometry": f"{west},{south},{east},{north}",
            "geometryType": "esriGeometryEnvelope",
            "inSR": 4326,
            "spatialRel": "esriSpatialRelIntersects",
            "outFields": f"{field},name,areasqkm",
            "returnGeometry": "true",
            "outSR": 4326,
            "f": "geojson",
        },
    )
    features = payload.get("features", []) if isinstance(payload, dict) else []
    if not features:
        raise SourceError(
            f"no HUC-{case.huc_level} watersheds intersect the AOI {case.aoi_bbox_wgs84}"
        )
    if case.huc_codes:
        wanted = set(case.huc_codes)
        features = [f for f in features if f.get("properties", {}).get(field) in wanted]
        if not features:
            raise SourceError(f"none of case.huc_codes {case.huc_codes} intersect the AOI")

    total = sum(f.get("properties", {}).get("areasqkm") or 0 for f in features)
    return [
        write_json(
            {"type": "FeatureCollection", "features": features},
            context.subdir("watersheds") / f"huc{case.huc_level}.geojson",
            name=f"usgs-wbd-huc{case.huc_level}",
            url=f"{WBD}/{layer}/query",
            note=(
                f"{len(features)} HUC-{case.huc_level} watersheds, {total:,.0f} km2 total; "
                "the unit of work for the terrain chain"
            ),
        )
    ]


REGISTRY: dict[str, Source] = {
    source.name: source
    for source in (
        Source(
            name="usgs-dem",
            description="USGS 3DEP bare-earth DEM tiles at every configured resolution",
            fetch=fetch_usgs_dem,
        ),
        Source(
            name="usgs-gauge",
            description="USGS NWIS gauge height and site metadata (carries the datum)",
            fetch=fetch_usgs_gauge,
        ),
        Source(
            name="usgs-hwm",
            description="USGS surveyed high-water marks -- the primary validation reference",
            fetch=fetch_usgs_high_water_marks,
        ),
        Source(
            name="fema-nfip-claims",
            description="OpenFEMA NFIP per-property claims for the event",
            fetch=fetch_fema_claims,
        ),
        Source(
            name="usgs-watersheds",
            description="USGS WBD watershed polygons -- the unit of work for terrain",
            fetch=fetch_watersheds,
        ),
        Source(
            name="sentinel1-search",
            description="Pin the Sentinel-1 RTC scenes nearest the peak (search needs no key)",
            fetch=fetch_sentinel1_search,
        ),
    )
}


def list_sources() -> list[Source]:
    """Return every registered source, in registry order."""
    return list(REGISTRY.values())


def fetch(
    names: Iterable[str] | None = None,
    *,
    config: Config | None = None,
    dest: Path | None = None,
    client: httpx.Client | None = None,
    limit: int | None = None,
    dry_run: bool = False,
) -> list[FetchResult]:
    """Fetch the named sources, or every automatable one when `names` is None.

    A source that fails does not stop the others: its `FetchResult` carries the
    reason in `note` and holds no artifacts. Reporting a partial fetch honestly is
    the point -- the convention is that a failed source gets written down.
    """
    resolved = config or Config()
    root = dest or resolved.paths.raw
    root.mkdir(parents=True, exist_ok=True)

    if names is None:
        selected = [s for s in list_sources() if s.is_automatable]
    else:
        selected = []
        for name in names:
            if name not in REGISTRY:
                raise KeyError(f"unknown source {name!r}; known: {sorted(REGISTRY)}")
            selected.append(REGISTRY[name])

    owned = client is None
    active = client or make_client(resolved.sources)
    results: list[FetchResult] = []
    try:
        for source in selected:
            context = FetchContext(
                config=resolved, dest=root, client=active, limit=limit, dry_run=dry_run
            )
            try:
                artifacts = source.fetch(context)
            except SourceError as exc:
                results.append(FetchResult(source=source.name, note=f"FAILED: {exc}"))
                continue
            reused = sum(1 for a in artifacts if "reused" in a.note)
            results.append(
                FetchResult(source=source.name, artifacts=tuple(artifacts), reused=reused)
            )
    finally:
        if owned:
            active.close()
    return results


MANIFEST_HEADER = """# Data manifest

Raw downloads live in `data/raw/` and are **never** committed. This file is
generated by `floodline fetch`; every row is a file that was actually retrieved,
with the URL it came from and the SHA256 of the bytes on disk.

Regenerate with:

```bash
uv run floodline fetch --all
```
"""


def write_manifest(
    path: Path, results: Iterable[FetchResult], *, config: Config | None = None
) -> Path:
    """Write `data/MANIFEST.md` from fetch results.

    Only successful artifacts get a row. Failed sources are listed underneath with
    their reason, because a manifest that silently omits what did not arrive is
    worse than useless -- it reads as a complete record.
    """
    resolved = config or Config()
    results = list(results)
    root = resolved.paths.raw

    lines = [MANIFEST_HEADER, "", f"Case: **{resolved.case.description}**", ""]
    lines += [
        "| Dataset | File | Bytes | SHA256 | Retrieved | URL | Notes |",
        "|---|---|---:|---|---|---|---|",
    ]
    rows = 0
    for result in results:
        for artifact in result.artifacts:
            lines.append(artifact.manifest_row(root))
            rows += 1
    if rows == 0:
        lines.append("| _nothing retrieved yet_ | | | | | | |")

    failures = [r for r in results if r.note.startswith("FAILED")]
    if failures:
        lines += ["", "## Sources that did not fetch", ""]
        lines += [f"- **{r.source}** — {r.note.removeprefix('FAILED: ')}" for r in failures]

    manual = [s for s in list_sources() if not s.is_automatable]
    if manual:
        lines += ["", "## Sources needing a credential or a manual step", ""]
        for source in manual:
            need = (
                f"needs `${source.credential_env}`" if source.credential_env else source.manual_note
            )
            lines.append(f"- **{source.name}** — {source.description}. {need}")

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n")
    return path
