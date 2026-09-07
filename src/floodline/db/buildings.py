"""The structure intersection, in the database.

*Which structures fall inside this watershed* is a spatial predicate over hundreds of
thousands of rows. In process it means holding every one of them resident and throwing
most away; in PostGIS it is an index scan on a GiST index, and the rows that come back
are the rows that were wanted.

The equivalence was measured before this became the default. GeoPandas in the analysis
CRS against `ST_Intersects` in EPSG:4326, on real NSI data:

| basin                         | GeoPandas | PostGIS |
|-------------------------------|-----------|---------|
| Whiteoak Bayou-Buffalo Bayou  |   258,527 | 258,527 |
| Little Whiteoak Bayou         |    80,104 |  80,104 |
| City of Philadelphia-Schuylkill |  104,780 | 104,780 |

Three of three exact. The question worth asking of a spatial index is not whether it is
fast but whether it is complete: an index that silently misses rows returns a smaller
number, and a smaller count of flooded buildings looks exactly like a better model.
`FLOODLINE_BUILDING_INDEX=geopandas` keeps the in-process path runnable so that
comparison can be repeated rather than cited.

Writes go through `COPY`. 258,527 rows by executemany is tens of seconds of round
trips for work the server does in about two.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

import geopandas as gpd
import shapely
from sqlalchemy import text

from floodline.db.models import STORAGE_SRID
from floodline.io.nsi import NSI_FIELDS, derive_nsi_columns

if TYPE_CHECKING:
    from shapely.geometry.base import BaseGeometry

__all__ = ["DatabaseUnavailableError", "load_structures", "store_structures"]

logger = logging.getLogger("floodline.db.buildings")

_TEXT_FIELDS = frozenset({"fd_id", "cbfips", "occtype", "st_damcat", "found_type", "med_yr_blt"})


class DatabaseUnavailableError(RuntimeError):
    """The database could not answer. Raised so a caller can name it as a gap."""


def store_structures(huc: str, structures: gpd.GeoDataFrame) -> int:
    """Replace this watershed's structures with `structures`, and report the row count.

    Idempotent by replacement rather than by upsert. A refetch of one basin is one
    delete and one `COPY`, which is both simpler and faster than reconciling a quarter
    of a million rows against what is already there, and it cannot leave behind rows
    for structures NSI has since dropped.
    """
    if not len(structures):
        return 0

    frame = structures.to_crs(f"EPSG:{STORAGE_SRID}")
    columns = ["huc", *NSI_FIELDS, "geom"]
    try:
        from floodline.db.session import engine

        with engine().begin() as connection:
            held = connection.execute(
                text("SELECT count(*) FROM buildings WHERE huc = :huc"), {"huc": huc}
            ).scalar_one()
            if held == len(frame):
                # The basin is already stored. Rewriting a quarter of a million
                # identical rows on every request would make the index cost more than
                # the scan it replaces.
                return int(held)
            connection.execute(text("DELETE FROM buildings WHERE huc = :huc"), {"huc": huc})
            raw = connection.connection.dbapi_connection
            if raw is None:  # pragma: no cover - only a mock connection reaches this
                raise DatabaseUnavailableError("no DBAPI connection behind the engine")
            with raw.cursor() as cursor:  # type: ignore[attr-defined]
                statement = f"COPY buildings ({', '.join(columns)}) FROM STDIN"
                with cursor.copy(statement) as copy:
                    for row in _rows(huc, frame):
                        copy.write_row(row)
    except DatabaseUnavailableError:
        raise
    except Exception as exc:
        raise DatabaseUnavailableError(f"{type(exc).__name__}: {exc}") from exc

    logger.info("stored %d structures for %s", len(frame), huc)
    return len(frame)


def load_structures(huc: str, geometry: BaseGeometry) -> gpd.GeoDataFrame:
    """Return this watershed's structures that intersect `geometry`, in EPSG:4326.

    `geometry` is a WGS84 polygon, and the predicate runs in the storage SRID rather
    than the analysis CRS. That is the comparison the equivalence table above measured:
    reprojecting the polygon costs one transform, reprojecting the points would cost
    one per row and would put the index out of play.
    """
    # The polygon crosses as WKB, not WKT. `shapely.to_wkt` rounds to six decimal
    # places by default - about 0.1 m here - so the text route hands PostGIS a
    # *different polygon* from the one GeoPandas tests against. Measured on Whiteoak
    # Bayou that lost exactly one structure of 258,527: a point sitting on the
    # boundary, inside the real polygon and outside the rounded one. A quiet
    # off-by-one in a building count is indistinguishable from a better model, which
    # is the whole reason the two paths are checked against each other.
    sql = text(
        f"""
        SELECT {", ".join(NSI_FIELDS)}, ST_AsBinary(geom) AS wkb
        FROM buildings
        WHERE huc = :huc
          AND ST_Intersects(geom, ST_GeomFromWKB(:polygon, :srid))
        ORDER BY id
        """
    )
    try:
        from floodline.db.session import engine

        with engine().connect() as connection:
            rows = connection.execute(
                sql,
                {
                    "huc": huc,
                    "polygon": shapely.to_wkb(geometry),
                    "srid": STORAGE_SRID,
                },
            ).mappings()
            records = [dict(row) for row in rows]
    except Exception as exc:
        raise DatabaseUnavailableError(f"{type(exc).__name__}: {exc}") from exc

    points = [shapely.from_wkb(record.pop("wkb")) for record in records]
    columns: dict[str, list[Any]] = {name: [] for name in NSI_FIELDS}
    for record in records:
        for name in NSI_FIELDS:
            columns[name].append(record[name])
    # Same derivation as the parse from the API, so the two routes cannot diverge in
    # anything but where the rows came from.
    return derive_nsi_columns(
        gpd.GeoDataFrame(columns, geometry=points, crs=f"EPSG:{STORAGE_SRID}")
    )


def _rows(huc: str, frame: gpd.GeoDataFrame) -> list[tuple[Any, ...]]:
    """One tuple per structure, in the column order `store_structures` declares."""
    values = {name: frame[name].to_numpy() for name in NSI_FIELDS}
    wkb = [shapely.to_wkb(point, hex=True, include_srid=True) for point in frame.geometry]
    out: list[tuple[Any, ...]] = []
    for index in range(len(frame)):
        row: list[Any] = [huc]
        for name in NSI_FIELDS:
            value = values[name][index]
            row.append(_textual(value) if name in _TEXT_FIELDS else _number(value))
        row.append(wkb[index])
        out.append(tuple(row))
    return out


def _number(value: Any) -> float | None:
    """Coerce to a float the driver can write, or None where NSI had nothing."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return None if number != number else number  # NaN is absence, not a value


def _textual(value: Any) -> str | None:
    """Coerce to text, keeping absence absent rather than writing the string "nan"."""
    if value is None or value != value:
        return None
    return str(value)
