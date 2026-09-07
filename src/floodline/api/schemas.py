"""Request and response shapes, and the range checks that happen before any work.

Validation belongs at the boundary, not in the model. A discharge of minus four is not
a modelling question - there is no physics to consult, no upstream to ask, and nothing
useful the pipeline could return. Rejecting it here costs a millisecond and produces an
error that names the field; letting it through costs a DEM fetch and produces a flood
map of nothing, or a stack trace from somewhere in the rating curves.

The bounds are wide on purpose. This is not the place to encode what is hydrologically
plausible for a particular basin - the model has better information about that and says
so in its own output. It is the place to reject what cannot be a discharge at all.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

__all__ = [
    "HealthResponse",
    "ReadyResponse",
    "ScenarioRequest",
    "ScenarioResponse",
    "UpstreamError",
]

# The largest flood ever gauged in the United States is a few hundred thousand cubic
# metres per second at the Mississippi's mouth. A million is comfortably past anything
# real while still rejecting a typo of an extra three digits.
MAX_DISCHARGE_CMS = 1_000_000.0


class ScenarioRequest(BaseModel):
    """One watershed, one discharge, one answer."""

    model_config = ConfigDict(extra="forbid")

    huc: Annotated[
        str,
        Field(
            min_length=2,
            max_length=16,
            pattern=r"^\d+$",
            description="Hydrologic unit code. Digits only, even length, 2 to 16.",
            examples=["1204010403"],
        ),
    ]
    discharge_cms: Annotated[
        float,
        Field(
            gt=0.0,
            le=MAX_DISCHARGE_CMS,
            description=(
                "Discharge at the basin outlet in cubic metres per second. Must be "
                "positive: a flood model driven by no water has nothing to say, and "
                "zero is more often a missing value than an intended one."
            ),
            examples=[1433.0],
        ),
    ]
    resolution_m: Annotated[
        float,
        Field(
            default=30.0,
            ge=1.0,
            le=100.0,
            description="Analysis cell size. Finer costs more and, measured, does not "
            "improve the headline error.",
        ),
    ] = 30.0

    @field_validator("huc")
    @classmethod
    def _even_length(cls, value: str) -> str:
        """Reject an odd-length code, which is not a hydrologic unit at any level.

        Levels go 2, 4, 6, 8, 10, 12, 14, 16 digits. An odd length is a typo, and
        catching it here turns a confusing upstream 404 into a clear 422.
        """
        if len(value) % 2:
            raise ValueError(
                f"a hydrologic unit code has an even number of digits; {value!r} has {len(value)}"
            )
        return value


class ScenarioResponse(BaseModel):
    """What one discharge does to one watershed."""

    huc: str
    name: str
    discharge_cms: float
    resolution_m: float

    flooded_km2: float
    basin_km2: float
    flooded_fraction: float
    max_depth_m: float
    wet_cells: int

    terrain_cached: bool
    """Whether the expensive half was served from the store. The difference between a
    request that fetched a DEM and one that did not is tens of seconds, and a caller
    timing us deserves to know which happened."""

    params_hash: str
    terrain_seconds: float
    scenario_seconds: float
    total_seconds: float
    notes: list[str] = Field(default_factory=list)


class HealthResponse(BaseModel):
    """Liveness: is this process running.

    Deliberately answers without touching the database or the filesystem. A liveness
    probe that checks dependencies restarts a healthy container because something else
    is down, which turns one outage into two.
    """

    ok: Literal[True] = True
    service: str = "floodline"
    version: str


class ReadyResponse(BaseModel):
    """Readiness: can this process actually serve a request.

    The opposite of liveness, and it does check dependencies. Reports each one
    separately so an operator reading a failure knows which thing to go and look at.
    """

    ready: bool
    checks: dict[str, Any]


class UpstreamError(BaseModel):
    """A public data source failed, and the response says which and what to do.

    Upstream availability is the single largest source of failure this service has:
    over one week of development the elevation API, the boundary service, the object
    store and FEMA's endpoint were each unreachable or rate-limiting at some point. A
    generic 500 sends the reader looking for a bug in the model. Naming the source and
    whether retrying is worthwhile is the difference between a useful error and a
    puzzle.
    """

    detail: str
    upstream: str
    retryable: bool
