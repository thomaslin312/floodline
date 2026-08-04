"""Configuration models.

Every numeric threshold, tolerance, CRS and curve selection used anywhere in the
pipeline is a field on one of these models. Algorithm code never contains a
literal that a user might reasonably want to change.
"""

from __future__ import annotations

import tomllib
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Any, Self

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, PlainSerializer, model_validator
from pyproj import CRS
from pyproj.exceptions import CRSError

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


def validate_projected_crs(value: str | int | CRS) -> CRS:
    """Return `value` as a `CRS`, refusing anything that is not projected in metres.

    A geographic CRS as the analysis CRS is an error, not a warning: cell sizes in
    degrees make every distance, area and slope in the terrain code wrong.

    Raises
    ------
    ValueError
        If the value is not a parseable CRS, is geographic, or has non-metre axes.
    """
    try:
        crs = CRS.from_user_input(value)
    except CRSError as exc:  # pragma: no cover - message passthrough
        raise ValueError(f"not a valid CRS: {value!r} ({exc})") from exc

    if crs.is_geographic:
        raise ValueError(
            f"{crs.to_string()} is a geographic CRS. floodline requires a projected CRS "
            "in metres (for Lismore: EPSG:7856, GDA2020 / MGA zone 56)."
        )
    if not crs.is_projected:
        raise ValueError(f"{crs.to_string()} is not a projected CRS.")

    units = {axis.unit_name for axis in crs.axis_info}
    allowed = {"metre", "meter", "m"}
    if not units <= allowed:
        raise ValueError(
            f"{crs.to_string()} has axis units {sorted(units)}; floodline requires metres."
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
        default_factory=lambda: CRS.from_epsg(7856),
        description="Analysis CRS. Must be projected, in metres. Default GDA2020 / MGA zone 56.",
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
    manning_n: Positive = Field(default=0.035, description="Manning's n for the synthetic rating.")

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
        default=0.15, description="Freeboard between ground and finished floor level."
    )


class DamageConfig(Frozen):
    """Depth-damage curves and costs."""

    curve_family: CurveFamily = Field(default=CurveFamily.JRC_OCEANIA)
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
        description="AUD per m2 of floor area by building class.",
    )
    default_class: str = Field(default="residential")

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
        default_factory=lambda: {CurveFamily.JRC_OCEANIA: 0.7, CurveFamily.JRC_GLOBAL: 0.3},
        description="Sampling weights over curve families.",
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


class PathsConfig(Frozen):
    """Where things live. Nothing under `raw` is ever committed."""

    raw: Path = Field(default=Path("data/raw"))
    interim: Path = Field(default=Path("data/interim"))
    processed: Path = Field(default=Path("data/processed"))
    outputs: Path = Field(default=Path("outputs"))


class Config(Frozen):
    """Top-level configuration: the single object the pipeline is parameterised by."""

    crs: CrsConfig = Field(default_factory=CrsConfig)
    raster: RasterConfig = Field(default_factory=RasterConfig)
    terrain: TerrainConfig = Field(default_factory=TerrainConfig)
    hydraulics: HydraulicsConfig = Field(default_factory=HydraulicsConfig)
    exposure: ExposureConfig = Field(default_factory=ExposureConfig)
    damage: DamageConfig = Field(default_factory=DamageConfig)
    monte_carlo: MonteCarloConfig = Field(default_factory=MonteCarloConfig)
    validation: ValidationConfig = Field(default_factory=ValidationConfig)
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
