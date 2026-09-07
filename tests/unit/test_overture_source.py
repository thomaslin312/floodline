"""Pure parts of the Overture reader: class mapping, cache keys, bbox validation.

The S3 read is not covered here. It needs the live bucket, takes minutes, and a
mocked pyarrow dataset would test pyarrow rather than this module.
"""

from __future__ import annotations

import pytest

from floodline.core.config import DamageConfig
from floodline.io.overture import OVERTURE_CLASSES, fetch_overture_buildings


def test_every_mapped_class_is_priced() -> None:
    priced = set(DamageConfig().replacement_cost_per_m2)
    assert set(OVERTURE_CLASSES.values()) <= priced


def test_the_obvious_subtypes_map_where_you_would_expect() -> None:
    assert OVERTURE_CLASSES["residential"] == "residential"
    assert OVERTURE_CLASSES["industrial"] == "industrial"
    assert OVERTURE_CLASSES["commercial"] == "commercial"
    # Civic and religious buildings are priced, just not as any of the three.
    assert OVERTURE_CLASSES["civic"] == "other"


def test_schools_and_hospitals_are_priced_as_commercial_not_dropped() -> None:
    assert OVERTURE_CLASSES["education"] == "commercial"
    assert OVERTURE_CLASSES["medical"] == "commercial"


@pytest.mark.parametrize(
    "bbox",
    [
        (-95.0, 29.0, -95.0, 30.0),  # zero width
        (-95.0, 29.0, -96.0, 30.0),  # inverted in x
        (-96.0, 30.0, -95.0, 29.0),  # inverted in y
    ],
)
def test_an_empty_or_inverted_bbox_is_refused_before_any_network_call(
    bbox: tuple[float, float, float, float],
) -> None:
    with pytest.raises(ValueError, match="empty or inverted"):
        fetch_overture_buildings(bbox)
