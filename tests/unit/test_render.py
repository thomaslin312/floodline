from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest
from rasterio.crs import CRS
from rasterio.transform import Affine
from shapely.geometry import box

from floodline.assess import Assessment
from floodline.io.ingest import Watershed
from floodline.io.raster import Raster
from floodline.report.render import ReportInputs, render_report, report_payload


def _assessment(**overrides: object) -> Assessment:
    transform = Affine.translation(0.0, 1000.0) * Affine.scale(30.0, -30.0)
    depth = np.zeros((40, 40), dtype=np.float64)
    depth[10:30, 12:28] = 1.6
    base = {
        "unit": Watershed(
            huc="1204010403",
            name="Whiteoak Bayou-Buffalo Bayou",
            area_km2=490.6,
            geometry=box(0.0, 0.0, 1200.0, 1200.0),
        ),
        "resolution_m": 30.0,
        "discharge_cms": 1433.0,
        "gauged": True,
        "depth": Raster(data=depth, transform=transform, crs=CRS.from_epsg(26915), nodata=-9999.0),
        "margin": depth - 0.15,
        "n_wet_cells": int((depth > 0).sum()),
        "flooded_km2": 112.2,
        "max_depth_m": 10.9,
        "history": None,
        "buildings": None,
        "people": None,
        "damage": None,
        "interval": None,
    }
    base.update(overrides)
    return Assessment(**base)  # type: ignore[arg-type]


def test_report_is_one_self_contained_file(tmp_path: Path) -> None:
    path = render_report(ReportInputs(assessment=_assessment()), tmp_path / "r.html")
    page = path.read_text()
    assert page.startswith("<!doctype html>")
    # No network dependencies: the image is inlined, the CSS is inline.
    assert "data:image/png;base64," in page
    assert "http://" not in page
    assert "<link" not in page


def test_limits_come_before_figures(tmp_path: Path) -> None:
    """The README's rule, enforced: state what it cannot do before what it found."""
    page = render_report(ReportInputs(assessment=_assessment()), tmp_path / "r.html").read_text()
    assert page.index("What this cannot tell you") < page.index(">Figures<")


def test_an_ungauged_run_says_the_discharge_was_supplied(tmp_path: Path) -> None:
    page = render_report(
        ReportInputs(assessment=_assessment(gauged=False)), tmp_path / "r.html"
    ).read_text()
    assert "supplied, not observed" in page
    assert "describes a scenario" in page


def test_a_gauged_run_does_not_claim_a_scenario(tmp_path: Path) -> None:
    page = render_report(
        ReportInputs(assessment=_assessment(gauged=True)), tmp_path / "r.html"
    ).read_text()
    assert "describes a scenario" not in page


def test_gaps_are_carried_into_the_limits(tmp_path: Path) -> None:
    page = render_report(
        ReportInputs(assessment=_assessment(gaps=["NSI unavailable: timeout"])),
        tmp_path / "r.html",
    ).read_text()
    assert "NSI unavailable: timeout" in page


def test_the_structural_caveat_is_always_present(tmp_path: Path) -> None:
    page = render_report(ReportInputs(assessment=_assessment()), tmp_path / "r.html").read_text()
    assert "parallels the drainage line" in page


def test_watershed_names_are_escaped_not_injected(tmp_path: Path) -> None:
    unit = Watershed(
        huc="1", name="<script>alert(1)</script>", area_km2=1.0, geometry=box(0, 0, 1, 1)
    )
    page = render_report(
        ReportInputs(assessment=_assessment(unit=unit)), tmp_path / "r.html"
    ).read_text()
    assert "<script>alert(1)</script>" not in page
    assert "&lt;script&gt;" in page


def test_a_dry_watershed_still_renders(tmp_path: Path) -> None:
    dry = _assessment(
        depth=Raster(
            data=np.zeros((10, 10)),
            transform=Affine.translation(0, 100) * Affine.scale(10, -10),
            crs=CRS.from_epsg(26915),
            nodata=-9999.0,
        ),
        flooded_km2=0.0,
        max_depth_m=0.0,
        n_wet_cells=0,
    )
    page = render_report(ReportInputs(assessment=dry), tmp_path / "r.html").read_text()
    assert "0.0 km2" in page


def test_nan_depth_does_not_break_the_image(tmp_path: Path) -> None:
    depth = np.full((20, 20), np.nan)
    depth[5:10, 5:10] = 2.0
    result = _assessment(
        depth=Raster(
            data=depth,
            transform=Affine.translation(0, 200) * Affine.scale(10, -10),
            crs=CRS.from_epsg(26915),
            nodata=-9999.0,
        )
    )
    page = render_report(ReportInputs(assessment=result), tmp_path / "r.html").read_text()
    assert "data:image/png;base64," in page


def test_payload_matches_what_the_page_reports() -> None:
    payload = report_payload(_assessment())
    assert payload["huc"] == "1204010403"
    assert payload["flooded_km2"] == pytest.approx(112.2)
    assert payload["damage"] is None
    assert payload["gaps"] == []


def test_marks_reach_the_report(tmp_path: Path) -> None:
    """Validation against surveyed marks is the project's only real extent check,
    so it has to appear on the page rather than only in the map's JavaScript."""
    from floodline.validate.metrics import mark_metrics

    scored = mark_metrics(
        np.array([10.0, 12.0, 11.0]),
        np.array([10.2, np.nan, 11.1]),
        np.array([8.0, 8.0, 8.0]),
    )
    page = render_report(
        ReportInputs(assessment=_assessment(marks=scored, n_marks_available=3)),
        tmp_path / "r.html",
    ).read_text()
    assert "RMSE" in page


def _ladder(damage: list[float]) -> object:
    from floodline.core.damage.ladder import DamageLadder

    n = len(damage)
    return DamageLadder(
        multipliers=tuple(i / (n - 1) * 3 for i in range(n)),
        discharge_cms=tuple(i / (n - 1) * 3000 for i in range(n)),
        damage=tuple(damage),
        structure=tuple(d * 0.6 for d in damage),
        contents=tuple(d * 0.4 for d in damage),
        inundated=tuple(int(d / 1e5) for d in damage),
        in_channel=tuple(0.0 for _ in damage),
        residents=tuple(d / 1e4 for d in damage),
    )


def test_the_damage_curve_is_drawn_when_there_is_a_ladder(tmp_path: Path) -> None:
    page = render_report(
        ReportInputs(assessment=_assessment(ladder=_ladder([0.0, 2e9, 6e9, 1.2e10]))),
        tmp_path / "r.html",
    ).read_text()
    assert "Damage against discharge" in page
    assert "<svg" in page
    assert "polyline" in page


def test_no_curve_is_drawn_without_a_ladder(tmp_path: Path) -> None:
    page = render_report(ReportInputs(assessment=_assessment()), tmp_path / "r.html").read_text()
    assert "Damage against discharge" not in page


def test_an_all_zero_ladder_draws_nothing_rather_than_dividing_by_zero(
    tmp_path: Path,
) -> None:
    page = render_report(
        ReportInputs(assessment=_assessment(ladder=_ladder([0.0, 0.0, 0.0, 0.0]))),
        tmp_path / "r.html",
    ).read_text()
    assert "Damage against discharge" not in page


def test_the_chart_marks_the_discharge_the_report_is_about(tmp_path: Path) -> None:
    page = render_report(
        ReportInputs(
            assessment=_assessment(ladder=_ladder([0.0, 2e9, 6e9, 1.2e10]), discharge_cms=1500.0)
        ),
        tmp_path / "r.html",
    ).read_text()
    assert ">observed<" in page
