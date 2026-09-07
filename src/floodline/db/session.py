"""Engine and session, with a connect timeout that fails rather than hangs.

A readiness probe that blocks is worse than one that returns false: an orchestrator
waiting on a hung health check keeps sending traffic to a container that cannot serve
it. So the connection carries an explicit timeout and `check_ready` reports rather
than raises.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from functools import lru_cache

from sqlalchemy import Engine, create_engine, text
from sqlalchemy.orm import Session, sessionmaker

from floodline.settings import settings

__all__ = ["check_ready", "engine", "session_scope"]


@lru_cache(maxsize=1)
def engine() -> Engine:
    """Build the process's engine, once.

    `pool_pre_ping` because a database restart otherwise hands out dead connections
    that fail on first use, which reads as a random request failure rather than as the
    restart it is.
    """
    config = settings()
    return create_engine(
        config.database_url,
        pool_size=config.db_pool_size,
        pool_pre_ping=True,
        connect_args={"connect_timeout": config.db_connect_timeout_s},
        future=True,
    )


@lru_cache(maxsize=1)
def _sessions() -> sessionmaker[Session]:
    return sessionmaker(bind=engine(), expire_on_commit=False, future=True)


@contextmanager
def session_scope() -> Iterator[Session]:
    """Open a transaction that commits on success and rolls back on anything else."""
    session = _sessions()()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def check_ready() -> tuple[bool, str]:
    """Report whether the database is reachable and has PostGIS.

    Returns a reason rather than raising, because the caller is a readiness endpoint
    whose job is to describe the problem, not to become it. Both halves are checked:
    a database that is up without PostGIS will accept the connection and fail every
    spatial query, which is a worse failure than being down.
    """
    try:
        with engine().connect() as connection:
            version = connection.execute(text("SELECT postgis_version()")).scalar_one()
    except Exception as exc:
        return False, f"{type(exc).__name__}: {str(exc).splitlines()[0][:160]}"
    return True, f"postgis {version}"
