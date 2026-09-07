"""Exposure and damage, reduced to something a browser can draw.

The building table is not a map layer. Whiteoak Bayou holds 258,527 structures, which
is 189 MB as GeoJSON and still 22 MB as damaged centroids alone - both far past what a
page should download to draw one watershed. So the same trick the depth overlay uses
applies here: rasterise onto the bundle's own display grid and ship a PNG. A few
hundred kilobytes, and it lines up with the flood layer pixel for pixel because it is
built on the same transform.

Four channels, one per reference discharge. The slider moves the water, so it has to
move the damage too, and shipping a raster per rung of the multiplier ladder would be
ten megabytes to draw one picture. Instead each channel holds log-damage per cell at
one of four multipliers and the browser interpolates between the two that bracket
wherever the slider is - the same trick the depth overlay plays with its per-reach
stage table, and for the same reason.

So the served image is packed data, not finished colour: the browser reads a value out
of the bracketing channels and picks the colour itself, which is what lets one image
serve every slider position. Alpha is decided the same way, and it is what keeps
undamaged ground transparent rather than a black rectangle over the bounding box. The
one place finished colour is written is `encode_damage`, for the static report, where
there is no slider and so nothing to interpolate.

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

from floodline.compute import to_web_mercator
from floodline.core.config import Config
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


# Warm, so the layer never reads as more water. Matches the legend swatch in the page.
DAMAGE_RAMP = np.array(
    [[253, 227, 199], [247, 178, 103], [239, 123, 69], [214, 73, 51], [140, 28, 19]],
    dtype=np.float64,
)


def log_scale(damage: npt.NDArray[np.floating]) -> npt.NDArray[np.uint8]:
    """Scale a damage grid onto 0-255, logarithmically, zero where nothing was hit.

    Logarithmic because flood damage spans five orders of magnitude across one
    watershed - a shed and a hospital in the same frame - and a linear 8-bit ramp
    would put almost every cell in the bottom two values and show nothing. Zero is
    reserved for "no damage", so the usable range starts at 1.
    """
    out = np.zeros(damage.shape, dtype=np.float64)
    hit = damage >= DAMAGE_FLOOR
    if np.any(hit):
        low = np.log10(DAMAGE_FLOOR)
        high = np.log10(DAMAGE_CEILING)
        span = (np.log10(np.clip(damage[hit], DAMAGE_FLOOR, DAMAGE_CEILING)) - low) / (high - low)
        out[hit] = 1.0 + span * 254.0
    return np.clip(np.rint(out), 0, 255).astype(np.uint8)


def encode_damage(damage: npt.NDArray[np.floating]) -> npt.NDArray[np.uint8]:
    """Colour a single damage grid on the warm ramp, for a static image."""
    scaled = log_scale(damage).astype(np.float64)
    hit = scaled > 0
    position = np.where(hit, (scaled - 1.0) / 254.0, 0.0) * (len(DAMAGE_RAMP) - 1)
    low_i = np.clip(np.floor(position).astype(int), 0, len(DAMAGE_RAMP) - 1)
    high_i = np.clip(low_i + 1, 0, len(DAMAGE_RAMP) - 1)
    frac = (position - low_i)[..., None]
    rgb = DAMAGE_RAMP[low_i] * (1 - frac) + DAMAGE_RAMP[high_i] * frac

    rgba = np.zeros((*damage.shape, 4), dtype=np.uint8)
    rgba[..., :3] = np.clip(rgb, 0, 255).astype(np.uint8)
    # Alpha, not a black background: undamaged ground has to disappear, and an RGB
    # image has no way to say that. The first version drew a black rectangle over the
    # whole bounding box.
    rgba[..., 3] = np.where(hit, 225, 0).astype(np.uint8)
    return rgba


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
    """RGBA data URI. Each channel is log-scaled damage per cell at one of
    `reference_multipliers`; the browser interpolates between the bracketing pair and
    colours the result, which is what lets one image serve every slider position."""

    reference_multipliers: tuple[float, ...] = ()

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
    config: Config,
    currency: str = "USD",
    stats: dict[str, Any] | None = None,
    notes: list[str] | None = None,
    damage_by_multiplier: dict[float, npt.NDArray[np.float64]] | None = None,
) -> ExposureBundle:
    """Rasterise a priced building table onto the display grid and encode it.

    `reduction` is the same block factor the depth overlay uses, so the two layers
    land on identical pixels and a reader comparing them is comparing the same ground.
    """
    # One channel per reference discharge, so the layer can follow the slider. Falls
    # back to a single channel repeated when only one damage field is available.
    by_multiplier = damage_by_multiplier or {}
    columns = sorted(by_multiplier)
    if columns:
        frames = [
            rasterise_damage(
                buildings.assign(_d=by_multiplier[m]), transform, shape, damage_column="_d"
            )[0]
            for m in columns
        ]
    else:
        frames = [rasterise_damage(buildings, transform, shape)[0]]
    damage = frames[-1]
    _, count = rasterise_damage(buildings, transform, shape)
    display_transform = transform
    if reduction > 1:
        # Sum, not mean: these are totals per cell, and averaging would quietly
        # divide the watershed's damage by the block area.
        frames = [block_reduce(f, reduction, how="sum") for f in frames]
        count = block_reduce(count.astype(np.float64), reduction, how="sum").astype(np.int32)
        display_transform = transform * Affine.scale(reduction, reduction)

    # The analysis grid is UTM and north-up there, which is not axis-aligned in Web
    # Mercator. Handing the map UTM bounds put this layer nowhere at all; corner-pinning
    # the unwarped grid would instead have placed it visibly skewed. Warp it, exactly as
    # the depth overlay does, so the two register pixel for pixel.
    # Nearest, not bilinear. Damage is a per-cell total over a sparse set of
    # buildings, and interpolating it spreads money into cells that hold none:
    # bilinear made 37% of the grid opaque for 32,833 damaged structures.
    warped = []
    mercator_bounds = bounds
    for frame in frames:
        out, mercator_bounds = to_web_mercator(frame, display_transform, config, "nearest")
        warped.append(np.nan_to_num(out, nan=0.0))

    if columns:
        # Up to four reference discharges, one per channel. More than four would need a
        # second image, and four brackets the slider closely enough that interpolating
        # between them is smaller than the model's own error.
        stack = np.zeros((*warped[0].shape, 4), dtype=np.uint8)
        for i, frame in enumerate(warped[:4]):
            stack[..., i] = log_scale(frame)
        rgba = stack
    else:
        rgba = encode_damage(warped[0])
    damage = warped[-1]
    return ExposureBundle(
        huc=huc,
        inventory=inventory,
        width=int(damage.shape[1]),
        height=int(damage.shape[0]),
        bounds=mercator_bounds,
        reference_multipliers=tuple(columns),
        damage_png=to_data_uri(encode_png(rgba, "RGBA")),
        currency=currency,
        stats=stats or {},
        notes=notes or [],
    )
