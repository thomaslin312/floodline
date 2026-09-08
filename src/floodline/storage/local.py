"""A terrain store on the local filesystem.

Layout is `hand/{huc}/{params_hash}.tif`, a two-band GeoTIFF holding HAND and the
stream mask together, because they are one artefact and separating them is how a HAND
grid ends up measured to somebody else's network.

Every write goes to a temporary path and is then renamed into place. `os.replace` is
atomic within a filesystem, so a process killed mid-write leaves a partial file under
a name nothing looks for, rather than a truncated file under a name that
`exists()` would answer yes to. That failure mode is worth designing against
specifically: a half-written cache entry does not raise, it returns wrong numbers.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import rasterio
from rasterio.transform import Affine

from floodline.cache import evict_to_budget
from floodline.settings import settings
from floodline.storage.base import TerrainArtifact, TerrainStore

__all__ = ["LocalTerrainStore"]


@dataclass
class LocalTerrainStore(TerrainStore):
    """Terrain artefacts kept as GeoTIFFs under a root directory."""

    root: Path | None = None

    def __post_init__(self) -> None:
        """Resolve the root from settings when the caller did not name one."""
        self.root = Path(self.root) if self.root is not None else settings().hand_cache_dir

    def _path(self, huc: str, params_hash: str) -> Path:
        # A HUC is digits and a hash is hex, so neither can escape the root. Checked
        # rather than assumed, because this is the one place a caller's string becomes
        # a filesystem path.
        if not huc.isalnum() or not params_hash.isalnum():
            raise ValueError(
                f"huc {huc!r} and params_hash {params_hash!r} must be alphanumeric; "
                "anything else could walk out of the cache root"
            )
        assert self.root is not None
        return self.root / "hand" / huc / f"{params_hash}.tif"

    def exists(self, huc: str, params_hash: str) -> bool:
        """Report whether a complete artefact is stored for this key."""
        path = self._path(huc, params_hash)
        return path.exists() and path.stat().st_size > 0

    def get_hand(self, huc: str, params_hash: str) -> TerrainArtifact:
        """Return the stored artefact, raising `KeyError` when there is none."""
        path = self._path(huc, params_hash)
        if not self.exists(huc, params_hash):
            raise KeyError(f"no terrain cached for {huc} at {params_hash}")
        with rasterio.open(path) as source:
            hand = source.read(1).astype(np.float32)
            streams = source.read(2).astype(bool)
            filled = source.read(3).astype(np.float32)
            flowdir = source.read(4).astype(np.int16)
            drainage_index = source.read(5).astype(np.int64)
            transform = source.transform
            crs = str(source.crs) if source.crs else ""
        return TerrainArtifact(
            hand=hand,
            streams=streams,
            filled=filled,
            flowdir=flowdir,
            drainage_index=drainage_index,
            transform=(
                transform.a,
                transform.b,
                transform.c,
                transform.d,
                transform.e,
                transform.f,
            ),
            crs=crs,
        )

    def put_hand(self, huc: str, params_hash: str, artifact: TerrainArtifact) -> None:
        """Store an artefact, writing to a temporary path and renaming into place."""
        path = self._path(huc, params_hash)
        path.parent.mkdir(parents=True, exist_ok=True)
        # Same directory as the destination, so the rename stays within one filesystem
        # and therefore stays atomic. A temp file in /tmp would make it a copy.
        partial = path.with_name(f".{path.name}.{os.getpid()}.partial")
        transform = Affine(*artifact.transform)
        try:
            with rasterio.open(
                partial,
                "w",
                driver="GTiff",
                height=artifact.hand.shape[0],
                width=artifact.hand.shape[1],
                count=5,
                dtype="float64",
                crs=artifact.crs or None,
                transform=transform,
                compress="deflate",
                predictor=2,
                tiled=True,
            ) as sink:
                # float64 throughout: the drainage index is a flat cell index that
                # exceeds float32's exact-integer range on a large grid, and a rounded
                # index points at the wrong cell rather than failing.
                sink.write(artifact.hand.astype(np.float64), 1)
                sink.write(artifact.streams.astype(np.float64), 2)
                sink.write(artifact.filled.astype(np.float64), 3)
                sink.write(artifact.flowdir.astype(np.float64), 4)
                sink.write(artifact.drainage_index.astype(np.float64), 5)
                for band, what in enumerate(
                    (
                        "height above nearest drainage (m)",
                        "stream network mask",
                        "conditioned surface (m)",
                        "D8 flow direction",
                        "nearest drainage cell, flat index",
                    ),
                    start=1,
                ):
                    sink.set_band_description(band, what)
            partial.replace(path)
        finally:
            partial.unlink(missing_ok=True)

        # Housekeeping after the write, not on a timer somebody has to start. Every
        # watershed anyone looks at leaves 4.5 MB here at 30 m and nine times that at
        # 10 m, and the service offers all of them, so unbounded means a disk that
        # fills at whatever rate visitors click.
        assert self.root is not None
        evict_to_budget(
            self.root,
            settings().terrain_cache_budget_mb,
            pattern="*.tif",
            what="terrain artefact",
        )
