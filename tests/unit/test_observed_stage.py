"""Water levels interpolated between gauges, along the channel rather than across it."""

from __future__ import annotations

import numpy as np

from floodline.hydraulics.observed_stage import (
    GaugeStage,
    interpolate_stage,
    peak_water_surface,
    reach_graph,
)


def _straight_channel(n_reaches: int, cells_each: int = 3):
    """One row of cells flowing left to right, cut into equal reaches."""
    cols = n_reaches * cells_each
    reach = np.full((1, cols), -1, dtype=np.int64)
    for r in range(n_reaches):
        reach[0, r * cells_each : (r + 1) * cells_each] = r
    downstream = np.arange(cols, dtype=np.int64) + 1
    downstream[-1] = -1  # the outlet leaves the raster
    return reach, downstream


def test_reach_graph_follows_flow_not_geometry() -> None:
    reach, downstream = _straight_channel(3)
    down, length = reach_graph(reach, downstream, cellsize_m=10.0)
    assert down == {0: 1, 1: 2}, "each reach hands off to the next one downstream"
    assert length[0] == 30.0, "three cells of ten metres"


def test_a_reach_between_two_gauges_is_interpolated_by_channel_distance() -> None:
    reach, downstream = _straight_channel(5)
    bed = {r: 100.0 - r for r in range(5)}  # falls one metre per reach
    gauges = [
        GaugeStage("up", 0, wse_m=105.0, bed_m=100.0, datum="NAVD88", date="", area_km2=10),
        GaugeStage("down", 4, wse_m=101.0, bed_m=96.0, datum="NAVD88", date="", area_km2=40),
    ]
    out = interpolate_stage(gauges, reach, downstream, bed, cellsize_m=10.0)

    assert out.gauged == {0, 4}
    assert out.interpolated == {1, 2, 3}
    assert not out.extrapolated
    # Depth is what is interpolated, then the local bed is added back. Both gauges
    # stand in 5 m of water, so every reach between them does too.
    assert np.isclose(out.wse_by_reach[2], bed[2] + 5.0)
    assert all(np.isclose(out.by_reach[r], 5.0) for r in range(5))
    # Levels fall monotonically downstream, which is the whole point.
    levels = [out.wse_by_reach[r] for r in range(5)]
    assert levels == sorted(levels, reverse=True)


def test_outside_the_gauged_span_depth_is_carried_not_level() -> None:
    """A flat water surface projected onto a rising bed floods the hillside."""
    reach, downstream = _straight_channel(4)
    bed = {0: 110.0, 1: 105.0, 2: 100.0, 3: 95.0}
    gauges = [GaugeStage("only", 2, wse_m=103.0, bed_m=100.0, datum="NAVD88", date="", area_km2=20)]
    out = interpolate_stage(gauges, reach, downstream, bed, cellsize_m=10.0)

    assert out.gauged == {2}
    assert out.extrapolated == {0, 1, 3}
    assert not out.interpolated
    # The gauge stands in 3 m of water; every extrapolated reach keeps that depth.
    assert all(np.isclose(out.by_reach[r], 3.0) for r in (0, 1, 3))
    assert np.isclose(out.wse_by_reach[0], 113.0), "level follows the bed upstream"


def test_a_dry_reach_cannot_have_negative_stage() -> None:
    reach, downstream = _straight_channel(2)
    bed = {0: 50.0, 1: 40.0}
    gauges = [GaugeStage("g", 1, wse_m=38.0, bed_m=40.0, datum="NAVD88", date="", area_km2=5)]
    out = interpolate_stage(gauges, reach, downstream, bed, cellsize_m=10.0)
    assert all(v >= 0.0 for v in out.by_reach.values())


def test_mixed_vertical_datums_are_flagged() -> None:
    """NAVD88 and NGVD29 differ by the size of the error being chased."""
    reach, downstream = _straight_channel(3)
    bed = dict.fromkeys(range(3), 10.0)
    same = [
        GaugeStage("a", 0, 12.0, 10.0, "NAVD88", "", 1),
        GaugeStage("b", 2, 11.0, 10.0, "NAVD88", "", 2),
    ]
    mixed = [
        GaugeStage("a", 0, 12.0, 10.0, "NAVD88", "", 1),
        GaugeStage("b", 2, 11.0, 10.0, "NGVD29", "", 2),
    ]
    assert not interpolate_stage(same, reach, downstream, bed, cellsize_m=10.0).mixed_datums
    assert interpolate_stage(mixed, reach, downstream, bed, cellsize_m=10.0).mixed_datums


def test_no_gauges_yields_nothing_rather_than_a_guess() -> None:
    reach, downstream = _straight_channel(3)
    out = interpolate_stage([], reach, downstream, {0: 1.0}, cellsize_m=10.0)
    assert out.by_reach == {} and out.n_gauges == 0


def test_a_peak_without_a_stage_is_not_invented() -> None:
    """Plenty of peaks carry a discharge and no gage height."""
    site = {"alt_va": "50.0", "alt_datum_cd": "NAVD88"}
    assert peak_water_surface(site, {"gage_ht": ""}) is None
    assert peak_water_surface({"alt_va": ""}, {"gage_ht": "12.0"}) is None
    wse, datum = peak_water_surface(site, {"gage_ht": "12.0"})  # type: ignore[misc]
    assert np.isclose(wse, 62.0 * 0.3048)
    assert datum == "NAVD88"


def test_a_gauge_that_cannot_be_reconciled_with_the_bed_is_dropped() -> None:
    """One bad datum propagates along the whole interpolated span, not just its reach.

    A real basin came back with a 1,232 m residual from a single record whose
    published altitude did not describe the channel the model built.
    """
    from floodline.hydraulics.observed_stage import select_gauges

    good = GaugeStage("good", 0, wse_m=103.0, bed_m=100.0, datum="NAVD88", date="", area_km2=10)
    absurd = GaugeStage("absurd", 1, wse_m=1300.0, bed_m=99.0, datum="NAVD88", date="", area_km2=12)
    below = GaugeStage("below", 2, wse_m=90.0, bed_m=98.0, datum="NAVD88", date="", area_km2=14)

    kept, notes = select_gauges([good, absurd, below])
    assert [g.site for g in kept] == ["good"]
    assert len(notes) == 2
    assert any("absurd" in n for n in notes) and any("below" in n for n in notes)


def test_the_minority_vertical_datum_is_dropped_not_mixed() -> None:
    """NAVD88 and NGVD29 differ by the size of the error being chased."""
    from floodline.hydraulics.observed_stage import select_gauges

    gauges = [
        GaugeStage("a", 0, 103.0, 100.0, "NAVD88", "", 1),
        GaugeStage("b", 1, 102.0, 100.0, "NAVD88", "", 2),
        GaugeStage("c", 2, 101.0, 100.0, "NGVD29", "", 3),
    ]
    kept, notes = select_gauges(gauges)
    assert [g.site for g in kept] == ["a", "b"]
    assert any("NAVD88" in n and "c" in n for n in notes)

    # A single consistent datum is left alone and reported on.
    kept2, notes2 = select_gauges(gauges[:2])
    assert len(kept2) == 2 and not notes2
