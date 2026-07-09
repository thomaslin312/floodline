# floodline

Flood extent, exposure and damage estimation from a DEM and a gauge reading.

Given a lidar DEM, a stream network and a water level at a river gauge, floodline
produces an inundation extent and depth raster (HAND method), intersects it with
building footprints and population grids, and estimates people affected and direct
economic damage using published depth–damage curves — with a Monte Carlo uncertainty
band, validated against the Lismore, NSW flood of 28 February 2022.

**The claim being tested:** a screening-grade flood damage model built from open data
can reproduce the extent of a real major flood to within a stated CSI and put the
observed building count inside its 90% interval.

The full brief is in [docs/SPEC.md](docs/SPEC.md); the working rules are in
[CLAUDE.md](CLAUDE.md).

## What this method cannot do

Stated first, on purpose. HAND is a screening model, not a hydraulic one.

- **HAND assumes the water surface is parallel to the drainage line.** It has no
  representation of backwater, levees, culverts, or flow routed across a catchment
  boundary. Lismore's CBD levee (overtopping around 10.6 m) is a good demonstration
  of where the assumption breaks and where it holds.
- **One gauge is not one stage for a whole reach.** A single stage applied to every
  reach is an approximation whose error grows with distance from the gauge.
- **Sentinel-1 is a lower bound on urban extent.** SAR misses water under tree canopy
  and in dense urban areas (double bounce), so agreement metrics against it are not
  symmetric in meaning.
- **Population grids disagree with each other by tens of percent** in small towns.
  floodline reports mesh blocks, WorldPop and HRSL rather than picking one.

## Status

Phase 0 (scaffold) and Phase 1 (terrain core) are done, and Phase 2's hydraulics
half — stage handling and inundation — is in. Nothing has been run against real
Lismore data yet, so this README contains no flood results. It will not contain
any that were not actually produced. The numbers below are runtimes and
synthetic-fixture diagnostics, measured on this machine.

| Phase | Scope | State |
|---|---|---|
| 0 | Scaffold, config, raster I/O, synthetic fixture, CLI, CI | done |
| 1 | `fill`, `flowdir`, `flowacc`, `streams`, `hand` — numba, property-tested | done, plus flat resolution |
| 2 | Stage handling, inundation, buildings and population | hydraulics done; exposure needs data |
| 3 | Depth–damage curves, costs, Monte Carlo | not started |
| 4 | SAR validation, resolution and population experiments | not started |
| 5 | Rendered report and write-up | not started |

## Install

Requires [uv](https://docs.astral.sh/uv/) and Python 3.12.

```bash
uv sync --all-extras --all-groups
uv run pre-commit install
```

## Use

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

A geographic CRS as the analysis CRS is refused, not warned about — cell sizes in
degrees make every slope, area and distance in the terrain code wrong. The Lismore
work is in GDA2020 / MGA zone 56 (EPSG:7856).

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
retrieval date and checksum is recorded in [`data/MANIFEST.md`](data/MANIFEST.md).

## Licence

MIT.
