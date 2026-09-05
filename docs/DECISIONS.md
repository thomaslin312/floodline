# Decisions

Judgment calls made without asking, one line each: what was decided, why, and what
the alternative was. Newest last. Dates are the day the decision landed.

Entries marked *(retro)* predate this log; they are recorded here so the review has
one place to look.

## 2026-09-03

- *(retro)* **Deleted `floodline_SPEC.md` after splitting it** into `CONTRIBUTING.md` and
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
  it is None.** Why: a gauge
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

### benchmarks

- **The full-chain benchmark runs at 1024² and 4096², not on the small fixtures.**
  Why: 4096² is ~17 M cells, the order of a Lismore 1 m tile set, so it is the
  number that says whether the resolution experiment can be run repeatedly.
  Measured: 176 ms and 3.36 s, 166 and 201 ns/cell, peak RSS 421 MB and 2.1 GB.
- **Memory is reported alongside runtime, and named as the binding constraint.**
  Why: priority-flood is global and holds ~25 bytes of scratch per cell, so it
  cannot be tiled without a boundary merge. Time scales fine; memory is what will
  decide whether the full 1 m tile set runs in one pass.

### 2026-09-03 (late) — a bug the JIT-off path caught

- **`downstream_index` now bounds-checks and returns -1 for a direction pointing
  off the raster.** Why: it previously assumed every direction code pointed at an
  in-bounds neighbour, which is true of `flow_direction` output but not of a
  hand-built or externally supplied direction grid. Callers then indexed past the
  end of their arrays. numba does not bounds-check by default, so under JIT this
  was a silent out-of-bounds write; it only raised with `NUMBA_DISABLE_JIT=1`,
  which is exactly why the spec insists that path keeps working. Found by running
  the suite both ways after step 7. Two regression tests pin it.

## 2026-09-03 (evening) — change of primary validation case

- **The primary validation case moved from Lismore, NSW to Hurricane Harvey /
  Houston, 2017.** Decided after establishing what is actually reachable by API.
  Why: every Harvey input is public and keyless, including the 1 m bare-earth lidar
  (USGS 3DEP via the TNM Products API, 53 GeoTIFF tiles over the Houston AOI on
  anonymous S3), whereas the Australian equivalent (ELVIS) delivers an emailed ZIP
  from an Angular SPA with no documented REST API. Two consequences beyond
  convenience: the gauge datum, which the original brief called the thing that
  "bites everyone", is an explicit API field (`alt_datum_cd = NAVD88`); and
  validation improves from an Otsu threshold on SAR backscatter to 2,298
  ground-surveyed high-water marks. The original brief already named Harvey as the
  secondary case for exactly the ground-truth reason, so this reorders rather than
  contradicts it.
  **What was given up, and it is real:** experiment 4, the population-grid
  disagreement, wanted a small town, because that is where mesh blocks, WorldPop and
  HRSL diverge by tens of percent. Across a metro the size of Houston they agree
  fairly well, so that experiment is weaker. This is stated in `docs/SPEC.md`, in the
  README's limits section, and will be stated in the write-up. Lismore is retained as
  the secondary case and the experiment should be re-run there if it happens.
  Also given up: the Australian framing, and the Lismore levee as the HAND-breaks
  demonstration — replaced by the Addicks and Barker reservoir releases, which are
  arguably a sharper example, since a controlled release is water arriving by a route
  no terrain-following model can infer at all.
- **Default analysis CRS changed from EPSG:7856 (GDA2020 / MGA zone 56) to EPSG:6587
  (NAD83(2011) / Texas South Central).** Both are projected in true metres. The
  synthetic fixture's default CRS and origin moved with it so the fixtures stay
  consistent with the analysis CRS and `read_raster` does not reject them. EPSG:7856
  remains an accepted value, so the Lismore case needs only a config change.
- **Whole-world Mercator is now refused as an analysis CRS.** Why: EPSG:3857 passed
  the old check — it is projected and its axis unit is nominally the metre — but the
  scale factor is 1/cos(latitude), so at Houston a "metre" is about 15% too long and
  areas are out by 30%. This is not hypothetical: both the USGS 3DEP and the NSW
  elevation image services serve 3857 natively, so it is the CRS a fetched raster is
  most likely to arrive in. Detection is by projection method name — refuse when it
  contains "Mercator" but not "Transverse" — which catches 3857, 900913, 3785 and
  World Mercator (3395) while leaving UTM, MGA and Lambert Conformal Conic alone.
  Alternative: blocklist EPSG:3857 by code — rejected because 900913 and 3785 are the
  same projection under different codes.
- **The survey-foot counter-example in the CRS tests changed from EPSG:2277 to
  EPSG:6588.** Why: 6588 is the US-survey-foot twin of the new analysis CRS, so it is
  the specific mistake most likely to be made on this project. 2277 is kept too.

### io/sources.py

- **Every source is a `Source` in one registry, with the event parameters on
  `CaseConfig`.** Why: swapping to Lismore is then a config change, not a code
  change, and `--list` can tell you what exists and what each needs. Alternative:
  a script per dataset — rejected; there would be no single place to see what the
  project depends on.
- **Credentials come from named environment variables and are never read, stored
  or logged by this module.** A source declares `credential_env`; `CredentialError`
  names the variable when it is missing. Alternative: a `.env` file or a config
  field — rejected, because both end up in the repo eventually.
- **Retry with exponential backoff, on transport errors and 429/5xx only.** Why:
  the TNM products endpoint returned a 500 on a real run minutes after serving the
  identical query, and a single attempt is not a fair test of whether a dataset is
  reachable. A 404 or 400 is *not* retried — the first version caught
  `httpx.HTTPError`, which `raise_for_status` raises for a 404 too, so it retried
  permanent failures three times before giving up. A test caught that.
- **Downloads land on a `.part` file and are renamed only when the stream
  completes.** Why: an interrupted fetch would otherwise leave a truncated GeoTIFF
  that looks finished, and the next run would reuse it. Cleaned up on failure and
  on `KeyboardInterrupt`, both tested.
- **A DEM fetch over budget is refused, and `--dry-run` reports the size first.**
  Why: the full AOI at 1 m is **158 tiles and 56.6 GB**, which is easy to start by
  accident, and at roughly 125 bytes of peak memory per cell is far past what the
  global priority-flood can hold in one pass. `case.dem_max_download_gb` defaults
  to 10 GB. Alternative: just download it — rejected.
- **1/9 arc-second (~3 m) is mapped but dropped from the default resolutions.**
  Why: it returns **zero tiles** over Houston. That is a property of 3DEP coverage,
  not of the query, so the source raises rather than silently skipping. The
  resolution experiment runs at 1, 10 and 30 m.
- **OpenFEMA is paged.** Why: the API caps a response at 10,000 records and the
  first run returned exactly 10,000 — which looked plausible. Paging returns
  **90,779 claims**. Taking the first page would have made the damage validation
  nine times too small, silently.
- **Tests run against `httpx.MockTransport`, never the network.** Why: a suite that
  needs the internet fails for reasons unrelated to the code. The real code paths —
  streaming, hashing, `.part` rename, retry, paging, the budget — are all exercised.
- **The manifest lists sources that failed, not just those that succeeded.** Why: a
  manifest that silently omits what did not arrive reads as a complete record.

### what the real data revealed

- **`hydraulics.gauge_reading_unit` is now required, like the datum.** Why: USGS
  NWIS reports gauge height in **feet**. The real Harvey peak at Buffalo Bayou is
  41.90 ft = 12.77 m; feeding 41.9 to a model that assumes metres would have put
  three times the water over Houston, and nothing downstream would have complained.
  This is the same class of un-guessable, catastrophic-if-wrong input as the datum,
  so it gets the same treatment: no default, and a `require_` accessor that refuses.
  `GaugeStage.reading_m`/`ahd_m` became `reading`/`datum_elevation_m`, since neither
  is AHD any more and the reading is not necessarily in metres.
- **The datum chain was verified end to end against real data.** NWIS states
  `alt_datum_cd = NAVD88` and `alt_va = 0.00` (method `L`, levelled, ±0.1 ft) for
  all three Houston gauges, so gage zero is surveyed at 0.00 ft NAVD88 and stage is
  NAVD88 elevation directly. Cross-check: peak 41.90 ft on 2017-08-28 01:00 (the
  correct Harvey timing) against a 30 m DEM ground elevation of 10.00 m (32.8 ft) at
  the gauge — about 9 ft of water over the surrounding land, which is plausible and
  consistent. `gauge_datum_offset_m = 0.0` is therefore correct here, on evidence
  rather than by default.
- **Open item: the 3DEP tiles arrive in EPSG:4269, a geographic CRS.** `read_raster`
  correctly refuses them, which is the CRS policy working exactly as intended, but
  it means conditioning needs a reprojection step before any of this data reaches
  the terrain core. Not built yet; it is the next piece of work.

### io/ingest.py, and the first real run

- **Ingest is the one module allowed to read a raster in a CRS the policy refuses.**
  Why: 3DEP publishes EPSG:4269, so `read_raster` rejects every tile — correctly.
  Something has to be permitted to open them, and confining that permission to one
  module whose entire job is to hand back an analysis-CRS raster is better than
  loosening the check. Alternative: relax `read_raster` — rejected; the refusal is
  the feature.
- **Tiles are grouped by bounds, not by filename, and the default vintage is the
  survey nearest the event.** Why: 3DEP publishes several surveys of the same
  ground — Houston has 2018, 2020, 2024 and 2026 versions — and modelling a 2017
  flood on 2026 terrain routes water over land that did not exist yet. On the real
  tiles this collapses 13 files to 4 footprints, choosing 2018–2020. Grouping by
  bounds means a change in USGS naming cannot silently split one footprint into
  several. `case.dem_vintage` can be set to `newest` instead.
- **`WarpedVRT` per tile, then merge.** Why: the warp then happens lazily per block
  rather than materialising each tile at full size first. Alternative: reproject
  each tile to a temporary file — rejected as slower and needing scratch disk.
- **Output is clipped to the AOI and refused above `max_cells`.** Why: the tiles
  cover four degrees for an AOI of less than one, and depression filling is global.
  Measured: the AOI is 6.1M cells at 30 m, 54.6M at 10 m, and **5.46 billion at 1 m
  (~683 GB peak)**. The 1 m run needs a much smaller AOI regardless of disk space.
- **Vertical datum is not transformed, and the docstring says so.** 3DEP and USGS
  gauge datums are both NAVD88 here, so they are already consistent. A case mixing
  vertical datums would need a step that does not exist.

### first real result, and what it says

Ran end to end on real Houston terrain at 30 m. The pipeline works; the *model*, as
configured, does not, and the number says so:

- Terrain chain: 6.1M cells in **1.7 s**. 20% of cells raised by filling, and
  **1,328,007 flat cells (22% of the grid)** — against 386 on the synthetic
  fixture. Houston's coastal plain is the case flat resolution was built for. All
  resolved; the grid drains completely.
- Inundation at the real Harvey peak (Buffalo Bayou 08074000, 41.90 ft on
  2017-08-28 01:00, = 12.77 m NAVD88, 7.97 m above the modelled bed) floods
  **5,067 km² of a 5,464 km² AOI — 93% of greater Houston.** That is plainly wrong.
- Against the 192 quality-1/2 surveyed high-water marks: 97% "hit rate", but the
  modelled water surface sits **+5.4 m above the surveyed marks (RMSE 5.86 m)**, and
  only 2% are within a metre. A high hit rate here means "flooded everything",
  not "got it right" — which is exactly why hit rate alone is a bad metric and why
  the spec asks for CSI and bias too.

Diagnosed rather than guessed. Restricting the stage to the gauge's own contributing
area (528,617 cells = 476 km², against a published drainage area of 870 km²) brings
the extent to a plausible 447 km², but the residual inside that catchment is still
+4.5 m. So there are two distinct problems, and the second is the interesting one:

1. **One gauge is being applied to a whole metropolitan region** of independent
   bayous, which the spec already names as a known failure mode. Scoping the stage
   to gauged catchments fixes the extent.
2. **At 30 m the channels are not resolved.** HAND is measured to whichever cell the
   accumulation threshold called drainage, and on a flat coastal plain at 30 m that
   is often a shallow tributary whose bed sits metres above the main channel.
   Adding the main channel's 8 m depth to that reference over-predicts everywhere.

Both are method limits, not implementation bugs, and both are what the resolution
experiment and the multi-gauge stage work exist to quantify. The spec predicted the
first half of this: "SRTM 30 m is much worse — which is what most global flood
products use." It is worth stating in the write-up that a 97% hit rate accompanied
a 5.9 m RMSE.

## 2026-09-04

- **Sentinel-1 dropped as a validation reference.** There is no Planetary Computer
  access here, and every RTC path needs a credential — MPC declares
  `msft:requires_account`, Copernicus Data Space and ASF need their own logins. The
  only keyless option is raw GRD on AWS, which is not terrain-corrected, so using it
  would mean the SNAP-style preprocessing the spec explicitly rules out. This costs
  the extent-CSI-against-SAR experiment and nothing else: the 659 surveyed
  high-water marks are the better reference anyway, being ground-truthed water
  surface elevations rather than a backscatter threshold that is blind under canopy
  and in dense urban areas. To be stated in the README as a source that did not work
  out, per the convention.
- **1 m deferred, not abandoned.** At 10 m a typical Houston single-family footprint
  (~15 x 12 m) is **1.8 cells**; at 30 m it is **0.2**, and at 1 m it is 180. So 10 m
  is adequate for extent and for the resolution comparison, and marginal for
  per-building depth — a p90 over two cells is barely a statistic. Revisit for the
  damage step if the building-level numbers look noisy, and then on a single
  subwatershed rather than the metro.
- **The unit of work is a watershed, not a bounding box.** Measured on the Houston
  30 m grid: computing the terrain chain on a clipped 800x800 window instead of the
  full grid left **4.3% of HAND cells more than 0.5 m out, worst case 10.8 m**, lost
  **12% of the stream network**, and saw a maximum flow accumulation of 340,293 cells
  against the true 886,740 — because the window cannot see its own contributing area.
  93.6% of cells were identical, which is exactly what makes this dangerous: it looks
  fine. A HUC is hydrologically complete, so the same computation over one is correct
  throughout. `case.huc_level` defaults to 10 (21 units over the AOI, median 540 km2,
  largest 1136 km2 = 25.5M cells = 3.2 GB at 10 m — comfortable). At 1 m the largest
  is 2,546M cells, which is the concrete reason 1 m needs either a much larger machine
  or a tiled priority-flood with boundary merge.
- **Cells outside the watershed boundary become nodata rather than being left in.**
  Why: filling and routing then treat the divide as the edge of the data, which is
  correct — water leaving the watershed has left the domain. Alternative: clip to the
  bounding box only — rejected, since it reintroduces exactly the contributing-area
  error the watershed unit exists to avoid.
- **Storage: raw stays local, derived products are what would go online.** `data/raw`
  is 890 MB and fully reproducible from public APIs via `sources.py` with checksums in
  the manifest, so hosting it duplicates something already reproducible. The terrain
  products (HAND, flow direction, streams) are the expensive part and the part an
  interactive layer reads windows of; COGs on object storage with HTTP range reads is
  what they are for, and the spec already mandates COG output. Not built yet.

### the 10 m watershed run, and why it is worse

Ran HUC 1204010403 (Whiteoak Bayou-Buffalo Bayou, 491 km2, contains gauges 08074000
and 08074500 and 16 quality-1/2 high-water marks) at 10 m. Performance is fine:
9.9M cells, chain in 1.7 s, 1,044,200 flats all resolved, drains completely. The
17.3M-cell Buffalo Bayou-San Jacinto unit ran in 2.4 s at 1.3 GB peak.

The accuracy got **worse**, not better:

| | 30 m, whole AOI | 10 m, watershed-scoped |
|---|---|---|
| mean residual | +5.38 m | **+6.72 m** |
| RMSE | 5.86 m | **7.86 m** |

The cause is in one number. At 30 m the snapped channel bed at the gauge reads
4.81 m NAVD88; at 10 m it reads **0.89 m**. Finer resolution resolves the actual
channel bottom, so `stage - bed` grows from 7.97 m to 11.89 m, and since the model
applies that as a uniform HAND threshold, everything floods deeper. **Better data
made the answer worse, because the stage-to-threshold conversion was wrong.**

Then the marks were asked what the threshold should have been. For each mark,
`WSE - (elevation of that cell's own drainage)` is the HAND threshold that would
place the water exactly right there:

- implied h: min **-0.19 m**, median **4.42 m**, max **12.32 m**
- the model used a single h = 11.89 m
- the marks' own drainage cells sit at a median of 17.09 m NAVD88 — nowhere near the
  gauge's 0.89 m channel bed, because HAND assigns each floodplain cell to its
  *nearest* drainage, which across a 491 km2 urban watershed is usually a small
  tributary, not the main stem
- the water actually stood a median of **0.58 m above the ground** at the marks:
  shallow overbank flooding, while the model was putting metres over everything

**The conclusion is that no constant works.** Fitting the best possible single h
(4.42 m) still gives RMSE 4.13 m and **0% of marks within a metre**. A spatially
constant HAND threshold cannot reproduce this event at any value, because the water
surface is not a fixed height above local drainage across a watershed with many
independent tributaries. That is a statement about the method, not about the DEM,
and it is the headline limitation for the write-up.

What actually fixes it, in order:

1. **`hydraulics/rating.py`** — in the spec's module list and not yet built. The
   standard HAND flood-inundation approach derives a synthetic rating curve per
   reach from HAND geometry and Manning's equation, converts *discharge* to a
   reach-specific stage, and uses that as the threshold. It is per-reach by
   construction, which is exactly what is missing.
2. **Use discharge, not stage.** NWIS parameter 00060 is what a rating curve
   consumes; we currently fetch only 00065 (gauge height).
3. More gauges, one per tributary, rather than one for the watershed.

Worth stating plainly in the README: the current model over-predicts by metres, the
reason is understood and measured, and the fix is a module the spec already
anticipated.

### hydraulics/rating.py — the fix, and it works

Built the synthetic rating curves the spec's module list anticipated, following
Zheng et al. (2018): each reach owns the cells whose flow path first reaches the
network there; for a trial stage the wet cells of that catchment give volume and
bed area; volume over reach length is cross-sectional area, bed area over reach
length is wetted perimeter, and Manning's equation turns the pair into a discharge.
Tabulating stage gives a curve per reach, and inverting it turns an observed
discharge into a reach-specific stage.

Re-validated on the identical ground — HUC 1204010403 at 10 m, the same 16 surveyed
high-water marks:

| | mean | RMSE | within 1 m | extent |
|---|---|---|---|---|
| constant stage | +6.72 m | 7.86 m | 12% | 464 km2 (98% of the unit) |
| best-fit constant | -0.74 m | 4.13 m | 0% | — |
| **per-reach rating curve** | **+1.04 m** | **1.38 m** | **50%** | **117 km2 (25%)** |

RMSE 7.86 -> 1.38 m, and it beats the best *any* constant could do by a factor of
three, which was the whole point. 575 of 579 reaches got a curve; per-reach stage
runs from 0.25 m to 23.34 m with a median of 0.40 m, against the single 11.89 m the
old model applied everywhere.

**The honest cost:** only 10 of the 16 marks now fall inside the modelled extent,
against 16 of 16 before. Extent recall dropped from 100% to 62% while depth accuracy
improved 5.7x. That is the recall-versus-bias trade the spec wants CSI and bias
reported for rather than hit rate alone, and it is worth stating both ways round in
the write-up: the old model "hit" every mark by flooding everything.

Decisions inside this:

- **Discharge, not stage, is the input.** A rating curve consumes discharge; stage
  is what it produces. `sources.py` now fetches NWIS parameter 00060 alongside
  00065. Buffalo Bayou (08074000) publishes no discharge for the event — common
  where backwater breaks the rating — so the gauged reach is Whiteoak Bayou
  (08074500), peak 50,600 ft3/s = 1,433 m3/s.
- **Ungauged reaches get discharge by drainage-area ratio**,
  `Q_reach = Q_gauge x (A_reach/A_gauge)^k` with `k` in config, default 1.0.
  Regional regressions usually put k between 0.7 and 1.0. This is one gauge's
  information spread over a watershed, so it cannot represent a storm that hit one
  tributary and missed another; it does give each reach a discharge suited to its
  own size, which a single threshold does not. The Monte Carlo should sample k.
- **Slope is floored at `min_reach_slope`** (1e-4). Manning's Q vanishes with slope,
  and a coastal plain has plenty of reaches that measure flat or numerically
  negative; without the floor they carry no water at any stage. `ReachGeometry`
  keeps `raw_slope` so a floored reach stays identifiable.
- **Reaches shorter than `min_reach_length_m` get no curve at all**, rather than a
  curve derived from one or two cells of geometry. Their cells then get stage zero,
  which floods nothing — the honest default when there is nothing to say —
  and `reaches_without_a_curve` reports how many, so the silence is visible.
- **Discharge beyond the top of the curve caps the stage instead of extrapolating.**
  Manning's equation on a cross-section the DEM never saw is not a prediction.
  `exceeds_curve` and `reaches_off_the_curve` report it; the CLI warns.
- **The tabulated curve is made monotone with a running maximum.** Discrete cell
  geometry can wobble where a stage step adds bed area faster than volume, and a
  non-monotone curve cannot be inverted by interpolation. Manning's Q is monotone in
  stage in principle, so this enforces the principle rather than inventing one.
- **One loose end:** the maximum per-reach stage came out at 23.34 m on a single
  reach, against a median of 0.40 m. Nothing hit the curve cap, so that reach
  genuinely needs 23 m of water to pass its area-scaled discharge, which suggests a
  very constrained derived cross-section. Not chased yet; worth a look before the
  damage numbers depend on it.

### report/figures.py and the interactive plate

- **Layers are block-reduced per layer, not uniformly.** Continuous surfaces
  (elevation, HAND, depth) reduce by mean; masks (streams, wet extent) reduce by
  **max**. A stream is one cell wide, and a mean-reduced stream network disappears at
  exactly the zoom someone wants to look at it. A test pins both behaviours.
- **Every layer in one call shares a single reduction factor**, so the PNGs are pixel
  aligned and a survey mark placed on one is placed on all of them. Without that the
  pins would drift between layers.
- **All-NaN blocks warn and the warning is suppressed deliberately.** A watershed does
  not fill its bounding box, so entirely-outside blocks are normal and NaN is the
  right answer for them.
- **The rating-curve figure picks reaches by assigned discharge, not by catchment
  cell count.** The first version sorted by `catchment_cells` and produced four
  headwater reaches carrying 22-38 m3/s, because a headwater reach owns a large
  hillslope while a main-stem reach owns only its immediate banks. Sorting by
  discharge gives the main stem: 1,455-1,819 m3/s. Worth remembering whenever
  "biggest reach" is needed - the two orderings mean different things.
- **The plate is published as an Artifact with the PNGs inlined as data URIs** (1.9 MB
  total). `outputs/` stays git-ignored: the figures are regenerated from the pipeline,
  and committing rendered output would breach the rule against results in the repo.
  The generator scripts live in `notebooks/viz/` so the page is reproducible.
- **Reach 412 is visible in the rating figure and worth explaining:** 127 m long on a
  floored slope of 1e-4, so it needs 14.78 m of stage to pass 1,459 m3/s where a
  neighbouring reach needs 7.11 m. That is the mechanism behind the 23.34 m maximum
  flagged earlier - short reach plus minimum slope equals almost no conveyance. A
  minimum-length floor already exists; a conveyance sanity check probably should too.

### report/bundle.py — multi-watershed interactivity without infrastructure

The goal was a discharge slider and the ability to look at areas other than the one
watershed, with AWS credits available if they turned out to be needed. **They are not
needed yet**, and
the measurement is why.

Inundation is `depth = stage - HAND`, and nothing expensive in that depends on
discharge. So a browser can re-run the model from three small things per watershed:
HAND as an 8-bit PNG at 0.1 m precision; reach id per pixel packed into a PNG's red
and green channels; and a stage lookup table, one uint8 row per reach across a ladder
of discharge multipliers. Measured on Whiteoak at a 746x527 display grid: HAND 97 kB,
reach ids **32 kB for 579 reaches**, stage table **3 kB** - about 180 kB per watershed
as base64. All 21 units with a terrain basemap come to **9.2 MB**, inside the 16 MB
Artifact cap.

The alternative - shipping a depth raster per discharge level - is hundreds of times
larger and only covers the levels chosen in advance.

- **Precision is deliberately lossy at 0.1 m** on both HAND and stage. The model's own
  agreement with surveyed marks is RMSE 1.5 m, so a decimetre costs nothing real, and
  it is what makes 8 bits enough. HAND clamps at 25.4 m; the clamp only ever makes a
  cell *drier*, so it cannot invent inundation.
- **Precomputing all 21 units took 94 seconds** on the laptop, from DEM tiles already
  on disk, and every unit is 100% covered by them. 281M cells, largest unit 3.4 GB peak.
  Compute was never the bottleneck.
- **AWS becomes worth it for**: native-resolution zoom (COG range reads rather than a
  display-reduced grid), metros beyond Houston, 1 m (which also needs a tiled
  priority-flood), or a public app rather than a private artifact. None of those block
  the current build. Noted rather than acted on.
- **The Artifact CSP forbids external images and fetches**, so a hosted bucket would
  not help a page delivered that way regardless. That constraint is what makes the
  inline budget the design driver.

**One of 21 units has an observed discharge.** Whiteoak Bayou carries gauge 08074500;
the other twenty are scaled from it by drainage-area ratio. Rather than hide that, the
page leads with it, badges every unit gauged or inferred, and makes the slider the
answer: for twenty basins the discharge *is* the assumption, and dragging it shows how
much the result depends on it.

**Live RMSE is computed in the browser over all marks in the unit**, counting a mark
the model leaves dry as an error equal to the water depth actually recorded there.
Dropping misses would let a model that floods almost nothing score well on the few
points it caught - which is exactly the mistake in the first comparison I reported.

### a bug the bundle tests caught

- **Zero discharge returned 0.25 m of stage.** The rating curve was tabulated from the
  first stage step upward, so inverting it below the first tabulated discharge clamped
  to the first *stage* rather than to zero. Every cell of a watershed showed a quarter
  metre of water at zero flow. The stage ladder now starts at 0.0, giving the curve a
  genuine (Q=0, stage=0) point. Found by a test asserting that no discharge means no
  stage, not by looking at the map.

### correcting a comparison I reported

- **The RMSEs I first quoted were over different denominators.** The constant model's
  7.86 m was over all 16 marks; the rating model's 1.38 m was over only the 10 it
  flooded. Fair, over all 16, treating a dry cell as "water surface no higher than the
  ground": constant mean +6.73, MAE 6.79, RMSE 7.86, 12% within 1 m; rating mean +0.37,
  MAE 1.12, **RMSE 1.54**, 56% within 1 m. The fair comparison favours the rating model
  more strongly, not less.
- **Three of the sixteen marks sit below the DEM ground surface** (-1.27, -1.06,
  -0.03 m). A high-water mark cannot physically be below ground, so that is DEM error
  or a coordinate landing in the wrong 10 m cell. Two of the rating model's six misses
  are those, and are unavoidable; four are genuine, only one of them large.
- **CSI cannot be computed.** With point ground truth and no observed extent polygon,
  hit rate and depth bias are available but false alarm ratio is not. That is a direct
  consequence of dropping Sentinel-1, and it leaves a gap against the definition of
  done. An observed extent polygon - FEMA or Harris County - would close it.
- **No precipitation enters the model anywhere.** The input is an observed discharge at
  one instant: 1,433 m3/s at 08074500, 27 Aug 2017 13:30 CDT. Harvey's rainfall is
  context, not input. NWIS returns no precipitation series for 2017 at the five AOI
  sites that list parameter 00045, on either the instantaneous or the daily service, so
  no rainfall figure is quoted from our own data.

### compute.py — live compute for any watershed, and why it needs AWS

The goal was live compute rather than a fixed set of 20 precomputed watersheds.
`compute_watershed` is that path: give it a HUC code or a map click and it fetches the
boundary from WBD, finds the 3DEP tiles that intersect it, reads them over HTTP range
requests, runs the whole chain, and returns the ~180 kB bundle. No local data, no
prior preparation, anywhere in the United States. It is deliberately shaped like a
request handler - identifier in, bundle out, no local state - because that is what a
service wraps.

**3DEP is cloud-optimised**, which is what makes this possible at all: 512x512 internal
tiles, LZW, overviews [2,4,8,16,32]. Reading a 500 km2 watershed out of a
10812x10812 tile costs the blocks it touches, not the 400 MB the tile weighs.

**But from this machine it was unusable during testing, and the reason is the network
rather than the code.** Measured against `prd-tnm`:

| | |
|---|---|
| one 512x512 block, cold | 1.40 s |
| sixteen blocks (16 MB raw) | 51.55 s |
| effective throughput | **0.3 MB/s** |

A 133 km2 HUC-12 at 30 m did not finish in ten minutes.

**This measurement is confounded and should not be quoted.** The local internet
connection turned out to be bad at the time, so the
0.3 MB/s figure describes a degraded local link at least as much as it describes the
route to S3. It needs re-running on a healthy connection before it means anything.
What it does establish is the shape of the failure - sustained throughput, not
per-request latency - because one block cost 1.40 s while sixteen cost 51.55 s, and a
latency-bound path would have amortised.

The architectural conclusion survives the confound, but on general grounds rather than
on this number: S3 to EC2 in the same region does not traverse a consumer link at all,
and a 500 km2 HUC-10 at 10 m is only ~5M cells and about 20 MB of source, on top of a
measured 0.2 s per Mcell of terrain chain. Whether on-demand compute is viable *from a
laptop* is genuinely unresolved and worth re-measuring.

**So this is the case for the AWS credits, and it is a real one.** The recommendation:

1. Run `compute_watershed` in `us-west-2`, next to `prd-tnm`. Lambda at 10 GB fits a
   HUC-10 at 10 m; anything larger wants Fargate or EC2.
2. Cache each finished bundle in S3, keyed by HUC and resolution. A watershed is
   computed once and served forever after.
3. Serve the page as a static site, **not** as an Artifact. The Artifact CSP forbids
   `fetch` to external hosts, so an Artifact page can never call a compute API - it can
   only carry what is inlined. That is the hard constraint that decides the delivery
   shape, and it is why the current atlas precomputes 21 units instead of calling out.

Deployment needs Thomas's credentials and is his to run; the compute function it would
wrap is built and tested here.

- **GDAL needs explicit retry settings.** A truncated range read - `got 15697 bytes,
  expected 188786` - is a normal fact of life over HTTP, and GDAL does not retry unless
  told. Without `GDAL_HTTP_MAX_RETRY`, one short read failed a whole watershed several
  minutes into the job. A test asserts the settings are present.
- **rasterio's `Env` wants real Python types, not strings**, for numeric GDAL options.
  `GDAL_CACHEMAX="1024"` raises `TypeError: an integer is required`.
- **`ingest_dem` and `select_tiles` now accept GDAL virtual filesystem URLs** as well
  as paths, which is the whole change needed on the ingest side to read remotely.
- **`compute_watershed` refuses an oversized watershed before doing any network work**,
  since depression filling is global and the grid has to fit in memory at once.

## 2026-09-05 — the AWS recommendation was wrong, and the bottleneck was my own code

Re-measured on a healthy connection, then found the real problem. The sequence matters
because I gave a recommendation on the strength of the first number:

1. **First measurement, 0.3 MB/s.** Taken while Thomas's connection was degraded. I
   attributed it to the route to S3 and said this was the case for the AWS credits.
2. **Re-measured: raw HTTP to `prd-tnm` is 4.2 MB/s serial, 4.7 MB/s across eight
   parallel range requests.** Parallelism barely helps, so ~4-5 MB/s is this link's
   ceiling, not a per-request latency problem.
3. **But 4-5 MB/s cannot explain a 133 km2 watershed failing to finish in ten
   minutes** - that is about 5 MB of source. So there was a large inefficiency, and it
   was mine.
4. **The `WarpedVRT` was unbounded.** Left to size itself from the source, a VRT over a
   10812x10812 3DEP tile is a **123M-cell** warp grid; the actual output for a 10 km
   window at 30 m is **0.10M cells**. A factor of 1,200. `merge` reading from that
   forced GDAL to compute warp geometry across the whole tile and fetch far more source
   blocks than the window touched.
5. **Fixed by pinning every VRT to the output grid** - explicit `transform`, `width`,
   `height`. As a bonus, all VRTs then share one grid, so combining them is a per-pixel
   "take the first valid" with no second resampling, and `rasterio.merge` is no longer
   needed at all.

Measured after the fix, from a laptop over that same 4-5 MB/s link, with **no local
data**:

| watershed | area | cells | total |
|---|---|---|---|
| Cole Creek-Whiteoak (HUC-12) | 133 km2 | 2.5M | **12.3 s** |
| Brays Bayou (HUC-10) | 367 km2 | 8.3M | **13.4 s** |
| Whiteoak Bayou (HUC-10) | 491 km2 | 9.9M | **24.5 s** |
| Dry Comal Creek, New Braunfels | 158 km2 | — | **11.0 s** |

Read time dominates and terrain is 0.3-1.6 s of it. From "did not finish in ten
minutes" to twelve seconds is roughly a 50x improvement, and none of it came from
better infrastructure.

**Revised recommendation: AWS is not needed for on-demand compute.** A watershed
anywhere in the United States computes in 11-25 s from a laptop, which is a perfectly
good cacheable job. What the credits would still buy:

- **Serving**, if the page is to be public. The Artifact CSP forbids `fetch` to
  external hosts, so live compute still means a hosted static site rather than an
  Artifact - that constraint is unchanged and is the real reason to host anything.
- **Latency**, if 11-25 s is too slow to feel interactive. In-region reads would cut
  the dominant term, but caching the ~180 kB bundle achieves the same thing for
  repeat visits at no cost.
- **Scale**, if precomputing thousands of units rather than tens.

The lesson worth keeping: I recommended infrastructure to solve what turned out to be a
1,200x inefficiency in the read path. Measure the code before buying a bigger machine.

### service.py — a map interface for any US watershed

The goal was a map where clicking a watershed or typing a postcode computes in real
time, with a minute of latency acceptable. It comes in well
under that: 13-29 s cold for a HUC-12, cached afterwards.

- **It is served, not published.** An Artifact cannot `fetch` an external host, so an
  interactive page that calls a compute API has to come from the same origin as the
  API. `floodline serve` is that. The same application deploys unchanged to anything
  running Python, which is what makes hosting later a deployment rather than a rewrite.
- **The analysis CRS follows the watershed.** EPSG:6587 is Texas South Central: right
  for Houston, meaningless in Oregon. `utm_crs_for` picks the NAD83 UTM zone from the
  watershed centroid, so one server is correct nationwide. Verified against Portland
  (26910), Chicago (26916), Miami (26917), Cambridge (26919).
- **Display arrays are warped to Web Mercator; analysis stays in UTM.** A north-up UTM
  grid is not axis-aligned in Web Mercator, so an overlay placed by its corners would
  be visibly skewed. Only the few-hundred-pixel display arrays are warped, and reach
  ids resample by nearest - averaging two reach numbers would invent a third.
- **Geocoding is the Census ZCTA layer for a bare postcode and the Census address
  geocoder otherwise.** Both public and keyless, and the same federal source as
  everything else. The address geocoder rejects a bare postcode, which is why the two
  are split.
- **Ungauged watersheds get a labelled scenario discharge, not zero.** The first
  version left discharge unscaled when no gauge was supplied, so every ungauged
  watershed - which is nearly all of them - computed successfully and flooded exactly
  nothing. It now defaults to `area x 5 m3/s/km2`, roughly Harvey's specific discharge
  at Whiteoak, and the bundle carries a warning that the interface prints. A tool that
  silently returns an empty flood is worse than one that says what it assumed.
- **Two interface bugs found by looking at it.** `hidden` loses to an explicit
  `display:flex`, so the controls showed before anything was computed; and Leaflet
  measures its container on construction, which inside a grid that has not laid out
  yet gives a tile grid at the wrong size. Both fixed - a `[hidden]` rule and a
  `ResizeObserver` calling `invalidateSize`.

- **The national watershed grid is now visible, not just clickable.** The first version
  required clicking a blind point and reported afterwards what had been hit. The WBD
  MapServer renders boundary images and swaps HUC level by map scale on its own -
  regions when zoomed out, subwatersheds when zoomed in - so a small `L.TileLayer`
  subclass that builds an `export` URL per tile puts the whole national grid on the map
  without shipping any geometry. A level selector (8 / 10 / 12) controls what a click
  resolves to, since the right granularity depends on the question rather than the zoom.

## 2026-09-05 (evening) — real gauges, national marks, and a rebuilt map

### the discharge is now observed wherever a gauge exists

- **Each watershed finds its own gauge.** Every NWIS station publishing discharge
  inside it is snapped to our own stream network, and the one draining the largest
  area wins - the station nearest the outlet. Contributing area comes from our own
  flow accumulation, not the published figure, so discharge and area are measured on
  the same grid. Whiteoak Bayou discovers 08074500 and its 1,433 m3/s Harvey peak
  unprompted: the number the model was validated against, found rather than supplied.
- **The discharge used is the peak of record**, not a design figure - the worst flow
  that gauge has actually measured. Record length is surfaced, because the Lower North
  Branch Chicago River's "peak" rests on four years and Whiteoak's on ninety.
- **When the marks come from one flood, the model is driven by *that* flood.** This
  mattered more than expected. City of Houston-Buffalo Bayou has a 1935 peak of record
  of 1,133 m3/s and 57 marks surveyed after Harvey. Driving the model with 1935 and
  scoring it against 2017 marks gave RMSE 6.75 m; matching the event - 923 m3/s on
  2017-08-28, taken from the same annual peak series, no extra request - gave
  **4.81 m**. Nearly two metres of the residual was measuring the difference between
  two floods. Where they cannot be matched, the panel says so in as many words.

### marks are national now

- **38,230 located marks across 258 named events** - Harvey, Irene, Sandy, Matthew, the
  2019 Central US floods - not one event. About 34 MB and fourteen seconds, so fetched
  once and filtered per watershed. The bulk endpoint carries `event_id` but not the
  name, so `Events.json` is joined in; "2017 Harvey" is worth more to a reader than
  "180". Every watershed now carries whatever ground truth exists for it, and the page
  computes agreement live rather than being handed a number.

### the map

- **Esri grey canvas, not Carto.** Carto now watermarks its basemaps "API KEY REQUIRED"
  without one. Esri publishes a deliberately quiet grey canvas keyless, and - the
  reason it is the right choice rather than merely an available one - it publishes the
  *labels as a separate layer*, so place names sit in a pane above the flood instead of
  vanishing under it.
- **HUC labels suppressed via `dynamicLayers`.** The WBD export prints a full HUC code
  across every polygon; at city scale that was most of the ink on the screen. Passing
  `drawingInfo:{showLabels:false}` per layer cuts drawn pixels by 65% and leaves the
  boundary lines that are actually wanted.
- **Floating panels over a full-bleed map**, rather than a sidebar. Layers are chips,
  not checkboxes; a thin progress bar replaces a spinner blocking the panel.
- **10 m is now the default** wherever the grid fits in one pass. 30 m does not resolve
  a channel and the residuals show it, so defaulting to the coarse option was quietly
  costing accuracy.

### two corrections from measuring rather than assuming

- **The 10 m default was reverted.** I made 10 m the default reasoning that "30 m does
  not resolve a channel and the residuals show it". Measured on City of
  Houston-Buffalo Bayou against its 57 Harvey marks, 10 m was **worse** - RMSE 6.25 m
  against 4.89 m at 30 m - and took 66 s against 16 s. More marks landed within a
  metre at 10 m (21% against 14%), but more were left dry (43 of 57 against 50), and
  the misses cost more than the near-hits gained. 30 m is the default again and 10 m
  is offered rather than assumed. This is the second time finer data has scored worse
  here, and the reason is the same both times: resolving the channel changes what HAND
  is measured against.
- **The headline RMSE was being set by the roughest surveys.** USGS grades every mark;
  1 and 2 are surveys good to a few centimetres, 3 and below progressively rougher. On
  this watershed the split is stark: quality 1-2 give **RMSE 1.03 m**, quality 3
  (51 of 57 marks) give **4.93 m**, quality 4 gives 5.64 m. Reporting one number over
  all of them let the least reliable surveys dominate. The interface now scores the
  graded subset by default and draws the rest smaller and faded, so they are visible
  but not deciding the verdict. Worth stating in the write-up: on this watershed the
  model agrees with the trustworthy marks to about a metre.
  The caveat on that caveat: only two marks here are quality 1-2, which is a thin
  basis. Whiteoak Bayou has sixteen and gave 1.54 m, so the two agree, but neither is
  a large sample.

### inverting the model against the marks

- **"Find the discharge the marks imply" reports a trade-off, not a single best.**
  The first version swept for the lowest RMSE and, on Whiteoak Bayou, returned 0.16x
  the observed Harvey discharge. That is a trap in the metric rather than a finding: a
  mark the model leaves dry scores only the depth of water that was actually there,
  often well under a metre, while an over-prediction scores several, so the minimum
  sits wherever the model floods almost nothing. It now reports the score at the
  observed discharge, the best fit that still wets 70% of the marks, and the
  unconstrained minimum with an explanation of why it is an artefact.
- **What that reveals on Whiteoak Bayou**: observed 1,433 m3/s gives RMSE 1.60 m with
  14 of 16 graded marks wet; the best constrained fit is 0.56x (802 m3/s) at 1.44 m
  with 12 of 16. Halving the discharge buys 0.16 m. The model is close to insensitive
  to discharge here, which says the residual is structural - the HAND assumption and
  the 30 m cross-sections - rather than an error in the flow. That is a more useful
  conclusion than a calibrated multiplier would have been.

### a stale tile took down two cities

- **A tile the 3DEP catalogue lists but no longer serves used to fail the whole
  watershed.** New Orleans (ZIP 70112) and Philadelphia (19104) both returned a bare
  500 on a 404 from one tile URL. A tile that cannot be opened is now skipped, so one
  stale entry costs that footprint rather than the request; the service reports a
  clear 502 only when *no* tile can be read. Regression-tested both ways.
- **Smoke-tested across the country** after the fix: New Orleans (Bayou Saint John,
  123 marks from 2005 Katrina), Philadelphia (City of Philadelphia-Schuylkill, gauge
  01474500, 22 marks from 2021 Ida of which 11 are graded), Cambridge (Outlet Charles
  River, 15 marks from a 2018 storm), Phoenix and Denver (gauged, no marks), Seattle
  (Lake Washington-Sammamish). 20-32 s each, cold.
  **Philadelphia is the second usable validation case** the project has: a gauge and
  eleven graded marks from a single named flood. Worth running properly.
- **Open question worth checking**: Lake Washington-Sammamish returned a gauge peak of
  only 14 m3/s for a 461 km2 watershed, which is implausibly low for an outlet. The
  gauge-selection rule takes the largest contributing area *among stations inside the
  unit*, and a lake-dominated basin may have its real outlet gauge outside the HUC
  boundary. Not chased.

## 2026-09-05 — the discharge slider resets to 1.00x on every new watershed

The slider is a multiplier on a per-watershed reference discharge, but `clearModel()`
never reset it. Running the best-fit sweep on Whiteoak Bayou left it at 0.56x, and the
next watershed searched (City of Philadelphia-Schuylkill River) then opened at 0.56x of
*its* peak of record — 2,141 m3/s instead of 3,823 — with no indication that the figure
came from a different basin. Every reported number downstream of it (extent, max depth,
RMSE) was for a discharge nobody chose. `clearModel()` now sets it back to 100.

Rejected: keeping the multiplier as a deliberate "hold the scenario across basins"
feature. A multiplier is only meaningful against the reference it was fitted to, so
carrying it is carrying a number that has lost its meaning.

## 2026-09-05 — depth colour is a square-root scale, not linear

The map painted depth as `ramp(d/80)` — linear from 0 to 8 m. Urban flooding is mostly
0.5-2 m, which is the bottom quarter of that range, so Whiteoak Bayou rendered almost
entirely in the palest ramp stop (#cfe6f2) and was near-invisible against the grey
basemap. It looked like nothing had been computed. The City of Philadelphia-Schuylkill
River unit looked fine only because its confined valley reaches 18.5 m and saturates the
dark end — the same bug, hidden by a steeper basin.

Now `ramp(sqrt(d/80))`. Legend ticks moved to the matching positions (0 / 0.5 / 2 / 4.5 /
8 m+) so the uneven spacing shows the scale is non-linear, and the legend gradient gained
the ramp's sixth stop, which it had been missing.

Rejected: a per-watershed percentile stretch. It would make every basin look equally
flooded and destroy comparability between them, which is most of the point of a national
interface. The scale stays absolute — a given depth is the same colour everywhere.

## 2026-09-05 — map UI rebuilt as floating glass instruments

The chrome was functional but generic: a native `<select>`, a browser-default range
input, stat figures in a card inside a card, and Leaflet's stock zoom bar. Reworked
around one token set — teal-biased neutrals, a single accent (#0a7d8c light / #3ecad8
dark) held clear of the blue depth ramp, semantic good/warn/bad kept separate from the
accent — and one radius, hairline and shadow scale applied to panels, tooltips, zoom
control and attribution alike.

Substantive changes, not just paint:

- The `<select>` became a segmented control. Three fixed choices are all worth showing
  at once, and it renders identically across platforms, which a native select does not.
  A hidden `input#lvl` keeps `$("lvl").value` working, so no call site changed.
- The range input got a real track and thumb. WebKit has no `::-moz-range-progress`
  equivalent, so the filled proportion is handed to CSS as `--pct` from `syncSlider()`,
  called by `repaint()` (which already runs on both input and load) and `clearModel()`.
- Stat figures moved from a boxed grid to ruled rows. Border, fill, radius and shadow
  each say "separate object"; spending all four on six numbers flattened the hierarchy
  the numbers should carry themselves.
- `--on-accent` added. The dark accent is a bright cyan, and the white button text
  carried over from the light theme failed contrast against it.
- The high-water-mark key gained its middle class (1-3 m out). The legend named only
  the best and worst bands while the map drew three.

## 2026-09-05 — the discharge unit is no longer uppercased

`.qunit` carried `text-transform:uppercase`, which rendered `m3/s` as `M3/S`. SI symbols
are case-sensitive and the capitals are a different quantity. Removed. The uppercase
treatment stays on the eyebrow and column labels, which are words rather than units.

## 2026-09-05 — the map is a globe: Leaflet replaced by MapLibre GL

Leaflet has no globe projection at any version, so showing the interface on a sphere
meant changing map engines. MapLibre GL 5.24.0, pinned, `projection: {type:"globe"}` in
the style. Chosen over CesiumJS (a 3D-globe engine that would have meant rewriting the
data path around its own imagery and entity model) and over deck.gl's GlobeView (a
rendering layer, not a map, so the basemap and the WBD service would both have needed
building from scratch). MapLibre keeps raster tile sources, corner-pinned image overlays
and GeoJSON, which is the entire data path this page uses.

v5 rather than the current v6: v6 ships ESM only, and the page loads libraries as plain
script tags with globals. v5.24.0 is the last line with a UMD build.

What the port had to translate:

- Leaflet panes became layer order. Everything dynamic is inserted `beforeId:"labels"`
  so place names still sit above the water, which was the point of the panes.
- `L.CRS.EPSG3857.project/unproject` became two local functions. The depth grid and the
  WBD export are both EPSG:3857, so the maths stays; only the provider changed. Verified
  numerically: Whiteoak Bayou returns 106.7 km2 / 10.1 m / 14 of 16 wet / RMSE 1.60 m and
  Philadelphia 30.4 km2 / 18.5 m / 9 of 11 / RMSE 4.73 m — identical to the Leaflet build
  to the last digit, which is the real test of the projection port.
- The hand-rolled `L.TileLayer` subclass that computed a bbox per tile for the WBD export
  became MapLibre's `{bbox-epsg-3857}` token in the URL template.
- One marker per high-water mark became a single data-driven circle layer. The globe
  renderer then handles occlusion behind the sphere for free, and 176 DOM nodes are no
  longer created and destroyed on every slider frame. Cost: the "dry" marks were dashed
  circles in Leaflet and are now hollow ones, because a circle layer has no dash.
- `bindTooltip` became one shared popup moved between features, cleared on `movestart`
  since a pan leaves the pointer somewhere else and `mouseleave` never fires.

Two defects the port surfaced, both fixed:

- Style readiness must come from `styledata` + `isStyleLoaded()`, not `load`. `load`
  waits on a first render and a backgrounded tab never renders, so a compute that
  finished while the tab was hidden threw "Style is not done loading" from `addSource`.
  Everything touching sources or layers now goes through a `whenReady` queue.
- `fitBounds` centres in the full viewport, but the panels float over roughly 45% of it,
  so a small unit could land entirely behind one — Philadelphia did, every time. Padding
  is now measured from the panels' own rects and clamped below what fitBounds accepts.
  This was wrong in the Leaflet build too; the globe just made it obvious.

Accepted cost: the globe is WebGL2-only, where the raster-tile map was not. A browser
without it now gets an explicit message naming the API endpoint instead of a blank
rectangle.

## 2026-09-05 — exposure and damage built; curve constants shipped unverified

Phases 2b and 3 implemented: `exposure/buildings.py`, `exposure/population.py`,
`damage/curves.py`, `damage/costs.py`, `damage/estimate.py`, `damage/uncertainty.py`,
and the `exposure` and `damage` CLI commands, which previously exited 2. 70 new tests;
the suite is 563.

Judgment calls made along the way:

- **Bundled curve constants carry each family's shape but are not transcribed from the
  source tables, and are marked `verified=False`.** Inventing digits and labelling them
  "Huizinga et al. 2017" would be exactly the dishonesty the conventions forbid, and
  refusing to run without transcribed tables would ship a pipeline nobody can execute.
  Instead the flag propagates: `DamageEstimate.curves_verified`, `DamageInterval`, a
  CLI warning on every run, and a README section. Ratios and counts stand; currency
  totals do not. `load_curves` takes real tables and sets the flag.
  Rejected: shipping the constants unflagged, and shipping no constants at all.
- **`p90` stays the default depth statistic.** `max` is decided by whichever cell the
  DEM dug lowest, `centroid` misses buildings whose centre sits on a locally high cell.
  Demonstrated in a test: one 9 m pit under a footprint of 0.5 m water gives max 9.0 m
  and p90 under 3 m.
- **Damage is capped to the storeys water can reach** (`costs.storey_exposure`).
  Multiplying by total floor area prices a metre of water against every floor of a
  tower. Uncapped remains available for comparison with published figures that use it.
- **Damage defaults moved to the US case.** `curve_family` HAZUS rather than
  JRC_OCEANIA, MC weights HAZUS 0.6 / JRC_GLOBAL 0.4, and a new `currency` field
  defaulting to USD so a total is never a bare number. The primary case moved to
  Harvey; the spec's own source table says HAZUS for the US. Cost magnitudes are
  unchanged and now explicitly labelled assumptions.
- **`population_affected` refuses a grid that is not on the depth grid.** Resampling a
  population count either duplicates people (nearest) or invents them (bilinear), and
  which is wrong is the caller's to state.

## 2026-09-05 — the Monte Carlo perturbs a signed margin, not the clamped depth

Caught by running the CLI end to end rather than by a unit test. On the synthetic
catchment the deterministic estimate was 57 buildings damaged and the Monte Carlo's
count interval came back 67-219 — the point estimate below its own lower bound.

Cause: `floor_depth_m` is clamped at zero, so a building the water missed by a
centimetre and one it missed by five metres both record 0.0. Adding symmetric noise to
a value floored at zero can only push it upward, so roughly half of the 168 dry
buildings were manufactured into the flood on every draw.

Fix: `building_depths` now takes an optional `unclamped_depth` field (`stage - HAND`,
negative on dry ground) and emits a signed `floor_margin_m`; the Monte Carlo perturbs
that and clamps afterwards. The same run now gives 57-58. Where no margin is supplied
the draws perturb only already-wet buildings and `count_interval_conditional` is set,
so a narrow interval is never mistaken for a confident one. Three tests cover it:
far-below-floor buildings stay dry under a 0.3 m stage sigma, just-below-floor
buildings are correctly uncertain, and the point estimate lies inside its own interval.

Rejected: perturbing the clamped depth and widening `min_depth_m` to compensate, which
would have hidden the bias rather than removed it.

## 2026-09-05 — README corrected to match what the code does

The opening described the full pipeline in the present tense while `damage/` was an
empty package, and the Status section still said "nothing has been run against real
Lismore data yet, so this README contains no flood results" — stale in three ways at
once, since the case is Harvey, results exist, and it disclaimed them. Rewritten to
separate what is built from what is validated, to state the CSI gap outright, and to
add a "what is and is not trustworthy" section for damage. This is the convention the
repo already had; the README had drifted out of compliance with it.

## 2026-09-05 — what is actually reachable for buildings and population, measured

The exposure pipeline had no data behind it. Every candidate source was probed rather
than assumed, and the results decided the design.

| Source | Result | Consequence |
|---|---|---|
| Overture buildings, S3 | anonymous listing works | primary footprints |
| Microsoft US Building Footprints | 206, range-readable | noted, unused |
| WorldPop USA 100 m | 200, 494 MB, **Range ignored** | download once, cache |
| GHS-POP global 100 m | server honours Range, but zip directory sits at the end | too slow, unused |
| LandScan Global / USA | **403**, registration form | recorded unavailable |
| Census ACS block groups | **"Missing Key"** | needs `CENSUS_API_KEY` |
| TIGERweb block-group geometry | layer 10, open | usable once a key exists |

Two findings worth keeping:

- **WorldPop advertises `Accept-Ranges: bytes` and does not honour it.** A Range
  request returns 200 with the whole body, which GDAL reports as "Range downloading
  not supported by this server!". The DEM pattern of reading only the window that
  matters does not transfer. The national raster is fetched once to `data/cache`
  (494 MB) and windowed locally after. The download is opt-in behind
  `floodline fetch-population`, because a function that quietly spends half a
  gigabyte is one nobody can safely call from a script.
- **LandScan is gated.** ORNL publishes it under CC BY but every download path is
  behind a registration form. The project's rule is that a source needing a login is
  recorded as unavailable, not worked around, so it is named in the module docstring
  and left out.

Overture is read with pyarrow rather than DuckDB, which the spec named. pyarrow is
already in the stack for GeoParquet and does the same predicate pushdown on the `bbox`
struct column; adding a second query engine to save one step was not worth the install.
Measured on release 2026-08-19.0: a 6 x 4 km Houston window took 83 s to open the
dataset and 210 s to read 12,151 buildings; the full Whiteoak Bayou box returned
459,667 buildings in 183 s. The file listing is cached per release because it is the
expensive part of a cold open, and results are cached per bounding box. If this becomes
the bottleneck, DuckDB's spatial extension is the next thing to try.

## 2026-09-05 — historical context: where a discharge sits in its gauge's record

`hydraulics/frequency.py`. Log-Pearson III by method of moments on log10 of the annual
peaks with the station skew - Bulletin 17B in the form still used for quick work. Full
17C is not implemented: no Expected Moments Algorithm, no regional skew weighting, no
Multiple Grubbs-Beck low-outlier test. That is why the empirical rank is reported
alongside the fitted return period rather than replaced by it: where the two disagree,
the rank is the fact and the fit is the model.

The normal quantile comes from `statistics.NormalDist.inv_cdf` rather than scipy. scipy
is present transitively but undeclared, and pulling a declared dependency on it for one
function was worse than using the standard library.

On real data this immediately earned its caveats. Harvey's 1,433 m3/s at 08074500 is
rank 1 of 90 years, and the log-Pearson III fit puts it past a 1-in-1000-year flow -
which is not a finding about Harvey, it is the fit failing. A century of urbanisation
upstream is exactly the non-stationarity the method assumes away. So a saturated fit now
reports `fit_saturated` and returns no number, instead of printing "1-in-10,000 year"
as though it meant something.

## 2026-09-05 — three bugs the first real run caught that no unit test did

Running `assess_watershed` on Whiteoak Bayou with live Overture footprints surfaced
three defects in code that passed 563 tests.

1. **NaN damage interval.** The margin grid marks undefined HAND with `-inf`. A
   footprint straddling that boundary handed `np.percentile` a window mixing `-inf`
   with real depths; its linear interpolation evaluates `-inf + inf`, returns NaN, and
   the NaN propagated to `USD nan to USD nan`. `_reduce` now excludes non-finite cells
   before reducing and keeps the sentinel only when every cell is undefined. Two tests
   cover both cases.
2. **A denominator that meant nothing.** Overture is queried on the watershed's
   bounding box, which over a meandering HUC holds far more ground than the unit. The
   run reported "42,556 of 499,769 buildings" and 19,851 footprints outside the DEM
   entirely. Footprints are now clipped to the watershed polygon, and the count that
   was dropped is reported rather than hidden.
3. **A saturated frequency fit printed as a number**, covered above.

The lesson recorded rather than the fix: all three are shape-of-real-data bugs that a
synthetic fixture cannot produce, because the synthetic catchment has no undefined
HAND, no bounding-box overhang and no 90-year gauge record. The integration test on
real data is worth its runtime.

## 2026-09-05 — first real end-to-end run, and what it produced

`floodline assess 1204010403 --resolution 30 --samples 800`, every stage live:

```
discharge 1,433 m3/s (observed at a gauge)
  1,433 m3/s is the largest in 90 years of record.
flooded 112.2 km2 at 30 m, max depth 10.9 m
buildings  42,556 above finished floor of 256,436 in the watershed
people     98,412 of 1,704,551 (5.8%) - WorldPop 2020 constrained, one grid
damage     USD 3.83bn on hazus curves, 5-95% USD 1.77bn to 9.99bn
           (19,027-71,236 buildings), loss ratio 0.9% of USD 423bn exposed
timings: dem 13.3s, terrain 0.3s, hydraulics 2.8s, buildings 45.6s,
         population 0.4s, damage 29.6s
```

Read the currency figures as unverified: the curve constants are not transcribed, and
the run says so. The building count, the loss ratio and the interval's *shape* are the
parts that stand.

The count interval is wide - 19,027 to 71,236 against a point of 42,556 - and that is
the model being honest rather than the model being bad. Whiteoak Bayou is very flat, so
a large share of buildings sit within a few tens of centimetres of the modelled water
surface, and a 0.15 m stage sigma moves tens of thousands of them across the threshold.
A narrow interval here would have meant the perturbation was not reaching the buildings
it should.

Flood history is now on the web bundle too, since `compute_watershed` already fetches
the annual peak series to pick the event's own peak. The map panel prints it at 1.00x
and stays quiet once the slider moves, because "largest in 90 years" is a fact about
the observed discharge, not about an arbitrary multiple of it.

## 2026-09-05 — a second watershed, and what the interval width is actually telling us

`floodline assess 020402031008` — City of Philadelphia–Schuylkill River, HUC-12, 86 km2,
EPSG:26918. A different UTM zone, a different gauge, and a confined valley rather than a
coastal plain, so it exercises the parts of the chain Houston cannot.

|  | Whiteoak Bayou (TX) | Philadelphia (PA) |
|---|---|---|
| discharge | 1,433 m3/s, rank 1 of 90 | 3,823 m3/s, rank 1 of 90 |
| flooded | 112.2 km2 | 30.5 km2 |
| max depth | 10.9 m | 18.5 m |
| buildings above floor | 42,556 of 256,436 | 24,100 of 110,530 |
| people | 98,412 of 1,704,551 (5.8%) | 62,781 of 581,093 (10.8%) |
| loss ratio | 0.9% | 3.9% |
| count interval | 19,027–71,236 | 21,799–26,121 |

The last row is the interesting one. Houston's building-count interval spans 3.7x;
Philadelphia's spans 1.2x, on the same stage and DEM sigmas. That is the terrain
talking, not the parameters. Whiteoak Bayou is flat enough that tens of thousands of
buildings sit within a few tens of centimetres of the modelled water surface, so a
0.15 m stage error moves them all across the threshold at once; the Schuylkill runs in a
valley where the same error moves almost nobody. An uncertainty band that came out the
same width in both places would have been describing the priors rather than the ground.

Neither figure in the currency rows should be quoted: the curve constants are still
untranscribed, and both runs say so.

## 2026-09-05 — real valuations: NSI structures and the published USACE curves

A depth-damage curve returns a *fraction*, so a damage figure is only as good as the
valuation it multiplies. Footprint area times a flat rate per class was not a
valuation. Measured against NSI over one 2 x 2 km Houston box, that proxy overstated
the total by 1.4x and landed within 30% for only 64% of structures, ranging 0.66x to
2.08x per building. Replaced with two sources that were built to work together:

- **USACE National Structure Inventory** (`io/nsi.py`) — ~120 million US structures,
  keyless. Per structure: `val_struct`, `val_cont`, `val_vehic`; a HAZUS `occtype`;
  real `num_story` and `found_ht`; and night/day population split under and over 65.
  That single source removes four separate guesses: value, storeys, freeboard, and
  a gridded population product standing in for people in buildings.
- **USACE depth-damage curve library** (`damage/usace.py`) — `occtypes.json` from
  github.com/USACE/go-consequences, MIT, 51 occupancy types, structure *and* contents
  curves, from the Economic Guidance Memoranda. Loaded `verified=True`, because these
  are read from the published file rather than typed in from a figure.

They join on `occtype`, which is the reason to prefer this pair over any other
combination: the inventory and the curve speak the same vocabulary.

Three things this forced:

- **The zero-clamp in `DamageCurve.damage_fraction` had to go.** It forced damage to
  zero at or below floor level, which is right for the bundled curves (they start at
  (0 m, 0)) and wrong for USACE, which starts at -0.61 m and is already at 13.4% when
  water reaches the slab. `np.interp` already holds a curve's first value below its
  first point, which is what the curve itself says should happen.
- **`CurveFamily.USACE` has no bundled approximation**, and `bundled_curves` raises for
  it rather than returning something. `BUNDLED_FAMILIES` now names the three that do,
  because iterating `CurveFamily` and calling `bundled_curves` on each was a pattern
  three tests had already adopted.
- **Family sampling is switched off when a single published library is supplied.** The
  family term stands in for disagreement between competing approximations; there is
  one USACE library, so sampling across families would be inventing spread.

**NSI values are modelled, not appraised** — derived from occupancy type, area and
regional construction costs. Nationally consistent, sound summed over tens of thousands
of buildings, not sound for any single one. And NSI gives a point plus a footprint area,
not an outline, so `structure_footprints` squares that area around the point; `p90` over
a square of the right size beats sampling whichever cell the centroid lands in.

## 2026-09-05 — the numbers moved a long way, and here is the accounting

Same watershed, same discharge, same terrain. Only the inventory and curves changed.

|  | Overture + guessed rates | NSI + USACE |
|---|---|---|
| structures | 256,436 | 258,527 |
| above finished floor | 42,556 | 32,833 |
| exposed value | USD 423 bn | USD 191 bn |
| damage | USD 3.83 bn | **USD 17.1 bn** |
| of which contents | — | USD 6.93 bn |
| loss ratio | 0.9% | 9.0% |
| people | 98,412 (WorldPop cells) | 132,389 overnight, 242,010 by day (NSI structures) |

Four independent movements, none of them cancelling:

1. **Exposed value halved** (423 -> 191 bn). The flat rate was too high, as the 1.4x
   measurement predicted.
2. **Fewer buildings clear the floor** (42,556 -> 32,833). NSI's real foundation
   heights are mostly above the 0.15 m freeboard the config assumed, so water that
   used to reach the floor now does not.
3. **Contents added USD 6.93 bn**, about 68% on top of structure damage. Previously
   missing entirely.
4. **The USACE curves are steeper than the guessed constants** at the depths that
   matter: 23.3% at one foot against the bundled 13%.

Net: damage up 4.5x on half the exposed value, so the loss ratio moved 0.9% -> 9.0%.
The old figure was not a worse estimate of the same thing; it was an estimate of a
different, smaller thing.

Worth its own line: WorldPop says 98,412 people in flooded *cells*, NSI says 132,389
residents in flooded *structures* — 35% apart on the same flood, from two open sources.
That gap is the population-disagreement experiment in miniature, and it arrived free.

## 2026-09-05 — damage on the map, and why it is a PNG and a separate button

The building table is not a map layer: 258,527 structures is 189 MB as GeoJSON and
still 22 MB as damaged centroids alone. So exposure follows the depth overlay's
pattern — rasterise onto the bundle's own display grid, ship a PNG. 247 kB for
Whiteoak Bayou, and it registers with the flood layer pixel for pixel because it is
built on the same transform.

Two channels, because damage and count answer different questions and neither recovers
the other: one costly commercial building and forty flooded houses can carry the same
dollar total. Red is log10 currency per cell (linear would put nearly every cell in the
bottom two values — flood damage spans five orders of magnitude across a watershed),
green is the building count.

`/api/exposure/{huc}` is deliberately separate from `/api/compute/{huc}` rather than
folded into it. Depth is 12-25 s; exposure is 80-200 s cold because the structure
inventory dominates. Putting them together would have made every map click pay for
buildings nobody asked to see.

Two defects found while wiring it, both worth recording as classes rather than fixes:

- **Two owners of one piece of state.** `paintDamage` and the damage chip's handler
  each set the layer's visibility and its legend, so whichever ran last won and the
  legend ended up hidden with the chip switched on. Now one `syncDamage` owns both and
  everything else calls it. The follow-on fix was to stop synthesising a click on the
  chip to turn the layer on — routing through the handler made the order of two
  queued callbacks decide what the reader saw.
- **Two resolutions for one picture.** The UI computed depth at the user's chosen
  resolution but asked for exposure at a hard-coded 30 m, so with the 10 m chip on the
  damage would have been priced off a coarser depth raster than the map was drawing.
  The exposure request now carries the resolution the model on screen was built at.

Not verified: the damage raster actually painting. The Browser pane in this environment
reports `document.hidden`, so requestAnimationFrame never runs and MapLibre never
initialises its style. The endpoint's output, the panel's rendered text and the encoder
are all checked; the raster landing on the map is not.

## 2026-09-06 — the last stubs closed, and a commit hook that finishes

`floodline validate` and `floodline report` were the two remaining commands that exited
2. Both now run, and `_not_implemented` is reachable from exactly one place:
`condition --streams`, which declines stream burning rather than faking it. A test
asserts that, so a future stub cannot creep back in unnoticed.

- **`validate/metrics.py`** computes CSI, hit rate, false alarm ratio and bias, and
  returns all four together rather than letting a caller quote one. Hit rate alone is
  worthless - a model that floods the whole watershed scores 1.0, which is the first
  test in the file. Cells outside the reference's coverage are excluded rather than
  counted dry, since a SAR swath edge would otherwise contribute correct negatives that
  flatter every ratio with them in the denominator. Verified end to end against a
  hand-computed CSI of 0.563 on two overlapping rectangles.
  Mark scoring lives here too, with the rule the project already follows written down:
  a mark the model leaves dry is scored from ground level, not dropped, because the
  marks a model misses are the ones it gets most wrong. A test asserts that dropping
  them would have produced a better-looking number.
- **`report/render.py`** writes a self-contained HTML page - limits before figures, per
  the README's own rule, with a test that asserts the section order. Images inline as
  data URIs, no stylesheet link, no network: the file can be moved or sent and still
  work. The Whiteoak Bayou report is 182 kB including a 623x443 depth map.

**The pre-commit hook was taking seven minutes**, of which 170 s was one 4096x4096
terrain benchmark. Benchmarks measure runtime, not correctness, so paying for them on
every commit buys nothing a developer acts on. The hook now runs `-m "not slow"` and
finishes in about 25 s; CI still runs the whole suite, benchmarks included.

One process note worth recording rather than hiding: the commit that wired the CLI to
`validate.metrics` used `git commit -am`, which skips untracked files, so it landed
referencing a module that was not in the repository. A clean clone of that commit would
not have imported. Caught by cloning the repo into a temp directory and walking every
intra-package import against the checked-out tree - worth doing after any commit that
adds a module.

## 2026-09-06 — a missing watershed is a 404, not an outage

Probing the service's error paths found `/api/compute` and `/api/exposure` both
answering a nonexistent HUC with `502 upstream data source failed`, and
`/api/watershed/{huc}` answering *any* upstream failure with `404`. Both directions
send whoever is debugging the wrong way: a 502 for a typo sends them hunting an outage,
a 404 for a real outage sends them hunting a typo.

`WatershedNotFoundError` now subclasses `SourceError`, so anything already catching the
parent still works, and the routes catch it first. Every route returns 404 for a
watershed that does not exist and 502 for a source that failed, with tests asserting
both across all three.

The rest of the error surface checked out: a malformed HUC is 400, an out-of-range
resolution 422, an unmatched geocode 404 with the text explaining what does work, a
point in the Atlantic 404.

## 2026-09-06 — the headline claim, actually tested against NFIP claims

The README has always said the observed building count should land inside the model's
90% interval. Nothing tested it. `validate/claims.py` does.

OpenFEMA publishes every NFIP claim: 48,689 in Harris County for Harvey, USD 4.16 bn
paid. Claim *coordinates* are rounded to 0.1 degrees - about 11 km, useless against a
491 km2 watershed - but `censusBlockGroupFips` is published in full, and block groups
run about a square kilometre in urban Houston. Groups straddling the watershed boundary
are weighted by area share, computed in Albers rather than in degrees.

Result for Whiteoak Bayou: 6,769 claims inside the watershed, USD 705 M paid, against
32,833 modelled inundated structures and USD 17.1 bn modelled damage.

**The interval does not contain the claim count, and the honest reading is that it
should not.** A claim needs a property insured, flooded, and its owner to file; NFIP
take-up outside mapped floodplains was a small share of Houston's stock and Harvey
flooded far beyond them. 6,769 is a floor. The model at 4.9x above it is the expected
direction, and 24x on dollars is expected harder still, since NFIP caps a building
claim at USD 250,000.

So this is a one-sided bound, and the API says so rather than returning a pass:
`model_below_claims` is the only unambiguous failure - an interval whose top sits under
the paid claims cannot be right - and `summary()` refuses to call anything else a
validation. Framing this as "observed count inside the interval, tick" would have been
the easy version and the dishonest one.

Fixed on the way: areas were being measured in degrees, which the project's own CRS
rules forbid. Over one city the distortion largely cancels in a ratio, which is exactly
why the rule exists - nobody should have to check that by hand.

## 2026-09-06 — mark scoring moved into the core, and the resolution experiment run

Validation against surveyed high-water marks lived only in the map's JavaScript, which
meant the CLI reported no RMSE and nothing about the project's only real extent check
was tested. `assess_watershed` now scores marks through `validate.metrics.mark_metrics`
and carries a `MarkMetrics` on the result; the CLI prints it and the report renders it.
Whiteoak Bayou at 30 m: RMSE 1.26 m over 16 graded marks, 12 wet, bias +0.04 m.

Both paths now read the same `high_water_marks_national.json` the service uses, rather
than two copies that could drift.

**The resolution experiment, finally run.** Same watershed, same discharge, same 16
marks:

| cell | cells | flooded | max depth | RMSE | median abs | wet | runtime |
|---|---|---|---|---|---|---|---|
| 10 m | 9.9 M | 117.4 km2 | 18.3 m | 1.21 m | 1.22 m | 9/16 | 39 s |
| 30 m | 1.1 M | 112.2 km2 | 10.9 m | 1.26 m | 0.84 m | 12/16 | 15 s |

Finer is not better. RMSE differs by less than noise on 16 marks, while at 10 m the
median absolute error is worse, three fewer marks are wet, and it costs 2.6x the time.
The mechanism is HAND's own definition: a finer DEM resolves the channel bed deeper,
which raises height-above-drainage for everything around it, so the same stage floods
less. Max depth going 10.9 -> 18.3 m is the same effect seen from the channel side.
This is the third time in this project that refining the grid made agreement worse, and
it is not a defect - it is what happens when the datum a method is built on is itself a
function of resolution.

3 m is unavailable: 1/9 arc-second has no coverage over Houston. 1 m exists (39 tiles)
and runs - 138 M cells in 389 s on a HUC-12 - but 994 M cells over the HUC-10 does not
fit in one pass, and the HUC-12 that fits holds 2 marks, which scores nothing. So 1 m is
demonstrated to run and not demonstrated to help.

## 2026-09-06 — three bugs in the damage layer that only a screenshot could find

The exposure endpoint returned correct JSON, its unit tests passed, and the panel
rendered the right numbers. The layer was still wrong in three ways, and every one of
them needed looking at the map.

1. **The layer was placed nowhere.** `ExposureBundle.bounds` carried the analysis CRS's
   own bounds - UTM 15N, easting 235,853 and northing 3,316,922 - and the page places
   an image source by Web Mercator corners. Those numbers are a valid Web Mercator
   point, somewhere off Antarctica. The depth overlay had always warped its display
   arrays with `_to_web_mercator` before shipping them; the exposure bundle skipped
   that step. Now it warps too, so the two layers register pixel for pixel, and the
   helper is public rather than private since it has a second caller.
2. **It drew a black rectangle.** The image was RGB with damage packed into channels -
   red log10 currency, green building count - copied from how HAND and reach ids are
   shipped. But those are packed because the browser recomputes depth on every slider
   move; damage does not change until the whole assessment is rerun, so there was
   nothing to decode and the raw channel values were being drawn as colour, with black
   wherever damage was zero. Replaced with finished RGBA on a warm ramp, alpha zero
   where nothing was hit. Packing data into an image is right when something will
   decode it and wrong when nothing will.
3. **Bilinear smeared a sparse field.** Warping per-cell damage totals with bilinear
   interpolation spread money into cells holding no buildings: 37% of the grid opaque
   for 32,833 damaged structures. Nearest brings that to 27.6% and keeps each total
   where it belongs. Same reasoning as reach ids, which have always resampled nearest.

Worth naming the pattern: all three passed a test of the payload's *shape* and failed
a look at the *picture*. A test that asserts a PNG is 247 kB and has non-zero pixels
cannot tell you those pixels are the wrong colour in the wrong place. The new tests are
narrower and better for it - one asserts the bounds land in Houston's Web Mercator
range rather than merely being four floats, one asserts undamaged ground is
transparent.

## 2026-09-06 — the curve's own uncertainty was loaded and never sampled

`load_usace_curves` reads a standard deviation at every point of every curve, and
`UsaceCurves.sigma` has carried it since the loader was written. Nothing used it. The
module docstring meanwhile claimed it let "the Monte Carlo sample the published
uncertainty of the curve itself rather than approximating it by switching families".

That was worse than an unused field. With the USACE library, family sampling is
deliberately off - there is one published library, not an ensemble of competing
approximations - so with the sigma unwired, **curve uncertainty was absent from the
interval altogether**. The band covered gauge stage, DEM error and cost, and nothing
about the function turning depth into damage.

Now sampled: `CurveLookup` carries the spread resampled onto its own grid, and each
draw shifts every curve by one standard normal. One draw per sample, not one per
building: the published spread is uncertainty about where the curve sits, and drawing
it independently per structure would average to nothing across 256,436 of them, which
would model the term away rather than model it. Clipped into [0, 1] so a draw cannot
invent damage above total loss.

Measured on Whiteoak Bayou, 300 draws over 256,436 structures: the interval widens from
USD 30.82 bn to USD 32.60 bn, **+6%**. Small, and it was the difference between an
interval that accounted for the curve and one that silently did not.

Also made explicit rather than silent: NSI publishes a vehicle value for every
structure and the USACE library has no vehicle function, so that exposure is collected
and left unpriced. It is now named in the report's limits and in the module docstring
instead of just being a column nothing reads.

## 2026-09-06 — every dry building was being charged 13.4% of its value

Spotted from the map: the damage layer covered far more ground than the flood did.
It was not a rendering problem.

USACE curves are non-zero at zero. RES1-1SNB is already at 13.4% when water touches the
slab, which is correct and is why the curves are indexed from -0.61 m. `floor_depth_m`
is clamped at zero, so a building the water missed by five metres and one the water is
touching are the same number: 0.0. Feeding the clamped depth to a curve defined below
zero charged **205,754 dry buildings 13.4% of their structure value each**.

I had removed the zero-clamp inside `damage_fraction` deliberately, so that USACE's
at-floor damage would survive - and by removing it globally, let every dry building
collect it. The fix that made one case right made the other wrong.

It was in two places. The point estimate took the clamped depth; and
`monte_carlo_damage` clamped the perturbed margin at zero before every draw, so the same
charge landed on every sample too. Both now take the signed `floor_margin_m`, which is
what the curves were always indexed on.

Corrected numbers for Whiteoak Bayou:

|  | before | after |
|---|---|---|
| damage | USD 17.12 bn | **USD 7.37 bn** |
| structure / contents | 10.19 / 6.93 | 3.33 / 4.05 |
| loss ratio | 9.0% | **3.86%** |
| opaque map cells | 27.6% | **11.7%** |
| vs NFIP paid | 24.3x | 10.5x |

11.7% of cells against 12.7% of structures inundated - those finally agree, which is
the check that says the layer is now drawing what the model actually computed.

A guard now refuses the combination outright: a curve set defined below zero, given
depths with a pile of values at exactly 0.0 and nothing negative, raises with the
explanation. Exact zeros are the signature - a genuine signed margin is continuous and
has almost none, while the clamped Houston array had 213,880. An all-wet batch has no
negatives either, so "no negatives" alone was too blunt and produced a false positive
on the first attempt; the zeros are what distinguish them.

The lesson is not the fix. It is that four separate tests asserted this pipeline was
right, the endpoint returned well-formed JSON, and the error was a factor of two in the
headline number - and what caught it was someone looking at the map and asking why the
orange went where the blue did not.

## 2026-09-06 — damage is a function of discharge, not one answer at one flow

The slider always moved the water and never moved the damage, which made the most
interesting question the model can answer — what would a different flood cost — the one
thing it could not show. Now it answers it, in the numbers and on the map.

The insight that makes it cheap: nothing about a building changes with discharge. Its
value, its curve, its foundation height and its height above the nearest drainage are
fixed; only the stage in its reach moves. So the expensive work — reading the raster,
reducing each footprint, matching occupancy codes — happens once, and each further
point is a gather:

    depth above floor at multiplier m = stage_m[reach of building] - HAND - foundation

Thirty-three multipliers over 258,527 structures costs about 13 s, against several
minutes if the raster were re-read at each. `exposure.building_depths` now returns
`hand_m` and `reach_id` per structure, which is all the ladder needs.

On the map, four channels of one RGBA image hold log-damage per cell at multipliers
0.5, 1.0, 2.0 and 3.0, and the browser interpolates between the bracketing pair and
recolours. A raster per rung of the ladder would have been ten megabytes; this is 235
kB and follows the slider continuously. Exactly the trick the depth overlay plays with
its per-reach stage table.

The interval is computed at the observed discharge only. A Monte Carlo at every rung
costs a quarter of an hour and says the same thing stretched, so the panel says which
discharge the band belongs to rather than implying it moved.

## 2026-09-06 — three defects the ladder exposed, all of them older than it

Building the ladder meant computing the same quantity two ways, which is the fastest
way to find out that one of them was wrong. All three predate the ladder.

1. **Zero discharge cost USD 0.46 bn.** The USACE curves are defined below floor level
   because water can sit in a crawlspace without reaching the boards - but that only
   means anything when there *is* water. At zero discharge the channel is empty, and a
   channel-side building was being charged 2.7% of its value against it. The gate is
   that `stage - HAND` must be positive before the below-floor part of a curve applies.
2. **A building with no water was still charged if it had a basement.** Four USACE
   with-basement types start at 1.7% at -2.44 m, correctly: a basement eight feet down
   does take water. `np.interp` holds a curve's first value below its first point, so
   the sentinel standing for "no water at all" collected it. `NO_WATER` is now a named
   constant and anything below `NO_WATER_BELOW` is dry regardless of the curve - a
   distinction the code previously did not make at all.
3. **The panel and the ladder disagreed by 12%.** The point estimate derived a
   building's depth by reducing the depth raster under its footprint; the ladder
   derived it from `stage[reach] - HAND`. Two samplings of one quantity, and no reason
   for a reader to trust either. There is now one definition, used by the point
   estimate, the Monte Carlo and the ladder alike, and they agree to 0.12% - which is
   float noise, not method.

Also fixed while verifying: the slider's redraw was scheduled on `requestAnimationFrame`
alone, which never fires while a page is hidden. A backgrounded tab was left with the
coalescing flag set and every later input dropped. It now falls back to a timer when
hidden, and redraws on becoming visible again.

## 2026-09-06 — the damage curve is drawn, not just tabulated

The report and the CLI now carry damage against discharge, not only the figure at the
observed flow. It is the most informative thing the model produces and the hardest to
put in a table: how fast the cost climbs with the water. One inline SVG, 33 points, the
observed discharge marked with a dashed line so a reader can see at a glance how much
worse a worse flood gets. On Whiteoak Bayou the curve runs from zero to USD 21.45 bn
at three times Harvey's peak, and the observed flood sits about a third of the way up
it.

Also moved to config, because the project's own rule is that no tunable literal lives
in algorithm code and five had crept in: the depth percentile under a footprint (was a
hardcoded 90), the height-above-drainage percentile (10), the nominal storey height
(3 m, used both to infer storeys from a building height and to cap how many the water
reaches), the fallback footprint side for a structure with no recorded area (4 m), and
the four reference multipliers the map layer is rendered at. Unit conversions stay
where they are - 0.3048 is not a preference.

## 2026-09-06 — the observed discharge is now a rung, not an interpolation

The multiplier ladder was `linspace(0, 3, 33)`, step 0.09375. One point on it matters
more than all the others - 1.00x, the discharge every figure in the model is anchored
to - and it was not on the ladder. The map's depth at "1.00x" was interpolated between
0.9375 and 1.03125, and so was the damage panel's. That is where the last 0.12%
disagreement between the ladder and the point estimate came from: not two methods, just
a rung that was not there.

The ladder now steps 0.1 from 0 to 3, which puts 1.0 on it along with every map
reference multiplier, and `discharge_ladder_step` is validated to divide 1.0 exactly -
a config that would step past the observed discharge is refused with the reason. Both
the stage table and the damage ladder read one `discharge_ladder` helper, so they
cannot drift apart about what a multiplier means.

The ladder and the point estimate now agree to 0.0005%, which is float rounding.

## 2026-09-06 — the cache had no schema, and served old payloads to new code

Found by running the demo, not by a test. A watershed cached before the damage ladder
existed was still being served afterwards: no `ladder`, no `reference_multipliers`. The
browser's new decoder fell through to its single-channel path and read the old image's
channels as something they were not, drawing a damage layer over most of a watershed
for a flood that reached 6% of it. The panel numbers came from the stale payload too,
so they looked plausible and simply did not match the picture.

Nothing was wrong with the model. The bug was that a payload's shape can change while
its filename does not, and the reader had no way to tell.

`CACHE_SCHEMA` is now written into every cached payload and checked on read; a mismatch
is a miss and the watershed recomputes, with a log line saying so. Bump it whenever a
field is added, removed or reinterpreted. An entry from before versioning existed has
no `schema` key at all and is therefore correctly treated as stale.

With a fresh cache the layer and the model agree: 4.0% of cells opaque against 5,372
damaged structures of 80,104, on a watershed where the flood reaches 6.7% of them.

This is the fourth defect in this project found by looking at a picture rather than by
a test, and the third where the tests were all green. Payload shape is easy to assert;
whether the payload still means what the reader thinks it means is not.

## 2026-09-06 — a four-step tour on first open

The map assumed a reader who already knew what HAND, a HUC and a discharge multiplier
were. Four steps now, because someone who has to be told five things about a map reads
none of them:

1. what this is, and the limit before the capability
2. how to pick a place
3. how to read the depth and the surveyed marks, and what RMSE means
4. how to price it, and that the slider moves the damage as well as the water

Step one states the model's limit before its capability - screening, not hydraulic;
no levees, no culverts, no reservoir releases - which is the same order the README
uses and for the same reason. Someone should know what a number cannot tell them
before they look at one.

Shown to anyone who has not finished it, including on a return visit, and remembered
in `localStorage` once they click through to the end. Someone who has been taught does
not need teaching again; a `?` beside the wordmark reopens it. A private window that
refuses storage shows the tour every time, which is the right failure.

The reopen button lives in the header, not floating over the map: bottom-right belongs
to the zoom control and bottom-left to the legend, and a floating button would have
landed on one of them at some viewport width.

One bug worth recording because it is a CSS classic: a bare `.tour svg` rule sized the
illustrations to the panel width and caught the 16px brand mark next to the wordmark
along with them, blowing it up to fill the dialog. Scoped to `.tour figure svg`.
