"""Population grids, read by window over HTTP.

Two open products, both readable without a credential, both fetched the same way the
DEM tiles are: a `/vsicurl/` range read of the window that matters rather than a
download of the whole raster.

* **WorldPop** - 100 m constrained (built-up areas only, Maxar/BSGM) and 100 m
  unconstrained. People per pixel, UN-adjusted. Best resolution of the open options.
* **GHS-POP** - the JRC's global layer, 100 m on the World Mollweide grid. An
  independent estimate built from a different settlement layer, which is the point of
  having it: `population_affected` run against both is the disagreement experiment.

**Neither product can be windowed over HTTP, which decides the design.** WorldPop's
server advertises `Accept-Ranges: bytes` and then answers a Range request with 200 and
the whole body; GDAL reports this as "Range downloading not supported by this server!".
GHS-POP ships as a zip whose central directory sits at the end of a multi-gigabyte
file, so `/vsizip//vsicurl/` spends longer seeking than downloading. The DEM trick of
reading only the window that matters does not transfer, so the national raster is
fetched once to `data/cache` and windowed locally afterwards - 494 MB for WorldPop USA
2020 constrained, paid once. That download is opt-in: nothing here starts half a
gigabyte of traffic without being asked.

**The US Census route needs a key.** ACS block groups would be the better US source -
authoritative, and vector geometry allows a proper dasymetric split - but the Census
Data API now answers keyless block-group queries with "Missing Key". Set
`CENSUS_API_KEY` and it becomes available; until then it is recorded as unavailable
rather than worked around.

**LandScan is not here, and that is not an oversight.** ORNL publishes LandScan Global
and LandScan USA under CC BY, but every download path is behind a registration form -
a direct request returns 403. The project's rule is that a source needing a login is
recorded as unavailable rather than worked around, so LandScan is named here and left
out. If a copy is obtained by hand, `read_population_window` will read it like any
other raster; nothing about the pipeline depends on where the grid came from.

**These grids disagree, and that is the finding, not a defect.** They model where
people sleep, from different settlement priors, and diverge by tens of percent - most
of all in small towns, where one misplaced dwelling moves the count. Over a metro the
size of Houston they agree far better, which is the main thing given up by choosing
Harvey as the primary case.
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any

import httpx
import numpy as np
import rasterio
from rasterio.crs import CRS
from rasterio.enums import Resampling
from rasterio.vrt import WarpedVRT
from rasterio.warp import transform_bounds
from rasterio.windows import from_bounds

from floodline.io.raster import Raster

__all__ = [
    "PopulationGrid",
    "PopulationNotLocalError",
    "PopulationProduct",
    "ensure_population_raster",
    "population_url",
    "read_population_window",
]


class PopulationNotLocalError(RuntimeError):
    """The product is not cached locally and downloading was not permitted."""


class PopulationProduct(StrEnum):
    """Open population products this module can read."""

    WORLDPOP_CONSTRAINED = "worldpop_constrained"
    WORLDPOP_UNCONSTRAINED = "worldpop_unconstrained"
    GHS_POP = "ghs_pop"


_WORLDPOP = "https://data.worldpop.org/GIS/Population"
_GHS = (
    "https://jeodpp.jrc.ec.europa.eu/ftp/jrc-opendata/GHSL/GHS_POP_GLOBE_R2023A/"
    "GHS_POP_E{year}_GLOBE_R2023A_54009_100/V1-0/GHS_POP_E{year}_GLOBE_R2023A_54009_100_V1_0.zip"
)


@dataclass(frozen=True, slots=True)
class PopulationGrid:
    """A population window resampled onto a target grid."""

    raster: Raster
    """People per cell, on the grid that was asked for."""

    product: PopulationProduct
    url: str
    total_people: float
    """Sum over the window. Compare against the product's own national total before
    trusting it: a resample that duplicates or drops people shows up here first."""

    resampled: bool
    """True when the source grid did not already match the target."""


def population_url(
    product: PopulationProduct,
    *,
    iso3: str = "USA",
    year: int = 2020,
) -> str:
    """Return the public URL for one product.

    WorldPop paths are per country and per year; GHS-POP is a single global raster
    per epoch. Both are anonymous.
    """
    lower = iso3.lower()
    if product is PopulationProduct.WORLDPOP_CONSTRAINED:
        return (
            f"{_WORLDPOP}/Global_2000_2020_Constrained/{year}/BSGM/{iso3}/"
            f"{lower}_ppp_{year}_UNadj_constrained.tif"
        )
    if product is PopulationProduct.WORLDPOP_UNCONSTRAINED:
        return f"{_WORLDPOP}/Global_2000_2020/{year}/{iso3}/{lower}_ppp_{year}_UNadj.tif"
    return _GHS.format(year=year)


def ensure_population_raster(
    product: PopulationProduct = PopulationProduct.WORLDPOP_CONSTRAINED,
    *,
    iso3: str = "USA",
    year: int = 2020,
    cache_dir: Path = Path("data/cache"),
    download: bool = False,
    client: Any | None = None,
) -> Path:
    """Return a local path to the national raster, downloading it once if allowed.

    Raises `PopulationNotLocalError` rather than downloading silently. WorldPop USA
    2020 constrained is 494 MB, and a function that quietly spends that is one nobody
    can call from a script safely.
    """
    source = population_url(product, iso3=iso3, year=year)
    target = cache_dir / f"{product.value}-{iso3.lower()}-{year}{Path(source).suffix}"
    if target.exists() and target.stat().st_size > 0:
        return target
    if not download:
        raise PopulationNotLocalError(
            f"{product.value} for {iso3} {year} is not cached at {target}. It cannot be "
            "windowed over HTTP - the server does not honour Range - so the whole raster "
            "has to be fetched once. Run `floodline fetch-population`, or pass "
            f"download=True. Source: {source}"
        )

    target.parent.mkdir(parents=True, exist_ok=True)
    partial = target.with_suffix(target.suffix + ".part")
    owned = client is None
    active = client or httpx.Client(timeout=httpx.Timeout(60.0, read=600.0), follow_redirects=True)
    try:
        with active.stream("GET", source) as response:
            response.raise_for_status()
            with partial.open("wb") as handle:
                for chunk in response.iter_bytes(chunk_size=1 << 20):
                    handle.write(chunk)
    finally:
        if owned:
            active.close()
    shutil.move(str(partial), str(target))
    return target


def read_population_window(
    like: Raster,
    *,
    product: PopulationProduct = PopulationProduct.WORLDPOP_CONSTRAINED,
    iso3: str = "USA",
    year: int = 2020,
    url: str | None = None,
    cache_dir: Path = Path("data/cache"),
    download: bool = False,
) -> PopulationGrid:
    """Read a population product onto the grid of `like`.

    Parameters
    ----------
    like
        The raster to match - normally the depth raster, so `population_affected` can
        be handed both without resampling anything itself.
    url
        Overrides the derived URL, for a local file or a hand-fetched product.

    Returns
    -------
    PopulationGrid

    Notes
    -----
    Resampling uses `Resampling.sum`, not bilinear or nearest. A population raster
    holds counts, not a field: bilinear invents people at the edges and nearest
    duplicates them when upsampling. Summing conserves the total, which is the only
    behaviour that leaves the number meaning what it says. Where the target grid is
    finer than the source, the sum still spreads a source cell's people across the
    fine cells that fall inside it rather than repeating the whole count in each.
    """
    if url is not None:
        source = url
    else:
        source = str(
            ensure_population_raster(
                product, iso3=iso3, year=year, cache_dir=cache_dir, download=download
            )
        )
    href = f"/vsicurl/{source}" if source.startswith("http") else source

    rows, cols = like.data.shape
    with rasterio.open(href) as src:
        target_crs = CRS.from_wkt(like.crs.to_wkt())
        same_grid = src.crs == target_crs and src.transform.almost_equals(like.transform)
        if same_grid:
            west, south, east, north = like.bounds
            window = from_bounds(west, south, east, north, src.transform)
            data = src.read(1, window=window, out_shape=(rows, cols), boundless=True, fill_value=0)
            resampled = False
        else:
            # Pinned to the output grid: an unpinned VRT over a national raster is a
            # warp grid of hundreds of millions of cells for a 10 km window.
            with WarpedVRT(
                src,
                crs=target_crs,
                transform=like.transform,
                width=cols,
                height=rows,
                resampling=Resampling.sum,
            ) as vrt:
                data = vrt.read(1)
            resampled = True

        nodata = src.nodata

    grid = np.asarray(data, dtype=np.float64)
    if nodata is not None:
        grid = np.where(grid == nodata, 0.0, grid)
    # WorldPop marks sea and no-data with large negatives rather than its declared
    # nodata in places; a negative count is never meaningful.
    grid = np.where(np.isfinite(grid) & (grid > 0), grid, 0.0)

    return PopulationGrid(
        raster=like.with_data(grid),
        product=product,
        url=source,
        total_people=float(grid.sum()),
        resampled=resampled,
    )


def bounds_wgs84(like: Raster) -> tuple[float, float, float, float]:
    """Return `like`'s bounds as WGS84 degrees, for services that want a lat/lon box."""
    west, south, east, north = like.bounds
    lon_lat = transform_bounds(
        CRS.from_wkt(like.crs.to_wkt()), CRS.from_epsg(4326), west, south, east, north
    )
    return (float(lon_lat[0]), float(lon_lat[1]), float(lon_lat[2]), float(lon_lat[3]))
