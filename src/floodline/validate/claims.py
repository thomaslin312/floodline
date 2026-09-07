"""NFIP insurance claims as an independent check on the building count.

The README's central claim is that the observed building count lands inside the
model's 90% interval. This is the observation half of that test: OpenFEMA publishes
every National Flood Insurance Program claim, and for Hurricane Harvey there are
48,702 in Harris County alone.

**A claim count is a floor, not a count of flooded buildings, and the comparison is
worthless if that is forgotten.** Three reasons it runs low:

* Only insured properties can file. NFIP take-up outside mapped floodplains was a
  small fraction of Houston's housing stock, and Harvey flooded a great deal of ground
  outside them.
* Only owners who chose to claim appear.
* Commercial policies above NFIP limits are written privately and never reach this
  dataset.

So a model interval sitting *below* the claim count is definitely wrong. One sitting
above it is what should happen, and does not by itself validate anything. The useful
statement is a ratio with that asymmetry attached, which is what `ClaimComparison`
reports rather than a pass/fail.

**Geography.** Claim coordinates are published rounded to 0.1 degrees - about 11 km,
useless for a 491 km2 watershed. `censusBlockGroupFips` is the usable field: block
groups run about a square kilometre in urban Houston. A block group straddling the
watershed boundary is counted by what fraction of its area falls inside, which is
better than all-or-nothing and still an approximation, since claims are not spread
evenly within one.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Any

import geopandas as gpd
import httpx
from shapely.geometry.base import BaseGeometry

from floodline.settings import settings

__all__ = ["ClaimComparison", "NfipClaims", "compare_to_interval", "fetch_nfip_claims"]

OPENFEMA_CLAIMS = f"{settings().openfema_url}/FimaNfipClaims"
TIGERWEB_BLOCK_GROUPS = f"{settings().tigerweb_url}/tigerWMS_ACS2023/MapServer/10/query"
_PAGE = 10_000
_MAX_PAGES = 40


@dataclass(frozen=True, slots=True)
class NfipClaims:
    """Claims for one event, aggregated by census block group."""

    by_block_group: dict[str, int]
    paid_by_block_group: dict[str, float]
    n_claims: int
    total_paid: float
    county_fips: str
    start: date
    end: date

    @property
    def n_block_groups(self) -> int:
        """Block groups with at least one claim."""
        return len(self.by_block_group)


@dataclass(frozen=True, slots=True)
class ClaimComparison:
    """A modelled interval set against the claims actually filed."""

    claims: int
    """Claims inside the watershed, area-weighted across boundary block groups."""

    paid: float
    modelled: int
    modelled_low: int
    modelled_high: int
    block_groups_matched: int
    modelled_damage: float | None = None

    notes: tuple[str, ...] = field(default=())

    @property
    def ratio(self) -> float:
        """Modelled inundated buildings per claim filed."""
        return self.modelled / self.claims if self.claims else float("nan")

    @property
    def damage_ratio(self) -> float | None:
        """Modelled damage per dollar actually paid out by NFIP.

        Expect a large multiple and do not read it as error. NFIP caps a building
        claim at USD 250,000 and covers only insured filers, so paid dollars are a far
        harder floor than the claim count is.
        """
        if self.modelled_damage is None or not self.paid:
            return None
        return self.modelled_damage / self.paid

    @property
    def model_below_claims(self) -> bool:
        """True when even the top of the interval is under the claim count.

        This is the one direction that is unambiguously a failure: the model cannot
        be flooding fewer buildings than were insured, filed and paid.
        """
        return self.modelled_high < self.claims

    def summary(self) -> str:
        """One sentence carrying the asymmetry, not just the numbers."""
        verdict = (
            "the model floods fewer buildings than were paid out, which it cannot be right about"
            if self.model_below_claims
            else "the model is above the claim floor, which is expected and is not "
            "by itself a validation"
        )
        return (
            f"{self.modelled:,} modelled ({self.modelled_low:,}-{self.modelled_high:,}) "
            f"against {self.claims:,} NFIP claims: {verdict}."
        )


def fetch_nfip_claims(
    county_fips: str,
    start: date,
    end: date,
    *,
    client: httpx.Client | None = None,
) -> NfipClaims:
    """Fetch and aggregate NFIP claims for one county and date range.

    OpenFEMA caps a response at 10,000 records, so this pages until a short page
    arrives rather than taking the first page and quietly under-counting.
    """
    owned = client is None
    active = client or httpx.Client(timeout=httpx.Timeout(30.0, read=300.0), follow_redirects=True)
    counts: dict[str, int] = {}
    paid: dict[str, float] = {}
    total = 0.0
    n = 0
    try:
        for page in range(_MAX_PAGES):
            response = active.get(
                OPENFEMA_CLAIMS,
                params={
                    "$filter": (
                        f"dateOfLoss ge '{start.isoformat()}' and "
                        f"dateOfLoss le '{end.isoformat()}' and "
                        f"countyCode eq '{county_fips}'"
                    ),
                    "$select": "censusBlockGroupFips,amountPaidOnBuildingClaim",
                    "$top": _PAGE,
                    "$skip": page * _PAGE,
                    "$format": "json",
                    "$metadata": "off",
                },
            )
            response.raise_for_status()
            batch: list[dict[str, Any]] = response.json().get("FimaNfipClaims", [])
            for record in batch:
                block = record.get("censusBlockGroupFips")
                if not block:
                    continue
                key = str(block)
                counts[key] = counts.get(key, 0) + 1
                amount = record.get("amountPaidOnBuildingClaim") or 0.0
                paid[key] = paid.get(key, 0.0) + float(amount)
                total += float(amount)
                n += 1
            if len(batch) < _PAGE:
                break
        else:
            raise RuntimeError(
                f"OpenFEMA paging did not terminate within {_MAX_PAGES} pages "
                f"({n:,} claims so far); narrow the date range"
            )
    finally:
        if owned:
            active.close()

    return NfipClaims(
        by_block_group=counts,
        paid_by_block_group=paid,
        n_claims=n,
        total_paid=total,
        county_fips=county_fips,
        start=start,
        end=end,
    )


def fetch_block_groups(
    bounds_wgs84: tuple[float, float, float, float],
    *,
    client: httpx.Client | None = None,
    page: int = 1000,
) -> gpd.GeoDataFrame:
    """Fetch census block-group polygons intersecting a WGS84 bounding box.

    TIGERweb caps a response and flags `exceededTransferLimit`, so this pages on
    `resultOffset` until the flag clears.
    """
    west, south, east, north = bounds_wgs84
    owned = client is None
    active = client or httpx.Client(timeout=httpx.Timeout(30.0, read=180.0), follow_redirects=True)
    geoids: list[str] = []
    shapes: list[Any] = []
    try:
        offset = 0
        while True:
            response = active.get(
                TIGERWEB_BLOCK_GROUPS,
                params={
                    "geometry": f"{west},{south},{east},{north}",
                    "geometryType": "esriGeometryEnvelope",
                    "inSR": "4326",
                    "outSR": "4326",
                    "spatialRel": "esriSpatialRelIntersects",
                    "outFields": "GEOID",
                    "returnGeometry": "true",
                    "f": "geojson",
                    "resultRecordCount": page,
                    "resultOffset": offset,
                },
            )
            response.raise_for_status()
            payload = response.json()
            features = payload.get("features", [])
            for feature in features:
                geoids.append(str(feature["properties"]["GEOID"]))
                shapes.append(feature["geometry"])
            if len(features) < page:
                break
            offset += page
    finally:
        if owned:
            active.close()

    import shapely

    return gpd.GeoDataFrame(
        {"GEOID": geoids},
        geometry=[shapely.geometry.shape(g) for g in shapes],
        crs="EPSG:4326",
    )


def compare_to_interval(
    claims: NfipClaims,
    block_groups: gpd.GeoDataFrame,
    watershed_wgs84: BaseGeometry,
    *,
    modelled: int,
    modelled_low: int,
    modelled_high: int,
    modelled_damage: float | None = None,
    equal_area_crs: str = "EPSG:5070",
) -> ClaimComparison:
    """Weigh a watershed's share of county claims against the modelled interval.

    Block groups are area-weighted by how much of each falls inside the watershed.
    Claims are not spread evenly within a block group, so this is an approximation -
    but a better one than counting a straddling group entirely in or entirely out.
    """
    if block_groups.crs is None:
        raise ValueError("block groups have no CRS")
    # Areas are measured in an equal-area projection, not in degrees. The ratio of two
    # degree-areas over one city happens to come out close, because the latitude
    # distortion cancels between numerator and denominator - but the project refuses
    # geographic CRSs for area work precisely so nobody has to check that by hand.
    # NAD83 / Conus Albers covers the lower 48; pass another for Alaska or Hawaii.
    frame = block_groups.to_crs("EPSG:4326")
    watershed = gpd.GeoSeries([watershed_wgs84], crs="EPSG:4326").to_crs(equal_area_crs).iloc[0]
    projected = frame.to_crs(equal_area_crs)
    area = projected.geometry.area
    inside = projected.geometry.intersection(watershed)
    share = (inside.area / area.where(area > 0, 1.0)).clip(0.0, 1.0)

    weighted = 0.0
    weighted_paid = 0.0
    matched = 0
    for geoid, fraction in zip(frame["GEOID"], share, strict=True):
        count = claims.by_block_group.get(str(geoid))
        if not count or fraction <= 0:
            continue
        matched += 1
        weighted += count * float(fraction)
        weighted_paid += claims.paid_by_block_group.get(str(geoid), 0.0) * float(fraction)

    notes = [
        "NFIP claims count insured properties whose owners filed; they are a floor on "
        "flooded buildings, not a count of them.",
        "Block groups straddling the watershed are counted by area share, which "
        "assumes claims are spread evenly inside one. They are not.",
    ]
    return ClaimComparison(
        modelled_damage=modelled_damage,
        claims=round(weighted),
        paid=weighted_paid,
        modelled=modelled,
        modelled_low=modelled_low,
        modelled_high=modelled_high,
        block_groups_matched=matched,
        notes=tuple(notes),
    )
