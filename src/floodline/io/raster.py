"""Raster I/O: windowed reads, COG writes, and refusal of geographic CRSs.

Two rules this module enforces so nothing downstream has to:

* A raster whose CRS is geographic (or missing) is an error. Cell sizes in degrees
  make every slope, area and distance in the terrain code silently wrong.
* Outputs are Cloud-Optimised GeoTIFFs with nodata set.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
import numpy.typing as npt
import rasterio
from pyproj import CRS
from rasterio.crs import CRS as RioCRS
from rasterio.enums import Resampling
from rasterio.shutil import copy as rio_copy
from rasterio.transform import Affine, array_bounds
from rasterio.windows import Window

from floodline.config import Config, RasterConfig, validate_projected_crs

__all__ = [
    "CrsError",
    "Raster",
    "iter_windows",
    "read_raster",
    "require_projected_crs",
    "write_cog",
]


class CrsError(ValueError):
    """Raised when a raster's CRS is missing, geographic, or not in metres."""


@dataclass(frozen=True, slots=True)
class Raster:
    """An in-memory raster and the georeferencing needed to write it back out.

    Attributes
    ----------
    data
        2-D array of values. Nodata cells hold `nodata` (or NaN for float rasters
        read with `masked=True` and filled).
    transform
        Affine transform mapping (col, row) to CRS coordinates.
    crs
        Projected CRS in metres.
    nodata
        Nodata value, or None if the source declared none.
    """

    data: npt.NDArray[Any]
    transform: Affine
    crs: CRS
    nodata: float | None = None

    @property
    def shape(self) -> tuple[int, int]:
        """Return (rows, cols)."""
        rows, cols = self.data.shape[-2:]
        return int(rows), int(cols)

    @property
    def cellsize(self) -> tuple[float, float]:
        """Return (x_size, y_size) in CRS units (metres), both positive."""
        return abs(self.transform.a), abs(self.transform.e)

    @property
    def cell_area_m2(self) -> float:
        """Return the area of one cell in square metres."""
        x_size, y_size = self.cellsize
        return x_size * y_size

    @property
    def bounds(self) -> tuple[float, float, float, float]:
        """Return (west, south, east, north) in CRS units."""
        rows, cols = self.shape
        west, south, east, north = array_bounds(rows, cols, self.transform)
        return float(west), float(south), float(east), float(north)

    def valid_mask(self) -> npt.NDArray[np.bool_]:
        """Return a boolean mask that is True where the cell holds real data."""
        if np.issubdtype(self.data.dtype, np.floating):
            finite = np.isfinite(self.data)
            if self.nodata is None:
                return finite
            return np.asarray(finite & (self.data != self.nodata), dtype=np.bool_)
        if self.nodata is None:
            return np.ones(self.data.shape, dtype=np.bool_)
        return np.asarray(self.data != self.nodata, dtype=np.bool_)

    def with_data(self, data: npt.NDArray[Any], nodata: float | None = None) -> Raster:
        """Return a copy carrying `data`, keeping this raster's georeferencing."""
        if data.shape[-2:] != self.data.shape[-2:]:
            raise ValueError(
                f"shape mismatch: {data.shape[-2:]} does not match {self.data.shape[-2:]}"
            )
        return Raster(
            data=data,
            transform=self.transform,
            crs=self.crs,
            nodata=self.nodata if nodata is None else nodata,
        )


def require_projected_crs(crs: RioCRS | CRS | str | int | None, *, source: str = "raster") -> CRS:
    """Return `crs` as a pyproj `CRS`, refusing missing, geographic or non-metre CRSs.

    Parameters
    ----------
    crs
        The CRS to check. `None` (an ungeoreferenced raster) is an error.
    source
        Human-readable name of what is being checked, used in the message.

    Raises
    ------
    CrsError
        If the CRS is absent, unparseable, geographic, or not in metres.
    """
    if crs is None:
        raise CrsError(
            f"{source} has no CRS. floodline will not guess one; "
            "assign a projected CRS in metres (for Houston: EPSG:6587)."
        )
    try:
        return validate_projected_crs(crs.to_wkt() if isinstance(crs, RioCRS) else crs)
    except ValueError as exc:
        raise CrsError(f"{source}: {exc}") from exc


def read_raster(
    path: Path | str,
    *,
    band: int = 1,
    window: Window | None = None,
    config: Config | None = None,
    masked: bool = True,
    dtype: npt.DTypeLike | None = None,
) -> Raster:
    """Read a raster band, refusing anything not in a projected CRS in metres.

    Parameters
    ----------
    path
        Raster to read.
    band
        1-based band index.
    window
        Optional read window; the returned transform is the window's transform.
    config
        If given and `config.crs.allow_reprojection` is False, a CRS that differs
        from `config.crs.analysis` is an error rather than a silent mismatch.
    masked
        Read as a masked array and fill masked cells with NaN (float outputs) or
        the source nodata (integer outputs).
    dtype
        Cast the result to this dtype.

    Returns
    -------
    Raster
    """
    path = Path(path)
    with rasterio.open(path) as src:
        crs = require_projected_crs(src.crs, source=str(path))
        if config is not None and not config.crs.allow_reprojection:
            analysis = config.crs.analysis
            if not crs.equals(analysis):
                raise CrsError(
                    f"{path} is in {crs.to_string()} but the analysis CRS is "
                    f"{analysis.to_string()}. Reproject it explicitly, or set "
                    "crs.allow_reprojection."
                )
        array = src.read(band, window=window, masked=masked)
        transform = src.window_transform(window) if window is not None else src.transform
        nodata = src.nodata

    if masked and np.ma.isMaskedArray(array):
        if np.issubdtype(array.dtype, np.floating):
            data = array.filled(np.nan)
        else:
            fill = nodata if nodata is not None else 0
            data = array.filled(fill)
    else:
        data = np.asarray(array)

    if dtype is not None:
        data = data.astype(dtype, copy=False)
    return Raster(data=np.ascontiguousarray(data), transform=transform, crs=crs, nodata=nodata)


def iter_windows(shape: tuple[int, int], block_size: int) -> Iterator[Window]:
    """Yield row-major square windows of at most `block_size` covering `shape`."""
    if block_size <= 0:
        raise ValueError(f"block_size must be positive, got {block_size}")
    rows, cols = shape
    for row_off in range(0, rows, block_size):
        height = min(block_size, rows - row_off)
        for col_off in range(0, cols, block_size):
            width = min(block_size, cols - col_off)
            yield Window(col_off=col_off, row_off=row_off, width=width, height=height)


def write_cog(
    path: Path | str,
    raster: Raster,
    *,
    config: Config | RasterConfig | None = None,
    dtype: npt.DTypeLike | None = None,
    overwrite: bool = True,
) -> Path:
    """Write `raster` as a Cloud-Optimised GeoTIFF with nodata set.

    The array is written to a tiled temporary GeoTIFF beside the target, given
    overviews, then copied with `driver="COG"` so the result passes a COG validator.

    Parameters
    ----------
    path
        Output path.
    raster
        Raster to write. Its CRS is re-checked; geographic CRSs never reach disk.
    config
        Source of nodata, block size, compression and overview settings.
    dtype
        Cast before writing. Defaults to the array's dtype.
    overwrite
        If False, an existing `path` is an error.

    Returns
    -------
    Path
        The path written.
    """
    raster_config = (
        config.raster if isinstance(config, Config) else (config if config else RasterConfig())
    )
    path = Path(path)
    if path.exists() and not overwrite:
        raise FileExistsError(path)
    path.parent.mkdir(parents=True, exist_ok=True)

    crs = require_projected_crs(raster.crs, source=str(path))
    data = raster.data if dtype is None else raster.data.astype(dtype, copy=False)
    if data.ndim != 2:
        raise ValueError(f"write_cog expects a 2-D array, got shape {data.shape}")

    nodata = raster.nodata if raster.nodata is not None else raster_config.nodata
    if np.issubdtype(data.dtype, np.floating):
        data = np.where(np.isnan(data), nodata, data).astype(data.dtype, copy=False)

    rows, cols = data.shape
    profile: dict[str, Any] = {
        "driver": "GTiff",
        "height": rows,
        "width": cols,
        "count": 1,
        "dtype": data.dtype.name,
        "crs": RioCRS.from_wkt(crs.to_wkt()),
        "transform": raster.transform,
        "nodata": nodata,
        "tiled": True,
        "blockxsize": raster_config.block_size,
        "blockysize": raster_config.block_size,
        "compress": raster_config.compress,
        "BIGTIFF": "YES" if raster_config.bigtiff else "IF_SAFER",
    }
    if raster_config.compress.lower() in {"deflate", "lzw", "zstd"}:
        profile["predictor"] = raster_config.predictor

    tmp = path.with_suffix(path.suffix + ".tmp.tif")
    resampling = Resampling[raster_config.overview_resampling]
    try:
        with rasterio.open(tmp, "w", **profile) as dst:
            dst.write(data, 1)
            levels = [lvl for lvl in raster_config.overview_levels if min(rows, cols) // lvl >= 1]
            if levels:
                dst.build_overviews(levels, resampling)
        rio_copy(
            tmp,
            path,
            driver="COG",
            compress=raster_config.compress,
            blocksize=raster_config.block_size,
            overview_resampling=raster_config.overview_resampling,
            BIGTIFF="YES" if raster_config.bigtiff else "IF_SAFER",
        )
    finally:
        tmp.unlink(missing_ok=True)
    return path
