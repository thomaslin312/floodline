"""Which structures fall inside this watershed, and where that question is answered.

Two implementations of one predicate. PostGIS is the deployed path and the default:
the structures are written once per basin and selected by `ST_Intersects` over a GiST
index, so the rows that come back are the rows that were wanted. The in-process
GeoPandas predicate is kept behind `FLOODLINE_BUILDING_INDEX=geopandas`, because the
claim that the two agree is only worth anything while both can still be run.

The fallback is deliberate and it is loud. A checkout with no database, or a
deployment whose database is down, still answers - and says in the run's gaps that the
intersection ran in process, so a slow answer is never silently a different answer.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import geopandas as gpd

from floodline.settings import settings

if TYPE_CHECKING:
    from shapely.geometry.base import BaseGeometry

__all__ = ["structures_inside"]


def structures_inside(
    huc: str,
    geometry: BaseGeometry,
    structures: gpd.GeoDataFrame,
) -> tuple[gpd.GeoDataFrame, str | None]:
    """Select the structures intersecting `geometry`, and report any fallback.

    Returns the frame and, when the configured index could not be used, a note for the
    run's gaps naming why. The note is not an error: the answer is the same either way,
    which is the whole point of keeping both paths runnable.
    """
    if settings().building_index != "postgis" or not len(structures):
        return _in_process(geometry, structures), None

    try:
        from floodline.db.buildings import (
            DatabaseUnavailableError,
            load_structures,
            store_structures,
        )
    except ImportError as exc:  # the db extra is not installed
        return (
            _in_process(geometry, structures),
            f"database support not installed ({exc}), so the structure intersection "
            "ran in process rather than on the spatial index",
        )

    try:
        store_structures(huc, structures)
        return load_structures(huc, geometry), None
    except DatabaseUnavailableError as exc:
        return (
            _in_process(geometry, structures),
            f"database unavailable ({exc}), so the structure intersection ran in "
            "process rather than on the spatial index",
        )


def _in_process(geometry: BaseGeometry, structures: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
    """Run the GeoPandas predicate, in the storage CRS the database also uses.

    Both sides are compared in EPSG:4326 rather than the analysis CRS. Reprojecting the
    polygon is one transform; reprojecting a quarter of a million points is one per row
    and puts the index out of play, so the cheap side is the side that moves.
    """
    if not len(structures):
        return structures
    inside = structures.geometry.intersects(geometry)
    return gpd.GeoDataFrame(structures.loc[inside]).reset_index(drop=True)
