"""Building footprints from Overture Maps, read straight out of public S3.

Overture publishes its buildings theme as GeoParquet on an anonymous bucket, spatially
sorted, with a `bbox` struct column on every row. That combination is what makes a
bounding-box read possible without downloading the theme: the filter prunes row groups
on file statistics, so a city-sized window touches a small fraction of the data.

It is not fast. Measured on this machine against release 2026-08-19.0, a 6 x 4 km
window over Houston took 83 s to open the dataset and 210 s to read its 12,151
buildings. The 83 s is S3 listing every file in the theme, which is why the file list
is cached per release, and the read is bounded by how much pruning the statistics
allow. This is a batch stage, not something to put behind a map click.

DuckDB would be the conventional way to do this and is what the spec named. It is not
a dependency here: pyarrow is already in the stack for GeoParquet, does the same
predicate pushdown, and adding a second query engine to save a step this size was not
worth the install. If read time becomes the bottleneck, DuckDB's spatial extension is
the next thing to try, not a rewrite.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path

import geopandas as gpd
import pyarrow.compute as pc
import pyarrow.dataset as ds
import pyarrow.fs as fs
import shapely

from floodline.core.config import CaseConfig, Config
from floodline.settings import settings

__all__ = ["OVERTURE_CLASSES", "BuildingFetch", "fetch_overture_buildings"]

BUCKET = "overturemaps-us-west-2"
REGION = "us-west-2"
_COLUMNS = ["id", "geometry", "height", "num_floors", "subtype", "class"]

# Overture's `subtype` vocabulary mapped onto the four classes the cost table prices.
# Anything unlisted becomes the configured default class rather than being dropped:
# an unclassified building is still a building, and dropping it would understate the
# count, which is the one number here that does not depend on unverified curves.
OVERTURE_CLASSES: dict[str, str] = {
    "residential": "residential",
    "commercial": "commercial",
    "entertainment": "commercial",
    "service": "commercial",
    "education": "commercial",
    "medical": "commercial",
    "industrial": "industrial",
    "agricultural": "industrial",
    "transportation": "industrial",
    "civic": "other",
    "religious": "other",
    "military": "other",
}


@dataclass(frozen=True, slots=True)
class BuildingFetch:
    """Footprints for one bounding box, and what it cost to get them."""

    buildings: gpd.GeoDataFrame
    """WGS84 footprints with `height`, `num_floors` and `building_class`."""

    release: str
    bbox: tuple[float, float, float, float]
    seconds: float
    from_cache: bool
    n_files_scanned: int


def _cache_key(bbox: tuple[float, float, float, float]) -> str:
    """Return a stable filename fragment for a bounding box."""
    return "_".join(f"{v:.4f}" for v in bbox)


def _file_list(s3: fs.S3FileSystem, release: str, cache_dir: Path) -> list[str]:
    """Return the theme's parquet files, listing S3 only when not already cached.

    The listing is the single most expensive part of a cold read and it does not
    change within a release, so it is worth keeping on disk.
    """
    cache = cache_dir / f"overture-{release}-files.json"
    if cache.exists():
        listed: list[str] = json.loads(cache.read_text())
        return listed
    prefix = f"{BUCKET}/release/{release}/theme=buildings/type=building"
    dataset = ds.dataset(prefix, filesystem=s3, format="parquet")
    files = list(dataset.files)
    if not files:
        raise FileNotFoundError(
            f"Overture release {release!r} has no building files at s3://{prefix}. "
            "Check the release name against the bucket listing."
        )
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text(json.dumps(files))
    return files


def fetch_overture_buildings(
    bbox: tuple[float, float, float, float],
    *,
    config: Config | CaseConfig | None = None,
    release: str | None = None,
    cache_dir: Path | None = None,
    default_class: str = "residential",
    use_cache: bool = True,
) -> BuildingFetch:
    """Read building footprints inside `bbox` from Overture Maps.

    Parameters
    ----------
    bbox
        `(west, south, east, north)` in WGS84 degrees. Overture's `bbox` column is in
        degrees, so the filter has to be too; reproject afterwards, not before.
    release
        Overture release, e.g. `2026-08-19.0`. Defaults to `case.overture_release`.
    cache_dir
        Where the file listing and fetched footprints are kept. A repeat call for the
        same box and release reads the cached GeoParquet instead of S3.

    Returns
    -------
    BuildingFetch
    """
    case = config.case if isinstance(config, Config) else config
    fallback = case.overture_release if case is not None else CaseConfig().overture_release
    chosen = release or fallback

    west, south, east, north = bbox
    if not (west < east and south < north):
        raise ValueError(f"bbox {bbox} is empty or inverted; expected (w, s, e, n)")

    cache_dir = cache_dir or settings().cache_dir
    cached = cache_dir / f"overture-{chosen}-{_cache_key(bbox)}.parquet"
    if use_cache and cached.exists():
        frame = gpd.read_parquet(cached)
        return BuildingFetch(
            buildings=frame,
            release=chosen,
            bbox=bbox,
            seconds=0.0,
            from_cache=True,
            n_files_scanned=0,
        )

    started = time.perf_counter()
    s3 = fs.S3FileSystem(anonymous=True, region=REGION)
    files = _file_list(s3, chosen, cache_dir)
    dataset = ds.dataset(files, filesystem=s3, format="parquet")

    # Overlap, not containment: a building whose bbox straddles the edge is inside.
    overlaps = (
        (pc.field("bbox", "xmin") < east)
        & (pc.field("bbox", "xmax") > west)
        & (pc.field("bbox", "ymin") < north)
        & (pc.field("bbox", "ymax") > south)
    )
    table = dataset.to_table(filter=overlaps, columns=_COLUMNS)

    geometry = shapely.from_wkb(table.column("geometry").to_pylist())
    frame = gpd.GeoDataFrame(
        {
            "id": table.column("id").to_pylist(),
            "height": table.column("height").to_pylist(),
            "num_floors": table.column("num_floors").to_pylist(),
            "subtype": table.column("subtype").to_pylist(),
            "overture_class": table.column("class").to_pylist(),
        },
        geometry=list(geometry),
        crs="EPSG:4326",
    )
    frame["building_class"] = [
        OVERTURE_CLASSES.get(s or "", default_class) for s in frame["subtype"]
    ]
    # The bbox filter is a coarse pass over row-group statistics; clip to the real box.
    frame = frame[frame.geometry.intersects(shapely.box(west, south, east, north))]
    frame = frame.reset_index(drop=True)

    if use_cache:
        cached.parent.mkdir(parents=True, exist_ok=True)
        frame.to_parquet(cached)

    return BuildingFetch(
        buildings=frame,
        release=chosen,
        bbox=bbox,
        seconds=time.perf_counter() - started,
        from_cache=False,
        n_files_scanned=len(files),
    )
