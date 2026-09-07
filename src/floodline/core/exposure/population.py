"""Population grids against the depth raster: how many people were in the water.

The arithmetic is trivial - sum the population of cells whose depth clears a
threshold - and the number it produces is the least trustworthy in the model. Three
reasons, all of which the caller is told about rather than left to discover:

* **Grids disagree.** Census block groups, WorldPop and Meta HRSL are built from
  different priors and differ by tens of percent over the same ground, and they
  diverge most in small towns, where a single misplaced dwelling moves the count.
  `population_affected` reports one grid; comparing grids is the point of running it
  three times.
* **Resolution mismatch.** A 100 m population cell against a 10 m depth raster is a
  hundred depth cells voting on one population value. `cell_fraction_wet` records
  what fraction of each population cell was actually wet, and `area_weighted` scales
  the count by that instead of treating a cell as all-or-nothing.
* **Night-time residence.** Every one of these grids models where people sleep. A
  daytime flood in a business district is not what they describe.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import numpy.typing as npt

from floodline.core.config import Config, ExposureConfig

__all__ = ["PopulationExposure", "population_affected"]


@dataclass(frozen=True, slots=True)
class PopulationExposure:
    """People in cells the model floods, on one population grid."""

    people_affected: float
    """Population of wet cells. All-or-nothing per cell unless `area_weighted`."""

    people_total: float
    """Population of the whole grid, so the affected share means something."""

    n_cells_affected: int
    cell_fraction_wet: npt.NDArray[np.float64]
    """Fraction of each population cell that was wet, before any threshold."""

    threshold_m: float
    area_weighted: bool

    @property
    def share_affected(self) -> float:
        """Affected people as a fraction of the grid's population."""
        return self.people_affected / self.people_total if self.people_total else 0.0


def _resolve(config: Config | ExposureConfig | None) -> ExposureConfig:
    """Return the `ExposureConfig` to use, defaulting when nothing is supplied."""
    if isinstance(config, Config):
        return config.exposure
    return config if config is not None else ExposureConfig()


def population_affected(
    depth: npt.NDArray[np.floating],
    population: npt.NDArray[np.floating],
    *,
    config: Config | ExposureConfig | None = None,
    threshold_m: float | None = None,
    area_weighted: bool = True,
) -> PopulationExposure:
    """Count the people in cells the model floods.

    Parameters
    ----------
    depth
        Depth raster in metres. NaN counts as dry.
    population
        People per cell, on the *same grid* as `depth`. Resample the population grid
        onto the depth grid before calling; this function will not do it silently,
        because a nearest-neighbour resample of a population count duplicates people
        and a bilinear one invents them, and which is wrong is the caller's problem
        to state.
    threshold_m
        Depth above which a cell counts as affected. Defaults to
        `population_depth_threshold_m`.
    area_weighted
        When true, a cell contributes its population scaled by the fraction of it
        that is wet. When false, a cell is all-or-nothing on the threshold. Only
        meaningful when the population grid is coarser than the depth grid and has
        been resampled up; on a matched grid the fractions are 0 or 1 either way.

    Returns
    -------
    PopulationExposure
    """
    exposure = _resolve(config)
    floor = exposure.population_depth_threshold_m if threshold_m is None else threshold_m

    depths = np.asarray(depth, dtype=np.float64)
    people = np.asarray(population, dtype=np.float64)
    if people.shape != depths.shape:
        raise ValueError(
            f"population shape {people.shape} does not match depth {depths.shape}; "
            "resample the population grid onto the depth grid first"
        )
    if np.any(people[np.isfinite(people)] < 0):
        raise ValueError("population grid has negative counts")

    people = np.where(np.isfinite(people), people, 0.0)
    wet = np.isfinite(depths) & (depths >= floor)
    fraction = wet.astype(np.float64)

    affected = float((people * fraction).sum()) if area_weighted else float(people[wet].sum())

    return PopulationExposure(
        people_affected=affected,
        people_total=float(people.sum()),
        n_cells_affected=int(wet.sum()),
        cell_fraction_wet=fraction,
        threshold_m=float(floor),
        area_weighted=area_weighted,
    )
