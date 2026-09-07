"""Applying migrations, and reporting whether they have been applied.

Nothing ran `alembic upgrade head` before this. `alembic.ini` was copied into the
image and the migrations shipped inside it, and the tables were still never created -
the schema existed in the repository and nowhere else. A deployment step that only a
person can remember to run is a deployment step that does not exist.

The container now migrates before it serves. It does not *refuse* to serve when that
fails, because the model degrades honestly without a database and refusing would turn
a degraded map into no map at all. What it does instead is tell the truth about it:
`/ready` compares the schema's revision against the head this build ships and answers
503 until they match, so an orchestrator holds traffic off a container whose database
is not where the code expects it. Liveness stays up, readiness stays honest.
"""

from __future__ import annotations

from pathlib import Path

__all__ = ["MigrationsNotFoundError", "alembic_ini", "check_schema", "upgrade_to_head"]


class MigrationsNotFoundError(RuntimeError):
    """`alembic.ini` is not where any of the layouts put it."""


def alembic_ini() -> Path:
    """Find `alembic.ini`, from a checkout or from an installed package.

    Its `script_location` is relative to itself, so locating the file locates the
    migrations. Searched rather than computed because the two layouts disagree: from
    `src/floodline/db/` the repository root is three parents up, and from
    `site-packages/floodline/db/` three parents up is the interpreter's library
    directory, which contains no such file and would fail as "already at None" -
    a container reporting a clean migration that never happened.
    """
    here = Path(__file__).resolve()
    for base in (Path.cwd(), *here.parents):
        candidate = base / "alembic.ini"
        if candidate.is_file():
            return candidate
    raise MigrationsNotFoundError(f"no alembic.ini above {here} or {Path.cwd()}")


def _config() -> object:
    from alembic.config import Config as AlembicConfig

    from floodline.settings import settings

    config = AlembicConfig(str(alembic_ini()))
    config.set_main_option("sqlalchemy.url", settings().database_url)
    return config


def _head() -> str | None:
    """Return the newest revision this build ships, or None when there are none."""
    from alembic.script import ScriptDirectory

    return ScriptDirectory.from_config(_config()).get_current_head()  # type: ignore[arg-type]


def _applied() -> str | None:
    """Return the revision the database is at, or None when it has never migrated."""
    from alembic.runtime.migration import MigrationContext

    from floodline.db.session import engine

    with engine().connect() as connection:
        return MigrationContext.configure(connection).get_current_revision()


def upgrade_to_head() -> str:
    """Migrate the database to head, and report what happened in one line.

    Idempotent: a database already at head runs no migrations, so a restart, a second
    replica and a redeploy all cost one query. Failures are returned rather than
    raised, because the caller is a container entrypoint whose job is to start the
    server either way.
    """
    from alembic import command

    try:
        head = _head()
    except Exception as exc:  # reported, never fatal
        return f"could not read the migration history: {type(exc).__name__}: {exc}"

    try:
        before = _applied()
    except Exception as exc:  # a database that is down is not an error here
        return f"database unreachable, schema not migrated: {type(exc).__name__}: {exc}"

    if head is None:
        # No migrations at all is a broken image, not a migrated database. Saying
        # "already at None" would read as success.
        return "no migrations found; the schema was not checked"

    if before == head:
        return f"schema already at {head}"

    try:
        command.upgrade(_config(), "head")  # type: ignore[arg-type]
    except Exception as exc:  # reported through /ready, not by exiting
        return f"migration to {head} failed: {type(exc).__name__}: {exc}"
    return f"schema migrated {before or 'empty'} -> {head}"


def check_schema() -> tuple[bool, str]:
    """Report whether the database's schema is at the revision this build expects.

    Returns a reason rather than raising: the caller is a readiness endpoint whose job
    is to describe the problem, not to become it.
    """
    try:
        head = _head()
    except Exception as exc:  # readiness describes, never raises
        return False, f"could not read the migration history: {type(exc).__name__}: {exc}"

    try:
        applied = _applied()
    except Exception as exc:  # readiness describes, never raises
        return False, f"{type(exc).__name__}: {str(exc).splitlines()[0][:160]}"

    if head is None:
        return False, "no migrations found; this build cannot say what the schema should be"
    if applied == head:
        return True, f"at {head}"
    return False, f"schema is at {applied or 'no revision'}, this build expects {head}"
