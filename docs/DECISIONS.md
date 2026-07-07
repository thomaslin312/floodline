# Decisions

Judgment calls made without asking, one line each: what was decided, why, and what
the alternative was. Newest last. Dates are the day the decision landed.

Entries marked *(retro)* were made and reported in conversation before this log
existed; they are recorded here so the review has one place to look.

## 2026-09-03

- *(retro)* **Deleted `floodline_SPEC.md` after splitting it** into `CLAUDE.md` and
  `docs/SPEC.md`. Why: the file's own header says it becomes those two once the repo
  exists, and a third copy would drift. Alternative: keep it as an archive — rejected
  because two sources of truth for conventions is exactly what the conventions warn about.
- *(retro)* **`git init` without committing, at scaffold time.** Why: pre-commit needs a
  work tree to install into, and the hooks could not otherwise be verified. Alternative:
  defer git entirely — rejected because the hook config would then be untested.
- *(retro)* **pre-commit runs ruff/mypy/pytest as local `uv run` hooks, not pinned
  upstream `rev`s.** Why: the hook and CI then enforce exactly the versions in `uv.lock`,
  so there is one source of truth for tool versions. Alternative: `ruff-pre-commit` at a
  pinned rev — rejected because it drifts from the locked ruff and produced a
  "legacy alias" warning.
- *(retro)* **`pysheds` is a `dependency-groups.oracle` group, never a runtime dep.**
  Why: the spec wants differential tests against it but forbids it in the pipeline.
  Tests `importorskip` it. Alternative: a test-only vendored copy — rejected as overkill.
- *(retro)* **Synthetic pits are deepened until they provably close.** Why: a sampled
  Gaussian on a tilted plane often still drains downslope, so the fixture would not
  actually exercise the depression filler. Alternative: trust the sampled depth and
  make the plane shallower — rejected because it couples fixture realism to pit validity.
- *(retro)* **`Pit` records both the Gaussian centre and the true sink cell.** Why: the
  background slope drags the lowest cell a cell or two downhill of the centre, and
  terrain tests need the sink. Alternative: snap the centre to the sink — rejected
  because then the recorded radius/depth would not describe the surface actually built.
- *(retro)* **`fill_depressions` returns the input dtype; epsilon is validated against
  it.** Why: float32 halves memory on 1 m tiles, but an epsilon smaller than the float32
  ulp at the DEM's peak elevation would silently be a no-op on every step. Alternative:
  always promote to float64 — rejected on memory grounds for the full Lismore tile set.
- *(retro)* **D8 ties break to the lowest ESRI direction code (E first).** Why: it is a
  one-sentence rule that can be pinned by a unit test. Alternative: match pysheds, which
  prefers north — rejected because "match whatever the oracle does" is not a rule anyone
  can check without running the oracle. Consequence: ties are an expected differential
  disagreement category, not only flats.
- *(retro)* **Flow leaves the data at its edge (`FLOW_OUTLET`), rather than being marked
  flat as pysheds does.** Why: accumulation has to terminate at the boundary, and it is
  consistent with filling, which already treats border and nodata as outlets.
  Alternative: pysheds' behaviour — rejected because it leaves the outlet row
  indistinguishable from a genuine undefined-direction cell.
- *(retro)* **`FLOW_FLAT` is reported rather than guessed.** Why: on an epsilon-free fill
  D8 is genuinely undefined mid-flat, and inventing a direction hides that. Alternative:
  fall back to an arbitrary direction — rejected as silently wrong. Superseded in part by
  the flat-resolution entry below.

### flowacc

- **Accumulation is carried in float64, with one code path for weighted and
  unweighted.** Why: float64 represents integers exactly to 2**53, so a plain cell
  count stays exact for any raster that fits in memory, and weighting (cell area, a
  rainfall field) comes free. Alternative: int64 for counts and a separate float
  path — rejected as two kernels to keep in step for no accuracy gained.
- **`FlowAccumulation` reports `cells_draining_to_outlets` and
  `cells_draining_to_flats` separately, rather than just returning the array.**
  Why: the spec's invariant "total accumulation at outlets = number of non-nodata
  cells" is *false* whenever flats exist, and silently returning an array hides
  that. Measured on the fixtures with `fill_epsilon = 0`: 26% of the plain
  synthetic catchment and 92% of the rough one drain into a flat and never reach
  an outlet. Alternative: assert the spec invariant directly — rejected because it
  would only have been satisfiable by quietly requiring epsilon filling everywhere.
- **The differential test against pysheds compares totals, the top-percentile
  cells, and the sub-grid where both route identically — not the whole array.**
  Why: accumulation inherits every flow-direction tie-break difference, and one
  differently-routed cell high in a catchment relocates a whole sub-basin's area,
  so a cell-by-cell comparison would measure the tie-break rule rather than the
  accumulation algorithm. Alternative: compare everything with a loose tolerance —
  rejected because a tolerance wide enough to pass would be wide enough to hide a
  real error.
- **`np.in1d` is restored in the differential conftest so the pysheds oracle runs
  under NumPy 2.** Why: pysheds 0.5 predates NumPy 2.0, which removed the alias;
  everything else in it works. Alternatives: pin NumPy < 2 for the whole project
  (holds the pipeline back for a test-only oracle) or drop the accumulation
  differential tests (loses real coverage). The shim reproduces `in1d`'s flattening
  contract rather than aliasing `isin` naively, and lives only in test code.

### streams

- **A separate `io/vector.py` with the same CRS policy as `io/raster.py`.** Why: the
  spec puts vectors in GeoParquet and refuses geographic CRSs everywhere, and a
  network written straight from `geopandas` would bypass that check. Alternative:
  call `to_parquet` at the call site — rejected because the CRS refusal would then
  live in whichever module happened to write a file.
- **`types-geopandas` added to the dev group rather than an `ignore_missing_imports`
  override.** Why: real stubs type-check the geopandas calls; an override silences
  them. Alternative: add geopandas to the existing mypy override list — rejected as
  strictly less checking for the same effort.
- **Each stream cell belongs to exactly one link; a junction belongs to the link it
  begins, not to the tributaries feeding it.** Why: the first cut let tributaries
  share their junction cell, which made each of them report the junction's
  accumulation — including the sibling's water — as its own outflow. `acc_outflow`
  is now this reach's own discharge. Alternative: keep the shared cell and add a
  separate "outflow excluding siblings" column — rejected as two conventions where
  one will do.
- **Links whose geometry would have fewer than two points are dropped, and the count
  is put in `frame.attrs["dropped_degenerate_links"]`.** Why: a lone junction cell
  on the raster edge has nothing downstream to draw a line to. In the synthetic
  catchment this is exactly the network outlet, so it is not nothing — but it is one
  cell, and a LineString needs two points. Alternative: emit a zero-length or
  fabricated geometry — rejected as inventing a coordinate. The attribute does not
  survive a parquet round-trip; the partition test uses it in memory.
- **Pruning clips a channel's own headwater, not only side stubs.** Why: above a
  junction *both* branches are first-order and there is no privileged stem. This is
  intended — the topmost cells are what the accumulation threshold is least sure
  about — but it surprised the first version of the test, so it is now pinned
  explicitly. Alternative: protect the highest-accumulation branch at each junction
  — rejected as an arbitrary rule that would leave threshold artefacts on the stem.
- **Pruning iterates to a fixed point (capped at `max_passes`).** Why: removing a
  stub can turn its junction into a plain link and expose a branch that was not
  first-order before; a single pass would leave a result that depended on head
  visit order. A property test asserts pruning twice equals pruning once.
- **Strahler order is resolved iteratively in ascending head-accumulation order.**
  Why: that is a valid topological order (a tributary's head always carries less
  water than the junction it feeds), and a real river is thousands of links deep
  against Python's recursion limit of one thousand. Alternative: recursion — it was
  the first version, and would have failed on Lismore rather than on the fixture.
- **Added a `floodline streams` command even though the spec's CLI list omits it.**
  Why: the network is a deliverable of this step and there was no way to produce it
  end to end. It also warns on stderr when accumulation is stranded in flats.
  Alternative: leave it library-only — rejected because an unreachable deliverable
  cannot be checked by anyone but a test.

### hand

- **Cells whose flow path never reaches a stream get NaN, and the count is
  reported on `HandResult`.** Why: a hillslope draining straight off the tile edge
  has no nearest drainage, and an inundation depth there would be meaningless.
  Alternative: fall back to the elevation above the flow path's terminus —
  rejected because that number looks like a HAND value and is not one.
- **`hand()` takes the filled DEM as an explicit argument rather than re-filling.**
  Why: `HAND >= 0` holds only because elevation is non-increasing downstream on the
  *conditioned* surface; passing the raw DEM would break the invariant silently.
  The docstring says so at the parameter. Alternative: fill internally — rejected
  as hiding an expensive step and re-deriving something the caller already has.
- **The HAND differential test compares cells where both implementations route
  identically, plus the distribution.** Why: pysheds derives its own flow
  directions inside `compute_hand`, so the comparison is of the whole chain. On the
  synthetic catchment 75.6% of cells are comparable and the agreement there is
  *exact* (max absolute difference 0.0), which the test now asserts rather than
  merely bounding by a tolerance.
- **pysheds leaves 39 stream cells NaN; the test asserts every one of them is on
  the raster border.** Why: it will not route a border cell off-raster, so it
  cannot find a drainage cell for one. Asserting the *reason* turns a tolerated
  difference into a check — an interior stream cell coming back NaN would now fail.
  Alternative: exclude border cells and say nothing — rejected as weaker.
- **`pysheds` boolean mask rasters need their own `ViewFinder` with
  `nodata=False`.** Why: pysheds validates that nodata is representable in the
  array dtype and the DEM's NaN is not a bool. Test-only plumbing, noted because it
  cost two attempts to find.
- **`floodline hand` derives the stream network itself unless `--streams` is
  given.** Why: the command is usable from a conditioned DEM alone, which is what
  the integration test and the eventual figures need. It reports on stderr how many
  cells have no drainage.

### flats

- **Flat resolution writes to a separate integer `flat_mask`, never to the DEM.**
  Why: an epsilon fill perturbs the elevations, so HAND inherits a few millimetres
  of fictional relief per flat cell and every depth derived from it carries that.
  Keeping the artificial gradient out of the elevation array keeps the artefact out
  of the results. Alternative: epsilon filling, which is cheaper and still
  supported via `fill_epsilon` — both now reach a fully draining grid, and a test
  asserts they do.
- **`flat_height` is the global maximum `d_high`, not a per-flat label.** Why: the
  term enters as `(flat_height - d_high)` and only its *differences between
  adjacent cells* matter to the descent guarantee, so a single constant works and
  saves a connected-component labelling pass. Alternative: Barnes' per-flat labels
  — rejected as machinery that changes no output here. Worth revisiting if a
  future use needs the per-flat height itself.
- **A flat cell never routes into higher non-flat ground.** Why: the first version
  treated *any* non-flat neighbour as an exit, so a flat could drain uphill into a
  cell whose own direction pointed straight back — 188 cells ended up in cycles on
  the first run. The exit test now requires `dem[neighbour] <= dem[cell]`, and the
  comment at that branch says why.
- **`terrain.resolve_flats` defaults to True.** Why: with it off and
  `fill_epsilon = 0` — the previous defaults — 26% of the plain synthetic catchment
  and 92% of the rough one never reached an outlet. A default that silently strands
  most of the grid is the wrong default. Alternative: default `fill_epsilon` to a
  non-zero value instead — rejected because it moves the artefact into the
  elevations.
- **The composition lives in a new `terrain/route.py`, not in `flowdir`.** Why:
  `flats` imports from `flowdir`, so putting the chain in either would make the
  import circular. It also gives the CLI, the integration test and the benchmark
  one definition of "the terrain chain" so they cannot drift. Alternative: repeat
  the six calls at each call site — rejected.
- **Measured result of step 4** (epsilon-free fill, `resolve_flats` on): plain
  120x90, 386 flat cells, all resolved, drain-to-flats 2829 -> 0; rough 90x70, 680
  resolved, 5805 -> 0; rough 200x160, 5717 resolved, 31014 -> 0. No cycles in any
  case. Every cell reaches an outlet.

### hydraulics

- **`gauge_datum_offset_m` has no default and `require_gauge_datum()` raises when
  it is None.** Why: the user asked for exactly this, and it is right — a gauge
  reading is relative to that gauge's own zero, which is not recoverable from the
  reading. Defaulting to 0.0 would put the whole modelled flood at the wrong
  elevation and every downstream number would be confidently wrong. 0.0 remains a
  legitimate value; it just has to be chosen. The error message names the Lismore
  gauge and where to get the offset.
- **A gauge reading below the channel bed is an error, not a zero depth.** Why: it
  is the commonest symptom of a wrong datum offset or a gauge snapped to the wrong
  cell, and silently clamping to zero would hide both.
- **The stage is a water *depth above the local channel bed*, not an AHD
  elevation.** Why: HAND thresholds on height above the nearest drainage, so the
  gauge's AHD stage has to be converted by subtracting the conditioned elevation of
  the gauge's own channel cell. Alternative: threshold HAND against an absolute
  elevation — wrong, it would flood by altitude rather than by depth.
- **`slope` stage propagation uses signed along-network distance from the gauge and
  clips at zero.** Why: it is a screening approximation, not a backwater solution,
  and far enough upstream the linear adjustment would imply a negative water depth.
  `constant` remains the default, since that is the plain HAND assumption the
  method is honest about. Cells the gauge's network never touches keep the gauge
  depth unchanged, there being no distance to adjust by.
- **`inundate` treats NaN HAND as permanently dry.** Why: those cells have no
  nearest drainage, so the model has nothing to say about them; wetting them would
  be inventing a result. They stay NaN in the depth raster rather than becoming 0,
  so "dry" and "unknown" remain distinguishable.
- **The connectivity filter needs an explicit stream mask and errors without one.**
  Why: "connected to a stream" is undefined otherwise. Alternative: fall back to
  no filtering — rejected as silently changing the result.
