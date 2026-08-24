"""Render pipeline outputs to web-sized PNG layers with the georeferencing to place them.

A 10 m watershed is around ten million cells, which no browser will accept, so every
layer is block-reduced to a target width before it is written. The reduction is
per-layer on purpose:

* Continuous surfaces (elevation, HAND, depth) reduce by **mean**, which is what
  "the value around here" means for them.
* Masks (streams, wet extent) reduce by **max**, because a stream is one cell wide
  and averaging would erase it. A mean-reduced stream network vanishes at exactly
  the zoom level someone wants to look at it.

Every layer in one call shares a single reduction factor, so the images are pixel
aligned and a point placed on one is placed on all of them.
"""

from __future__ import annotations

import json
import warnings
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import numpy as np
import numpy.typing as npt
from matplotlib import colormaps
from matplotlib import image as mpimg
from matplotlib.colors import Normalize
from rasterio.transform import Affine

__all__ = ["LayerSet", "RenderedLayer", "block_reduce", "render_layers"]


@dataclass(frozen=True, slots=True)
class RenderedLayer:
    """One PNG, and what it shows."""

    name: str
    file: str
    label: str
    kind: str
    """`continuous` or `mask`, which is how it was reduced."""
    vmin: float | None = None
    vmax: float | None = None
    units: str = ""
    colormap: str = ""


@dataclass(frozen=True, slots=True)
class LayerSet:
    """A pixel-aligned stack of layers plus everything needed to georeference them."""

    width: int
    height: int
    reduction: int
    """Source cells per rendered pixel, along each axis."""
    bounds: tuple[float, float, float, float]
    """(west, south, east, north) in the analysis CRS."""
    crs: str
    layers: list[RenderedLayer] = field(default_factory=list)
    points: dict[str, Any] = field(default_factory=dict)
    series: dict[str, Any] = field(default_factory=dict)
    notes: dict[str, Any] = field(default_factory=dict)

    def to_json(self, path: Path) -> Path:
        """Write the manifest beside the images."""
        path.write_text(json.dumps(asdict(self), indent=2, default=float))
        return path


def block_reduce(
    array: npt.NDArray[np.floating], factor: int, *, how: str = "mean"
) -> npt.NDArray[np.float64]:
    """Reduce `array` by an integer factor, ignoring NaN.

    `how="max"` preserves one-cell features such as a stream network, which a mean
    would average away to nothing. `how="mean"` is right for a continuous surface, and
    `how="sum"` for a per-cell total such as damage or a building count, where a mean
    would quietly divide the watershed's total by the block area.
    """
    if factor < 1:
        raise ValueError(f"factor must be at least 1, got {factor}")
    if how not in {"mean", "max", "sum"}:
        raise ValueError(f"how must be 'mean', 'max' or 'sum', got {how!r}")
    data = np.asarray(array, dtype=np.float64)
    if factor == 1:
        return data

    rows = (data.shape[0] // factor) * factor
    cols = (data.shape[1] // factor) * factor
    trimmed = data[:rows, :cols].reshape(rows // factor, factor, cols // factor, factor)
    # A block wholly outside the domain is all-NaN, and reducing it warns. That is
    # expected here -- a watershed does not fill its bounding box -- and NaN is the
    # right answer, so the warning is suppressed rather than worked around.
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", RuntimeWarning)
        if how == "max":
            return np.asarray(np.nanmax(trimmed, axis=(1, 3)), dtype=np.float64)
        if how == "sum":
            return np.asarray(np.nansum(trimmed, axis=(1, 3)), dtype=np.float64)
        return np.asarray(np.nanmean(trimmed, axis=(1, 3)), dtype=np.float64)


def _write_png(
    path: Path,
    values: npt.NDArray[np.float64],
    *,
    colormap: str,
    vmin: float,
    vmax: float,
    alpha_from: npt.NDArray[np.bool_] | None = None,
) -> None:
    """Colour-map `values` and write RGBA, transparent where there is nothing to show."""
    normalise = Normalize(vmin=vmin, vmax=vmax, clip=True)
    rgba = colormaps[colormap](normalise(np.nan_to_num(values, nan=vmin)))
    visible = np.isfinite(values) if alpha_from is None else alpha_from
    rgba[..., 3] = visible.astype(np.float64)
    path.parent.mkdir(parents=True, exist_ok=True)
    mpimg.imsave(path, rgba)


def render_layers(
    out_dir: Path,
    *,
    transform: Affine,
    crs: str,
    continuous: dict[str, tuple[npt.NDArray[np.floating], str, str, str]],
    masks: dict[str, tuple[npt.NDArray[np.bool_], str, str]],
    target_width: int = 1100,
) -> LayerSet:
    """Render a pixel-aligned layer stack sized for a browser.

    Parameters
    ----------
    out_dir
        Directory for the PNGs and the manifest.
    transform, crs
        Georeferencing of the *source* arrays. The manifest reports the bounds so a
        page can place geographic points onto the images.
    continuous
        `name -> (array, label, units, colormap)` for surfaces reduced by mean.
    masks
        `name -> (array, label, colormap)` for masks reduced by max.
    target_width
        Rendered width in pixels. The reduction factor is derived from it, and every
        layer shares it so the images stay aligned.

    Returns
    -------
    LayerSet
    """
    if not continuous and not masks:
        raise ValueError("nothing to render")
    if continuous:
        source_rows, source_cols = next(iter(continuous.values()))[0].shape
    else:
        source_rows, source_cols = next(iter(masks.values()))[0].shape
    factor = max(1, int(np.ceil(source_cols / target_width)))

    layers: list[RenderedLayer] = []
    height = width = 0

    for name, (array, label, units, colormap) in continuous.items():
        reduced = block_reduce(array, factor, how="mean")
        height, width = reduced.shape
        finite = reduced[np.isfinite(reduced)]
        vmin = float(finite.min()) if finite.size else 0.0
        vmax = float(finite.max()) if finite.size else 1.0
        if vmax <= vmin:
            vmax = vmin + 1.0
        _write_png(out_dir / f"{name}.png", reduced, colormap=colormap, vmin=vmin, vmax=vmax)
        layers.append(
            RenderedLayer(
                name=name,
                file=f"{name}.png",
                label=label,
                kind="continuous",
                vmin=vmin,
                vmax=vmax,
                units=units,
                colormap=colormap,
            )
        )

    for name, (mask, label, colormap) in masks.items():
        reduced = block_reduce(mask.astype(np.float64), factor, how="max")
        height, width = reduced.shape
        present = np.nan_to_num(reduced) > 0
        _write_png(
            out_dir / f"{name}.png",
            np.where(present, 1.0, 0.0),
            colormap=colormap,
            vmin=0.0,
            vmax=1.0,
            alpha_from=present,
        )
        layers.append(
            RenderedLayer(
                name=name, file=f"{name}.png", label=label, kind="mask", colormap=colormap
            )
        )

    west, north = transform * (0, 0)
    east, south = transform * (source_cols, source_rows)
    return LayerSet(
        width=width,
        height=height,
        reduction=factor,
        bounds=(float(west), float(south), float(east), float(north)),
        crs=crs,
        layers=layers,
    )
