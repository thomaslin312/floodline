"""Pack a watershed's model into the few kilobytes a browser needs to re-run it.

Inundation is `depth = stage - HAND`, and the expensive parts - conditioning,
routing, HAND, the rating curves - do not depend on discharge. So a browser can
recompute the flood at any discharge from three small things:

* **HAND**, as an 8-bit greyscale PNG at 0.1 m precision. A smooth surface at low
  bit depth compresses hard.
* **Reach id per pixel**, packed into the red and green channels of a PNG. Reach
  ids are spatially coherent, so this compresses harder still - around 30 kB for
  600 reaches over a third of a million pixels.
* **A stage lookup table**: each reach's stage at a ladder of discharge multipliers,
  as one uint8 row per reach. A few kilobytes for a whole watershed.

Together that is roughly 180 kB per watershed, so a page can carry twenty of them
and still recompute any of them live. The alternative - shipping a depth raster per
discharge level - is hundreds of times larger and only covers the levels you chose
in advance.

Precision is deliberate and lossy: 0.1 m on HAND and on stage. That is far finer
than the model's own agreement with surveyed marks (RMSE 1.5 m), so it costs
nothing real, and it is what makes 8 bits enough.
"""

from __future__ import annotations

import base64
import io
from dataclasses import dataclass, field
from typing import Any

import numpy as np
import numpy.typing as npt
from PIL import Image

from floodline.hydraulics.rating import RatingCurve

__all__ = [
    "HAND_NODATA",
    "HAND_SCALE",
    "REACH_NODATA",
    "UnitBundle",
    "encode_hand",
    "encode_png",
    "encode_reach_ids",
    "encode_stage_table",
    "to_data_uri",
]

HAND_SCALE = 10.0
"""Decimetres per unit. 0.1 m precision, well under the model's own error."""

HAND_NODATA = 255
"""Reserved 8-bit value for a cell outside the watershed. Caps HAND at 25.4 m."""

REACH_NODATA = 65535
"""Reserved 16-bit reach id for a pixel that drains to no reach."""


def encode_png(array: npt.NDArray[np.uint8], mode: str) -> bytes:
    """Encode an array as an optimised PNG."""
    buffer = io.BytesIO()
    Image.fromarray(array, mode=mode).save(buffer, format="PNG", optimize=True)
    return buffer.getvalue()


def to_data_uri(payload: bytes, media_type: str = "image/png") -> str:
    """Return `payload` as a data URI, which is the only way to ship it inside a page."""
    return f"data:{media_type};base64," + base64.b64encode(payload).decode()


def encode_hand(hand: npt.NDArray[np.floating]) -> bytes:
    """Encode HAND as 8-bit decimetres, with `HAND_NODATA` outside the watershed.

    Values above 25.4 m clamp. Nothing floods that deep in these watersheds, and
    the clamp only ever makes a cell *drier* than it should be, never wetter, so it
    cannot invent inundation.
    """
    scaled = np.where(
        np.isfinite(hand), np.clip(np.asarray(hand) * HAND_SCALE, 0, HAND_NODATA - 1), HAND_NODATA
    )
    return encode_png(scaled.astype(np.uint8), "L")


def encode_reach_ids(reach_of_cell: npt.NDArray[np.integer]) -> bytes:
    """Pack per-pixel reach ids into a PNG's red and green channels.

    Blue is unused and left at zero; it compresses to nothing and keeps the
    unpacking in the browser to `(r << 8) | g`.
    """
    ids = np.where(reach_of_cell >= 0, reach_of_cell, REACH_NODATA).astype(np.uint16)
    rgb = np.dstack(
        [
            (ids >> 8).astype(np.uint8),
            (ids & 0xFF).astype(np.uint8),
            np.zeros(ids.shape, dtype=np.uint8),
        ]
    )
    return encode_png(rgb, "RGB")


def encode_stage_table(
    n_reaches: int,
    curves: dict[int, RatingCurve],
    discharge_cms: dict[int, float],
    multipliers: npt.NDArray[np.floating],
) -> bytes:
    """Tabulate each reach's stage across a ladder of discharge multipliers.

    One row per reach, one column per multiplier, 8-bit decimetres. A reach with no
    rating curve stays at zero, which floods nothing - the same honest default the
    stage field uses.
    """
    table = np.zeros((max(n_reaches, 1), len(multipliers)), dtype=np.uint8)
    for reach in range(n_reaches):
        curve = curves.get(reach)
        if curve is None:
            continue
        base = discharge_cms.get(reach, 0.0)
        stages = [curve.stage_for_discharge(base * float(m)) * HAND_SCALE for m in multipliers]
        table[reach] = np.clip(stages, 0, 255).astype(np.uint8)
    return encode_png(table, "L")


@dataclass
class UnitBundle:
    """Everything a browser needs to re-run one watershed at any discharge."""

    huc: str
    name: str
    area_km2: float
    width: int
    height: int
    reduction: int
    bounds: tuple[float, float, float, float]
    n_reaches: int
    base_discharge_cms: float
    """Discharge the multipliers are relative to: 1.0 is the modelled event peak."""
    multipliers: list[float]
    gauged: bool
    """False when this unit's discharge was inferred from another unit's gauge."""
    hand: str = ""
    reach: str = ""
    stage_table: str = ""
    basemap: str = ""
    gauge: dict[str, Any] | None = None
    marks: list[dict[str, Any]] = field(default_factory=list)
    stats: dict[str, Any] = field(default_factory=dict)
