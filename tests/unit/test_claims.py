from __future__ import annotations

from datetime import date
from typing import Any

import geopandas as gpd
import httpx
import pytest
from shapely.geometry import box

from floodline.validate.claims import (
    NfipClaims,
    compare_to_interval,
    fetch_block_groups,
    fetch_nfip_claims,
)

# Two 0.01-degree block groups side by side; the watershed covers the first and
# exactly half of the second.
BG = gpd.GeoDataFrame(
    {"GEOID": ["482010000001", "482010000002"]},
    geometry=[box(-95.50, 29.80, -95.49, 29.81), box(-95.49, 29.80, -95.48, 29.81)],
    crs="EPSG:4326",
)
WATERSHED = box(-95.50, 29.80, -95.485, 29.81)


def _claims(**counts: int) -> NfipClaims:
    return NfipClaims(
        by_block_group=dict(counts),
        paid_by_block_group={k: v * 50_000.0 for k, v in counts.items()},
        n_claims=sum(counts.values()),
        total_paid=sum(counts.values()) * 50_000.0,
        county_fips="48201",
        start=date(2017, 8, 25),
        end=date(2017, 9, 5),
    )


def test_a_wholly_contained_block_group_contributes_all_its_claims() -> None:
    got = compare_to_interval(
        _claims(**{"482010000001": 100}),
        BG,
        WATERSHED,
        modelled=500,
        modelled_low=300,
        modelled_high=800,
    )
    assert got.claims == 100


def test_a_straddling_block_group_is_weighted_by_area_share() -> None:
    got = compare_to_interval(
        _claims(**{"482010000002": 100}),
        BG,
        WATERSHED,
        modelled=500,
        modelled_low=300,
        modelled_high=800,
    )
    # Half the second group lies inside.
    assert got.claims == pytest.approx(50, abs=2)


def test_block_groups_outside_the_watershed_contribute_nothing() -> None:
    outside = gpd.GeoDataFrame(
        {"GEOID": ["482010000009"]},
        geometry=[box(-90.0, 40.0, -89.99, 40.01)],
        crs="EPSG:4326",
    )
    got = compare_to_interval(
        _claims(**{"482010000009": 500}),
        outside,
        WATERSHED,
        modelled=10,
        modelled_low=5,
        modelled_high=20,
    )
    assert got.claims == 0
    assert got.block_groups_matched == 0


def test_a_model_below_the_claim_floor_is_flagged_as_wrong() -> None:
    """The one unambiguous failure: fewer flooded buildings than paid claims."""
    got = compare_to_interval(
        _claims(**{"482010000001": 1000}),
        BG,
        WATERSHED,
        modelled=100,
        modelled_low=50,
        modelled_high=200,
    )
    assert got.model_below_claims is True
    assert "cannot be right" in got.summary()


def test_a_model_above_the_floor_is_not_called_validated() -> None:
    got = compare_to_interval(
        _claims(**{"482010000001": 100}),
        BG,
        WATERSHED,
        modelled=500,
        modelled_low=300,
        modelled_high=800,
    )
    assert got.model_below_claims is False
    assert "not by itself a validation" in got.summary()


def test_the_asymmetry_is_always_stated_in_the_notes() -> None:
    got = compare_to_interval(
        _claims(**{"482010000001": 100}),
        BG,
        WATERSHED,
        modelled=500,
        modelled_low=300,
        modelled_high=800,
    )
    assert any("floor on" in note for note in got.notes)
    assert any("evenly" in note for note in got.notes)


def test_damage_ratio_is_none_without_a_modelled_damage() -> None:
    got = compare_to_interval(
        _claims(**{"482010000001": 100}),
        BG,
        WATERSHED,
        modelled=500,
        modelled_low=300,
        modelled_high=800,
    )
    assert got.damage_ratio is None


def test_damage_ratio_compares_against_dollars_paid() -> None:
    got = compare_to_interval(
        _claims(**{"482010000001": 100}),
        BG,
        WATERSHED,
        modelled=500,
        modelled_low=300,
        modelled_high=800,
        modelled_damage=50_000_000.0,
    )
    # Not exactly 5,000,000: the watershed is a degree-aligned box, and reprojecting
    # it to Albers curves its edges, so the block group is 99.999% inside rather than
    # wholly so. That is the area weighting working, not an error.
    assert got.paid == pytest.approx(5_000_000.0, rel=1e-4)
    assert got.damage_ratio == pytest.approx(10.0, rel=1e-4)


def test_block_groups_without_a_crs_are_refused() -> None:
    naked = gpd.GeoDataFrame({"GEOID": ["1"]}, geometry=[box(0, 0, 1, 1)], crs=None)
    with pytest.raises(ValueError, match="no CRS"):
        compare_to_interval(
            _claims(), naked, WATERSHED, modelled=1, modelled_low=1, modelled_high=1
        )


def _paged_client(pages: list[list[dict[str, Any]]]) -> httpx.Client:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        i = calls["n"]
        calls["n"] += 1
        body = pages[i] if i < len(pages) else []
        return httpx.Response(200, json={"FimaNfipClaims": body})

    return httpx.Client(transport=httpx.MockTransport(handler))


def test_claims_are_aggregated_by_block_group() -> None:
    page = [
        {"censusBlockGroupFips": "482010000001", "amountPaidOnBuildingClaim": 100.0},
        {"censusBlockGroupFips": "482010000001", "amountPaidOnBuildingClaim": 200.0},
        {"censusBlockGroupFips": "482010000002", "amountPaidOnBuildingClaim": 50.0},
    ]
    got = fetch_nfip_claims(
        "48201", date(2017, 8, 25), date(2017, 9, 5), client=_paged_client([page])
    )
    assert got.n_claims == 3
    assert got.by_block_group["482010000001"] == 2
    assert got.paid_by_block_group["482010000001"] == pytest.approx(300.0)
    assert got.total_paid == pytest.approx(350.0)


def test_a_null_payout_counts_as_a_claim_with_no_money() -> None:
    page = [{"censusBlockGroupFips": "482010000001", "amountPaidOnBuildingClaim": None}]
    got = fetch_nfip_claims(
        "48201", date(2017, 8, 25), date(2017, 9, 5), client=_paged_client([page])
    )
    assert got.n_claims == 1
    assert got.total_paid == 0.0


def test_a_claim_with_no_block_group_is_skipped_not_counted_nowhere() -> None:
    page = [
        {"censusBlockGroupFips": None, "amountPaidOnBuildingClaim": 100.0},
        {"censusBlockGroupFips": "482010000001", "amountPaidOnBuildingClaim": 100.0},
    ]
    got = fetch_nfip_claims(
        "48201", date(2017, 8, 25), date(2017, 9, 5), client=_paged_client([page])
    )
    assert got.n_claims == 1


def test_paging_continues_past_a_full_page() -> None:
    """OpenFEMA caps at 10,000; stopping at the first page would under-count."""
    full = [
        {"censusBlockGroupFips": f"4820100{i:05d}", "amountPaidOnBuildingClaim": 1.0}
        for i in range(10_000)
    ]
    tail = [{"censusBlockGroupFips": "482010000001", "amountPaidOnBuildingClaim": 1.0}]
    got = fetch_nfip_claims(
        "48201", date(2017, 8, 25), date(2017, 9, 5), client=_paged_client([full, tail])
    )
    assert got.n_claims == 10_001


def test_block_group_geometry_pages_too() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        offset = int(request.url.params.get("resultOffset", 0))
        n = 1000 if offset == 0 else 3
        return httpx.Response(
            200,
            json={
                "features": [
                    {
                        "properties": {"GEOID": f"48201{offset + i:07d}"},
                        "geometry": {
                            "type": "Polygon",
                            "coordinates": [
                                [[-95.5, 29.8], [-95.49, 29.8], [-95.49, 29.81], [-95.5, 29.8]]
                            ],
                        },
                    }
                    for i in range(n)
                ]
            },
        )

    frame = fetch_block_groups(
        (-95.6, 29.7, -95.3, 29.96),
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    assert len(frame) == 1003
    assert frame.crs is not None
