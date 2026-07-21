# floodline — flood extent, exposure and damage estimation from a DEM and a gauge


> Project brief. Conventions and Definition of done live in [CLAUDE.md](../CLAUDE.md).

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
- HAND assumes water surface parallel to the drainage line: misses backwater, levees, culverts, and flow routing across catchment boundaries. Lismore's levee (the CBD levee overtops around 10.6 m) is a good demonstration of when the assumption breaks vs when it doesn't.
- One gauge ≠ one stage for the whole reach.
- SAR misses water under tree canopy and in dense urban areas (double bounce); it's a lower bound on urban extent.
- Population grids disagree with each other by tens of percent in small towns — report all three.

---

## Validation case: Lismore, NSW, 28 February 2022

Why this event: record stage (~14.4 m at the Lismore gauge, ~2 m above the 1974 record), good open lidar, a Sentinel-1 pass near the peak, and published building and loss figures. Secondary case if time: Hurricane Harvey, Houston 2017 (FEMA per-structure damage assessments — best ground truth in the world for this).

### Data acquisition checklist (Thomas does these — they need accounts/clicks)

| # | Dataset | Source | Notes |
|---|---|---|---|
| 1 | 1 m lidar DEM, Lismore / Wilsons River | ELVIS (elevation.fsdf.org.au) | Free, needs email. Get DEM (bare earth), not DSM. Download tiles covering Lismore + ~10 km upstream. Also grab the 5 m and note SRTM 30 m for the resolution-sensitivity experiment |
| 2 | Gauge record, Wilsons River at Lismore | BoM Water Data Online / WaterNSW | Hourly stage Feb 22 – Mar 5 2022. Record the gauge datum (stage is relative to gauge zero — you need the AHD conversion, this bites everyone) |
| 3 | Stream network | GA Geofabric, or derive from DEM | Use Geofabric to burn; derive from DEM as a check |
| 4 | Building footprints | Overture Maps (buildings theme) via DuckDB/S3, or Microsoft Global Footprints | Overture preferred (consistent with GeoDiff). Clip to AOI |
| 5 | Population | ABS 2021 Census mesh blocks (counts + boundaries), WorldPop 100 m AU, Meta HRSL | Mesh blocks are the best available and are ground-truth-adjacent; use the gridded ones for the disagreement analysis |
| 6 | Depth–damage curves | JRC Global flood depth-damage functions (Huizinga et al. 2017) — the Excel supplement | Free. Take Oceania curves + max damage values. Optional: FEMA HAZUS curves for the Houston case |
| 7 | Sentinel-1 RTC | Microsoft Planetary Computer `sentinel-1-rtc` collection (STAC) | Analysis-ready, terrain-corrected, no SNAP. Find the scene closest to 28 Feb–1 Mar 2022 over Lismore. Also check Copernicus Global Flood Monitoring (GFM) for a ready-made flood mask as a second reference — verify coverage |
| 8 | Reference figures | Lismore City Council flood reports, NSW Flood Inquiry 2022, Insurance Council of Australia loss estimates | Note scope carefully: ICA figures are for the whole Northern Rivers / east coast event, not Lismore alone; council building counts are the cleaner comparison |

Put raw downloads in `data/raw/` (git-ignored), record every URL, date and checksum in `data/MANIFEST.md`. `sources.py` should be able to reproduce every download that doesn't need a login.

### Experiments to report
1. Extent vs SAR at peak stage: hit rate, FAR, CSI, bias. Map of agreement / miss / false alarm.
2. Same, across DEM resolution: 1 m, 5 m, 30 m SRTM. (Expected result: SRTM is much worse — which is what most global flood products use.)
3. Inundated building count vs council figures, with the MC interval.
4. People affected: mesh block vs WorldPop vs HRSL.
5. Damage: point estimate and 5–95% band, broken down by building class, with the curve-family sensitivity shown separately.
6. Runtime and memory for the terrain pipeline on the full 1 m tile set.

---

## Phases

**Phase 0 — scaffold (Claude Code, day 1).** Repo, `uv`, pyproject, ruff/mypy/pre-commit, GitHub Actions running tests on push, `config.py`, `io/raster.py` with CRS refusal, synthetic catchment fixture generator (a tilted plane with a carved valley and a few pits), typer skeleton. Push and confirm CI is green before anything else.

**Phase 1 — terrain core (weeks 1–2).** `fill`, `flowdir`, `flowacc`, `streams`, `hand`, all numba, all property-tested, differential-tested against pysheds on the synthetic fixture and one real ELVIS tile. Benchmark. This is the part that reads as engineering; don't lean on a library for it.

**Phase 2 — hydraulics + exposure (week 3).** Stage handling with gauge-datum conversion, inundate with connectivity filter, building and population intersection. First Lismore extent map.

**Phase 3 — damage + uncertainty (week 4).** Curves, costs, MC. First damage number with a band.

**Phase 4 — validation (week 5).** SAR mask, metrics, resolution experiment, population comparison. This is where the write-up's tables come from.

**Phase 5 — report + write-up (week 6).** Rendered report, README with the pitch, the tables, the failure modes and the honest limits. Then the resume bullet.

Stretch, in order of value: Houston/Harvey second case; D∞ flow direction; Rust core for fill/HAND via PyO3 with a benchmark vs numba (doubles as Esri-flavoured systems evidence); dasymetric population refinement using footprints.
