"""Alembic environment, wired to the application's own settings.

The database URL is deliberately not in `alembic.ini`. A migration that runs against a
different database from the service is the kind of mistake that is only discovered
when a column is missing in production, so both read `floodline.settings` and there is
one place to be wrong.
"""

from __future__ import annotations

from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

from floodline.db.models import Base
from floodline.settings import settings

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

config.set_main_option("sqlalchemy.url", settings().database_url)
target_metadata = Base.metadata


def run_migrations_offline() -> None:
    """Emit SQL without a connection, for review or for a DBA to run."""
    context.configure(
        url=settings().database_url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run against a live database."""
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            # GeoAlchemy2 manages its own spatial indexes and the geometry_columns
            # view; without this, autogenerate proposes dropping them on every run.
            include_object=_include,
        )
        with context.begin_transaction():
            context.run_migrations()


POSTGIS_OWN = {"spatial_ref_sys", "geometry_columns", "geography_columns"}


def _include(
    obj: object, name: str | None, type_: str, reflected: bool, compare_to: object
) -> bool:
    """Keep PostGIS's own objects out of our migrations."""
    return not (type_ == "table" and name in POSTGIS_OWN)


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
