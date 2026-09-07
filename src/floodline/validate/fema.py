"""Modelled damage against FEMA's own record, by census tract.

Two public series, both keyless, both lower bounds, and biased in opposite directions:

* **NFIP claims** need a property insured, flooded, and its owner to file, so they
  concentrate where insurance take-up is high, which is inside mapped floodplains.
* **Individual Assistance** needs uninsured loss and a registration, so it concentrates
  where insurance take-up is low.

Neither is ground truth. What makes them a test rather than two more caveats is that
they agree with each other: they rank tracts almost identically despite sampling
complementary populations, which means there is a real spatial signal in a flood's
damage and a comparison against it has the power to fail.

Rank correlation is the measure, not ratio. Both series are floors, so a level offset
is expected and uninformative; whether the model puts the same neighbourhoods at the
top is the question. Nothing here is fed back into the model - calibrating costs to
claims would erase the only independent test the damage half has, and would do it by
fitting to a series that is itself a lower bound.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import httpx

__all__ = ["TractDamage", "compare_to_fema", "fetch_ia_by_tract", "fetch_nfip_by_tract"]

NFIP_URL = "https://www.fema.gov/api/open/v2/FimaNfipClaims"
IA_URL = "https://www.fema.gov/api/open/v2/IndividualsAndHouseholdsProgramValidRegistrations"
PAGE = 5000
MAX_PAGES = 400

HARVEY_DISASTER = 4332
HARRIS_COUNTY = "48201"
HARVEY_START, HARVEY_END = "2017-08-25", "2017-09-15"


@dataclass(frozen=True, slots=True)
class TractDamage:
    """One census tract, as the model sees it and as FEMA recorded it."""

    tract: str
    modelled_usd: float
    modelled_wet_buildings: int
    nfip_paid_usd: float
    nfip_claims: int
    ia_damage_usd: float
    ia_registrations: int


def _page_through(
    client: httpx.Client,
    url: str,
    key: str,
    params: dict[str, Any],
    take: Callable[[dict[str, Any], dict[str, dict[str, float]]], None],
) -> dict[str, dict[str, float]]:
    """Read an OpenFEMA collection to exhaustion.

    OpenFEMA caps a response, so a caller that takes the first page silently
    under-counts. Pages until a short page arrives, retrying each one: a transient
    failure halfway through would otherwise look like the end of the data.
    """
    out: dict[str, dict[str, float]] = {}
    for page in range(MAX_PAGES):
        query = dict(
            params,
            **{"$top": PAGE, "$skip": page * PAGE, "$format": "json", "$metadata": "off"},
        )
        batch: list[dict[str, Any]] | None = None
        for attempt in (1, 2, 3):
            try:
                response = client.get(url, params=query, timeout=180.0)
                if response.status_code == 200:
                    batch = response.json().get(key, [])
                    break
            except httpx.HTTPError:
                pass
            time.sleep(4 * attempt)
        if batch is None:
            raise RuntimeError(f"OpenFEMA {key} page {page} failed after 3 attempts")
        for record in batch:
            take(record, out)
        if len(batch) < PAGE:
            break
    return out


def fetch_nfip_by_tract(
    client: httpx.Client,
    *,
    county: str = HARRIS_COUNTY,
    start: str = HARVEY_START,
    end: str = HARVEY_END,
) -> dict[str, dict[str, float]]:
    """NFIP claim count and paid amount per census tract.

    Building and contents are summed, because the model's total is both. Increased
    cost of compliance is left out: it pays for elevating or demolishing after the
    fact, not for the damage the flood did.
    """

    def take(record: dict[str, Any], out: dict[str, dict[str, float]]) -> None:
        tract = str(record.get("censusTract") or "")[:11]
        if not tract:
            return
        row = out.setdefault(tract, {"n": 0.0, "paid": 0.0})
        row["n"] += 1.0
        row["paid"] += float(record.get("amountPaidOnBuildingClaim") or 0.0)
        row["paid"] += float(record.get("amountPaidOnContentsClaim") or 0.0)

    return _page_through(
        client,
        NFIP_URL,
        "FimaNfipClaims",
        {
            "$filter": (
                f"countyCode eq '{county}' and dateOfLoss ge '{start}' and dateOfLoss le '{end}'"
            ),
            "$select": "censusTract,amountPaidOnBuildingClaim,amountPaidOnContentsClaim",
        },
        take,
    )


def fetch_ia_by_tract(
    client: httpx.Client, *, disaster: int = HARVEY_DISASTER, state: str = "TX"
) -> dict[str, dict[str, float]]:
    """FEMA-assessed flood damage per census tract, from Individual Assistance.

    `floodDamageAmount` is FEMA's own inspected estimate of what the flood did, which
    is closer to what this model computes than a payout is: assistance is capped, and
    a cap measures the programme rather than the flood.
    """

    def take(record: dict[str, Any], out: dict[str, dict[str, float]]) -> None:
        tract = str(record.get("censusGeoid") or "")[:11]
        if not tract:
            return
        row = out.setdefault(tract, {"n": 0.0, "flood_damage": 0.0})
        row["n"] += 1.0
        row["flood_damage"] += float(record.get("floodDamageAmount") or 0.0)

    return _page_through(
        client,
        IA_URL,
        "IndividualsAndHouseholdsProgramValidRegistrations",
        {
            "$filter": (f"disasterNumber eq {disaster} and damagedStateAbbreviation eq '{state}'"),
            "$select": "censusGeoid,floodDamageAmount",
        },
        take,
    )


def compare_to_fema(huc: str = "1204010403", *, samples: int = 200) -> dict[str, Any]:
    """Run the model on one watershed and rank its tracts against both FEMA series.

    The join is exact rather than spatial: the National Structure Inventory publishes
    a census block per structure and both FEMA series publish a tract, so the two sides
    meet on an identifier instead of on a polygon overlay that would add its own error
    to the comparison.
    """
    from floodline.assess import assess_watershed
    from floodline.compute import watershed_by_huc
    from floodline.config import Config
    from floodline.reproduce import spearman

    with httpx.Client(follow_redirects=True) as client:
        nfip = fetch_nfip_by_tract(client)
        ia = fetch_ia_by_tract(client)

    unit, config = watershed_by_huc(huc, config=Config())
    assessment = assess_watershed(
        unit, config=config, resolution_m=30.0, inventory="nsi", samples=samples
    )
    if assessment.buildings is None or assessment.damage is None:
        raise RuntimeError(f"no damage estimate for {huc}")

    frame = assessment.buildings.buildings.copy()
    frame["damage"] = assessment.damage.per_building
    frame["wet"] = frame["floor_depth_m"] > 0.0
    modelled = frame.groupby("tract_fips")["damage"].sum()
    wet = frame.groupby("tract_fips")["wet"].sum()

    rows = [
        TractDamage(
            tract=str(tract),
            modelled_usd=float(value),
            modelled_wet_buildings=int(wet.get(tract, 0)),
            nfip_paid_usd=nfip.get(str(tract), {}).get("paid", 0.0),
            nfip_claims=int(nfip.get(str(tract), {}).get("n", 0)),
            ia_damage_usd=ia.get(str(tract), {}).get("flood_damage", 0.0),
            ia_registrations=int(ia.get(str(tract), {}).get("n", 0)),
        )
        for tract, value in modelled.items()
        if value > 0
    ]
    both = [r for r in rows if r.nfip_paid_usd > 0 and r.ia_damage_usd > 0]

    def ratios(pairs: list[tuple[float, float]]) -> float:
        ordered = sorted(a / b for a, b in pairs)
        return ordered[len(ordered) // 2] if ordered else float("nan")

    return {
        "huc": huc,
        "tracts_modelled": len(rows),
        "tracts_with_both_series": len(both),
        "nfip_claims_total": int(sum(v["n"] for v in nfip.values())),
        "nfip_paid_total_usd": round(sum(v["paid"] for v in nfip.values()), 0),
        "ia_registrations_total": int(sum(v["n"] for v in ia.values())),
        "ia_damage_total_usd": round(sum(v["flood_damage"] for v in ia.values()), 0),
        "rho_nfip_vs_ia": round(
            spearman([r.nfip_paid_usd for r in both], [r.ia_damage_usd for r in both]), 3
        ),
        "rho_model_vs_nfip": round(
            spearman([r.modelled_usd for r in both], [r.nfip_paid_usd for r in both]), 3
        ),
        "rho_model_vs_ia": round(
            spearman([r.modelled_usd for r in both], [r.ia_damage_usd for r in both]), 3
        ),
        "median_ratio_model_over_nfip": round(
            ratios([(r.modelled_usd, r.nfip_paid_usd) for r in both]), 1
        ),
        "median_ratio_model_over_ia": round(
            ratios([(r.modelled_usd, r.ia_damage_usd) for r in both]), 1
        ),
    }
