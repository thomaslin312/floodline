"""What a terrain cache has to be able to do, and how its keys are built.

Terrain is the expensive, request-independent half of the pipeline: conditioning a
DEM, routing flow across it, and measuring height above nearest drainage. It depends
on the watershed and on a handful of parameters, and on nothing a request supplies.
So it is worth computing once and keeping, and the interface for keeping it is small
enough to state in three methods.

The key is a watershed code and a hash of the parameters that produced the result.
Both halves are necessary: the same basin at 10 m and at 30 m are different answers,
and serving one for the other is the kind of bug that produces a plausible map nobody
can explain. `params_hash` is what makes the second half honest - it is derived from
the values themselves rather than from a version number somebody has to remember to
bump.

**HAND and the stream network are one artefact, not two.** HAND is measured *to* the
network, so a HAND grid paired with a different network is meaningless in a way that
is hard to detect downstream: every depth is measured from the wrong datum and nothing
raises. They are written together and read together, and the interface offers no way
to fetch one without the other.
"""

from __future__ import annotations

import hashlib
import json
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any

import numpy as np
import numpy.typing as npt

__all__ = ["TerrainArtifact", "TerrainStore", "params_hash"]


def params_hash(params: dict[str, Any], *, length: int = 16) -> str:
    """Return a stable hash of the parameters that determine a terrain result.

    Stable across processes and machines, which `hash()` is not: Python salts string
    hashing per process, so a cache keyed on it would miss every restart and slowly
    fill the disk with duplicates.

    Keys are sorted and values rendered canonically, so two callers that pass the same
    parameters in a different order get the same key. A value that will not serialise
    raises here rather than silently becoming its repr, because a repr that includes a
    memory address would make every run a cache miss.
    """
    canonical = json.dumps(params, sort_keys=True, separators=(",", ":"), default=_canonical)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:length]


def _canonical(value: Any) -> Any:
    """Render a value that JSON will not take, or refuse it loudly."""
    if isinstance(value, np.integer | np.floating):
        return value.item()
    if hasattr(value, "value"):  # an enum, whose value is the stable part
        return value.value
    if isinstance(value, tuple | set | frozenset):
        return sorted(value) if isinstance(value, set | frozenset) else list(value)
    raise TypeError(
        f"{type(value).__name__} has no canonical form, so it cannot take part in a "
        "cache key. Convert it to a primitive at the call site rather than letting it "
        "fall back to repr, which would change between runs."
    )


@dataclass(frozen=True, slots=True)
class TerrainArtifact:
    """Everything a scenario needs from terrain, cached as one indivisible unit.

    HAND and the stream network are the pair the interface is named for, and they are
    the two a depth map needs. Three more grids ride along because a scenario needs
    more than a depth map: the per-reach rating curves are built from the conditioned
    surface, the reach partition from the flow directions, and the reach a cell belongs
    to from the drainage index. Storing only HAND and streams would mean a cache hit
    re-ran depression filling and flow routing to rebuild them, which is the expensive
    half this split exists to avoid - the cache would be correct and pointless.

    All five are written and read together for the same reason HAND and streams are.
    Each is measured against the others; any mismatched combination is wrong in a way
    that produces plausible numbers rather than an error.
    """

    hand: npt.NDArray[np.float32]
    """Height above nearest drainage, in metres. NaN off the raster."""

    streams: npt.NDArray[np.bool_]
    """The extracted channel network HAND is measured to."""

    filled: npt.NDArray[np.float32]
    """The conditioned surface, which the rating curves take their geometry from."""

    flowdir: npt.NDArray[np.int16]
    """D8 flow direction, which the reach partition is cut from."""

    drainage_index: npt.NDArray[np.int64]
    """Flat index of each cell's nearest drainage cell, which assigns cells to reaches."""

    transform: tuple[float, float, float, float, float, float]
    """Affine coefficients, so the grid can be placed without the DEM it came from."""

    crs: str
    """Analysis CRS as a string, so a reader can check it is in metres."""

    def __post_init__(self) -> None:
        """Refuse a mismatched set at construction rather than downstream."""
        shapes = {
            "hand": self.hand.shape,
            "streams": self.streams.shape,
            "filled": self.filled.shape,
            "flowdir": self.flowdir.shape,
            "drainage_index": self.drainage_index.shape,
        }
        if len(set(shapes.values())) != 1:
            raise ValueError(
                f"terrain grids describe different rasters and so cannot be one artefact: {shapes}"
            )


class TerrainStore(ABC):
    """Where computed terrain is kept between requests.

    Three methods, deliberately. A cache that can be asked anything grows a query
    language; this one can be asked whether it has a thing, for the thing, and to keep
    a thing, which is all the pipeline needs.
    """

    @abstractmethod
    def exists(self, huc: str, params_hash: str) -> bool:
        """Report whether a complete artefact is stored for this key."""

    @abstractmethod
    def get_hand(self, huc: str, params_hash: str) -> TerrainArtifact:
        """Return the stored artefact, raising `KeyError` when there is none."""

    @abstractmethod
    def put_hand(self, huc: str, params_hash: str, artifact: TerrainArtifact) -> None:
        """Store an artefact, replacing any existing one for the same key."""
