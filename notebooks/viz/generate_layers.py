import json
import subprocess
from pathlib import Path

import numpy as np
from pyproj import Transformer
from shapely.geometry import Point

from floodline.config import Config
from floodline.hydraulics.inundate import inundate
from floodline.hydraulics.rating import (
    build_rating_curves,
    discharge_by_area_ratio,
    reach_catchments,
)
from floodline.hydraulics.stage import stage_field_from_discharge
from floodline.io.ingest import ingest_dem, load_watersheds
from floodline.report.figures import render_layers
from floodline.terrain.route import route_terrain
from floodline.terrain.streams import link_raster

CFS = 0.0283168
FT = 0.3048
OUT = Path("outputs/viz")
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
shed = next(
    w
    for w in load_watersheds(Path("data/raw/watersheds/huc10.geojson"), config=cfg)
    if w.huc == "1204010403"
)
dem = ingest_dem(tiles, resolution_m=10.0, config=cfg, watershed=shed)
chain = route_terrain(dem.data, config=cfg, nodata=dem.nodata, cellsize=dem.cellsize)
ids, links = link_raster(chain.streams, chain.flowdir)
reach_of = reach_catchments(chain.hand.drainage_index, ids)
curves = build_rating_curves(
    chain.hand.hand, chain.filled, links, reach_of, config=cfg, cellsize=dem.cellsize
)

tf = Transformer.from_crs("EPSG:4326", "EPSG:6587", always_xy=True)
gx, gy = tf.transform(-95.3971612, 29.77522777)
cc, rr = dem.transform.__invert__() * (gx, gy)
r0, c0 = int(rr), int(cc)
_, gr, gc = min(
    (dr * dr + dc * dc, r0 + dr, c0 + dc)
    for dr in range(-40, 41)
    for dc in range(-40, 41)
    if 0 <= r0 + dr < ids.shape[0]
    and 0 <= c0 + dc < ids.shape[1]
    and chain.streams[r0 + dr, c0 + dc]
)
gauge_area = float(chain.accumulation.accumulation[gr, gc])
q = 50600 * CFS
flows = discharge_by_area_ratio(q, gauge_area, links, chain.accumulation.accumulation, config=cfg)
stages = stage_field_from_discharge(reach_of, curves, flows)
rf = inundate(
    chain.hand.hand,
    stages.stage_m,
    streams=chain.streams,
    config=cfg,
    cell_area_m2=dem.cell_area_m2,
)
cf = inundate(
    chain.hand.hand, 11.89, streams=chain.streams, config=cfg, cell_area_m2=dem.cell_area_m2
)

ls = render_layers(
    OUT,
    transform=dem.transform,
    crs="EPSG:6587",
    continuous={
        "elevation": (chain.filled, "Elevation (conditioned)", "m NAVD88", "terrain"),
        "hand": (chain.hand.hand, "Height above nearest drainage", "m", "viridis"),
        "depth": (
            np.where(rf.wet, rf.depth, np.nan),
            "Depth - per-reach rating curves",
            "m",
            "Blues",
        ),
        "depth_const": (
            np.where(cf.wet, cf.depth, np.nan),
            "Depth - one constant stage",
            "m",
            "Blues",
        ),
    },
    masks={
        "streams": (chain.streams, "Stream network", "spring"),
        "wet_rating": (rf.wet, "Extent - rating curves", "cool"),
        "wet_const": (cf.wet, "Extent - constant stage", "autumn"),
    },
    target_width=1100,
)

marks = json.loads(Path("data/raw/validation/high_water_marks.json").read_text())
pts = []
for m in marks:
    if m.get("elev_ft") is None or m.get("hwm_quality_id") not in (1, 2):
        continue
    x, y = tf.transform(m["longitude_dd"], m["latitude_dd"])
    if not shed.geometry.contains(Point(x, y)):
        continue
    cc2, rr2 = dem.transform.__invert__() * (x, y)
    rr2, cc2 = int(rr2), int(cc2)
    if not (0 <= rr2 < chain.hand.hand.shape[0] and 0 <= cc2 < chain.hand.hand.shape[1]):
        continue
    if not np.isfinite(chain.hand.hand[rr2, cc2]):
        continue
    surveyed = m["elev_ft"] * FT

    def resid(f, rr2=rr2, cc2=cc2, surveyed=surveyed):
        return (
            float(chain.filled[rr2, cc2] + f.depth[rr2, cc2] - surveyed)
            if f.wet[rr2, cc2]
            else None
        )

    a, b = resid(rf), resid(cf)
    pts.append(
        {
            "px": round(cc2 / ls.reduction, 1),
            "py": round(rr2 / ls.reduction, 1),
            "surveyed_m": round(surveyed, 2),
            "ground_m": round(float(chain.filled[rr2, cc2]), 2),
            "hand_m": round(float(chain.hand.hand[rr2, cc2]), 2),
            "quality": m.get("hwm_quality_id"),
            "rating": None if a is None else round(a, 2),
            "constant": None if b is None else round(b, 2),
        }
    )
ls.points["high_water_marks"] = pts
ls.points["gauge"] = {
    "px": round(gc / ls.reduction, 1),
    "py": round(gr / ls.reduction, 1),
    "site": "08074500",
    "name": "Whiteoak Bayou at Houston",
    "discharge_cms": round(q),
    "area_km2": round(gauge_area * dem.cell_area_m2 / 1e6),
}

# The main stem, not the headwaters: reaches carrying the most water. Sorting by
# catchment_cells instead picks headwater reaches, which own big hillslopes but
# little discharge.
big = sorted(
    (c for c in curves.values() if c.geometry.link_id in flows),
    key=lambda c: -flows[c.geometry.link_id],
)[:4]
ls.series["rating_curves"] = [
    {
        "link": c.geometry.link_id,
        "length_m": round(c.geometry.length_m),
        "slope": float(f"{c.geometry.slope:.2e}"),
        "catchment_km2": round(c.geometry.catchment_cells * dem.cell_area_m2 / 1e6, 1),
        "stage": [round(float(v), 2) for v in c.stage_m],
        "q": [round(float(v), 1) for v in c.discharge_cms],
        "assigned_q": round(flows.get(c.geometry.link_id, 0.0), 1),
        "assigned_stage": round(stages.by_reach.get(c.geometry.link_id, 0.0), 2),
    }
    for c in big
]

sv = np.array(list(stages.by_reach.values()))
ls.notes.update(
    {
        "huc": shed.huc,
        "name": shed.name,
        "area_km2": round(shed.area_km2),
        "resolution_m": 10,
        "cells": int(dem.data.size),
        "reaches": len(links),
        "curves": len(curves),
        "stage_median": round(float(np.median(sv)), 2),
        "stage_max": round(float(sv.max()), 2),
        "rating_area_km2": round(rf.area_m2 / 1e6, 1),
        "const_area_km2": round(cf.area_m2 / 1e6, 1),
        "unit_area_km2": round(
            float(np.isfinite(chain.hand.hand).sum()) * dem.cell_area_m2 / 1e6, 1
        ),
        "flats_resolved": int(chain.flat_cells_before),
        "gauge_discharge_cms": round(q),
        "peak_time": "2017-08-27T13:30",
    }
)
ls.to_json(OUT / "layers.json")
print(f"{ls.width}x{ls.height} px, reduction {ls.reduction}x, {len(pts)} marks")
for L in ls.layers:
    print(
        f"  {L.file:16s} {L.label[:42]:44s}{'' if L.vmin is None else f'{L.vmin:.1f}..{L.vmax:.1f}'}"
    )
print(subprocess.run(["du", "-sh", "outputs/viz"], capture_output=True, text=True).stdout.strip())
