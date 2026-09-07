"""Configuration models.

Every numeric threshold, tolerance, CRS and curve selection used anywhere in the
pipeline is a field on one of these models. Algorithm code never contains a
literal that a user might reasonably want to change.
"""

from __future__ import annotations

import tomllib
from datetime import date
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Any, Self

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, PlainSerializer, model_validator
from pyproj import CRS
from pyproj.exceptions import CRSError

from floodline.settings import settings

Positive = Annotated[float, Field(gt=0)]
NonNegative = Annotated[float, Field(ge=0)]
Fraction = Annotated[float, Field(ge=0.0, le=1.0)]


class Frozen(BaseModel):
    """Base for every config model: immutable, no unknown fields, validated on assignment."""

    model_config = ConfigDict(frozen=True, extra="forbid", validate_assignment=True)


class Connectivity(StrEnum):
    """Neighbourhood used by flood-fill style operations."""

    FOUR = "d4"
    EIGHT = "d8"


class FlowDirMethod(StrEnum):
    """Flow direction algorithm."""

    D8 = "d8"
    DINF = "dinf"


class LengthUnit(StrEnum):
    """Units a gauge reading can arrive in."""

    METRE = "m"
    FOOT = "ft"
    US_SURVEY_FOOT = "usft"

    @property
    def metres(self) -> float:
        """Metres per unit."""
        return {
            LengthUnit.METRE: 1.0,
            LengthUnit.FOOT: 0.3048,
            LengthUnit.US_SURVEY_FOOT: 1200.0 / 3937.0,
        }[self]


class StageMethod(StrEnum):
    """How a single gauge reading becomes a stage for every reach."""

    CONSTANT = "constant"
    SLOPE = "slope"


class BuildingDepthStat(StrEnum):
    """How a per-building depth is reduced from the depth raster under its footprint."""

    MAX = "max"
    CENTROID = "centroid"
    P90 = "p90"
    MEAN = "mean"


class CurveFamily(StrEnum):
    """Depth-damage curve family."""

    JRC_OCEANIA = "jrc_oceania"
    JRC_GLOBAL = "jrc_global"
    HAZUS = "hazus"
    USACE = "usace"
    """The published USACE library, keyed by HAZUS occupancy code. Unlike the other
    three this is not a bundled approximation - see `damage.usace`."""


def validate_projected_crs(value: str | int | CRS) -> CRS:
    """Return `value` as a `CRS`, refusing anything not projected in true metres.

    Three refusals, in increasing subtlety:

    * **Geographic** CRSs. Cell sizes in degrees make every distance, area and
      slope in the terrain code wrong.
    * **Non-metre axes.** A projected CRS in US survey feet (EPSG:6588, say) would
      pass a naive "is it projected" check and then silently scale every length.
    * **Whole-world Mercator.** EPSG:3857 and friends are projected and their axis
      unit is nominally the metre, but it is not a *ground* metre: the scale factor
      is 1/cos(latitude), so at Houston's 29.8 deg N a "metre" is about 15% too
      long, and at Lismore's 28.8 deg S about 14%. Areas are out by the square of
      that. Both the USGS 3DEP and NSW elevation image services serve 3857
      natively, so this is the CRS a fetched raster is most likely to arrive in --
      which is exactly why it has to be refused here rather than trusted.

    Transverse Mercator (UTM, MGA) is fine and is not caught: its scale factor is
    referenced to a central meridian, not the equator.

    Raises
    ------
    ValueError
        If the value is not a parseable CRS, is geographic, has non-metre axes, or
        is a whole-world Mercator.
    """
    try:
        crs = CRS.from_user_input(value)
    except CRSError as exc:  # pragma: no cover - message passthrough
        raise ValueError(f"not a valid CRS: {value!r} ({exc})") from exc

    if crs.is_geographic:
        raise ValueError(
            f"{crs.to_string()} is a geographic CRS. floodline requires a projected CRS "
            "in metres (for Houston: EPSG:6587, NAD83(2011) / Texas South Central)."
        )
    if not crs.is_projected:
        raise ValueError(f"{crs.to_string()} is not a projected CRS.")

    units = {axis.unit_name for axis in crs.axis_info}
    allowed = {"metre", "meter", "m"}
    if not units <= allowed:
        raise ValueError(
            f"{crs.to_string()} has axis units {sorted(units)}; floodline requires metres."
        )

    operation = crs.coordinate_operation
    method = operation.method_name if operation is not None else ""
    if "Mercator" in method and "Transverse" not in method:
        raise ValueError(
            f"{crs.to_string()} is a whole-world Mercator ({method}). Its axis unit "
            "is the metre but its scale factor is 1/cos(latitude), so distances are "
            "inflated by roughly 15% at Houston and areas by 30%. Reproject to a "
            "local projected CRS in true metres - EPSG:6587 (NAD83(2011) / Texas "
            "South Central) for the Harvey case, or the appropriate UTM zone."
        )
    return crs


def serialise_crs(crs: CRS) -> str:
    """Serialise a CRS as an authority string, falling back to WKT."""
    return crs.to_string()


ProjectedCRS = Annotated[
    CRS,
    BeforeValidator(validate_projected_crs),
    PlainSerializer(serialise_crs, return_type=str),
]


class CrsConfig(Frozen):
    """Coordinate reference system policy."""

    model_config = ConfigDict(frozen=True, extra="forbid", arbitrary_types_allowed=True)

    analysis: ProjectedCRS = Field(
        default_factory=lambda: CRS.from_epsg(6587),
        description="Analysis CRS. Must be projected, in true metres. Default "
        "NAD83(2011) / Texas South Central, the Houston/Harvey case.",
    )
    allow_reprojection: bool = Field(
        default=False,
        description="If False, inputs whose CRS differs from `analysis` are an error, "
        "not silently reprojected.",
    )


class RasterConfig(Frozen):
    """Raster I/O policy."""

    nodata: float = Field(default=-9999.0, description="Nodata value written to output rasters.")
    block_size: int = Field(
        default=512, gt=0, description="Internal tile size for windowed reads and COG blocks."
    )
    compress: str = Field(default="deflate", description="COG compression codec.")
    predictor: int = Field(default=2, ge=1, le=3, description="Deflate/LZW predictor.")
    overview_levels: tuple[int, ...] = Field(
        default=(2, 4, 8, 16), description="Overview decimation factors written into the COG."
    )
    overview_resampling: str = Field(default="average", description="Overview resampling method.")
    bigtiff: bool = Field(default=True, description="Write BigTIFF so >4 GB outputs are legal.")
    warp_resampling: str = Field(
        default="bilinear",
        description="Resampling used when reprojecting continuous surfaces such as "
        "elevation. Nearest would introduce stair-stepping that the terrain code "
        "would then route water along.",
    )
    warp_memory_limit_mb: Positive = Field(
        default=512.0, description="Working memory GDAL may use per warp operation."
    )


class BathymetryConfig(Frozen):
    """Estimated channel bed below the surface a lidar DEM can see.

    Airborne lidar does not penetrate water: it images the water surface on the day of
    the flight, so a DEM's channel is a lid over the real one and the cross-section is
    missing whatever was flowing at the time. The rating curve therefore under-counts
    in-channel conveyance and pushes flow overbank that should have stayed in it.

    Burning an estimated bed back in is the standard correction. Depth comes from
    downstream hydraulic geometry, `d = coefficient * A^exponent` with A the upstream
    drainage area in km2, which is a regional relation and not a survey: it is right on
    average over many reaches and wrong on any particular one.
    """

    enabled: bool = Field(
        default=False,
        description="Whether to burn an estimated channel bed before computing HAND. "
        "Off by default: see docs/DECISIONS.md for the sweep that decided it.",
    )
    depth_coefficient_m: Positive = Field(
        default=0.381,
        description="Coefficient of the bankfull depth relation d = c * A^e, A in km2. "
        "Fitted, not quoted: the 90th percentile of mean depth (channel area over "
        "channel width) over 36,308 USGS field measurements at 116 Texas Gulf Coast "
        "gauges spanning 13 to 117,000 km2, regressed on published drainage area. "
        "R2 = 0.63. The 90th percentile stands in for bankfull because measurements "
        "are made across the flow range and the high ones are the near-bank ones; the "
        "median would be a low-flow channel and the maximum an overbank one.",
    )
    depth_exponent: Positive = Field(
        default=0.246,
        description="Exponent of the same relation. Published downstream hydraulic "
        "geometry puts depth exponents near 0.3 to 0.4 on drainage area; this sits a "
        "little below that range, which is what flat wide Gulf Coast streams should "
        "look like, and is a check that the fit is not nonsense rather than a claim "
        "that it reproduces any particular published curve.",
    )
    width_coefficient_m: Positive = Field(
        default=4.774,
        description="Coefficient of the bankfull width relation w = c * A^e, A in km2. "
        "Same method as the depth relation but a much smaller sample - 20 gauges, R2 = "
        "0.66 - because the measurement API rate-limited the second pass and it was "
        "not worth another day of polling for a term that only decides how many cells "
        "wide the burn runs. At 30 m that is the largest rivers alone.",
    )
    width_exponent: Positive = Field(
        default=0.321,
        description="Exponent of the width relation, from the same 20-gauge fit.",
    )
    max_depth_m: Positive = Field(
        default=8.0,
        description="Ceiling on the burned depth. A power law has no upper bound and a "
        "very large drainage area would otherwise cut a canyon into the DEM.",
    )


class TerrainConfig(Frozen):
    """Terrain conditioning and flow routing."""

    fill_epsilon: NonNegative = Field(
        default=0.0,
        description="Per-cell increment applied along filled flats so they drain "
        "(priority-flood+epsilon). 0.0 gives flat-surfaced filling.",
    )
    fill_connectivity: Connectivity = Field(
        default=Connectivity.EIGHT, description="Neighbourhood used by depression filling."
    )
    burn_depth: NonNegative = Field(
        default=5.0, description="Metres a mapped stream is lowered into the DEM when burning."
    )
    burn_buffer_cells: int = Field(
        default=0, ge=0, description="Cells either side of the stream included in the burn."
    )
    flowdir_method: FlowDirMethod = Field(default=FlowDirMethod.D8)
    resolve_flats: bool = Field(
        default=True,
        description="Give filled flats an artificial drainage gradient (Barnes et al. "
        "2014b) so D8 is defined across them. Without it, and without a non-zero "
        "fill_epsilon, water routed into a filled depression never reaches an outlet.",
    )
    stream_threshold_cells: int = Field(
        default=1000,
        gt=0,
        description="Flow-accumulation threshold (cells) above which a cell is a stream.",
    )
    min_stream_length_cells: int = Field(
        default=5, ge=0, description="Stream branches shorter than this are pruned."
    )


class HydraulicsConfig(Frozen):
    """Stage handling and inundation."""

    gauge_datum_offset_m: float | None = Field(
        default=None,
        description="Metres added to a gauge reading to convert gauge zero to AHD. "
        "There is no default: a gauge reading is relative to that gauge's own zero, "
        "and assuming zero silently would put the whole flood at the wrong elevation. "
        "Set it explicitly - 0.0 is a legitimate value, but it has to be chosen.",
    )
    gauge_reading_unit: LengthUnit | None = Field(
        default=None,
        description="Unit the raw gauge readings are in. No default, for the same "
        "reason the datum has none: USGS NWIS reports gauge height in FEET, and "
        "feeding 41.9 ft to a model that assumes metres puts 41.9 m of water over "
        "Houston. The unit is stated in the NWIS response; it has to be carried "
        "across deliberately.",
    )
    stage_method: StageMethod = Field(
        default=StageMethod.CONSTANT,
        description="Constant applies the gauge's depth-above-drainage everywhere; "
        "slope adjusts it by water_surface_slope along the network.",
    )
    water_surface_slope: NonNegative = Field(
        default=0.0005,
        description="Water-surface gradient (m/m) used by the slope stage method.",
    )
    min_depth_m: Positive = Field(
        default=0.05, description="Cells shallower than this are treated as dry."
    )
    connectivity: Connectivity = Field(
        default=Connectivity.EIGHT,
        description="Neighbourhood for the filter that removes wet cells disconnected "
        "from the stream network.",
    )
    require_connectivity: bool = Field(
        default=True, description="Drop wet regions not connected to a stream cell."
    )
    manning_n: Positive = Field(
        default=0.035,
        description="Manning's roughness for the synthetic rating curve. 0.035 is a "
        "natural channel with some vegetation; an engineered concrete bayou is nearer "
        "0.015, which is a factor of two on discharge at the same stage. "
        "Calibrated against 16 watersheds with the marks held out, and left at the "
        "physical value on purpose: lowering it to 0.020 improves held-out median "
        "RMSE from 2.25 m to 2.04 m, but the curve keeps improving down to 0.008, "
        "which is smoother than glass, and per-basin optima scatter across the whole "
        "range from 0.008 to 0.110. A parameter whose best value runs past physical "
        "plausibility is absorbing someone else's error - most likely channel "
        "capacity, since a lidar DEM sees the water surface rather than the bed - and "
        "shipping the fitted value would label a bias correction as a roughness. Set "
        "it per basin if you have marks to fit against.",
    )
    rating_max_stage_m: Positive = Field(
        default=25.0,
        description="Highest stage the synthetic rating curve is built to. A discharge "
        "beyond the top of the curve is reported, not extrapolated.",
    )
    rating_stage_step_m: Positive = Field(
        default=0.25,
        description="Stage increment the curve is tabulated at. Finer costs one pass "
        "over the reach catchment per step.",
    )
    min_reach_slope: Positive = Field(
        default=1e-4,
        description="Floor on reach bed slope. Manning's Q goes to zero as slope does, "
        "so a flat or numerically negative reach would otherwise carry no water at any "
        "stage. On a coastal plain plenty of reaches are that flat.",
    )
    default_specific_discharge: Positive = Field(
        default=5.0,
        description="Discharge per km2 of catchment used when no gauge is available, "
        "in m3/s/km2. 5.0 is roughly what Harvey delivered at Whiteoak Bayou - "
        "1,433 m3/s over 246 km2 - so it stands for a severe flood. It is a scenario, "
        "not an observation, and anything derived from it should say so.",
    )
    discharge_area_exponent: Positive = Field(
        default=1.0,
        description="Exponent in the drainage-area ratio used to carry a gauged "
        "discharge to ungauged reaches: Q_reach = Q_gauge x (A_reach / A_gauge)^k. "
        "k = 1 is simple area proportionality; regional regressions usually put it "
        "between 0.7 and 1.0, smaller meaning small catchments yield more per unit "
        "area. It is a real assumption and the Monte Carlo should sample it.",
    )
    min_reach_length_m: Positive = Field(
        default=30.0,
        description="Reaches shorter than this get no rating curve; their geometry is "
        "one or two cells and the derived hydraulic radius is noise.",
    )

    def require_gauge_reading_unit(self) -> LengthUnit:
        """Return `gauge_reading_unit`, refusing to proceed if it was never set.

        Raises
        ------
        ValueError
            If `gauge_reading_unit` is None.
        """
        if self.gauge_reading_unit is None:
            raise ValueError(
                "hydraulics.gauge_reading_unit is not set. USGS NWIS reports gauge "
                "height (parameter 00065) in feet; a reading of 41.9 ft is 12.8 m, "
                "and treating it as metres would put three times the water over the "
                "city. Set it to 'ft' for NWIS, or 'm' if your readings are metric."
            )
        return self.gauge_reading_unit

    def require_gauge_datum(self) -> float:
        """Return `gauge_datum_offset_m`, refusing to proceed if it was never set.

        Gauge readings are relative to a datum that differs per gauge and is not
        recoverable from the reading. Defaulting it to zero would silently place
        the entire modelled flood at the wrong elevation, and every downstream
        number - extent, depths, buildings, damage - would be confidently wrong.

        Raises
        ------
        ValueError
            If `gauge_datum_offset_m` is None.
        """
        if self.gauge_datum_offset_m is None:
            raise ValueError(
                "hydraulics.gauge_datum_offset_m is not set. A gauge reading is "
                "relative to that gauge's zero, so converting it to AHD needs the "
                "offset for the specific gauge. For Wilsons River at Lismore, take "
                "it from the BoM/WaterNSW station metadata. Set it to 0.0 only if "
                "the readings really are already in AHD."
            )
        return self.gauge_datum_offset_m


class ExposureConfig(Frozen):
    """Buildings and population intersection."""

    building_depth_stat: BuildingDepthStat = Field(default=BuildingDepthStat.P90)
    min_building_area_m2: Positive = Field(
        default=10.0, description="Footprints smaller than this are dropped as noise."
    )
    population_depth_threshold_m: Positive = Field(
        default=0.3, description="Depth above which a population cell counts as affected."
    )
    default_storeys: Positive = Field(
        default=1.0, description="Storeys assumed when a footprint carries no height."
    )
    floor_height_m: Positive = Field(
        default=0.15,
        description="Freeboard between ground and finished floor level. Used only "
        "where the inventory does not carry a real one; NSI does, per structure.",
    )
    depth_percentile: Fraction = Field(
        default=0.90,
        description="Percentile of depth under a footprint when building_depth_stat "
        "is p90. Lower is more conservative about what counts as flooded.",
    )
    hand_percentile: Fraction = Field(
        default=0.10,
        description="Percentile of height-above-drainage under a footprint. The low "
        "end, to mirror the high end taken for depth: a building's own depth and the "
        "depth its stage implies have to describe the same cell.",
    )
    storey_height_m: Positive = Field(
        default=3.0,
        description="Nominal floor-to-floor height, for turning a building height "
        "into a storey count and for capping how many storeys water reaches.",
    )
    min_footprint_side_m: Positive = Field(
        default=4.0,
        description="Side of the square used for a structure whose inventory records "
        "no footprint area. NSI gives a point and an area, not an outline.",
    )


class DamageConfig(Frozen):
    """Depth-damage curves and costs."""

    curve_family: CurveFamily = Field(default=CurveFamily.HAZUS)
    currency: str = Field(
        default="USD",
        description="Denomination of replacement_cost_per_m2, carried into every output "
        "so a total is never a bare number.",
    )
    max_curve_depth_m: Positive = Field(
        default=6.0, description="Depth beyond which the curve is held at its terminal value."
    )
    clamp_damage_fraction: bool = Field(
        default=True, description="Clip interpolated damage fractions into [0, 1]."
    )
    replacement_cost_per_m2: dict[str, Positive] = Field(
        default_factory=lambda: {
            "residential": 2000.0,
            "commercial": 2500.0,
            "industrial": 1500.0,
            "other": 1800.0,
        },
        description="Replacement cost per m2 of floor area by building class, in "
        "`currency`. Assumptions, not quotes: they are order-of-magnitude US "
        "rebuild rates and the Monte Carlo samples cost_sigma_frac around them.",
    )
    default_class: str = Field(default="residential")
    discharge_ladder_step: Positive = Field(
        default=0.1,
        description="Spacing of the discharge multiplier ladder, from 0 to "
        "discharge_ladder_max. Must divide 1.0 exactly, so the observed discharge is a "
        "rung rather than a point interpolated between two: it is the one multiplier "
        "every number in the model is anchored to.",
    )
    discharge_ladder_max: Positive = Field(
        default=3.0, description="Top of the multiplier ladder the slider spans."
    )
    map_reference_multipliers: tuple[Fraction | Positive, ...] = Field(
        default=(0.5, 1.0, 2.0, 3.0),
        description="Discharge multipliers the map's damage layer is rendered at; the "
        "browser interpolates between them as the slider moves. Four is the limit - "
        "they are packed one per channel of an RGBA image.",
    )
    usace_default_occupancy: str = Field(
        default="RES1-1SNB",
        description="Occupancy code for structures the USACE library does not cover. "
        "Single-family, one storey, no basement - the commonest US dwelling, and the "
        "conservative choice for an unclassified building.",
    )

    @model_validator(mode="after")
    def _ladder_contains_the_observed_discharge(self) -> Self:
        rungs = round(1.0 / self.discharge_ladder_step)
        if abs(rungs * self.discharge_ladder_step - 1.0) > 1e-9:
            raise ValueError(
                f"discharge_ladder_step {self.discharge_ladder_step} does not divide 1.0, "
                "so the observed discharge would fall between two rungs and every "
                "figure reported at it would be interpolated"
            )
        return self

    @model_validator(mode="after")
    def _default_class_priced(self) -> Self:
        if self.default_class not in self.replacement_cost_per_m2:
            raise ValueError(
                f"default_class {self.default_class!r} has no entry in replacement_cost_per_m2"
            )
        return self


class MonteCarloConfig(Frozen):
    """Monte Carlo uncertainty settings."""

    n_samples: int = Field(default=1000, gt=0)
    seed: int = Field(default=20220228, description="Seeded so runs are reproducible.")
    stage_sigma_m: NonNegative = Field(default=0.15, description="1-sigma gauge stage error.")
    dem_sigma_m: NonNegative = Field(default=0.15, description="1-sigma vertical DEM error.")
    cost_sigma_frac: Fraction = Field(
        default=0.25, description="1-sigma relative error on replacement cost."
    )
    curve_family_weights: dict[CurveFamily, Fraction] = Field(
        default_factory=lambda: {CurveFamily.HAZUS: 0.6, CurveFamily.JRC_GLOBAL: 0.4},
        description="Sampling weights over curve families. HAZUS leads because the "
        "validation case is US; JRC_GLOBAL carries the disagreement between families. "
        "That disagreement is large per building - about 2.3x at one metre - but it "
        "is not the largest term in the reported interval, which was claimed here "
        "before it was measured. Decomposed on Whiteoak Bayou, each term alone as a "
        "share of the point estimate: stage 100%, cost 84%, curve and family together "
        "16%, DEM 8%. Stage and cost win because they move how many buildings are "
        "wet; the curve only moves what each wet building costs.",
    )
    sample_across_families: bool = Field(
        default=True,
        description="Whether an explicitly loaded curve library is still sampled "
        "against the bundled families. Loading one library used to collapse the "
        "family term to nothing, which silently removed the largest single source "
        "of damage uncertainty. Set False only to price against one library on "
        "purpose, knowing the interval then understates itself.",
    )
    supplied_family_weight: Fraction = Field(
        default=0.6,
        description="Weight given to an explicitly loaded library when it is sampled "
        "alongside the bundled families. It leads because it was chosen deliberately "
        "and is usually the most specific to the study area; the remainder is split "
        "over curve_family_weights in proportion.",
    )
    interval: tuple[Fraction, Fraction] = Field(
        default=(0.05, 0.95), description="Reported credible interval quantiles."
    )

    @model_validator(mode="after")
    def _check_weights_and_interval(self) -> Self:
        total = sum(self.curve_family_weights.values())
        if total <= 0:
            raise ValueError("curve_family_weights must sum to a positive number")
        low, high = self.interval
        if low >= high:
            raise ValueError(f"interval must be increasing, got {self.interval}")
        return self


class ValidationConfig(Frozen):
    """SAR comparison and metrics."""

    sar_water_threshold_db: float = Field(
        default=-18.0, description="Fallback VV dB threshold if Otsu is disabled."
    )
    use_otsu: bool = Field(default=True, description="Pick the VV threshold with Otsu.")
    otsu_bins: int = Field(default=256, gt=1)
    min_water_patch_cells: int = Field(
        default=10, ge=0, description="Speckle patches smaller than this are removed."
    )


class SourcesConfig(Frozen):
    """How `io.sources` talks to the outside world."""

    max_attempts: int = Field(
        default=4,
        ge=1,
        description="Attempts per request before giving up. Public agency APIs return "
        "transient 5xx often enough that one attempt is not a fair test of whether a "
        "dataset is reachable.",
    )
    backoff_seconds: NonNegative = Field(
        default=2.0, description="Base for exponential backoff between attempts."
    )
    connect_timeout_s: Positive = Field(default=30.0)
    read_timeout_s: Positive = Field(
        default=300.0, description="Generous: some DEM tiles are hundreds of megabytes."
    )
    retry_status_codes: tuple[int, ...] = Field(
        default=(429, 500, 502, 503, 504),
        description="HTTP statuses worth retrying. 4xx other than 429 will not "
        "improve on a second attempt.",
    )


class TileVintage(StrEnum):
    """How to choose between DEM tiles covering the same ground at different dates."""

    NEAREST_TO_EVENT = "nearest_to_event"
    NEWEST = "newest"


class CaseConfig(Frozen):
    """The event being modelled: area, dates, and the identifiers each source needs.

    Every value here is an input to `io.sources`, which is why none of them is a
    literal buried in a fetch function. Swapping case - to Lismore, say - is a
    config change, not a code change.
    """

    name: str = Field(default="harvey_houston_2017", description="Slug used in filenames.")
    description: str = Field(
        default="Hurricane Harvey over Houston, Texas, 26 August - 1 September 2017"
    )
    aoi_bbox_wgs84: tuple[float, float, float, float] = Field(
        default=(-95.80, 29.50, -95.00, 30.10),
        description="(west, south, east, north) in EPSG:4326. Used to query every "
        "source. Geographic on purpose: it is a query parameter, not an analysis CRS.",
    )
    event_start: date = Field(default=date(2017, 8, 25))
    event_end: date = Field(default=date(2017, 9, 2))
    peak_start: date = Field(
        default=date(2017, 8, 29), description="Start of the window to look for a SAR scene."
    )
    peak_end: date = Field(default=date(2017, 8, 31))

    gauge_sites: tuple[str, ...] = Field(
        default=("08074000", "08074500", "08076000"),
        description="USGS NWIS site numbers. Site metadata carries the vertical datum, "
        "which is what hydraulics.gauge_datum_offset_m needs.",
    )
    stn_event_id: int = Field(
        default=180, description="USGS Short-Term Network flood event id (2017 Harvey)."
    )
    fema_disaster_number: int = Field(
        default=4332, description="FEMA disaster declaration number (Hurricane Harvey, TX)."
    )
    dem_resolutions_m: tuple[int, ...] = Field(
        default=(1, 10, 30),
        description="3DEP resolutions to fetch, which is the resolution experiment. "
        "1/9 arc-second (~3 m) is mapped but not included by default: it has patchy "
        "US coverage and returns zero tiles over Houston.",
    )
    dem_max_download_gb: Positive = Field(
        default=10.0,
        description="Refuse a DEM fetch whose planned total exceeds this. The full "
        "AOI at 1 m is about 57 GB, which is easy to start by accident and, at "
        "roughly 125 bytes per cell of peak memory, far past what the global "
        "priority-flood can hold in one pass. Raise it deliberately, or shrink the AOI.",
    )
    huc_level: int = Field(
        default=10,
        description="Watershed level to use as the unit of work. Flow accumulation "
        "depends on the whole upstream catchment, so terrain products computed over "
        "an arbitrary box are wrong near its edges; a HUC is hydrologically complete. "
        "10 (~540 km2 here) fits comfortably at 10 m; 12 is the smaller subwatershed.",
    )
    huc_codes: tuple[str, ...] = Field(
        default=(),
        description="Specific HUCs to work on. Empty means every one intersecting the AOI.",
    )
    dem_vintage: TileVintage = Field(
        default=TileVintage.NEAREST_TO_EVENT,
        description="3DEP publishes several vintages of the same tile footprint - "
        "Houston has 2018, 2024 and 2026 versions of the same ground. Modelling a "
        "2017 flood on 2026 terrain would put the water over land that did not exist "
        "yet, so the default picks the survey closest to the event.",
    )
    overture_release: str = Field(
        default="2026-08-19.0", description="Overture Maps release to read buildings from."
    )

    @model_validator(mode="after")
    def _check_window(self) -> Self:
        west, south, east, north = self.aoi_bbox_wgs84
        if not (west < east and south < north):
            raise ValueError(
                f"aoi_bbox_wgs84 must be (w, s, e, n) increasing, got {self.aoi_bbox_wgs84}"
            )
        if not (west >= -180 and east <= 180 and south >= -90 and north <= 90):
            raise ValueError(f"aoi_bbox_wgs84 is out of range: {self.aoi_bbox_wgs84}")
        if self.event_start > self.event_end:
            raise ValueError("event_start must not be after event_end")
        if self.peak_start > self.peak_end:
            raise ValueError("peak_start must not be after peak_end")
        if self.huc_level not in {2, 4, 6, 8, 10, 12, 14, 16}:
            raise ValueError(f"huc_level must be an even number from 2 to 16, got {self.huc_level}")
        return self


class PathsConfig(Frozen):
    """Where things live. Nothing under `raw` is ever committed.

    Defaults come from `floodline.settings`, so a deployment sets them once in the
    environment rather than threading them through every call. They stay on `Config`
    because a run should be able to record where its inputs came from alongside the
    parameters that shaped it.
    """

    raw: Path = Field(default_factory=lambda: settings().data_raw)
    interim: Path = Field(default_factory=lambda: settings().data_interim)
    processed: Path = Field(default_factory=lambda: settings().data_processed)
    outputs: Path = Field(default_factory=lambda: settings().outputs)


class Config(Frozen):
    """Top-level configuration: the single object the pipeline is parameterised by."""

    crs: CrsConfig = Field(default_factory=CrsConfig)
    raster: RasterConfig = Field(default_factory=RasterConfig)
    terrain: TerrainConfig = Field(default_factory=TerrainConfig)
    bathymetry: BathymetryConfig = Field(default_factory=BathymetryConfig)
    hydraulics: HydraulicsConfig = Field(default_factory=HydraulicsConfig)
    exposure: ExposureConfig = Field(default_factory=ExposureConfig)
    damage: DamageConfig = Field(default_factory=DamageConfig)
    monte_carlo: MonteCarloConfig = Field(default_factory=MonteCarloConfig)
    validation: ValidationConfig = Field(default_factory=ValidationConfig)
    case: CaseConfig = Field(default_factory=CaseConfig)
    sources: SourcesConfig = Field(default_factory=SourcesConfig)
    paths: PathsConfig = Field(default_factory=PathsConfig)

    @classmethod
    def from_file(cls, path: Path) -> Config:
        """Load a `Config` from a TOML file.

        An empty file, or a file with no `[floodline]` table, yields the defaults.
        Unknown keys are an error: a typo'd threshold must not silently do nothing.
        """
        with path.open("rb") as handle:
            data: dict[str, Any] = tomllib.load(handle)
        return cls.model_validate(data.get("floodline", data))
