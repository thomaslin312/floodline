"""Tables the service reads and writes.

Three, and each earns its place by answering a question the filesystem cannot.

`basin_cache` records what terrain has been computed and where its grids landed. The
grids themselves stay on disk - a HAND raster is megabytes and a database is the wrong
place for it - but *which* grids exist, for which parameters, and when they were last
served is a question with concurrent readers and writers, which is exactly what a
database is for. The filesystem answers "is this file here"; this answers "is this
result current, and has anyone wanted it lately".

`buildings` and `admin_units` are here for the opposite reason: they are geometry, and
the question asked of them is "which of these intersect that", over hundreds of
thousands of rows. PostGIS answers it with a GiST index in milliseconds. GeoPandas
answers it correctly too - and the SQL path is checked against the GeoPandas one rather
than trusted, because an index that silently misses rows returns a smaller number, not
an error.

Every geometry column is EPSG:4326. Not because the model works in degrees - it does
not, and a geographic CRS as an analysis CRS raises - but because storage and analysis
are different jobs. One canonical storage CRS means a basin in Texas and a basin in
Iowa live in the same table; the analysis projection is chosen per watershed when the
rows come out.
"""

from __future__ import annotations

from datetime import UTC, datetime

from geoalchemy2 import Geometry
from sqlalchemy import (
    BigInteger,
    DateTime,
    Float,
    Index,
    Integer,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

__all__ = ["AdminUnit", "Base", "BasinCache", "Building"]

STORAGE_SRID = 4326


class Base(DeclarativeBase):
    """Declarative base, so Alembic can find every table from one import."""


def _now() -> datetime:
    return datetime.now(UTC)


class BasinCache(Base):
    """One computed terrain result: what it was computed from, and where it went.

    The row is the index; the grids are on disk. A row without its file is a miss, not
    a corruption, which is why `exists` on the store is still consulted rather than
    trusted from here - two systems can disagree and the filesystem is the one holding
    the arrays.
    """

    __tablename__ = "basin_cache"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    huc: Mapped[str] = mapped_column(String(16), nullable=False)
    params_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    """Hash of the terrain parameters, computed from their values. Same basin at two
    resolutions is two rows, which is the point."""

    resolution_m: Mapped[float] = mapped_column(Float, nullable=False)
    crs: Mapped[str] = mapped_column(String(64), nullable=False)
    cell_count: Mapped[int] = mapped_column(BigInteger, nullable=False)
    stream_cells: Mapped[int] = mapped_column(Integer, nullable=False)
    reach_count: Mapped[int] = mapped_column(Integer, nullable=False)
    compute_seconds: Mapped[float] = mapped_column(Float, nullable=False)

    storage_key: Mapped[str] = mapped_column(String(512), nullable=False)
    """Where the store put the grids. Opaque to the database on purpose: a filesystem
    path today, an object key later, and this column does not care which."""

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    last_used_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, nullable=False
    )
    """Touched on every hit, so eviction can be least-recently-*used* rather than
    least-recently-written. The filesystem cannot answer this: many volumes mount
    with relatime or noatime and stop maintaining access time at all."""

    use_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    __table_args__ = (
        # The lookup the API does on every request, and the constraint that makes it
        # unambiguous. Two rows for one (basin, parameters) would mean two answers.
        UniqueConstraint("huc", "params_hash", name="uq_basin_cache_huc_params"),
        Index("ix_basin_cache_last_used", "last_used_at"),
    )


class Building(Base):
    """A structure from the National Structure Inventory.

    Kept because the intersection - which structures fall inside this watershed - is a
    spatial query over hundreds of thousands of rows, and doing it in process means
    loading all of them to discard most. The GiST index turns that into an index scan.

    Values are NSI's modelled replacement costs, not appraisals. That caveat belongs
    with the data wherever it goes, and the column comments carry it into the schema.
    """

    __tablename__ = "buildings"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    fd_id: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    """NSI's own structure identifier, so a refetch updates rather than duplicates."""

    huc: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    """The watershed this row was fetched for. A structure near a boundary can belong
    to two, which is why this is not unique on its own."""

    cbfips: Mapped[str | None] = mapped_column(String(15))
    """15-digit census block. Its first 11 are the tract, which is how modelled damage
    meets FEMA's record without a spatial join."""

    occtype: Mapped[str | None] = mapped_column(String(32), index=True)
    """HAZUS occupancy code, which joins to the USACE curve library."""

    val_struct: Mapped[float | None] = mapped_column(Float)
    val_cont: Mapped[float | None] = mapped_column(Float)
    sqft: Mapped[float | None] = mapped_column(Float)
    num_story: Mapped[float | None] = mapped_column(Float)
    found_ht: Mapped[float | None] = mapped_column(Float)
    """Foundation height in feet, as published. The measured cause of the damage
    model's failed validation: a 0.23 m median against a 1.5 m water-surface error."""

    pop_night: Mapped[float | None] = mapped_column(Float)
    pop_day: Mapped[float | None] = mapped_column(Float)

    geom: Mapped[object] = mapped_column(
        Geometry(geometry_type="POINT", srid=STORAGE_SRID, spatial_index=False),
        nullable=False,
    )
    """Structure centroid in EPSG:4326. The index is declared below rather than here so
    its name is ours and a migration can find it."""

    __table_args__ = (
        UniqueConstraint("fd_id", "huc", name="uq_buildings_fd_huc"),
        Index("ix_buildings_geom", "geom", postgresql_using="gist"),
    )


class AdminUnit(Base):
    """An administrative or statistical polygon: a census tract, a county, a watershed.

    One table rather than three, keyed by a `kind`, because every question asked of
    them is the same question - which of these contains, or intersects, that - and
    three tables would mean three indexes and three code paths for one operation.
    """

    __tablename__ = "admin_units"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    kind: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    """`tract`, `county`, `watershed`. What the code means by this row."""

    code: Mapped[str] = mapped_column(String(32), nullable=False)
    """The identifier in its own vocabulary: a HUC, a tract GEOID, a county FIPS."""

    name: Mapped[str | None] = mapped_column(String(256))
    area_km2: Mapped[float | None] = mapped_column(Float)

    geom: Mapped[object] = mapped_column(
        Geometry(geometry_type="MULTIPOLYGON", srid=STORAGE_SRID, spatial_index=False),
        nullable=False,
    )

    __table_args__ = (
        UniqueConstraint("kind", "code", name="uq_admin_units_kind_code"),
        Index("ix_admin_units_geom", "geom", postgresql_using="gist"),
    )
