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

Phase 0 (scaffold) and the depression-filling half of Phase 1 (terrain core) are
done. Nothing has been run against real Lismore data yet, so this README contains
no results. It will not contain any that were not actually produced.

| Phase | Scope | State |
|---|---|---|
| 0 | Scaffold, config, raster I/O, synthetic fixture, CLI, CI | done |
| 1 | `fill`, `flowdir`, `flowacc`, `streams`, `hand` — numba, property-tested | `fill` done; `flowdir` next |
| 2 | Stage handling, inundation, buildings and population | not started |
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

pysheds is an oracle for the tests only. Nothing under `src/` imports it.

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
