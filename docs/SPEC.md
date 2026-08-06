# floodline — flood extent, exposure and damage estimation from a DEM and a gauge


> Project brief. Conventions and Definition of done live in [CONTRIBUTING.md](../CONTRIBUTING.md).

## One-paragraph pitch

Given a lidar DEM, a stream network and a water level at a river gauge, produce an
inundation extent and depth raster (HAND method), intersect it with building
footprints and population grids, and estimate people affected and direct economic
damage using published depth–damage curves — with a Monte Carlo uncertainty band,
and validated against a real event (Lismore, NSW, February 2022) using Sentinel-1
flood extent, council building-inundation counts and insured-loss figures as
independent references.

The claim being tested: a screening-grade flood damage model built from open data
can reproduce the extent of a real major flood to within a stated CSI and put the
observed building count inside its 90% interval.

---

## Stack (decided — don't relitigate without a reason)

| Layer | Choice | Why |
|---|---|---|
| Language | Python 3.12 | Matches GeoDiff; geospatial ecosystem lives here |
| Env / packaging | `uv`, `pyproject.toml`, hatchling | Fast, lockfile, one tool |
| Hydro core | numpy + **numba** (`@njit`) | Priority-flood pit filling, D8 flow direction and HAND are pointer-chasing loops; pure numpy can't express them, numba makes them C-speed with no build step. Rust/PyO3 is a stretch goal, not the baseline |
| Hydro oracle | `pysheds` and/or WhiteboxTools | **Only** used in tests for differential checks against our own implementation. Never in the pipeline |
| Rasters | `rasterio`, `rioxarray` for windowed I/O; outputs as **COG** | Standard; COGs stream in QGIS and web viewers |
| Vectors | `geopandas` 1.x, `shapely` 2.x, `pyogrio` | Same as GeoDiff |
| Tabular / interchange | **GeoParquet** everywhere, `pyarrow` | Fast, typed, no shapefile nonsense |
| Config | `pydantic` v2 models | Every threshold, curve choice and CRS validated; no magic numbers in algorithm code |
| CLI | `typer` | `floodline condition`, `floodline hand`, `floodline inundate`, `floodline exposure`, `floodline damage`, `floodline validate`, `floodline report` |
| Tests | `pytest`, `hypothesis`, `pytest-benchmark` | Property tests on hydrological invariants |
| Quality | `ruff`, `mypy --strict`, `pre-commit`, GitHub Actions | **CI must actually run this time.** Push to GitHub from day one |
| Report | `matplotlib` + `contextily` figures, static HTML via Jinja2 or Quarto | No dashboard. A rendered report is the deliverable |
| Notebooks | Only in `notebooks/` for exploration; nothing in the pipeline imports from them |

Things deliberately **not** used: SNAP / SAR preprocessing (use analysis-ready RTC
instead — see data), HEC-RAS / LISFLOOD (this is a screening model, that's the
point), Streamlit, shapefiles.

---

## Pipeline

```
DEM ──► condition ──► flow_dir ──► flow_acc ──► streams
                                      │
                                      ▼
                                    HAND ──► inundate(stage) ──► depth raster
                                                                     │
buildings.parquet ───────────────────────────────────────────────────┤
population grid ─────────────────────────────────────────────────────┤
                                                                     ▼
                                                 exposure ──► damage ──► MC uncertainty
                                                                     │
SAR extent / council counts / ICA losses ──► validate ◄──────────────┘
                                                 │
                                                 ▼
                                              report
```

### Modules

```
src/floodline/
  config.py         # pydantic: CRS, tolerances, curve family, MC settings
  io/
    raster.py       # windowed read/write, COG writer, CRS refusal (geographic CRS = error)
    vector.py       # geoparquet read/write
    sources.py      # download helpers with checksums; data never committed
  terrain/
    fill.py         # priority-flood depression filling (Barnes et al. 2014), numba
    burn.py         # stream burning / AGREE-style conditioning
    flowdir.py      # D8 (default) and D∞ (stretch) flow direction, numba
    flowacc.py      # flow accumulation via topological order, numba
    streams.py      # threshold accumulation → stream raster → vector network
    hand.py         # height above nearest drainage, numba
  hydraulics/
    stage.py        # gauge stage → per-reach stage (constant, or slope-propagated)
    rating.py       # synthetic rating curve (Manning) for return-period flows
    inundate.py     # HAND < stage → extent + depth; connectivity filter
  exposure/
    buildings.py    # footprints × depth: per-building depth (max/centroid/p90)
    population.py   # grid × depth: people above threshold; dasymetric refinement (stretch)
  damage/
    curves.py       # JRC Huizinga 2017 curves (Oceania/Australia), HAZUS optional; interpolation, bounds
    costs.py        # replacement cost per m² by class, storeys assumption
    estimate.py     # per-building damage fraction × cost; aggregation
    uncertainty.py  # Monte Carlo over stage σ, DEM σ, curve family, cost σ
  validate/
    sar.py          # Sentinel-1 RTC → water mask (Otsu on VV dB), compare to modelled extent
    metrics.py      # hit rate, false alarm ratio, CSI, bias; building-count comparison
  report/
    figures.py
    render.py
  cli.py
tests/
  unit/             # each module
  property/         # hypothesis invariants (below)
  differential/     # ours vs pysheds/whitebox on real tiles
  integration/      # tiny synthetic catchment end to end
  fixtures/         # small synthetic DEMs, 3 known floods
```

### Invariants to property-test (this is where the rigour lives)

Terrain
- After filling: every cell has a monotone non-increasing path to the raster edge (no pits).
- Filling never lowers a cell; filled − original ≥ 0 everywhere.
- Flow accumulation at any cell = 1 + sum over upstream neighbours; total accumulation at outlets = number of non-nodata cells.
- HAND ≥ 0 everywhere; HAND = 0 on stream cells; HAND is exactly `filled_dem[cell] − filled_dem[drainage(cell)]`.
- Our D8/HAND agrees with pysheds on synthetic and real tiles (allowing documented tie-break differences).

Hydraulics
- Inundated area is monotone non-decreasing in stage.
- depth = stage − HAND on wet cells, 0 elsewhere; max depth ≤ stage.
- With the connectivity filter on, every wet cell is 8-connected to a stream cell.

Damage
- Damage fraction ∈ [0, 1], monotone non-decreasing in depth, 0 at depth 0.
- Total damage is monotone in stage.
- MC mean converges (seeded) and the interval contains the point estimate.

### Known failure modes to state in the README (and ideally demonstrate)
- HAND assumes water surface parallel to the drainage line: misses backwater, levees, culverts, and flow routing across catchment boundaries. The Addicks and Barker reservoir releases during Harvey are a sharp demonstration — water arrived downstream by a controlled release no terrain-following model can infer. (Lismore's CBD levee, which overtops around 10.6 m, is the equivalent demonstration for the secondary case.)
- One gauge ≠ one stage for the whole reach.
- SAR misses water under tree canopy and in dense urban areas (double bounce); it's a lower bound on urban extent. This is why the primary reference here is the surveyed high-water marks, with SAR as a secondary check.
- Population grids disagree with each other by tens of percent in small towns — report all three.

---

## Validation case: Hurricane Harvey, Houston, Texas, 26 August – 1 September 2017

**Changed from Lismore, NSW on 2026-09-03.** The original brief named Lismore as the
primary case and Harvey as the secondary, on the grounds that Harvey has "FEMA
per-structure damage assessments — best ground truth in the world for this". That
judgment held up; what tipped the order was data access. Every Harvey input is
reachable from a public API with no account, including the 1 m bare-earth lidar,
whereas the equivalent Australian lidar (ELVIS) is delivered by emailed ZIP with no
documented REST API. See `docs/DECISIONS.md` for the full reasoning and what was
given up.

Why this event: record rainfall (over 1500 mm in places), a metropolitan area with
complete 1 m lidar coverage, 2,298 ground-surveyed high-water marks, per-property
NFIP insurance claims, dense USGS stream gauging, and Sentinel-1 passes through the
flood peak. The Addicks and Barker reservoir releases are a sharp demonstration of
where the HAND assumption breaks: water arrived by a route no terrain-following
model can infer.

Secondary case if time: Lismore, NSW, 28 February 2022 — record stage (~14.4 m,
about 2 m above the 1974 record), and a small town, which is where population grids
disagree most. Needs one manual ELVIS request for the 1 m DEM; the NSW 5 m elevation
service is scriptable in the meantime.

### Data acquisition (all automatable unless marked)

`io/sources.py` fetches these with checksums; `data/MANIFEST.md` records every URL,
retrieval date and hash.

| # | Dataset | Source | Access |
|---|---|---|---|
| 1 | 1 m bare-earth lidar DEM | USGS 3DEP via TNM Products API → `prd-tnm` S3 | Public, no key. 53 GeoTIFF tiles over the Houston AOI |
| 1b | 3 m / 10 m / 30 m DEM | Same 3DEP service, resolution as a parameter | Public. This is the resolution experiment in one API |
| 2 | Gauge stage + **datum** | USGS NWIS `iv` (param 00065) and site metadata | Public, no key. Site metadata states `alt_datum_cd` (NAVD88) explicitly — the datum problem the original brief flagged is an API field here |
| 3 | Stream network | USGS NHDPlus HR, or derive from the DEM | Public. Derive from DEM as the cross-check |
| 4 | Building footprints | Overture Maps buildings via DuckDB/S3 | Public anonymous S3 |
| 5 | Population | US Census ACS block groups (TIGERweb), WorldPop 100 m, Meta HRSL | All public. See the caveat under experiment 4 |
| 6 | Depth–damage curves | FEMA HAZUS curves (US); JRC Huizinga et al. 2017 for the cross-family comparison | Public downloads |
| 7 | High-water marks | USGS STN Flood Event Viewer, event 180 | Public, no key. 2,298 surveyed marks with elevation, datum and a quality flag |
| 8 | Per-property damage | OpenFEMA `NfipClaims` v3, `HousingAssistanceOwners` v2 | Public, no key |
| 9 | Sentinel-1 RTC | Planetary Computer STAC (`sentinel-1-rtc`) | **Search is public; download needs a key.** The collection declares `msft:requires_account: true`. Supply it through an environment variable; it never enters the repo. Copernicus Data Space and ASF are alternatives with the same constraint |
| 10 | Reference figures | Harris County Flood Control District reports, NOAA/NWS post-event summaries | **Manual.** These are documents whose *scope* matters, not datasets |

### Experiments to report

1. Extent vs USGS high-water marks at peak stage: elevation residuals at 2,298
   surveyed points, plus hit rate, false alarm ratio, CSI and bias against the
   Sentinel-1 mask where a key is available. Map of agreement / miss / false alarm.
2. Same, across DEM resolution: 1 m, 3 m, 10 m, 30 m. (Expected result: 30 m is much
   worse — which is what most global flood products use.)
3. Inundated building count vs OpenFEMA NFIP claim locations, with the MC interval.
4. People affected: Census block groups vs WorldPop vs HRSL. **Caveat, and it has to
   be stated in the write-up:** these grids diverge most in small towns and agree
   fairly well across a metro the size of Houston, so this experiment is weaker here
   than it would have been at Lismore. It is the main thing lost by the change of
   case. Run it on the Lismore AOI too if the secondary case happens.
5. Damage: point estimate and 5–95% band, broken down by building class, with the
   curve-family sensitivity (HAZUS vs JRC) shown separately.
6. Runtime and memory for the terrain pipeline on the full 1 m tile set.
7. Where HAND breaks: the Addicks and Barker reservoir releases, as a worked example
   of water arriving by a route the model cannot infer from terrain.

## Phases

**Phase 0 — scaffold.** Repo, `uv`, pyproject, ruff/mypy/pre-commit, GitHub Actions running tests on push, `config.py`, `io/raster.py` with CRS refusal, synthetic catchment fixture generator (a tilted plane with a carved valley and a few pits), typer skeleton. Push and confirm CI is green before anything else.

**Phase 1 — terrain core (weeks 1–2).** `fill`, `flowdir`, `flowacc`, `streams`, `hand`, all numba, all property-tested, differential-tested against pysheds on the synthetic fixture and one real ELVIS tile. Benchmark. This is the part that reads as engineering; don't lean on a library for it.

**Phase 2 — hydraulics + exposure (week 3).** Stage handling with gauge-datum conversion, inundate with connectivity filter, building and population intersection. First Lismore extent map.

**Phase 3 — damage + uncertainty (week 4).** Curves, costs, MC. First damage number with a band.

**Phase 4 — validation (week 5).** SAR mask, metrics, resolution experiment, population comparison. This is where the write-up's tables come from.

**Phase 5 — report + write-up (week 6).** Rendered report, README with the pitch, the tables, the failure modes and the honest limits. Then the resume bullet.

Stretch, in order of value: Houston/Harvey second case; D∞ flow direction; Rust core for fill/HAND via PyO3 with a benchmark vs numba (doubles as Esri-flavoured systems evidence); dasymetric population refinement using footprints.
