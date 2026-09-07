"""Neighbourhood offsets shared by every terrain kernel.

Kept in one place so `fill`, `flowdir`, `flowacc` and `hand` cannot drift into
disagreeing about what "adjacent" means, and so the D8 ordering is defined once.
"""

from __future__ import annotations

import numpy as np
import numpy.typing as npt

from floodline.core.config import Connectivity

__all__ = ["D4_OFFSETS", "D8_OFFSETS", "neighbour_offsets"]

D8_OFFSETS: npt.NDArray[np.int64] = np.array(
    [(-1, -1), (-1, 0), (-1, 1), (0, -1), (0, 1), (1, -1), (1, 0), (1, 1)],
    dtype=np.int64,
)
"""Row/column offsets of the eight neighbours, in raster (row-major) order."""

D4_OFFSETS: npt.NDArray[np.int64] = np.array(
    [(-1, 0), (0, -1), (0, 1), (1, 0)],
    dtype=np.int64,
)
"""Row/column offsets of the four rook-adjacent neighbours."""


def neighbour_offsets(connectivity: Connectivity) -> npt.NDArray[np.int64]:
    """Return the (n, 2) array of row/column offsets for `connectivity`."""
    return D8_OFFSETS if connectivity is Connectivity.EIGHT else D4_OFFSETS
