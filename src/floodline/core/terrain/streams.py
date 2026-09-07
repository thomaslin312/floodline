"""Stream network: threshold the accumulation, prune stubs, vectorise the result.

Three steps, each separately useful:

1. `stream_mask` — cells whose contributing area exceeds
   `TerrainConfig.stream_threshold_cells` are channel. This one threshold decides
   drainage density, and therefore how far anything is from "the nearest
   drainage", so it is a config field and never a literal.
2. `prune_stream_mask` — first-order branches shorter than
   `TerrainConfig.min_stream_length_cells` are removed. Thresholding a smooth
   accumulation surface sprouts one- and two-cell stubs wherever a hillslope
   happens to tip over the threshold; they are an artefact of the threshold, not
   channels.
3. `stream_network` — the pruned mask, walked along the D8 pointers into
   LineStrings, one per link between stream heads, junctions and outlets, with
   Strahler order.

Links, not cells: a network you can attribute, join a gauge to, and give a stage
per reach, which is what `hydraulics.stage` will need.
"""

from __future__ import annotations

import geopandas as gpd
import numpy as np
import numpy.typing as npt
from numba import njit
from pyproj import CRS
from rasterio.transform import Affine
from shapely.geometry import LineString

from floodline.core.config import Config, TerrainConfig
from floodline.core.terrain.flowdir import FLOW_NODATA, downstream_index

__all__ = [
    "link_raster",
    "partition_links",
    "prune_stream_mask",
    "stream_mask",
    "stream_network",
]


def _resolve_terrain(config: Config | TerrainConfig | None) -> TerrainConfig:
    """Return the `TerrainConfig` to use, defaulting when nothing is supplied."""
    if isinstance(config, Config):
        return config.terrain
    return config if config is not None else TerrainConfig()


def stream_mask(
    accumulation: npt.NDArray[np.floating],
    flowdir: npt.NDArray[np.int16],
    *,
    config: Config | TerrainConfig | None = None,
    threshold: float | None = None,
) -> npt.NDArray[np.bool_]:
    """Return a boolean mask of channel cells.

    Parameters
    ----------
    accumulation
        Contributing-cell counts from `terrain.flowacc.flow_accumulation`.
    flowdir
        Matching D8 direction codes; nodata cells are excluded from the mask.
    config
        Source of `stream_threshold_cells`.
    threshold
        Overrides the configured threshold.
    """
    if accumulation.shape != flowdir.shape:
        raise ValueError(
            f"accumulation shape {accumulation.shape} does not match flowdir {flowdir.shape}"
        )
    terrain = _resolve_terrain(config)
    cutoff = terrain.stream_threshold_cells if threshold is None else threshold
    return np.asarray((accumulation >= cutoff) & (flowdir != FLOW_NODATA), dtype=np.bool_)


@njit(cache=True, nogil=True)
def _upstream_counts(
    mask: npt.NDArray[np.bool_], receiver: npt.NDArray[np.int64]
) -> npt.NDArray[np.int64]:
    """Return, per cell, how many *stream* cells drain directly into it."""
    counts = np.zeros(mask.shape[0], dtype=np.int64)
    for cell in range(mask.shape[0]):
        if not mask[cell]:
            continue
        target = receiver[cell]
        if target >= 0 and mask[target]:
            counts[target] += 1
    return counts


@njit(cache=True, nogil=True)
def _prune(
    mask: npt.NDArray[np.bool_],
    receiver: npt.NDArray[np.int64],
    min_length: int,
    max_passes: int,
) -> npt.NDArray[np.bool_]:
    """Remove first-order branches shorter than `min_length`, repeatedly.

    One pass is not enough: removing a stub can turn the junction it hung off into
    a plain link, exposing a second short branch that was not first-order before.
    Iterating to a fixed point is what makes the result independent of the order
    the heads happened to be visited in.
    """
    n_cells = mask.shape[0]
    kept = mask.copy()
    branch = np.empty(max(min_length, 1), dtype=np.int64)

    for _ in range(max_passes):
        upstream = _upstream_counts(kept, receiver)
        removed = 0

        for cell in range(n_cells):
            if not kept[cell] or upstream[cell] != 0:
                continue
            # Walk down from this head, stopping at a junction or the end of the
            # network. Running to `min_length` without stopping means the branch is
            # long enough and nothing needs measuring beyond that.
            length = 0
            walker = cell
            ended = False
            while length < min_length:
                branch[length] = walker
                length += 1
                target = receiver[walker]
                if target < 0 or not kept[target] or upstream[target] > 1:
                    ended = True
                    break
                walker = target
            if not ended:
                continue
            for i in range(length):
                kept[branch[i]] = False
            removed += 1

        if removed == 0:
            break

    return kept


def prune_stream_mask(
    mask: npt.NDArray[np.bool_],
    flowdir: npt.NDArray[np.int16],
    *,
    config: Config | TerrainConfig | None = None,
    min_length: int | None = None,
    max_passes: int = 20,
) -> npt.NDArray[np.bool_]:
    """Drop first-order stream branches shorter than `min_stream_length_cells`.

    Parameters
    ----------
    mask
        Channel mask from `stream_mask`.
    flowdir
        Matching D8 direction codes.
    config
        Source of `min_stream_length_cells`.
    min_length
        Overrides the configured minimum. A value of 1 or less disables pruning.
    max_passes
        Cap on the fixed-point iteration, so a pathological network cannot hang.
    """
    terrain = _resolve_terrain(config)
    minimum = terrain.min_stream_length_cells if min_length is None else min_length
    if minimum <= 1:
        return mask.copy()

    rows, cols = mask.shape
    receiver = np.ascontiguousarray(downstream_index(flowdir)).ravel()
    kept = _prune(np.ascontiguousarray(mask).ravel(), receiver, int(minimum), int(max_passes))
    return kept.reshape(rows, cols)


def partition_links(
    mask_flat: npt.NDArray[np.bool_],
    receiver: npt.NDArray[np.int64],
    upstream: npt.NDArray[np.int64],
) -> list[list[int]]:
    """Split the stream cells into links, each cell belonging to exactly one link.

    A link starts where the network cannot simply continue: at a head (nothing
    upstream) or at a junction (two or more upstream). It runs down to the last
    cell before the next such start.

    The junction belongs to the link it *begins*, not to the tributaries that feed
    it. Letting tributaries share their junction cell would make each of them
    report the junction's accumulation as their own outflow, which includes their
    sibling's water — the attribute would be wrong in exactly the place a reach is
    most interesting.
    """
    links: list[list[int]] = []
    for start in np.flatnonzero(mask_flat & (upstream != 1)):
        cells = [int(start)]
        walker = int(start)
        while True:
            target = int(receiver[walker])
            if target < 0 or not mask_flat[target] or upstream[target] != 1:
                break
            cells.append(target)
            walker = target
        links.append(cells)
    return links


def link_raster(
    mask: npt.NDArray[np.bool_], flowdir: npt.NDArray[np.int16]
) -> tuple[npt.NDArray[np.int64], list[list[int]]]:
    """Return a per-cell link index for the stream network, and the partition itself.

    Off-network cells are -1. Indices are positions in the returned partition, which
    is *not* the same numbering as `stream_network`'s `link_id` — that one drops
    degenerate links and renumbers. Rating curves need every link including the
    degenerate ones, so they use these indices.
    """
    if mask.shape != flowdir.shape:
        raise ValueError(f"mask shape {mask.shape} does not match flowdir {flowdir.shape}")
    rows, cols = mask.shape
    mask_flat = np.ascontiguousarray(mask).ravel()
    receiver = np.ascontiguousarray(downstream_index(flowdir)).ravel()
    upstream = _upstream_counts(mask_flat, receiver)
    links = partition_links(mask_flat, receiver, upstream)

    ids = np.full(rows * cols, -1, dtype=np.int64)
    for index, cells in enumerate(links):
        ids[cells] = index
    return ids.reshape(rows, cols), links


def _strahler_orders(
    links: list[list[int]],
    receiver: npt.NDArray[np.int64],
    mask_flat: npt.NDArray[np.bool_],
    acc_flat: npt.NDArray[np.float64],
) -> list[int]:
    """Return the Strahler order of each link.

    A link with no tributaries is order 1. Otherwise it takes the greatest order
    among its tributaries, plus one when two or more tributaries share that order.

    Links are resolved in ascending order of the accumulation at their head cell,
    which is a valid topological order: a tributary's head carries less water than
    the junction it drains into, so every tributary is resolved before the link it
    feeds. Iterative rather than recursive, because a long river is thousands of
    links deep and Python's recursion limit is a thousand.
    """
    start_to_link = {cells[0]: index for index, cells in enumerate(links)}
    tributaries: list[list[int]] = [[] for _ in links]
    for index, cells in enumerate(links):
        target = int(receiver[cells[-1]])
        if target < 0 or not mask_flat[target]:
            continue
        parent = start_to_link.get(target)
        if parent is not None and parent != index:
            tributaries[parent].append(index)

    orders = [0] * len(links)
    for index in sorted(range(len(links)), key=lambda i: acc_flat[links[i][0]]):
        feeders = [orders[t] for t in tributaries[index]]
        if not feeders or max(feeders) == 0:
            orders[index] = 1
        else:
            highest = max(feeders)
            orders[index] = highest + 1 if feeders.count(highest) > 1 else highest
    return orders


def stream_network(
    mask: npt.NDArray[np.bool_],
    flowdir: npt.NDArray[np.int16],
    accumulation: npt.NDArray[np.floating],
    transform: Affine,
    crs: CRS,
) -> gpd.GeoDataFrame:
    """Vectorise a stream mask into a link network.

    Parameters
    ----------
    mask
        Channel mask, ideally already pruned.
    flowdir
        Matching D8 direction codes.
    accumulation
        Matching contributing-cell counts, used for the per-link attributes.
    transform
        Affine transform of the raster, used to place cell centres.
    crs
        Projected CRS in metres.

    Returns
    -------
    GeoDataFrame
        One row per link, `link_id` numbered contiguously from zero, with:

        ``n_cells``
            Cells belonging to this link. Each stream cell belongs to exactly one.
        ``length_m``
            Length of the LineString, which runs through this link's cell centres
            and on to the first cell of the link below, so the network is
            topologically connected.
        ``acc_head``, ``acc_outflow``
            Contributing cells at the link's first and last *own* cell. Because the
            junction below belongs to the next link, ``acc_outflow`` is this
            reach's own discharge and never includes a sibling tributary's.
        ``strahler``
            Strahler stream order.
        ``terminates``
            True when no stream cell lies downstream: the network's outlet.

        A link whose geometry would have fewer than two distinct points is dropped
        — a lone junction cell sitting on the raster edge, with nothing downstream
        to draw a line to. The count is recorded in
        ``frame.attrs["dropped_degenerate_links"]`` rather than passed over in
        silence.
    """
    if not (mask.shape == flowdir.shape == accumulation.shape):
        raise ValueError("mask, flowdir and accumulation must have the same shape")

    cols = mask.shape[1]
    mask_flat = np.ascontiguousarray(mask).ravel()
    receiver = np.ascontiguousarray(downstream_index(flowdir)).ravel()
    acc_flat = np.ascontiguousarray(accumulation, dtype=np.float64).ravel()
    upstream = _upstream_counts(mask_flat, receiver)

    links = partition_links(mask_flat, receiver, upstream)
    orders = _strahler_orders(links, receiver, mask_flat, acc_flat)

    def centre(cell: int) -> tuple[float, float]:
        x, y = transform * (cell % cols + 0.5, cell // cols + 0.5)
        return float(x), float(y)

    records: list[dict[str, object]] = []
    geometries: list[LineString] = []
    dropped = 0

    for index, cells in enumerate(links):
        points = [centre(cell) for cell in cells]
        outflow = cells[-1]
        target = int(receiver[outflow])
        continues = target >= 0 and bool(mask_flat[target])
        if continues:
            points.append(centre(target))
        if len(points) < 2:
            dropped += 1
            continue

        line = LineString(points)
        records.append(
            {
                "link_id": len(records),
                "n_cells": len(cells),
                "length_m": float(line.length),
                "acc_head": float(acc_flat[cells[0]]),
                "acc_outflow": float(acc_flat[outflow]),
                "strahler": orders[index],
                "terminates": not continues,
            }
        )
        geometries.append(line)

    frame = gpd.GeoDataFrame(records, geometry=geometries, crs=crs)
    if not frame.empty:
        frame = frame.astype({"link_id": "int64", "n_cells": "int64", "strahler": "int64"})
    frame.attrs["dropped_degenerate_links"] = dropped
    return frame
