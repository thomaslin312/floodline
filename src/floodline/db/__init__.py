"""Persistence: what has been computed, and the geometry worth indexing.

Deliberately small. The grids live on the filesystem because a raster is not a row;
what is here is the metadata with concurrent readers, and the geometry whose whole
purpose is to be intersected.
"""

from floodline.db.models import AdminUnit, Base, BasinCache, Building
from floodline.db.session import check_ready, engine, session_scope

__all__ = [
    "AdminUnit",
    "Base",
    "BasinCache",
    "Building",
    "check_ready",
    "engine",
    "session_scope",
]
