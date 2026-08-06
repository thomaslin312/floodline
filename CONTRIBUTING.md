# Contributing — floodline

Project brief lives in [docs/SPEC.md](docs/SPEC.md). This file holds the rules that
apply to every change.

## Commands

```bash
uv sync --all-extras --all-groups   # create/refresh .venv from uv.lock
uv run pytest -q              # test suite
uv run ruff check src tests   # lint
uv run ruff format --check .  # format check
uv run mypy --strict src      # types
uv run pre-commit run -a      # everything the hooks enforce
NUMBA_DISABLE_JIT=1 uv run pytest -q   # debuggable pure-Python path
uv run floodline --help       # CLI
```

## Conventions

- Conventional commits. One feature per PR-sized commit.
- `uv run pytest -q` and `uv run ruff check && uv run mypy --strict src` must pass before every commit; pre-commit enforces it.
- Every numeric threshold, tolerance, CRS and curve selection is a field on a pydantic model in `config.py`. Algorithm code never contains a literal that a user might want to change.
- Geographic CRS as analysis CRS is an error, not a warning. So is a projected CRS whose axes are not true metres: US survey feet, and whole-world Mercator (EPSG:3857 and friends), whose "metre" is inflated by 1/cos(latitude). Everything is in a local projected CRS in metres — NAD83(2011) / Texas South Central, EPSG:6587, for the Houston/Harvey case.
- Rasters are written as COGs with nodata set. Vectors are GeoParquet.
- numba functions are pure: arrays in, arrays out, no I/O, no Python objects. Keep a `NUMBA_DISABLE_JIT=1` path working so tests can be debugged.
- No results in the repo that weren't actually produced. If a data source fails, say so in the README and move on.
- Tests come with the code, not after it. Property tests for invariants, differential tests for agreement with the oracle, one small integration test end to end.
- Data is never committed. `data/MANIFEST.md` records provenance.

## Definition of done (for the resume)

- CI green on GitHub with tests, ruff, mypy.
- Harvey 2017 reproduced: extent validated against the 2,298 USGS surveyed high-water marks (elevation residuals) and, where a Sentinel-1 key is available, CSI against the SAR mask; building count within the MC interval against OpenFEMA NFIP claims (or an honest explanation of why not).
- The resolution experiment (1 m / 3 m / 10 m / 30 m) and the population-grid disagreement experiment written up with figures — the latter with its Houston caveat stated, since these grids diverge most in small towns.
- README states the method's limits before it states its results.
