"""Schema shape: the indexes and constraints the queries depend on."""

from __future__ import annotations

from floodline.db.models import AdminUnit, Base, BasinCache, Building


def test_the_geometry_columns_have_gist_indexes() -> None:
    """Without one, `ST_Intersects` is a sequential scan over every structure."""
    for model in (Building, AdminUnit):
        spatial = [
            index
            for index in model.__table__.indexes
            if index.dialect_options.get("postgresql", {}).get("using") == "gist"
        ]
        assert spatial, f"{model.__tablename__} has no GiST index on its geometry"
        assert [c.name for c in spatial[0].columns] == ["geom"]


def test_one_basin_and_parameter_set_cannot_have_two_answers() -> None:
    """Two rows for one key would mean the cache could return either."""
    names = {c.name for c in BasinCache.__table__.constraints}
    assert "uq_basin_cache_huc_params" in names


def test_a_structure_is_unique_within_a_watershed_but_not_across_them() -> None:
    """A structure near a boundary legitimately belongs to two basins."""
    unique = {
        tuple(c.name for c in constraint.columns)
        for constraint in Building.__table__.constraints
        if constraint.__class__.__name__ == "UniqueConstraint"
    }
    assert ("fd_id", "huc") in unique
    assert ("fd_id",) not in unique


def test_every_geometry_is_stored_in_one_crs() -> None:
    """Storage and analysis are different jobs; one storage CRS keeps the table joinable."""
    for model in (Building, AdminUnit):
        assert model.__table__.c.geom.type.srid == 4326


def test_the_metadata_holds_exactly_the_three_tables() -> None:
    """A table nobody planned is a table nobody migrates."""
    assert set(Base.metadata.tables) == {"basin_cache", "buildings", "admin_units"}


def test_the_buildings_table_can_hold_every_field_the_inventory_carries() -> None:
    """The database has to round-trip a structure, not most of one.

    Everything the pipeline reads is derived from NSI's raw fields, so a field that is
    fetched but not stored comes back as a hole - and a hole in `ftprntsqft` or
    `found_ht` does not raise, it quietly changes a damage total. Deriving on read is
    only safe while the raw fields are all here.
    """
    from floodline.io.nsi import NSI_FIELDS

    stored = {column.name for column in Building.__table__.columns}
    missing = set(NSI_FIELDS) - stored
    assert not missing, f"buildings cannot store {sorted(missing)}"


def test_the_derived_population_columns_are_not_stored_twice() -> None:
    """One quantity in two places is one quantity that can disagree with itself."""
    stored = {column.name for column in Building.__table__.columns}
    assert not stored & {"pop_night", "pop_day"}, "night and day totals are derived on read"
