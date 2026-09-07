"""Fetching the USACE curve library, which is I/O and so lives outside core.

The parsing of `occtypes.json` is pure and stayed in `core.damage.usace`. Only the
download is here, because reaching the network is exactly the thing `core` is not
allowed to do - a model that can be run without a socket is a model that can be tested
without one.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import httpx

from floodline.settings import settings

__all__ = ["USACE_CURVES_URL", "ensure_usace_curves"]

USACE_CURVES_URL = settings().usace_curves_url


def ensure_usace_curves(
    *,
    cache_dir: Path | None = None,
    download: bool = False,
    client: httpx.Client | None = None,
) -> Path:
    """Return a local path to `occtypes.json`, fetching it once if allowed.

    Half a megabyte rather than half a gigabyte, but the same rule as the population
    rasters: nothing here reaches the network unless asked.
    """
    target = (cache_dir or settings().cache_dir) / "usace-occtypes.json"
    if target.exists() and target.stat().st_size > 0:
        return target
    if not download:
        raise FileNotFoundError(
            f"USACE curve library is not cached at {target}. Run `floodline fetch-curves`, "
            f"or pass download=True. Source: {USACE_CURVES_URL}"
        )
    target.parent.mkdir(parents=True, exist_ok=True)
    partial = target.with_suffix(".json.part")
    owned = client is None
    active = client or httpx.Client(timeout=httpx.Timeout(30.0, read=180.0), follow_redirects=True)
    try:
        response = active.get(USACE_CURVES_URL)
        response.raise_for_status()
        partial.write_bytes(response.content)
    finally:
        if owned:
            active.close()
    shutil.move(str(partial), str(target))
    return target
