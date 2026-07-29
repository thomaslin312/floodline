"""Vector I/O: GeoParquet in, GeoParquet out.

Same CRS policy as the raster side. A geographic CRS is an error, not a warning,
and a layer whose CRS differs from the analysis CRS is an error unless
reprojection has been explicitly allowed.

GeoParquet everywhere, no shapefiles: typed columns, no 10-character field-name
truncation, no silent geometry-type coercion, and it round-trips a CRS properly.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import geopandas as gpd

from floodline.config import Config
from floodline.io.raster import CrsError, require_projected_crs

__all__ = ["read_vector", "write_vector"]


def read_vector(
    path: Path | str,
    *,
    config: Config | None = None,
    columns: list[str] | None = None,
) -> gpd.GeoDataFrame:
    """Read a GeoParquet file, refusing anything not in a projected CRS in metres.

    Parameters
    ----------
    path
        GeoParquet file to read.
    config
        If given and `config.crs.allow_reprojection` is False, a CRS that differs
        from `config.crs.analysis` is an error rather than a silent mismatch.
    columns
        Optional subset of columns to read; the geometry column is always included.
    """
    path = Path(path)
    frame = gpd.read_parquet(path, columns=columns)
    crs = require_projected_crs(frame.crs, source=str(path))

    if config is not None and not config.crs.allow_reprojection:
        analysis = config.crs.analysis
        if not crs.equals(analysis):
            raise CrsError(
                f"{path} is in {crs.to_string()} but the analysis CRS is "
                f"{analysis.to_string()}. Reproject it explicitly, or set "
                "crs.allow_reprojection."
            )
    return frame


def write_vector(
    path: Path | str,
    frame: gpd.GeoDataFrame,
    *,
    config: Config | None = None,
    overwrite: bool = True,
    **kwargs: Any,
) -> Path:
    """Write `frame` as GeoParquet, refusing a geographic or missing CRS.

    Parameters
    ----------
    path
        Output path.
    frame
        The layer to write. Its CRS is re-checked; geographic CRSs never reach disk.
    config
        Unused today beyond symmetry with `write_cog`; kept so callers pass the
        same object through the whole pipeline.
    overwrite
        If False, an existing `path` is an error.
    **kwargs
        Passed through to `GeoDataFrame.to_parquet`.
    """
    path = Path(path)
    if path.exists() and not overwrite:
        raise FileExistsError(path)
    require_projected_crs(frame.crs, source=str(path))
    path.parent.mkdir(parents=True, exist_ok=True)
    frame.to_parquet(path, **kwargs)
    return path
