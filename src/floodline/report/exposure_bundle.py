"""Exposure and damage, reduced to something a browser can draw.

The building table is not a map layer. Whiteoak Bayou holds 258,527 structures, which
is 189 MB as GeoJSON and still 22 MB as damaged centroids alone - both far past what a
page should download to draw one watershed. So the same trick the depth overlay uses
applies here: rasterise onto the bundle's own display grid and ship a PNG. A few
hundred kilobytes, and it lines up with the flood layer pixel for pixel because it is
built on the same transform.

Two channels, because damage and count answer different questions and one cannot be
recovered from the other. A single expensive commercial building and forty flooded
houses can carry the same dollar total; a reader looking for where people were hit
needs the count, and one looking for where the money went needs the damage.

Per-building detail is not in here. It is served separately, for the small window a
reader has actually clicked on, because that is the only scale at which 258,527 rows
is a sensible thing to send.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import geopandas as gpd
import numpy as np
import numpy.typing as npt
from rasterio.transform import Affine, rowcol

from floodline.report.bundle import encode_png, to_data_uri
from floodline.report.figures import block_reduce

__all__ = ["ExposureBundle", "build_exposure_bundle", "rasterise_damage"]

# Damage per cell is encoded logarithmically. Flood damage spans five orders of
# magnitude across a watershed - a shed and a hospital in the same frame - so a linear
# 8-bit ramp would put almost every cell in the bottom two values and show nothing.
DAMAGE_FLOOR = 1_000.0
"""Currency per cell below which a cell reads as undamaged."""

DAMAGE_CEILING = 100_000_000.0
"""Currency per cell at the top of the ramp; above this the encoding saturates."""


def rasterise_damage(
    buildings: gpd.GeoDataFrame,
    transform: Affine,
    shape: tuple[int, int],
    *,
    damage_column: str = "damage",
) -> tuple[npt.NDArray[np.float64], npt.NDArray[np.int32]]:
    """Sum damage and count damaged buildings into each cell of a grid.

    Buildings are placed by centroid. At the display resolutions this feeds - tens of
    metres a pixel - a footprint is smaller than a cell, so spreading it across cells
    would be false precision.
    """
    rows, cols = shape
    damage = np.zeros((rows, cols), dtype=np.float64)
    count = np.zeros((rows, cols), dtype=np.int32)
    if not len(buildings):
        return damage, count

    centroids = buildings.geometry.centroid
    row_idx, col_idx = rowcol(transform, centroids.x.to_numpy(), centroids.y.to_numpy())
    row_idx = np.asarray(row_idx, dtype=np.int64)
    col_idx = np.asarray(col_idx, dtype=np.int64)
    values = buildings[damage_column].to_numpy(dtype=np.float64)

    inside = (row_idx >= 0) & (row_idx < rows) & (col_idx >= 0) & (col_idx < cols)
    hit = inside & (values > 0)
    # np.add.at rather than fancy assignment: several buildings share a cell, and
    # plain indexed assignment would keep only the last one.
    np.add.at(damage, (row_idx[hit], col_idx[hit]), values[hit])
    np.add.at(count, (row_idx[hit], col_idx[hit]), 1)
    return damage, count


def encode_damage(
    damage: npt.NDArray[np.floating], count: npt.NDArray[np.integer]
) -> npt.NDArray[np.uint8]:
    """Pack damage and count into an RGB image.

    Red carries log-scaled currency, green carries the building count clipped at 255,
    and blue is left at zero. The browser reads both from one request.
    """
    scaled = np.zeros(damage.shape, dtype=np.float64)
    wet = damage >= DAMAGE_FLOOR
    if np.any(wet):
        low = np.log10(DAMAGE_FLOOR)
        high = np.log10(DAMAGE_CEILING)
        scaled[wet] = (np.log10(np.clip(damage[wet], DAMAGE_FLOOR, DAMAGE_CEILING)) - low) / (
            high - low
        )
    red = np.clip(np.rint(scaled * 255.0), 0, 255).astype(np.uint8)
    green = np.clip(count, 0, 255).astype(np.uint8)
    return np.dstack([red, green, np.zeros(damage.shape, dtype=np.uint8)])


@dataclass(frozen=True, slots=True)
class ExposureBundle:
    """Everything the map needs to draw exposure and damage for one watershed."""

    huc: str
    inventory: str
    width: int
    height: int
    bounds: tuple[float, float, float, float]
    """Web Mercator, matching the depth overlay so the layers register."""

    damage_png: str = ""
    """RGB data URI: red is log10 damage, green is building count."""

    damage_floor: float = DAMAGE_FLOOR
    damage_ceiling: float = DAMAGE_CEILING
    currency: str = "USD"
    stats: dict[str, Any] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)


def build_exposure_bundle(
    huc: str,
    inventory: str,
    buildings: gpd.GeoDataFrame,
    transform: Affine,
    shape: tuple[int, int],
    bounds: tuple[float, float, float, float],
    *,
    reduction: int,
    currency: str = "USD",
    stats: dict[str, Any] | None = None,
    notes: list[str] | None = None,
) -> ExposureBundle:
    """Rasterise a priced building table onto the display grid and encode it.

    `reduction` is the same block factor the depth overlay uses, so the two layers
    land on identical pixels and a reader comparing them is comparing the same ground.
    """
    damage, count = rasterise_damage(buildings, transform, shape)
    if reduction > 1:
        # Sum, not mean: these are totals per cell, and averaging would quietly
        # divide the watershed's damage by the block area.
        damage = block_reduce(damage, reduction, how="sum")
        count = block_reduce(count.astype(np.float64), reduction, how="sum").astype(np.int32)

    rgb = encode_damage(damage, count)
    return ExposureBundle(
        huc=huc,
        inventory=inventory,
        width=int(damage.shape[1]),
        height=int(damage.shape[0]),
        bounds=bounds,
        damage_png=to_data_uri(encode_png(rgb, "RGB")),
        currency=currency,
        stats=stats or {},
        notes=notes or [],
    )
