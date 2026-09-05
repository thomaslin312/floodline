"""Replacement cost per building: floor area times a rate per square metre.

Deliberately the simplest defensible model, because everything it could do instead
needs data that is not open. Value at risk is `footprint area x storeys x rate`,
where the rate comes from `replacement_cost_per_m2` keyed by building class.

Three things this is not. It is not market value - replacement cost is what
rebuilding costs, which is the quantity depth-damage curves are fractions of. It is
not content value, which for residential buildings runs at roughly a third again on
top and is excluded here rather than guessed. And it does not depreciate: an old
house and a new one of the same size are priced identically, because building age is
not in any open footprint database at national scale.

Multiplying floor area by storeys assumes damage is spread evenly through the
building, which is wrong in the direction of over-estimating: a metre of water in a
three-storey building damages the ground floor, not all three. `storey_exposure`
caps the storeys the curve is applied to, which is closer to right and is the
default; the uncapped behaviour is available for comparison with published figures
that use it.
"""

from __future__ import annotations

import numpy as np
import numpy.typing as npt

from floodline.config import Config, DamageConfig, ExposureConfig

__all__ = ["exposed_value", "storey_exposure"]


def _exposure(config: Config | ExposureConfig | None) -> ExposureConfig:
    """Return the `ExposureConfig` to use, defaulting when nothing is supplied."""
    if isinstance(config, Config):
        return config.exposure
    return config if config is not None else ExposureConfig()


def _resolve(config: Config | DamageConfig | None) -> DamageConfig:
    """Return the `DamageConfig` to use, defaulting when nothing is supplied."""
    if isinstance(config, Config):
        return config.damage
    return config if config is not None else DamageConfig()


def storey_exposure(
    depth_m: npt.ArrayLike,
    storeys: npt.ArrayLike,
    *,
    storey_height_m: float | None = None,
    config: Config | ExposureConfig | None = None,
) -> npt.NDArray[np.float64]:
    """Return how many storeys the water can plausibly reach, as a fraction.

    A building's damage fraction applies to the part of it the water is in. Water
    0.8 m deep in a two-storey house reaches one storey, so half the floor area is
    at risk, not all of it. The result is in [1/storeys, 1] - the ground floor is
    always fully exposed once water is above the floor.
    """
    height = storey_height_m if storey_height_m is not None else _exposure(config).storey_height_m
    depths = np.asarray(depth_m, dtype=np.float64)
    counts = np.maximum(np.asarray(storeys, dtype=np.float64), 1.0)
    reached = np.ceil(np.maximum(depths, 0.0) / height)
    reached = np.clip(reached, 1.0, counts)
    return np.asarray(reached / counts, dtype=np.float64)


def exposed_value(
    floor_area_m2: npt.ArrayLike,
    building_class: npt.ArrayLike,
    *,
    config: Config | DamageConfig | None = None,
    cost_scale: float = 1.0,
) -> npt.NDArray[np.float64]:
    """Return the replacement value of each building.

    Parameters
    ----------
    floor_area_m2
        Footprint area times storeys.
    building_class
        Class per building; anything not priced in `replacement_cost_per_m2` falls
        back to `default_class` rather than raising, since footprint databases carry
        long tails of classes no cost table anticipates.
    cost_scale
        Multiplier on every rate. The Monte Carlo uses it to sample cost error; at
        1.0 the result is the point estimate.

    Returns
    -------
    Replacement value per building, in the currency of `replacement_cost_per_m2`.
    """
    damage = _resolve(config)
    areas = np.asarray(floor_area_m2, dtype=np.float64)
    classes = np.asarray(building_class, dtype=object)
    if classes.shape != areas.shape:
        raise ValueError(f"class shape {classes.shape} does not match area {areas.shape}")

    rates = np.full(areas.shape, damage.replacement_cost_per_m2[damage.default_class])
    for name in {str(c) for c in classes.ravel()}:
        rate = damage.replacement_cost_per_m2.get(name)
        if rate is not None:
            rates[classes == name] = rate

    return np.asarray(areas * rates * cost_scale, dtype=np.float64)
