"""Structures with values attached: the USACE National Structure Inventory.

A depth-damage curve returns a *fraction* of a building's replacement value, so a
damage figure is only as good as the valuation it multiplies. Footprint area times a
flat rate per class is not a valuation. Measured against NSI over one 2 x 2 km Houston
box, that proxy overstated the total by 1.4x and landed within 30% for only 64% of
buildings, ranging from 0.66x to 2.08x per structure.

NSI is the fix. Roughly 120 million US structures, free and keyless, each carrying:

* `val_struct`, `val_cont`, `val_vehic` - replacement value, separately for the
  building, its contents and vehicles. Contents are worth about as much again as the
  structure, so a model without them is not conservative, it is half a model.
* `occtype` - a HAZUS occupancy code, the same vocabulary that keys the USACE curve
  library. Inventory and curve join natively; that pairing is the reason to prefer
  these two sources over any other combination.
* `num_story` and `found_ht` - real storey counts and real foundation heights, in
  place of a height/3 guess and one global freeboard constant.
* `pop2amu65` / `pop2amo65` / `pop2pmu65` / `pop2pmo65` - night and day population,
  split under and over 65, per structure. Better than any gridded product for the US,
  and it makes "people affected" a structure-level number rather than a cell-level one.

**These values are modelled, not appraised.** NSI derives them from occupancy type,
footprint area and regional construction costs. That is a nationally consistent
estimate, not a valuation of a particular house, and the distinction matters at the
scale a reader might quote: sound summed over tens of thousands of buildings, not
sound for any single one.

**Geometry is a point, not a footprint.** NSI places one point per structure and
carries `ftprntsqft` alongside. `structure_footprints` squares that area around the
point so the depth statistic still has an area to work over; it is an approximation to
the real outline, and `p90` over a square of the right size is closer to the truth than
sampling the single cell the centroid happens to land in.

**The host is a `.mil` domain**, which some networks and CI runners will not resolve.
Failure is reported as a gap rather than retried into a hang.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import geopandas as gpd
import httpx
import numpy as np
import shapely
from shapely.geometry.base import BaseGeometry

__all__ = ["NSI_URL", "NsiFetch", "fetch_nsi_structures", "structure_footprints"]

NSI_URL = "https://nsi.sec.usace.army.mil/nsiapi/structures"
SQFT_TO_M2 = 0.092903
FEET_TO_M = 0.3048

# Fields kept from the ~40 NSI publishes. The rest are FIRM zones, damage categories
# and identifiers that this model has no use for.
_FIELDS = (
    "fd_id",
    "occtype",
    "st_damcat",
    "val_struct",
    "val_cont",
    "val_vehic",
    "sqft",
    "ftprntsqft",
    "num_story",
    "found_ht",
    "found_type",
    "ground_elv",
    "med_yr_blt",
    "pop2amu65",
    "pop2amo65",
    "pop2pmu65",
    "pop2pmo65",
)


@dataclass(frozen=True, slots=True)
class NsiFetch:
    """Structures inside one polygon, and what it cost to get them."""

    structures: gpd.GeoDataFrame
    """WGS84 points with values in USD, areas in m2, heights in m."""

    seconds: float
    from_cache: bool

    @property
    def night_population(self) -> float:
        """Residents present overnight, the population most flood models mean."""
        frame = self.structures
        return float(frame["pop_night"].sum()) if len(frame) else 0.0

    @property
    def day_population(self) -> float:
        """People present during the day, which a business district changes entirely."""
        frame = self.structures
        return float(frame["pop_day"].sum()) if len(frame) else 0.0


def fetch_nsi_structures(
    geometry: BaseGeometry,
    *,
    cache_dir: Path = Path("data/cache"),
    cache_key: str = "",
    client: httpx.Client | None = None,
    timeout_s: float = 300.0,
    use_cache: bool = True,
) -> NsiFetch:
    """Fetch every NSI structure inside `geometry`.

    Parameters
    ----------
    geometry
        A WGS84 polygon. The API takes GeoJSON by POST and returns a FeatureCollection;
        a bounding-box GET exists but returns nothing for the boxes tried here.
    cache_key
        Distinguishes cached results. Pass the HUC; without one the geometry's bounds
        are used, which is stable but long.

    Returns
    -------
    NsiFetch
    """
    key = cache_key or "_".join(f"{v:.4f}" for v in geometry.bounds)
    cached = cache_dir / f"nsi-{key}.parquet"
    if use_cache and cached.exists():
        return NsiFetch(structures=gpd.read_parquet(cached), seconds=0.0, from_cache=True)

    started = time.perf_counter()
    payload = {
        "type": "Feature",
        "properties": {},
        "geometry": json.loads(shapely.to_geojson(geometry)),
    }
    owned = client is None
    active = client or httpx.Client(
        timeout=httpx.Timeout(30.0, read=timeout_s), follow_redirects=True
    )
    try:
        response = active.post(NSI_URL, json=payload, params={"fmt": "fc"})
        response.raise_for_status()
        collection = response.json()
    finally:
        if owned:
            active.close()

    features = collection.get("features", [])
    frame = _to_frame(features)
    if use_cache:
        cached.parent.mkdir(parents=True, exist_ok=True)
        frame.to_parquet(cached)
    return NsiFetch(structures=frame, seconds=time.perf_counter() - started, from_cache=False)


def _to_frame(features: list[dict[str, Any]]) -> gpd.GeoDataFrame:
    """Turn an NSI FeatureCollection into a typed frame in SI units."""
    columns: dict[str, list[Any]] = {name: [] for name in _FIELDS}
    points = []
    for feature in features:
        properties = feature.get("properties", {})
        for name in _FIELDS:
            columns[name].append(properties.get(name))
        lon, lat = feature["geometry"]["coordinates"]
        points.append(shapely.Point(lon, lat))

    frame = gpd.GeoDataFrame(columns, geometry=points, crs="EPSG:4326")
    if not len(frame):
        # Give an empty result the same columns, so callers need no special case.
        for name in ("footprint_m2", "floor_area_m2", "found_ht_m", "pop_night", "pop_day"):
            frame[name] = np.zeros(0, dtype=np.float64)
        return frame

    numeric = (
        "val_struct",
        "val_cont",
        "val_vehic",
        "sqft",
        "ftprntsqft",
        "num_story",
        "found_ht",
        "ground_elv",
        "pop2amu65",
        "pop2amo65",
        "pop2pmu65",
        "pop2pmo65",
    )
    for name in numeric:
        frame[name] = frame[name].astype("float64").fillna(0.0)

    frame["footprint_m2"] = frame["ftprntsqft"] * SQFT_TO_M2
    frame["floor_area_m2"] = frame["sqft"] * SQFT_TO_M2
    # Foundation height is the real per-structure freeboard, replacing a global guess.
    frame["found_ht_m"] = frame["found_ht"] * FEET_TO_M
    frame["pop_night"] = frame["pop2amu65"] + frame["pop2amo65"]
    frame["pop_day"] = frame["pop2pmu65"] + frame["pop2pmo65"]
    frame["num_story"] = frame["num_story"].clip(lower=1.0)
    frame["occtype"] = frame["occtype"].fillna("").astype(str)
    return frame


def structure_footprints(
    structures: gpd.GeoDataFrame,
    *,
    min_side_m: float = 4.0,
) -> gpd.GeoDataFrame:
    """Square each structure's footprint area around its point.

    NSI gives a point and an area, not an outline. A square of the right area centred
    on the point lets the depth statistic work over ground rather than a single cell,
    which matters because `p90` over a real footprint was chosen precisely to stop one
    low cell deciding a building's depth. Structures with no recorded footprint area
    get `min_side_m`, so they are sampled rather than dropped.
    """
    if structures.crs is None or structures.crs.is_geographic:
        raise ValueError(
            "structure_footprints squares an area in metres, so it needs a projected "
            f"CRS; got {structures.crs}. Reproject to the analysis CRS first."
        )
    frame = structures.copy()
    side = np.sqrt(np.maximum(frame["footprint_m2"].to_numpy(), 0.0))
    side = np.maximum(side, min_side_m)
    half = side / 2.0
    xs = frame.geometry.x.to_numpy()
    ys = frame.geometry.y.to_numpy()
    frame["geometry"] = shapely.box(xs - half, ys - half, xs + half, ys + half)
    return frame
