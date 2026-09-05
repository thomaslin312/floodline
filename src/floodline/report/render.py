"""A standing report for one watershed: the numbers, and what is wrong with them.

The map is better than this for exploring. What a static page is for is being sent to
someone, kept alongside a decision, and read in six months by a person who was not in
the conversation. So this leads with limits, states provenance next to every figure,
and never prints a number whose source it cannot name.

Self-contained HTML with the images inlined as data URIs. No network, no assets
directory, nothing to break when it is moved.
"""

from __future__ import annotations

import html
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np

from floodline.assess import Assessment
from floodline.report.bundle import encode_png, to_data_uri
from floodline.report.figures import block_reduce

__all__ = ["ReportInputs", "render_report"]

# Same ramp as the map, so a reader moving between them is reading one scale.
_DEPTH_RAMP = np.array(
    [[207, 230, 242], [127, 184, 220], [61, 143, 192], [26, 96, 152], [11, 63, 115], [6, 34, 66]],
    dtype=np.float64,
)


@dataclass(frozen=True, slots=True)
class ReportInputs:
    """What the report is built from."""

    assessment: Assessment
    title: str = ""
    max_width_px: int = 900


def _depth_image(depth: np.ndarray, max_width: int) -> str:
    """Render the depth raster as a PNG data URI, on the map's square-root ramp."""
    factor = max(1, int(np.ceil(depth.shape[1] / max_width)))
    reduced = block_reduce(np.where(np.isfinite(depth), depth, np.nan), factor, how="mean")
    wet = np.isfinite(reduced) & (reduced > 0)

    # Square root, matching the map: an urban flood is mostly 0.5-2 m, and a linear
    # ramp puts almost all of it in the palest stop.
    t = np.zeros(reduced.shape, dtype=np.float64)
    t[wet] = np.sqrt(np.clip(reduced[wet] / 8.0, 0.0, 1.0))
    position = t * (len(_DEPTH_RAMP) - 1)
    low = np.clip(np.floor(position).astype(int), 0, len(_DEPTH_RAMP) - 1)
    high = np.clip(low + 1, 0, len(_DEPTH_RAMP) - 1)
    frac = (position - low)[..., None]
    rgb = _DEPTH_RAMP[low] * (1 - frac) + _DEPTH_RAMP[high] * frac

    rgba = np.zeros((*reduced.shape, 4), dtype=np.uint8)
    rgba[..., :3] = np.clip(rgb, 0, 255).astype(np.uint8)
    rgba[..., 3] = np.where(wet, 235, 0).astype(np.uint8)
    return to_data_uri(encode_png(rgba, "RGBA"))


def _ladder_chart(result: Assessment, currency: str) -> str:
    """Draw damage against discharge as an inline SVG.

    The single most informative thing the model produces, and the hardest to convey in
    a table: how fast the cost climbs with flow. Drawn as one path with the observed
    discharge marked, because the reader's first question about any figure here is what
    happens if the flood is worse.
    """
    ladder = result.ladder
    if ladder is None or not ladder.damage or max(ladder.damage) <= 0:
        return ""

    width, height, pad = 620, 210, 44
    xs = np.asarray(ladder.discharge_cms, dtype=np.float64)
    ys = np.asarray(ladder.damage, dtype=np.float64)
    top = float(ys.max())
    right = float(xs.max()) or 1.0

    def place(x: float, y: float) -> tuple[float, float]:
        return (
            pad + (x / right) * (width - pad - 14),
            height - pad - (y / top) * (height - pad - 18),
        )

    plotted = (place(x, y) for x, y in zip(xs, ys, strict=True))
    points = " ".join(f"{px:.1f},{py:.1f}" for px, py in plotted)
    base_x, base_y = place(result.discharge_cms, float(np.interp(result.discharge_cms, xs, ys)))
    floor_y = height - pad

    ticks = []
    for frac in (0.0, 0.5, 1.0):
        value = top * frac
        _, ty = place(0.0, value)
        ticks.append(
            f'<line x1="{pad}" y1="{ty:.1f}" x2="{width - 14}" y2="{ty:.1f}" '
            f'class="grid"/><text x="{pad - 6}" y="{ty + 4:.1f}" class="ylab">'
            f"{_money(value, currency)}</text>"
        )
    for frac in (0.0, 0.5, 1.0):
        value = right * frac
        tx, _ = place(value, 0.0)
        ticks.append(
            f'<text x="{tx:.1f}" y="{height - pad + 16:.1f}" class="xlab">{value:,.0f}</text>'
        )

    return f"""<figure><svg viewBox="0 0 {width} {height}" role="img"
 aria-label="Modelled damage against discharge">
<style>
 .grid{{stroke:var(--line);stroke-width:1}}
 .curve{{fill:none;stroke:var(--accent);stroke-width:2.4;stroke-linejoin:round}}
 .fill{{fill:var(--accent);opacity:.10}}
 .ylab{{fill:var(--ink-3);font:11px ui-monospace,Menlo,monospace;text-anchor:end}}
 .xlab{{fill:var(--ink-3);font:11px ui-monospace,Menlo,monospace;text-anchor:middle}}
 .mark{{stroke:var(--ink-2);stroke-width:1;stroke-dasharray:3 3}}
 .dot{{fill:var(--accent)}}
 .note{{fill:var(--ink-2);font:11.5px system-ui,sans-serif}}
</style>
{"".join(ticks)}
<polygon class="fill" points="{pad},{floor_y} {points} {width - 14},{floor_y}"/>
<polyline class="curve" points="{points}"/>
<line class="mark" x1="{base_x:.1f}" y1="{base_y:.1f}" x2="{base_x:.1f}" y2="{floor_y}"/>
<circle class="dot" cx="{base_x:.1f}" cy="{base_y:.1f}" r="4"/>
<text class="note" x="{base_x + 8:.1f}" y="{base_y - 8:.1f}">observed</text>
<text class="xlab" x="{width / 2:.0f}" y="{height - 6:.0f}">discharge at outlet, m3/s</text>
</svg><figcaption>Damage against discharge, priced building by building at every rung.
 The dashed line is the discharge this report is about; everything to its right is a
 larger flood than the one observed.</figcaption></figure>"""


def _row(label: str, value: str, note: str = "") -> str:
    """One line of the figures table."""
    suffix = f'<span class="note">{html.escape(note)}</span>' if note else ""
    return f"<tr><th>{html.escape(label)}</th><td>{html.escape(value)}{suffix}</td></tr>"


def _money(value: float | None, unit: str) -> str:
    """Format a currency figure at a readable magnitude."""
    if value is None:
        return "—"
    if abs(value) >= 1e9:
        return f"{unit} {value / 1e9:,.2f} bn"
    if abs(value) >= 1e6:
        return f"{unit} {value / 1e6:,.0f} M"
    return f"{unit} {value:,.0f}"


def _limits(result: Assessment, currency: str) -> list[str]:
    """Return the caveats that apply to this run, in the order they bite."""
    out = [
        "HAND assumes the water surface parallels the drainage line. There is no "
        "backwater, no levee, no culvert and no flow routed across a catchment divide.",
    ]
    if not result.gauged:
        out.append(
            "The discharge was supplied, not observed at a gauge. Every figure below "
            "describes a scenario."
        )
    if result.interval is not None and not result.interval.curves_verified:
        out.append(
            "The depth-damage curves are unverified approximations. Building counts and "
            f"loss ratios stand; the {currency} totals do not."
        )
    if result.inventory == "nsi":
        out.append(
            "Vehicles are not priced. NSI carries a vehicle value for every structure "
            "and the USACE curve library has no vehicle function, so that exposure is "
            "collected and left out rather than guessed at."
        )
        out.append(
            "Structure values are NSI's modelled replacement costs, derived from "
            "occupancy type, area and regional construction costs. They are sound "
            "summed over tens of thousands of buildings and not sound for any one."
        )
    if result.interval is not None and result.interval.count_interval_conditional:
        out.append(
            "The building-count interval is conditional on this extent: without a "
            "signed depth margin, structures the model leaves dry stay dry in every draw."
        )
    out.append(
        "The uncertainty band covers gauge stage, DEM error, curve choice and cost. It "
        "does not cover storey counts, floor area, class assignment, inventory "
        "completeness or HAND's structural assumption, so it is a lower bound."
    )
    if result.history is not None and result.history.fit_saturated:
        out.append(
            "The flood-frequency fit could not place this discharge. A century of "
            "catchment change breaks the stationarity a log-Pearson III assumes."
        )
    if result.marks is None:
        out.append(
            "Nothing here is validated against an observation. No surveyed high-water "
            "marks fall inside this unit, and CSI needs an observed extent polygon "
            "that this project does not have."
        )
    for gap in result.gaps:
        out.append(f"Gap: {gap}")
    return out


_CSS = """
:root{--ink:#10222a;--ink-2:#42606c;--ink-3:#7d949c;--line:#dfe6e9;--accent:#0a7d8c;
--warn:#a3722a;--bg:#ffffff}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);
 font:15px/1.6 "Public Sans",system-ui,-apple-system,sans-serif;
 -webkit-font-smoothing:antialiased}
main{max-width:940px;margin:0 auto;padding:48px 28px 96px}
h1{font-size:30px;line-height:1.15;letter-spacing:-.02em;margin:0 0 6px}
h2{font-size:13px;letter-spacing:.14em;text-transform:uppercase;color:var(--ink-3);
 margin:44px 0 14px;font-weight:600}
.meta{font:12px/1.7 ui-monospace,Menlo,monospace;color:var(--ink-3)}
.lede{font-size:17px;color:var(--ink-2);margin:18px 0 0;max-width:66ch}
table{border-collapse:collapse;width:100%;margin:0}
th{text-align:left;font-weight:500;color:var(--ink-2);padding:9px 12px 9px 0;
 border-bottom:1px solid var(--line);width:42%;vertical-align:top}
td{padding:9px 0;border-bottom:1px solid var(--line);
 font:14px ui-monospace,Menlo,monospace;font-variant-numeric:tabular-nums}
.note{display:block;font-family:"Public Sans",system-ui,sans-serif;font-size:12px;
 color:var(--ink-3);margin-top:2px}
ul.limits{margin:0;padding:0;list-style:none;max-width:72ch}
ul.limits li{padding:11px 0 11px 16px;border-left:2px solid var(--line);
 margin-bottom:9px;color:var(--ink-2);font-size:14px}
ul.limits li:first-child{border-left-color:var(--warn);color:var(--ink)}
figure{margin:0}
img{display:block;width:100%;height:auto;background:#eef1f2;border:1px solid var(--line);
 border-radius:8px}
figcaption{font-size:12.5px;color:var(--ink-3);margin-top:8px}
footer{margin-top:56px;padding-top:18px;border-top:1px solid var(--line);
 font-size:12px;color:var(--ink-3)}
@media (prefers-color-scheme:dark){
 :root{--ink:#e7f1f3;--ink-2:#a7bcc2;--ink-3:#75898f;--line:#22343b;--accent:#3ecad8;
  --warn:#ddaa5c;--bg:#0b171b}
 img{background:#12232a}
}
"""


def render_report(inputs: ReportInputs, destination: Path) -> Path:
    """Write a self-contained HTML report for one assessment.

    Returns
    -------
    The path written.
    """
    result = inputs.assessment
    unit = result.unit
    currency = "USD"
    title = inputs.title or f"{unit.name} — modelled flood"

    figures: list[str] = [
        _row("Watershed", f"{unit.name} (HUC-{len(unit.huc)} {unit.huc})"),
        _row("Area", f"{unit.area_km2:,.0f} km2"),
        _row("Resolution", f"{result.resolution_m:g} m"),
        _row(
            "Discharge",
            f"{result.discharge_cms:,.0f} m3/s",
            "observed at a gauge" if result.gauged else "supplied, not observed",
        ),
    ]
    if result.history is not None:
        figures.append(_row("In the record", result.history.summary()))
    figures += [
        _row("Flooded area", f"{result.flooded_km2:,.1f} km2"),
        _row("Deepest cell", f"{result.max_depth_m:,.1f} m"),
    ]

    if result.marks is not None:
        marks = result.marks
        figures.append(
            _row(
                "Against surveyed marks",
                f"RMSE {marks.rmse_m:.2f} m over {marks.n} marks",
                f"{marks.n_wet} wet, bias {marks.mean_bias_m:+.2f} m, "
                f"median |error| {marks.median_absolute_m:.2f} m",
            )
        )
    elif result.n_marks_available == 0:
        figures.append(
            _row("Against surveyed marks", "none available", "no USGS marks in this unit")
        )

    if result.buildings is not None:
        exposed = result.buildings
        note = f"{result.inventory.upper()} inventory"
        figures.append(
            _row(
                "Structures inundated",
                f"{exposed.n_inundated:,} of {len(exposed.buildings):,}",
                note,
            )
        )
        if result.interval is not None:
            low, high = result.interval.count_interval
            lo_q, hi_q = result.interval.quantiles
            figures.append(
                _row(
                    "Count interval",
                    f"{low:,} to {high:,}",
                    f"{int(lo_q * 100)}-{int(hi_q * 100)}%",
                )
            )
    if result.night_population is not None:
        figures.append(
            _row(
                "Residents affected",
                f"{result.night_population:,.0f} overnight",
                f"{result.day_population:,.0f} present by day",
            )
        )
    elif result.people is not None:
        figures.append(
            _row(
                "People affected",
                f"{result.people.people_affected:,.0f}",
                "one gridded product; these disagree by tens of percent",
            )
        )

    if result.damage is not None and result.interval is not None:
        damage = result.damage
        figures += [
            _row(
                "Direct damage",
                _money(damage.total, currency),
                f"{damage.family.value} curves"
                + ("" if result.interval.curves_verified else " — UNVERIFIED constants"),
            ),
            _row(
                "Damage interval",
                f"{_money(result.interval.lower, currency)} to "
                f"{_money(result.interval.upper, currency)}",
            ),
            _row(
                "Loss ratio",
                f"{damage.loss_ratio:.1%}",
                f"of {_money(damage.exposed_value_total, currency)} exposed",
            ),
        ]
        if result.contents_damage:
            figures.append(
                _row(
                    "Structure / contents",
                    f"{_money(damage.total - result.contents_damage, currency)} / "
                    f"{_money(result.contents_damage, currency)}",
                )
            )

    limits = "".join(f"<li>{html.escape(item)}</li>" for item in _limits(result, currency))
    chart = _ladder_chart(result, currency)
    ladder_block = f"<h2>Damage against discharge</h2>{chart}" if chart else ""
    depth_uri = _depth_image(result.depth.data, inputs.max_width_px)
    stamp = datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC")
    timings = ", ".join(f"{k} {v:.1f}s" for k, v in result.seconds.items())

    page = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{html.escape(title)}</title>
<style>{_CSS}</style></head>
<body><main>
<h1>{html.escape(title)}</h1>
<p class="meta">floodline · {html.escape(stamp)} · analysis CRS
 {html.escape(str(unit.huc))} at {result.resolution_m:g} m</p>
<p class="lede">A screening-grade estimate from open data: terrain from USGS 3DEP,
 discharge from a USGS gauge, structures and their values from the USACE National
 Structure Inventory, damage from published USACE depth-damage curves. Read the limits
 before the figures.</p>

<h2>What this cannot tell you</h2>
<ul class="limits">{limits}</ul>

<h2>Figures</h2>
<table>{"".join(figures)}</table>

{ladder_block}<h2>Modelled depth</h2>
<figure><img src="{depth_uri}" alt="Modelled flood depth over {html.escape(unit.name)}">
<figcaption>Depth on a square-root scale, 0 to 8 m+, the same ramp the interactive map
 uses. Blank is dry or outside the watershed.</figcaption></figure>

<footer>Generated by floodline. Stages: {html.escape(timings)}.
 Every number here was produced by running the model; none was transcribed from
 another source.</footer>
</main></body></html>
"""
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(page, encoding="utf-8")
    return destination


def report_payload(result: Assessment) -> dict[str, Any]:
    """Return the report's figures as plain data, for tests and for JSON output."""
    return {
        "huc": result.unit.huc,
        "name": result.unit.name,
        "resolution_m": result.resolution_m,
        "discharge_cms": result.discharge_cms,
        "gauged": result.gauged,
        "flooded_km2": result.flooded_km2,
        "max_depth_m": result.max_depth_m,
        "inventory": result.inventory,
        "inundated": result.buildings.n_inundated if result.buildings else None,
        "damage": result.damage.total if result.damage else None,
        "curves_verified": result.interval.curves_verified if result.interval else None,
        "gaps": list(result.gaps),
    }
