# floodline

Flood extent, exposure and damage estimation from a DEM and a gauge reading.

Given a lidar DEM, a stream network and a discharge at a river gauge, floodline
produces an inundation extent and depth raster (HAND method), intersects it with
building footprints and population grids, and estimates people affected and direct
economic damage using depth–damage curves, with a Monte Carlo uncertainty band.

**The claim being tested:** a screening-grade flood damage model built from open data
can reproduce the extent of a real major flood to within a stated error and put the
observed building count inside its 90% interval.

**How far that claim has actually been taken.** The extent half is done and measured:
against 2,298 USGS surveyed high-water marks from Hurricane Harvey, the modelled water
surface has an RMSE of 1.60 m in the best-validated watershed. The exposure and damage
half runs end to end and is tested, but has not been validated against anything, and
its depth–damage curve constants are not transcribed from the source tables — see
[Damage: what is and is not trustworthy](#damage-what-is-and-is-not-trustworthy).
CSI is not reported at all, because it needs an observed extent polygon and the
Sentinel-1 route was dropped for want of credentials.

The full brief is in [docs/SPEC.md](docs/SPEC.md); the working rules are in
[CONTRIBUTING.md](CONTRIBUTING.md).

## What this method cannot do

Stated first, on purpose. HAND is a screening model, not a hydraulic one.

- **HAND assumes the water surface is parallel to the drainage line.** It has no
  representation of backwater, levees, culverts, or flow routed across a catchment
  boundary. The Addicks and Barker reservoir releases during Harvey are a sharp
  demonstration: water arrived downstream by a controlled release that no
  terrain-following model can infer.
- **One gauge is not one stage for a whole reach.** A single stage applied to every
  reach is an approximation whose error grows with distance from the gauge.
- **Sentinel-1 is a lower bound on urban extent.** SAR misses water under tree canopy
  and in dense urban areas (double bounce), so agreement metrics against it are not
  symmetric in meaning. That is why the primary reference here is 2,298 surveyed
  high-water marks, with SAR as a secondary check.
- **Population grids disagree with each other by tens of percent** in small towns.
  floodline reports Census block groups, WorldPop and HRSL rather than picking one —
  but they agree far better across a metro the size of Houston than they would in a
  small town, so this comparison is weaker here than it would have been at Lismore.
  That is the main thing given up by choosing Harvey as the primary case.

## Where it stands against ground truth

Validated against USGS surveyed high-water marks in the gauged watershed
(HUC 1204010403, Whiteoak Bayou–Buffalo Bayou, 491 km² at 10 m, 16 quality-1/2
marks). Harvey peak discharge 1,433 m³/s at gauge 08074500.

| | mean residual | RMSE | within 1 m | modelled extent |
|---|---|---|---|---|
| Constant stage from one gauge | +6.72 m | 7.86 m | 12% | 464 km² (98% of the unit) |
| Best-fit constant, fitted to the marks | −0.74 m | 4.13 m | 0% | — |
| **Per-reach synthetic rating curves** | **+1.04 m** | **1.38 m** | **50%** | **117 km² (25%)** |

Two things that matter more than the headline number.

**A constant HAND threshold cannot work here, at any value.** The threshold that
would place the water correctly ranges from −0.19 m to 12.32 m across those 16
points, because HAND measures each floodplain cell against its *nearest* drainage —
usually a small tributary, not the main stem. Fitting the best possible single value
still misses every mark by more than a metre. Giving each reach its own stage from
its own geometry is what fixes it.

**Finer data made the constant-threshold model worse, not better.** At 30 m the
channel bed at the gauge reads 4.81 m NAVD88; at 10 m it reads 0.89 m, because 10 m
resolves the channel. `stage − bed` therefore grew from 7.97 m to 11.89 m and
everything flooded deeper. Resolution only helps once the stage conversion is right.

**And the improvement cost recall.** 10 of 16 marks now fall inside the modelled
extent, against 16 of 16 before. The old model "hit" every mark by flooding 98% of
the watershed. This is exactly why hit rate alone is a useless metric and why CSI
and bias are reported alongside it.

## Status

Terrain, hydraulics, exposure and damage all run. Validation is where the gaps are:
extent is measured against surveyed high-water marks, and nothing else is measured
against anything. Every number in this README was produced by running the code on
this machine.

| Phase | Scope | State |
|---|---|---|
| 0 | Scaffold, config, raster I/O, synthetic fixture, CLI, CI | done |
| 1 | `fill`, `flowdir`, `flowacc`, `streams`, `hand` — numba, property-tested | done, plus flat resolution |
| 2 | Stage handling, inundation, buildings, population | done |
| 3 | Depth–damage curves, costs, Monte Carlo | code done; curve constants unverified |
| 4 | SAR validation, resolution and population experiments | not started |
| 5 | Rendered report and write-up | not started |
| — | Live compute service and national map UI (unplanned, built anyway) | done |
| — | Overture and population fetchers, flood-frequency context, `assess` | done |

## Where the data actually comes from

Every source below was probed, not assumed. Two of the obvious ones do not work, and
the design follows from that rather than around it.

| Source | Probe result | Used |
|---|---|---|
| USGS 3DEP, TNM API | COG range reads over `/vsicurl/` | yes — DEM |
| USGS NWIS peak flow | open RDB | yes — discharge and the 90-year record |
| USGS STN high-water marks | open JSON | yes — validation |
| USGS WBD MapServer | open | yes — watershed boundaries |
| Overture Maps buildings | anonymous S3, GeoParquet with a `bbox` column | yes — footprints |
| WorldPop 100 m | 494 MB, **advertises `Accept-Ranges` and ignores it** | yes — downloaded once |
| GHS-POP 100 m | zip directory at the end of a multi-GB file | no — too slow to window |
| Microsoft US Building Footprints | 206, range-readable | no — Overture carries height and class |
| LandScan Global / USA | **403**, registration form | no — gated |
| Census ACS block groups | **"Missing Key"** | no — needs `CENSUS_API_KEY` |

The WorldPop result is the one that shapes the code. Its server says it supports HTTP
range requests and then answers one with `200` and the entire body, so the windowed
read that works for 3DEP is impossible. The national raster is fetched once and
windowed locally afterwards, and that download is opt-in:

```bash
uv run floodline fetch-population
```

LandScan would be the better product and is licensed CC BY, but every download path is
behind a registration form. The rule here is that a source needing a login is recorded
as unavailable rather than worked around, so it is named and left out.

## Damage: what is and is not trustworthy

The pipeline is real and tested. The constants are not all real.

**Trustworthy:** the count of buildings the model floods, the loss *ratio*, the
relative comparison between curve families, and how the interval responds to each
source of error. These follow from the depth raster and the curve shapes.

**Not trustworthy:** any absolute currency figure. The bundled curves carry the shape
of each published family — HAZUS residential saturating near two-thirds, the JRC
continental curves rising faster to unity — but their digits have not been checked
against FEMA's technical manual or Huizinga et al. (2017). Every bundled curve is
marked `verified=False`, that flag propagates into the result object, and the CLI
prints a warning on every run. Transcribe real tables and pass them with
`floodline damage --curves`, which sets the flag and silences the warning.
Replacement costs per m² are likewise stated assumptions, not quotes.

**What the Monte Carlo covers:** gauge stage error, DEM vertical error, curve-family
choice, and replacement cost. **What it does not:** storey counts, floor area,
finished-floor freeboard, building class assignment, footprint-database completeness,
and HAND's structural assumption. The interval is a lower bound on the real
uncertainty — an honest account of four known errors, not of everything that could be
wrong.

## Install

Requires [uv](https://docs.astral.sh/uv/) and Python 3.12.

```bash
uv sync --all-extras --all-groups
uv run pre-commit install
```

## Any watershed in the United States

```bash
uv sync --all-extras --all-groups
uv run floodline serve          # then open http://127.0.0.1:8000
```

Every watershed in the country is drawn on the map — the boundary layer switches from
regions to subwatersheds as you zoom — so you can see what you are choosing before you
choose it. Click one, or type a ZIP code, address, or HUC code, and pick the level
(HUC-8 basin, HUC-10 watershed, HUC-12 subwatershed) a click should resolve to. The service
fetches the watershed boundary, finds the 3DEP tiles that intersect it, reads them
over HTTP range requests without downloading them, runs the whole chain, and
returns a bundle the browser uses to recompute the flood live as you move a
discharge slider. Measured cold, over an ordinary connection:

| Watershed | Area | Total |
|---|---:|---:|
| Lower North Branch Chicago River | 109 km² | 13 s |
| Upper Biscayne Bay, Miami | 165 km² | 16 s |
| Balch Creek–Willamette, Portland | 168 km² | 21 s |
| Outlet Charles River, Cambridge | 154 km² | 29 s |
| Whiteoak Bayou, Houston | 491 km² | 25 s |

Cached afterwards, so the second request is a file read. The analysis CRS follows
the watershed — a NAD83 UTM zone chosen from its centroid — because a fixed
projection is only correct for a fixed study area.

Each watershed finds **its own gauge**: every NWIS station publishing discharge
inside it is snapped to the derived stream network, and the one draining the largest
area wins. The discharge used is that gauge's peak of record — the worst flow it has
actually measured. Where the surveyed marks all come from one flood, the model is
driven by *that* flood's peak instead, so the comparison is between one event and
itself rather than between two. Watersheds with no gauge fall back to a labelled
scenario of 5 m³/s per km², and the interface says so wherever it shows a number.

**38,230 surveyed high-water marks** across 258 named events — Harvey, Irene, Sandy,
Matthew, the 2019 Central US floods — are overlaid wherever they exist, and agreement
is recomputed live as you move the slider. USGS grades each mark; only the graded
surveys (quality 1–2) set the reported RMSE, with the rougher ones drawn faded, since
on one measured watershed the graded marks gave 1.03 m and the quality-3 marks 4.93 m.

"Find the discharge the marks imply" sweeps the range and reports both the best fit
that keeps most marks wet and the unconstrained minimum — because a mark left dry
scores only the depth of water that was there, so minimising RMSE alone drives toward
flooding nothing.

It has to be served rather than published as a static page: an Artifact's content
security policy forbids `fetch` to external hosts, so a page delivered that way can
only carry what was inlined into it.

## Use

The whole chain for any US watershed, on live data — terrain, hydraulics, footprints,
population, damage, and where the discharge sits in its gauge's record:

```bash
uv run floodline assess 1204010403 --resolution 30 --samples 800 --out damage.parquet
```

Every stage that cannot reach its data is reported as a gap rather than filled with a
default. The Overture read is the slow part, minutes rather than seconds, and is cached
per release and bounding box under `data/cache`.

Exposure and damage separately, once you have a depth raster and footprints in the
analysis CRS:

```bash
uv run floodline exposure depth.tif buildings.parquet exposed.parquet \
    --unclamped-depth margin.tif --population pop.tif
```

```bash
uv run floodline damage exposed.parquet damage.parquet --samples 1000
```

`--unclamped-depth` is `stage - HAND` before the floor at zero, so dry ground carries
a negative value. It matters more than it looks: the depth raster records every dry
building as exactly 0, which loses the difference between a building the water missed
by a centimetre and one it missed by five metres. Without it the Monte Carlo holds
every dry building dry, and the count interval becomes conditional on the
deterministic extent rather than a real interval.


```bash
uv run floodline --help
uv run floodline config                              # resolved configuration as JSON
uv run floodline synth dem.tif                       # synthetic catchment DEM (COG)
uv run floodline condition dem.tif filled.tif        # priority-flood depression filling
```

Depression filling is Barnes, Lehman & Mutlu (2014) Priority-Flood with the
plain-FIFO improvement, in numba. On this machine (M-series, float32) it fills a
1024 x 1024 grid in about 85 ms and a 512 x 512 grid in about 20 ms — near-linear
in cell count. It is checked three ways: property tests for the invariants in the
spec, hand-built surfaces whose answer is obvious by inspection, and a differential
test against [pysheds](https://github.com/mdbartos/pysheds), which fills by
morphological reconstruction — a completely different algorithm. The two agree to
1e-9 on random grids, on the synthetic catchment, and across nodata.

D8 flow direction ranks neighbours by *slope* — drop divided by centre-to-centre
distance — so a diagonal must be sqrt(2) times further down to beat a cardinal
one, and anisotropic cells are ranked correctly. Output is ESRI direction codes
plus three explicit sentinels: nodata, "drains off the edge of the data", and
"flat, D8 undefined here". 1024 x 1024 in about 22 ms, 512 x 512 in about 6 ms.

Two policies worth knowing, because they are where we differ from pysheds:

- **Flats are reported, not guessed.** Filling with `fill_epsilon = 0` leaves
  filled depressions perfectly flat, and a cell in the middle of one has no
  strictly lower neighbour — D8 is genuinely undefined. Setting
  `terrain.fill_epsilon > 0` gives those surfaces a gradient, after which every
  interior cell has exactly one downstream neighbour.
- **Ties break to the lowest ESRI direction code.** When two neighbours offer the
  same steepest slope either answer is correct; ours is written down next to the
  loop that implements it. pysheds prefers north.

The differential test asserts that every cell where we disagree with pysheds falls
into one of three documented categories — edge of the data, flat, or tied steepest
descent — and that the categories are identified independently of both
implementations. On the synthetic catchment there are no unexplained
disagreements.

Filled depressions are flat, and D8 is undefined in the middle of a flat, so
`flowdir` reports `FLOW_FLAT` there rather than inventing a direction. That is not
a corner case: with an epsilon-free fill, **26% of the plain synthetic catchment
and 92% of the rough one** drained into a flat and never reached an outlet.
`terrain/flats.py` implements Barnes et al. (2014b), which gives each flat an
artificial gradient in a *separate* integer field, so the elevations — and
therefore HAND, and every depth derived from it — stay exactly as filling left
them. With it on (the default) the flat-drainage count is zero on every fixture.

`hydraulics/` turns a gauge reading into an extent. There is no default gauge
datum: a reading is relative to that gauge's own zero, so `require_gauge_datum()`
refuses to run without one rather than silently placing the whole flood at the
wrong elevation. The connectivity filter drops wet regions with no path to a
stream cell, which is what stops the map showing flooded paddocks a kilometre from
the channel.

pysheds is an oracle for the tests only. Nothing under `src/` imports it.

## Performance

Apple M-series, float32 input, single-threaded, measured by `pytest-benchmark`.
The full chain is fill → flow direction → flat resolution → accumulation →
streams → HAND.

| Stage | 512² (0.26 M) | 1024² (1.0 M) | 4096² (16.8 M) |
|---|---:|---:|---:|
| Depression fill | 20 ms | 85 ms | — |
| D8 flow direction | 5.6 ms | 22 ms | — |
| **Full terrain chain** | — | **176 ms** | **3.36 s** |
| Peak RSS, full chain | — | 421 MB | 2.1 GB |
| Time per cell | — | 166 ns | 201 ns |

16× the cells costs 19× the time, so the chain is near-linear; the drift is the
priority queue's log factor and cache pressure. 4096² is about 17 million cells,
the order of a Lismore 1 m tile set. Memory is the binding constraint, not time:
priority-flood is global and holds roughly 25 bytes of scratch per cell, so it
cannot be tiled without a merge step across tile boundaries.

Reproduce with:

```bash
uv run pytest tests/integration -q --benchmark-only
```

Every threshold, tolerance, CRS and curve choice is a field on a pydantic model in
[`config.py`](src/floodline/config.py). Point any command at a TOML file with
`--config` to change them; unknown keys are an error rather than a silent no-op.

Three kinds of CRS are refused outright, not warned about. Geographic, because cell
sizes in degrees make every slope, area and distance wrong. Projected-but-not-metres
(US survey feet), because lengths silently rescale. And whole-world Mercator
(EPSG:3857 and friends), whose axis unit is nominally the metre but whose scale
factor is 1/cos(latitude) — about 15% too long at Houston, with areas out by 30%.
That last one matters in practice: both the USGS 3DEP and the NSW elevation image
services serve 3857 natively, so it is the CRS a fetched raster is most likely to
arrive in. The Harvey work is in NAD83(2011) / Texas South Central (EPSG:6587).

## Development

```bash
uv run pytest -q                        # tests
uv run ruff check src tests             # lint
uv run mypy --strict src                # types
uv run pre-commit run --all-files       # everything CI runs
NUMBA_DISABLE_JIT=1 uv run pytest -q    # step through the kernels in pure Python
```

The numba kernels are pure functions — arrays in, arrays out, no I/O, no Python
objects — so `NUMBA_DISABLE_JIT=1` gives an identical, debuggable code path. CI runs
the suite both ways.

## Data

No data is committed. Raw downloads go in `data/raw/` (git-ignored), and every URL,
retrieval date and SHA256 is recorded in [`data/MANIFEST.md`](data/MANIFEST.md),
which is generated by the fetcher rather than maintained by hand.

```bash
uv run floodline fetch --list        # what exists, and what each source needs
uv run floodline fetch --dry-run     # how much it would download
uv run floodline fetch               # everything automatable, with checksums
```

Five sources, all public and keyless: USGS 3DEP elevation (1 m / 10 m / 30 m), USGS
NWIS gauge height and site metadata, USGS surveyed high-water marks, OpenFEMA NFIP
claims, and a Sentinel-1 RTC scene search. Downloading the Sentinel-1 assets does
need a Planetary Computer key; supply it in an environment variable, which the
fetcher reads and never writes anywhere.

Two things worth knowing before running it:

- **The full AOI at 1 m is 158 tiles and 56.6 GB.** A fetch over
  `case.dem_max_download_gb` (10 GB by default) is refused rather than started.
  At roughly 125 bytes of peak memory per cell that volume is also well past what
  the global priority-flood can hold in one pass, so the AOI needs narrowing for a
  1 m run regardless of disk.
- **NWIS reports gauge height in feet.** `hydraulics.gauge_reading_unit` has no
  default for the same reason the datum offset has none: the real Harvey peak at
  Buffalo Bayou is 41.90 ft, and reading that as metres would put three times the
  water over the city.

## Licence

MIT.
