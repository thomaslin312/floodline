"""Everything that changes between one deployment and the next.

The line this file draws is between *what the model is* and *where it runs*. Manning's
roughness, an accumulation threshold, a curve family: those describe the model and live
in `core.config`, versioned with the code that reads them, because changing one changes
the answer. A cache directory, an agency's hostname, a concurrency cap: those describe
one machine on one afternoon, and hard-coding them is what makes a package impossible
to deploy twice.

Read from the environment, and from a `.env` file when one is present, under the
`FLOODLINE_` prefix. Every field has a default that works from a checkout, so a
developer needs no `.env` at all and an operator can override any single field without
supplying the rest.

`core` may import this module and nothing else outside itself, which is the one rule
that keeps the modelling half testable without a network or a filesystem. In practice
`core` needs almost nothing from here - it takes arrays and parameters - and that is
the point: if a core module starts needing a URL, something has moved to the wrong side
of the line.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

__all__ = ["Settings", "settings"]


def section(title: str, note: str = "") -> dict[str, Any]:
    """Mark a field as opening a new block of `.env.example`.

    The grouping lives on the model rather than in the generator, so a new setting
    lands in the section it is declared in and there is no second list to keep in
    step. Only the first field of a block carries this.
    """
    return {"section": {"title": title, "note": note}}


class Settings(BaseSettings):
    """Deployment configuration: paths, endpoints, and limits.

    Immutable once built. A setting that changed under a running request would make two
    halves of one answer disagree about where the cache was.
    """

    model_config = SettingsConfigDict(
        env_prefix="FLOODLINE_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        frozen=True,
    )

    # ---- where things live -------------------------------------------------------
    data_raw: Path = Field(
        default=Path("data/raw"),
        description="Downloaded source data. Never committed; deletable and refetched.",
        json_schema_extra=section("Where things live"),
    )
    data_interim: Path = Field(default=Path("data/interim"))
    data_processed: Path = Field(default=Path("data/processed"))
    outputs: Path = Field(default=Path("outputs"))
    cache_dir: Path = Field(
        default=Path("data/cache"),
        description="Fetched artefacts that are expensive to re-download: the structure "
        "inventory, curve libraries, the population raster.",
    )
    bundle_cache_dir: Path = Field(
        default=Path("outputs/cache"),
        description="Computed watershed bundles the service serves. Evicted to "
        "bundle_cache_budget_mb.",
    )
    hand_cache_dir: Path = Field(
        default=Path("outputs/hand"),
        description="Cached terrain results, keyed by watershed and parameter hash. "
        "The expensive, request-independent half of the pipeline.",
    )
    results_path: Path = Field(
        default=Path("docs/RESULTS.json"),
        description="Published values that `floodline reproduce` regenerates and checks.",
    )
    marks_path: Path = Field(
        default=Path("data/raw/validation/high_water_marks_national.json"),
        description="Cached national high-water marks, the ground truth for validation.",
    )

    # ---- upstream services -------------------------------------------------------
    # Every one of these is a public, keyless endpoint. They are settings rather than
    # constants because agencies move them: three of these changed host or format
    # during development, and each time it was a code change rather than a config one.
    tnm_products_url: str = Field(
        default="https://tnmaccess.nationalmap.gov/api/v1/products",
        description="USGS National Map product search, for 3DEP elevation tiles.",
        json_schema_extra=section(
            "Upstream services (public, keyless)",
            "Every one of these is a public, keyless endpoint. They are settings "
            "rather than constants because agencies move them: three changed host or "
            "format during development, and each time it was a code change.",
        ),
    )
    nwis_instantaneous_url: str = Field(
        default="https://waterservices.usgs.gov/nwis/iv/",
        description="USGS NWIS instantaneous values.",
    )
    nwis_site_url: str = Field(
        default="https://waterservices.usgs.gov/nwis/site/",
        description="USGS NWIS site service, for gauges within a bounding box.",
    )
    nwis_peak_url: str = Field(
        default="https://nwis.waterdata.usgs.gov/nwis/peak",
        description="USGS annual peak-flow series, including peak gage height.",
    )
    stn_hwm_url: str = Field(
        default="https://stn.wim.usgs.gov/STNServices/HWMs/FilteredHWMs.json",
        description="USGS Short-Term Network high-water marks, filtered.",
    )
    stn_all_hwm_url: str = Field(
        default="https://stn.wim.usgs.gov/STNServices/HWMs.json",
        description="Every high-water mark the STN holds. About 26 MB; fetched once.",
    )
    stn_events_url: str = Field(
        default="https://stn.wim.usgs.gov/STNServices/Events.json",
        description="Named flood events, joined to marks so a mark says which flood.",
    )
    wbd_url: str = Field(
        default="https://hydro.nationalmap.gov/arcgis/rest/services/wbd/MapServer",
        description="USGS Watershed Boundary Dataset, for hydrologic unit polygons.",
    )
    nsi_url: str = Field(
        default="https://nsi.sec.usace.army.mil/nsiapi/structures",
        description="USACE National Structure Inventory. POST a polygon, get structures.",
    )
    usace_curves_url: str = Field(
        default=(
            "https://raw.githubusercontent.com/USACE/go-consequences/main/structures/occtypes.json"
        ),
        description="USACE depth-damage curve library, MIT licensed.",
    )
    openfema_url: str = Field(
        default="https://www.fema.gov/api/open/v2",
        description="OpenFEMA, for NFIP claims and Individual Assistance.",
    )
    tigerweb_url: str = Field(
        default="https://tigerweb.geo.census.gov/arcgis/rest/services/TIGERweb",
        description="Census TIGERweb, for ZIP and block-group geography.",
    )
    census_geocode_url: str = Field(
        default="https://geocoding.geo.census.gov/geocoder/locations/onelineaddress",
        description="Census geocoder, for turning an address into a coordinate.",
    )
    planetary_computer_stac_url: str = Field(
        default="https://planetarycomputer.microsoft.com/api/stac/v1/search",
        description="Microsoft Planetary Computer STAC, for Sentinel-1 scenes.",
    )
    worldpop_url: str = Field(
        default=(
            "https://data.worldpop.org/GIS/Population/Global_2000_2020_Constrained/"
            "2020/BSGM/USA/usa_ppp_2020_UNadj_constrained.tif"
        ),
        description="WorldPop constrained population. Advertises range support and "
        "ignores it, so this is downloaded once rather than windowed.",
    )
    ghsl_url: str = Field(
        default=("https://jeodpp.jrc.ec.europa.eu/ftp/jrc-opendata/GHSL/GHS_POP_GLOBE_R2023A/"),
        description="JRC Global Human Settlement population, the WorldPop alternative.",
    )

    # ---- how to talk to them -----------------------------------------------------
    user_agent: str = Field(
        default="floodline/0.1 (+https://github.com/thomaslin312/floodline)",
        description="Sent on every request. Agencies block anonymous bulk readers, and "
        "identifying the client is the difference between being throttled and blocked.",
        json_schema_extra=section("How to talk to them"),
    )
    http_max_attempts: int = Field(
        default=4, ge=1, description="Attempts per request before giving up."
    )
    http_backoff_seconds: float = Field(
        default=2.0, ge=0.0, description="Base for exponential backoff between attempts."
    )
    http_connect_timeout_s: float = Field(default=30.0, gt=0)
    http_read_timeout_s: float = Field(
        default=300.0, gt=0, description="Generous: some DEM tiles are hundreds of MB."
    )

    # ---- service limits ----------------------------------------------------------
    max_cells: int = Field(
        default=40_000_000,
        gt=0,
        description="Largest grid the service will attempt. Depression filling is "
        "global, so the whole watershed must be resident in memory.",
        json_schema_extra=section("Service limits"),
    )
    max_concurrent: int = Field(
        default=2,
        ge=1,
        description="Watershed computations in flight at once. Guards this machine.",
    )
    rate_per_minute: float = Field(
        default=30.0, gt=0, description="Per-client request budget. Guards everyone upstream."
    )
    rate_burst: int = Field(
        default=10, ge=1, description="Requests a client may make back to back."
    )
    bundle_cache_budget_mb: float = Field(
        default=2048.0, gt=0, description="Disk the served bundles may occupy before eviction."
    )
    terrain_cache_budget_mb: float = Field(
        default=8192.0,
        gt=0,
        description="Disk the cached terrain artefacts may occupy before the oldest "
        "are evicted. Roughly 4.5 MB per watershed at 30 m and nine times that at "
        "10 m, and this service offers every watershed in the country, so unbounded "
        "means a disk that fills at whatever rate visitors click.",
    )
    inventory_cache_budget_mb: float = Field(
        default=8192.0,
        gt=0,
        description="Disk the cached structure inventory may occupy before the oldest "
        "are evicted. About 16 MB per watershed. Evicting one costs a refetch from "
        "NSI, which is minutes, so this wants to be generous.",
    )
    request_timeout_s: float = Field(
        default=180.0,
        gt=0,
        description="Ceiling on one synchronous /scenario request. A cache miss must "
        "fetch a DEM, which is the slow part; past this the caller is told to retry "
        "rather than left holding a socket open.",
    )

    # ---- database ----------------------------------------------------------------
    database_url: str = Field(
        default="postgresql+psycopg://floodline:floodline@localhost:5432/floodline",
        description="PostGIS connection string. The compose stack sets this to the db "
        "service; a developer without one can leave it and the API will report itself "
        "not ready rather than failing at the first query.",
        json_schema_extra=section(
            "Database",
            "compose interpolates POSTGRES_USER and POSTGRES_PASSWORD into the URL "
            "below, so setting those is usually enough and this line is for a "
            "database compose does not run.",
        ),
    )
    db_pool_size: int = Field(
        default=5, ge=1, description="Connections held open. Small: requests are seconds, not ms."
    )
    db_connect_timeout_s: int = Field(
        default=5,
        ge=1,
        description="Refuse quickly when the database is down. A readiness probe that "
        "hangs is worse than one that fails.",
    )
    building_index: Literal["postgis", "geopandas"] = Field(
        default="postgis",
        description="Where the structure intersection runs. PostGIS is the deployed "
        "path: an index scan over a GiST index rather than a quarter of a million rows "
        "held resident to discard most. `geopandas` keeps the in-process predicate "
        "runnable, which is what makes the equivalence between them a measurement "
        "rather than a citation. A deployment with no database falls back to it and "
        "says so in the run's gaps.",
    )


@lru_cache(maxsize=1)
def settings() -> Settings:
    """Return the process's settings, read once.

    Cached because reading the environment on every call would let two halves of one
    request disagree if the environment changed under them, and because a settings
    object is not cheap enough to rebuild in a loop. Tests that need different values
    construct `Settings(...)` directly rather than mutating the environment.
    """
    return Settings()
