"""Keeping the caches inside a budget.

Three caches grow with the number of watersheds anyone looks at, and this service
offers every one in the country. Measured on Whiteoak Bayou: 4.5 MB of terrain
artefacts at 30 m, nine times that at 10 m, and 16 MB of structure inventory. A
thousand basins is tens of gigabytes, accumulated silently over months.

Only the served bundles were bounded. The terrain store and the inventory cache were
not, which on a machine left running is a disk that fills at whatever rate visitors
click - and a full disk takes the service down with a failure that looks nothing like
its cause. The database stops accepting writes, the fetches fail on a partial file,
and none of it says "disk".

Eviction runs after each write rather than on a timer, because a timer is another
thing to start and another thing to notice has stopped.

Ordered by modification time, not access time. Many filesystems mount with relatime or
noatime and stop maintaining access time, so a least-recently-*used* policy built on it
would quietly become arbitrary - which is worse than a least-recently-*written* policy
that is at least predictable.
"""

from __future__ import annotations

import logging
from pathlib import Path

__all__ = ["evict_to_budget"]

logger = logging.getLogger("floodline.cache")


def evict_to_budget(root: Path, budget_mb: float, *, pattern: str = "*", what: str = "file") -> int:
    """Delete the oldest matching files under `root` until the total fits `budget_mb`.

    Returns the number removed. Directories left empty are removed too, so a cache of
    per-watershed subdirectories does not accumulate thousands of empty ones.

    Failures to unlink are skipped rather than raised: this is housekeeping running
    after a successful write, and turning a full disk into a failed request the caller
    cannot act on helps nobody. The next write tries again.
    """
    if not root.exists():
        return 0

    files = [path for path in root.rglob(pattern) if path.is_file()]
    try:
        files.sort(key=lambda f: f.stat().st_mtime)
        total = sum(f.stat().st_size for f in files)
    except OSError:  # a file vanished under us; nothing to do but try again later
        return 0

    budget = budget_mb * 1024 * 1024
    removed = 0
    while files and total > budget:
        oldest = files.pop(0)
        try:
            total -= oldest.stat().st_size
            oldest.unlink()
            removed += 1
        except OSError:
            continue

    if removed:
        for directory in sorted(root.rglob("*"), key=lambda p: len(p.parts), reverse=True):
            if directory.is_dir() and not any(directory.iterdir()):
                directory.rmdir()
        logger.info(
            "evicted %d %s(s) from %s to stay under %.0f MB", removed, what, root, budget_mb
        )
    return removed
