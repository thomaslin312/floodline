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
  Houston, 2017.** Decided by Thomas after I established what is reachable by API.
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

- **Sentinel-1 dropped as a validation reference.** Thomas has no Planetary Computer
  access, and every RTC path needs a credential — MPC declares
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
