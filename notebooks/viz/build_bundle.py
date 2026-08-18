"""Precompute every watershed in the AOI into one browser-sized bundle.

Runs the whole chain per hydrologic unit, then packs HAND, reach ids and a stage
lookup table for each. Writes outputs/bundle.json, which the interactive page inlines.
"""

import io as _io
import json
from pathlib import Path

import numpy as np
from matplotlib import colormaps
from matplotlib import image as mpimg
from matplotlib.colors import Normalize
from pyproj import Transformer
from shapely.geometry import Point

from floodline.config import Config
from floodline.hydraulics.rating import (
    build_rating_curves,
    discharge_by_area_ratio,
    reach_catchments,
)
from floodline.io.ingest import ingest_dem, load_watersheds
from floodline.report.bundle import (
    UnitBundle,
    encode_hand,
    encode_reach_ids,
    encode_stage_table,
    to_data_uri,
)
from floodline.report.figures import block_reduce

CFS, FT, TARGET = 0.0283168, 0.3048, 800
MULTS = np.linspace(0.0, 3.0, 33)
# Whiteoak Bayou at Houston: the only gauge in the AOI publishing discharge through
# the event. Every ungauged unit is scaled from it by drainage-area ratio, which is
# flagged per unit so the page can say which numbers are inferred.
GAUGE = {
    "site": "08074500",
    "name": "Whiteoak Bayou at Houston",
    "lon": -95.3971612,
    "lat": 29.77522777,
    "peak_cfs": 50600.0,
}

cfg = Config.model_validate(
    {
        "terrain": {"stream_threshold_cells": 5000},
        "hydraulics": {
            "gauge_reading_unit": "ft",
            "gauge_datum_offset_m": 0.0,
            "min_depth_m": 0.05,
        },
    }
)
tiles = sorted(Path("data/raw/dem/10m").glob("*.tif"))
sheds = load_watersheds(Path("data/raw/watersheds/huc10.geojson"), config=cfg)
marks_all = json.loads(Path("data/raw/validation/high_water_marks.json").read_text())
tf = Transformer.from_crs("EPSG:4326", "EPSG:6587", always_xy=True)
gx, gy = tf.transform(GAUGE["lon"], GAUGE["lat"])
gauge_point = Point(gx, gy)
gauge_q = GAUGE["peak_cfs"] * CFS


def basemap_png(elev, f):
    r = block_reduce(elev, f, how="mean")
    finite = r[np.isfinite(r)]
    lo, hi = (float(finite.min()), float(finite.max())) if finite.size else (0.0, 1.0)
    rgba = colormaps["terrain"](Normalize(lo, max(hi, lo + 1))(np.nan_to_num(r, nan=lo)))
    rgba[..., 3] = np.isfinite(r).astype(float)
    b = _io.BytesIO()
    mpimg.imsave(b, rgba, format="png")
    return b.getvalue()


units, skipped = [], []
for shed in sheds:
    try:
        dem = ingest_dem(tiles, resolution_m=10.0, config=cfg, watershed=shed)
        chain = __import__("floodline.terrain.route", fromlist=["route_terrain"]).route_terrain(
            dem.data, config=cfg, nodata=dem.nodata, cellsize=dem.cellsize
        )
        ids, links = __import__("floodline.terrain.streams", fromlist=["link_raster"]).link_raster(
            chain.streams, chain.flowdir
        )
        reach_of = reach_catchments(chain.hand.drainage_index, ids)
        curves = build_rating_curves(
            chain.hand.hand, chain.filled, links, reach_of, config=cfg, cellsize=dem.cellsize
        )
    except Exception as exc:
        skipped.append((shed.huc, shed.name, str(exc)[:90]))
        continue

    # Discharge: use the gauge's own cell when it falls in this unit, otherwise
    # scale from it by the ratio of outlet contributing areas.
    gauged = shed.geometry.contains(gauge_point)
    if gauged:
        cc, rr = dem.transform.__invert__() * (gx, gy)
        r0, c0 = int(rr), int(cc)
        cand = [
            (dr * dr + dc * dc, r0 + dr, c0 + dc)
            for dr in range(-40, 41)
            for dc in range(-40, 41)
            if 0 <= r0 + dr < ids.shape[0]
            and 0 <= c0 + dc < ids.shape[1]
            and chain.streams[r0 + dr, c0 + dc]
        ]
        if not cand:
            gauged = False
        else:
            _, gr, gc = min(cand)
            ref_area = float(chain.accumulation.accumulation[gr, gc])
    if not gauged:
        # 215 km2 is the gauge's own contributing area, measured in its own unit.
        ref_area = 215e6 / dem.cell_area_m2
        gr = gc = None
    flows = discharge_by_area_ratio(
        gauge_q, ref_area, links, chain.accumulation.accumulation, config=cfg
    )

    f = max(1, int(np.ceil(dem.data.shape[1] / TARGET)))
    hand_r = block_reduce(chain.hand.hand, f, how="mean")
    reach_r = block_reduce(np.where(reach_of >= 0, reach_of, np.nan).astype(float), f, how="max")
    reach_i = np.where(np.isfinite(reach_r), np.nan_to_num(reach_r), -1).astype(np.int64)
    H, W = hand_r.shape

    ms = []
    for m in marks_all:
        if m.get("elev_ft") is None or m.get("hwm_quality_id") not in (1, 2):
            continue
        x, y = tf.transform(m["longitude_dd"], m["latitude_dd"])
        if not shed.geometry.contains(Point(x, y)):
            continue
        cc, rr = dem.transform.__invert__() * (x, y)
        rr, cc = int(rr), int(cc)
        if not (0 <= rr < chain.hand.hand.shape[0] and 0 <= cc < chain.hand.hand.shape[1]):
            continue
        if not np.isfinite(chain.hand.hand[rr, cc]):
            continue
        ms.append(
            {
                "px": round(cc / f, 1),
                "py": round(rr / f, 1),
                "surveyed_m": round(m["elev_ft"] * FT, 2),
                "ground_m": round(float(chain.filled[rr, cc]), 2),
                "hand_m": round(float(chain.hand.hand[rr, cc]), 2),
                "quality": m["hwm_quality_id"],
            }
        )

    b = UnitBundle(
        huc=shed.huc,
        name=shed.name,
        area_km2=round(shed.area_km2),
        width=W,
        height=H,
        reduction=f,
        bounds=shed.bounds,
        n_reaches=len(links),
        base_discharge_cms=round(gauge_q, 1),
        multipliers=[round(float(x), 3) for x in MULTS],
        gauged=bool(gauged),
        hand=to_data_uri(encode_hand(hand_r)),
        reach=to_data_uri(encode_reach_ids(reach_i)),
        stage_table=to_data_uri(encode_stage_table(len(links), curves, flows, MULTS)),
        basemap=to_data_uri(basemap_png(chain.filled, f)),
        gauge=(
            {
                "px": round(gc / f, 1),
                "py": round(gr / f, 1),
                **{k: GAUGE[k] for k in ("site", "name")},
                "discharge_cms": round(gauge_q),
            }
            if gauged
            else None
        ),
        marks=ms,
        stats={
            "cells": int(dem.data.size),
            "curves": len(curves),
            "flats": int(chain.flat_cells_before),
            "streams": int(chain.streams.sum()),
            "valid_km2": round(
                float(np.isfinite(chain.hand.hand).sum()) * dem.cell_area_m2 / 1e6, 1
            ),
        },
    )
    units.append(b)
    print(
        f"  {shed.huc}  {shed.name[:30]:32s} {W}x{H}  {len(links):>4} reaches  "
        f"{len(ms):>2} marks  {'gauged' if gauged else 'inferred'}"
    )

out = Path("outputs/bundle.json")
out.parent.mkdir(parents=True, exist_ok=True)
out.write_text(
    json.dumps(
        {
            "units": [u.__dict__ for u in units],
            "gauge": GAUGE,
            "multipliers": [round(float(x), 3) for x in MULTS],
        },
        default=float,
    )
)
print(f"\n{len(units)} units -> {out} ({out.stat().st_size / 1e6:.1f} MB)")
for huc, name, err in skipped:
    print(f"  SKIPPED {huc} {name}: {err}")
