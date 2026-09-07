"""floodline command line interface.

The subcommands mirror the pipeline stages. Stages that are not implemented yet
exit with a clear "not implemented (phase N)" message rather than pretending.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer

from floodline import __version__
from floodline.config import Config

app = typer.Typer(
    name="floodline",
    help="Flood extent, exposure and damage estimation from a DEM and a gauge.",
    no_args_is_help=True,
    add_completion=False,
)

ConfigOption = Annotated[
    Path | None,
    typer.Option("--config", "-c", exists=True, dir_okay=False, help="TOML config file."),
]


def load_config(path: Path | None) -> Config:
    """Return the config at `path`, or the defaults when `path` is None."""
    return Config.from_file(path) if path is not None else Config()


def _not_implemented(stage: str, phase: int) -> None:
    """Exit with a message naming the stage and the phase that will deliver it."""
    typer.secho(
        f"`floodline {stage}` is not implemented yet (phase {phase}).",
        fg=typer.colors.YELLOW,
        err=True,
    )
    raise typer.Exit(code=2)


def _version_callback(value: bool) -> None:
    """Print the version and exit. Eager, so it works without a subcommand."""
    if value:
        typer.echo(__version__)
        raise typer.Exit


@app.callback()
def main(
    version: Annotated[
        bool,
        typer.Option(
            "--version",
            is_eager=True,
            callback=_version_callback,
            help="Print the version and exit.",
        ),
    ] = False,
) -> None:
    """Flood extent, exposure and damage estimation from a DEM and a gauge."""


@app.command("config")
def show_config(config: ConfigOption = None) -> None:
    """Print the resolved configuration as JSON."""
    resolved = load_config(config)
    typer.echo(resolved.model_dump_json(indent=2))


@app.command()
def synth(
    out: Annotated[Path, typer.Argument(help="Output GeoTIFF (written as a COG).")],
    rows: Annotated[int, typer.Option(help="Grid rows.")] = 120,
    cols: Annotated[int, typer.Option(help="Grid columns.")] = 90,
    cellsize: Annotated[float, typer.Option(help="Cell size in metres.")] = 5.0,
    pits: Annotated[int, typer.Option(help="Number of depressions to punch in.")] = 5,
    seed: Annotated[int, typer.Option(help="Random seed.")] = 0,
    config: ConfigOption = None,
) -> None:
    """Write a synthetic catchment DEM: tilted plane, carved valley, a few pits."""
    from floodline.io.raster import write_cog
    from floodline.synthetic import DEFAULT_CRS_EPSG, make_synthetic_catchment

    resolved = load_config(config)
    catchment = make_synthetic_catchment(
        rows=rows,
        cols=cols,
        cellsize=cellsize,
        n_pits=pits,
        seed=seed,
        nodata=resolved.raster.nodata,
        epsg=resolved.crs.analysis.to_epsg() or DEFAULT_CRS_EPSG,
    )
    path = write_cog(out, catchment.as_raster(), config=resolved)
    typer.echo(f"wrote {path} ({rows}x{cols}, {len(catchment.pits)} pits)")


@app.command("fetch")
def fetch_data(
    sources: Annotated[
        list[str] | None,
        typer.Argument(help="Source names. Defaults to every automatable source."),
    ] = None,
    dest: Annotated[
        Path | None, typer.Option(help="Download directory. Defaults to paths.raw.")
    ] = None,
    manifest: Annotated[Path, typer.Option(help="Manifest to regenerate.")] = Path(
        "data/MANIFEST.md"
    ),
    limit: Annotated[
        int | None, typer.Option(help="Cap files per source, for a smoke run.")
    ] = None,
    dry_run: Annotated[
        bool, typer.Option("--dry-run", help="Report what would be fetched, and how big.")
    ] = False,
    show: Annotated[bool, typer.Option("--list", help="List sources and exit.")] = False,
    config: ConfigOption = None,
) -> None:
    """Download the case's input data, with checksums, into data/raw."""
    from floodline.io import sources as src

    resolved = load_config(config)

    if show:
        for source in src.list_sources():
            need = ""
            if source.credential_env:
                need = f"  [needs ${source.credential_env}]"
            elif source.manual_note:
                need = f"  [manual: {source.manual_note}]"
            typer.echo(f"{source.name:20s} {source.description}{need}")
        return

    results = src.fetch(
        sources or None,
        config=resolved,
        dest=dest,
        limit=limit,
        dry_run=dry_run,
    )

    failed = 0
    for result in results:
        if result.note.startswith("FAILED"):
            failed += 1
            typer.secho(f"{result.source:20s} {result.note}", fg=typer.colors.RED, err=True)
            continue
        typer.echo(
            f"{result.source:20s} {len(result.artifacts):>4} file(s)  "
            f"{result.total_bytes / 1e6:>10.1f} MB"
            + (f"  ({result.reused} reused)" if result.reused else "")
            + ("  [dry run]" if dry_run else "")
        )

    if not dry_run:
        path = src.write_manifest(manifest, results, config=resolved)
        typer.echo(f"wrote {path}")

    if failed:
        raise typer.Exit(code=1)


@app.command()
def watersheds(
    path: Annotated[Path, typer.Option(help="Fetched WBD GeoJSON.")] = Path(
        "data/raw/watersheds/huc10.geojson"
    ),
    resolution: Annotated[
        float, typer.Option(help="Resolution to size each unit against, in metres.")
    ] = 10.0,
    config: ConfigOption = None,
) -> None:
    """List the watersheds available as units of work, and what each would cost.

    Terrain products computed over an arbitrary box are wrong near its edges,
    because flow accumulation depends on contributing area the box cannot see. A
    watershed is hydrologically complete, so it is the right unit to run over.
    """
    from floodline.io.ingest import load_watersheds

    resolved = load_config(config)
    if not path.exists():
        typer.secho(
            f"{path} not found. Run: floodline fetch usgs-watersheds",
            fg=typer.colors.RED,
            err=True,
        )
        raise typer.Exit(code=1)

    units = load_watersheds(path, config=resolved)
    typer.echo(f"{'HUC':<13}{'name':<38}{'km2':>7}{'Mcells':>9}{'peak':>9}")
    for unit in units:
        cells = unit.cells_at(resolution)
        typer.echo(
            f"{unit.huc:<13}{unit.name[:36]:<38}{unit.area_km2:>7.0f}"
            f"{cells / 1e6:>9.1f}{cells * 125 / 1e9:>8.1f}G"
        )
    typer.echo(
        f"\n{len(units)} units at {resolution:g} m; "
        f"peak memory is roughly 125 bytes per cell for the global fill."
    )


@app.command()
def ingest(
    tiles: Annotated[Path, typer.Argument(exists=True, help="Directory of downloaded DEM tiles.")],
    out: Annotated[Path, typer.Argument(help="Output DEM (COG, analysis CRS).")],
    resolution: Annotated[float, typer.Option(help="Output cell size in metres.")] = 30.0,
    huc: Annotated[
        str | None,
        typer.Option(help="Clip to this watershed instead of the AOI box."),
    ] = None,
    watersheds_path: Annotated[
        Path, typer.Option("--watersheds", help="Fetched WBD GeoJSON.")
    ] = Path("data/raw/watersheds/huc10.geojson"),
    no_clip: Annotated[
        bool, typer.Option("--no-clip", help="Keep the full tile extent instead of the AOI.")
    ] = False,
    config: ConfigOption = None,
) -> None:
    """Reproject, mosaic and clip fetched DEM tiles into one analysis-ready raster.

    Downloaded 3DEP tiles are in EPSG:4269, a geographic CRS the rest of the
    pipeline refuses. This is the step that makes them usable.

    Prefer `--huc`: a watershed is hydrologically complete, so the terrain chain run
    over it is correct throughout. Over an arbitrary box it is not, because flow
    accumulation depends on contributing area the box cannot see.
    """
    from floodline.io.ingest import ingest_dem, load_watersheds, select_tiles
    from floodline.io.raster import write_cog

    resolved = load_config(config)

    unit = None
    if huc is not None:
        if not watersheds_path.exists():
            typer.secho(
                f"{watersheds_path} not found. Run: floodline fetch usgs-watersheds",
                fg=typer.colors.RED,
                err=True,
            )
            raise typer.Exit(code=1)
        matches = [w for w in load_watersheds(watersheds_path, config=resolved) if w.huc == huc]
        if not matches:
            typer.secho(
                f"no watershed {huc} in {watersheds_path}; try: floodline watersheds",
                fg=typer.colors.RED,
                err=True,
            )
            raise typer.Exit(code=1)
        unit = matches[0]
        typer.echo(f"clipping to HUC {unit.huc} {unit.name} ({unit.area_km2:.0f} km2)")
    paths = sorted(tiles.glob("*.tif")) if tiles.is_dir() else [tiles]
    if not paths:
        typer.secho(f"no .tif files under {tiles}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1)

    groups = select_tiles(paths, config=resolved)
    dropped = sum(len(g.rejected) for g in groups)
    if dropped:
        typer.echo(
            f"{len(paths)} tiles -> {len(groups)} footprints "
            f"({dropped} superseded by vintage: {resolved.case.dem_vintage.value})"
        )
    undated = [g for g in groups if g.chosen_date is None]
    if undated:
        typer.secho(
            f"warning: {len(undated)} footprint(s) had no survey date in the filename; "
            "vintage was chosen by name order, not by date.",
            fg=typer.colors.YELLOW,
            err=True,
        )

    dem = ingest_dem(paths, resolution_m=resolution, config=resolved, clip_to_aoi=not no_clip)
    path = write_cog(out, dem, config=resolved, dtype="float32")
    import numpy as np

    valid = np.isfinite(dem.data)
    typer.echo(
        f"wrote {path} ({dem.shape[1]} x {dem.shape[0]} at {resolution:g} m, "
        f"{dem.crs.to_string()}, {valid.mean():.1%} valid, "
        f"{np.nanmin(dem.data):.1f}..{np.nanmax(dem.data):.1f} m)"
    )


@app.command()
def serve(
    host: Annotated[str, typer.Option(help="Interface to bind.")] = "127.0.0.1",
    port: Annotated[int, typer.Option(help="Port to listen on.")] = 8000,
    cache: Annotated[Path, typer.Option(help="Where computed watersheds are kept.")] = Path(
        "outputs/cache"
    ),
    config: ConfigOption = None,
) -> None:
    """Serve the map interface: click or search for any US watershed and compute it.

    A page published as an Artifact cannot call an API - its content security policy
    forbids `fetch` to external hosts - so the interactive version has to be served
    from the same origin as the service. This is that server, and the same
    application deploys unchanged anywhere that runs Python.
    """
    try:
        import uvicorn
    except ModuleNotFoundError as exc:  # pragma: no cover - depends on install extras
        typer.secho(
            "the serve extra is not installed. Run: uv sync --all-extras",
            fg=typer.colors.RED,
            err=True,
        )
        raise typer.Exit(code=1) from exc

    from floodline.service import create_app

    typer.echo(f"floodline on http://{host}:{port}  (cache: {cache})")
    uvicorn.run(create_app(config=load_config(config), cache_dir=cache), host=host, port=port)


@app.command()
def condition(
    dem: Annotated[Path, typer.Argument(exists=True, dir_okay=False, help="Input DEM.")],
    out: Annotated[Path, typer.Argument(help="Output filled DEM.")],
    streams: Annotated[
        Path | None, typer.Option(help="Stream network to burn before filling.")
    ] = None,
    config: ConfigOption = None,
) -> None:
    """Condition a DEM: burn streams, then fill depressions.

    Stream burning is not implemented yet, so `--streams` is refused rather than
    silently ignored.
    """
    from floodline.io.raster import read_raster, write_cog
    from floodline.terrain.fill import fill_depressions

    if streams is not None:
        _not_implemented("condition --streams", 1)

    resolved = load_config(config)
    raster = read_raster(dem, config=resolved)
    filled, raised = fill_depressions(
        raster.data, config=resolved, nodata=raster.nodata, return_raised=True
    )
    path = write_cog(out, raster.with_data(filled), config=resolved)
    typer.echo(f"wrote {path} ({raised} cells raised by depression filling)")


@app.command()
def streams(
    dem: Annotated[Path, typer.Argument(exists=True, dir_okay=False, help="Conditioned DEM.")],
    out: Annotated[Path, typer.Argument(help="Output stream network (GeoParquet).")],
    raster_out: Annotated[
        Path | None, typer.Option(help="Also write the stream mask as a COG.")
    ] = None,
    threshold: Annotated[
        int | None, typer.Option(help="Accumulation threshold in cells; overrides config.")
    ] = None,
    config: ConfigOption = None,
) -> None:
    """Derive the stream network from a conditioned DEM."""
    from floodline.io.raster import read_raster, write_cog
    from floodline.io.vector import write_vector
    from floodline.terrain.route import route_terrain
    from floodline.terrain.streams import stream_network

    resolved = load_config(config)
    raster = read_raster(dem, config=resolved)
    chain = route_terrain(
        raster.data,
        config=resolved,
        nodata=raster.nodata,
        cellsize=raster.cellsize,
        stream_threshold=threshold,
    )
    _warn_if_stranded(chain)

    network = stream_network(
        chain.streams,
        chain.flowdir,
        chain.accumulation.accumulation,
        raster.transform,
        raster.crs,
    )
    path = write_vector(out, network, config=resolved)
    if raster_out is not None:
        write_cog(
            raster_out,
            raster.with_data(chain.streams.astype("uint8"), nodata=0),
            config=resolved,
            dtype="uint8",
        )
    typer.echo(f"wrote {path} ({len(network)} links, {int(chain.streams.sum())} stream cells)")


def _warn_if_stranded(chain: object) -> None:
    """Warn on stderr when any water fails to reach the edge of the data."""
    from floodline.terrain.route import TerrainChain

    assert isinstance(chain, TerrainChain)
    if chain.accumulation.cells_draining_to_flats:
        typer.secho(
            f"warning: {chain.accumulation.cells_draining_to_flats} cells "
            f"({chain.accumulation.flat_drainage_fraction:.1%}) drain into flats and "
            "never reach an outlet. Enable terrain.resolve_flats, or set "
            "terrain.fill_epsilon > 0.",
            fg=typer.colors.YELLOW,
            err=True,
        )


@app.command()
def hand(
    dem: Annotated[Path, typer.Argument(exists=True, dir_okay=False, help="Conditioned DEM.")],
    out: Annotated[Path, typer.Argument(help="Output HAND raster.")],
    threshold: Annotated[
        int | None, typer.Option(help="Accumulation threshold in cells; overrides config.")
    ] = None,
    config: ConfigOption = None,
) -> None:
    """Compute height above nearest drainage."""
    from floodline.io.raster import read_raster, write_cog
    from floodline.terrain.route import route_terrain

    resolved = load_config(config)
    raster = read_raster(dem, config=resolved)
    chain = route_terrain(
        raster.data,
        config=resolved,
        nodata=raster.nodata,
        cellsize=raster.cellsize,
        stream_threshold=threshold,
    )
    _warn_if_stranded(chain)
    if chain.hand.cells_without_drainage:
        typer.secho(
            f"note: {chain.hand.cells_without_drainage} cells "
            f"({chain.hand.undrained_fraction:.1%}) never reach a stream and are nodata "
            "in the HAND raster.",
            fg=typer.colors.YELLOW,
            err=True,
        )
    path = write_cog(out, raster.with_data(chain.hand.hand), config=resolved, dtype="float32")
    typer.echo(
        f"wrote {path} ({int(chain.streams.sum())} stream cells, "
        f"{chain.flat_cells_before} flats resolved)"
    )


@app.command()
def inundate(
    dem: Annotated[Path, typer.Argument(exists=True, dir_okay=False, help="Conditioned DEM.")],
    discharge_cms: Annotated[
        float, typer.Argument(help="Observed discharge at the gauge, cubic metres per second.")
    ],
    out: Annotated[Path, typer.Argument(help="Output depth raster.")],
    gauge_row: Annotated[int, typer.Option(help="Row of the gauge's channel cell.")],
    gauge_col: Annotated[int, typer.Option(help="Column of the gauge's channel cell.")],
    threshold: Annotated[
        int | None, typer.Option(help="Accumulation threshold in cells; overrides config.")
    ] = None,
    config: ConfigOption = None,
) -> None:
    """Turn an observed discharge into an inundation extent and depth raster.

    Discharge, not stage. Each reach gets its own stage from a synthetic rating
    curve built out of its own HAND geometry, because a single stage applied across
    a watershed does not work: measured against surveyed high-water marks, the
    constant-threshold model was 6.7 m high with an RMSE of 7.9 m, and no constant
    does better than 4.1 m. The per-reach curves bring that to 1.4 m.
    """
    import numpy as np

    from floodline.hydraulics.inundate import inundate as flood
    from floodline.hydraulics.rating import (
        build_rating_curves,
        discharge_by_area_ratio,
        reach_catchments,
    )
    from floodline.hydraulics.stage import stage_field_from_discharge
    from floodline.io.raster import read_raster, write_cog
    from floodline.terrain.route import route_terrain
    from floodline.terrain.streams import link_raster

    resolved = load_config(config)
    raster = read_raster(dem, config=resolved)
    chain = route_terrain(
        raster.data,
        config=resolved,
        nodata=raster.nodata,
        cellsize=raster.cellsize,
        stream_threshold=threshold,
    )
    _warn_if_stranded(chain)

    if not chain.streams[gauge_row, gauge_col]:
        typer.secho(
            f"error: gauge cell ({gauge_row}, {gauge_col}) is not on the stream network, "
            "so it has no contributing area to scale discharge from. Snap it first.",
            fg=typer.colors.RED,
            err=True,
        )
        raise typer.Exit(code=1)

    link_ids, links = link_raster(chain.streams, chain.flowdir)
    reach_of = reach_catchments(chain.hand.drainage_index, link_ids)
    curves = build_rating_curves(
        chain.hand.hand,
        chain.filled,
        links,
        reach_of,
        config=resolved,
        cellsize=raster.cellsize,
    )
    gauge_area = float(chain.accumulation.accumulation[gauge_row, gauge_col])
    flows = discharge_by_area_ratio(
        discharge_cms, gauge_area, links, chain.accumulation.accumulation, config=resolved
    )
    stages = stage_field_from_discharge(reach_of, curves, flows)
    if stages.reaches_off_the_curve:
        typer.secho(
            f"warning: {stages.reaches_off_the_curve} reaches carry more than the top of "
            f"their rating curve ({resolved.hydraulics.rating_max_stage_m:g} m); their stage "
            "was capped rather than extrapolated.",
            fg=typer.colors.YELLOW,
            err=True,
        )

    result = flood(
        chain.hand.hand,
        stages.stage_m,
        streams=chain.streams,
        config=resolved,
        cell_area_m2=raster.cell_area_m2,
    )
    path = write_cog(out, raster.with_data(result.depth), config=resolved, dtype="float32")
    reach_stages = np.array(list(stages.by_reach.values())) if stages.by_reach else np.zeros(1)
    typer.echo(
        f"wrote {path} ({discharge_cms:,.0f} m3/s at a gauge draining "
        f"{gauge_area * raster.cell_area_m2 / 1e6:,.0f} km2; {len(curves)} rating curves; "
        f"reach stage median {np.median(reach_stages):.2f} m, max {reach_stages.max():.2f} m; "
        f"{result.n_wet:,} cells wet, {result.area_m2 / 1e6:.1f} km2, "
        f"max depth {result.max_depth_m:.2f} m)"
    )


@app.command()
def exposure(
    depth: Annotated[Path, typer.Argument(exists=True, dir_okay=False, help="Depth raster.")],
    buildings: Annotated[Path, typer.Argument(exists=True, help="Building footprints.")],
    out: Annotated[Path, typer.Argument(help="Output GeoParquet.")],
    population: Annotated[
        Path | None,
        typer.Option(help="Population grid on the depth raster's own grid."),
    ] = None,
    unclamped_depth: Annotated[
        Path | None,
        typer.Option(
            help="stage - HAND before the floor at zero, so dry ground is negative. "
            "Supply it: without it the Monte Carlo cannot tell a building the water "
            "missed by a centimetre from one it missed by five metres."
        ),
    ] = None,
    config: ConfigOption = None,
) -> None:
    """Attach a water depth to every building footprint.

    Footprints must already be in the depth raster's CRS; a mismatch is refused
    rather than reprojected, because a silent reprojection is how an exposure table
    ends up describing the wrong ground.
    """
    from floodline.exposure.buildings import building_depths
    from floodline.exposure.population import population_affected
    from floodline.io.raster import read_raster
    from floodline.io.vector import read_vector, write_vector

    resolved = load_config(config)
    raster = read_raster(depth, config=resolved)
    footprints = read_vector(buildings, config=resolved)

    if footprints.crs != raster.crs:
        found = footprints.crs.to_string() if footprints.crs is not None else "no CRS"
        typer.secho(
            f"error: footprints are in {found} but the depth raster is in "
            f"{raster.crs.to_string()}. Reproject the footprints first.",
            fg=typer.colors.RED,
            err=True,
        )
        raise typer.Exit(code=1)

    margin = None
    if unclamped_depth is not None:
        margin_raster = read_raster(unclamped_depth, config=resolved)
        if margin_raster.data.shape != raster.data.shape:
            typer.secho(
                f"error: unclamped depth is {margin_raster.data.shape} but the depth raster "
                f"is {raster.data.shape}; they must be the same grid.",
                fg=typer.colors.RED,
                err=True,
            )
            raise typer.Exit(code=1)
        margin = margin_raster.data

    result = building_depths(
        raster.data,
        raster.transform,
        footprints,
        unclamped_depth=margin,
        config=resolved,
        default_class=resolved.damage.default_class,
    )
    path = write_vector(out, result.buildings, config=resolved)

    dropped = ""
    if result.n_dropped_small or result.n_outside_raster:
        dropped = (
            f"; dropped {result.n_dropped_small:,} under "
            f"{resolved.exposure.min_building_area_m2:g} m2"
            f", {result.n_outside_raster:,} off-raster"
        )
    typer.echo(
        f"wrote {path} ({result.n_inundated:,} of {len(result.buildings):,} buildings above "
        f"finished floor, {result.n_wet_ground:,} with water on the ground"
        f", depth by {resolved.exposure.building_depth_stat.value}{dropped})"
    )
    if not result.has_margin:
        typer.secho(
            "note: no --unclamped-depth given, so every dry building records a depth of "
            "exactly zero and `floodline damage` will hold them dry through the whole "
            "Monte Carlo. The count interval will be conditional on this extent.",
            fg=typer.colors.YELLOW,
            err=True,
        )

    if population is not None:
        grid = read_raster(population, config=resolved)
        if grid.data.shape != raster.data.shape:
            typer.secho(
                f"error: population grid is {grid.data.shape} but the depth raster is "
                f"{raster.data.shape}. Resample it onto the depth grid first — this tool "
                "will not, because resampling a population count either duplicates or "
                "invents people and the choice is yours to state.",
                fg=typer.colors.RED,
                err=True,
            )
            raise typer.Exit(code=1)
        people = population_affected(raster.data, grid.data, config=resolved)
        typer.echo(
            f"{people.people_affected:,.0f} people in cells deeper than "
            f"{people.threshold_m:g} m ({people.share_affected:.1%} of "
            f"{people.people_total:,.0f} in the grid). One grid, one estimate: these "
            "products disagree by tens of percent, most of all in small towns."
        )


@app.command()
def damage(
    exposed: Annotated[Path, typer.Argument(exists=True, dir_okay=False, help="Exposure table.")],
    out: Annotated[Path, typer.Argument(help="Output GeoParquet.")],
    curves: Annotated[
        Path | None, typer.Option(help="Transcribed curve tables as JSON; see damage.curves.")
    ] = None,
    samples: Annotated[
        int | None, typer.Option(help="Monte Carlo draws; overrides config.")
    ] = None,
    config: ConfigOption = None,
) -> None:
    """Apply depth-damage curves and a Monte Carlo interval to an exposure table.

    Reads what `floodline exposure` wrote. The point estimate uses the configured
    curve family; the interval samples stage error, DEM error, curve family and
    replacement cost. It does not sample storey counts, floor area, freeboard, class
    assignment or footprint completeness, so it is a lower bound on the real spread.
    """
    from floodline.damage.curves import load_curves
    from floodline.damage.estimate import estimate_damage
    from floodline.damage.uncertainty import monte_carlo_damage
    from floodline.io.vector import read_vector, write_vector

    resolved = load_config(config)
    table = read_vector(exposed, config=resolved)

    required = {"floor_depth_m", "floor_area_m2", "building_class", "storeys"}
    missing = required - set(table.columns)
    if missing:
        typer.secho(
            f"error: {exposed.name} is missing {', '.join(sorted(missing))}. "
            "Run `floodline exposure` to produce it.",
            fg=typer.colors.RED,
            err=True,
        )
        raise typer.Exit(code=1)

    curve_sets = load_curves(curves, config=resolved) if curves is not None else None
    monte_carlo = (
        resolved.monte_carlo
        if samples is None
        else resolved.monte_carlo.model_copy(update={"n_samples": samples})
    )

    args = (
        table["floor_depth_m"].to_numpy(),
        table["floor_area_m2"].to_numpy(),
        table["building_class"].to_numpy(dtype=object),
    )
    storeys = table["storeys"].to_numpy()
    family = resolved.damage.curve_family
    point = estimate_damage(
        *args,
        storeys=storeys,
        config=resolved,
        curves=curve_sets.get(family) if curve_sets else None,
        family=family,
    )
    margins = table["floor_margin_m"].to_numpy() if "floor_margin_m" in table.columns else None
    interval = monte_carlo_damage(
        *args,
        storeys=storeys,
        floor_margin_m=margins,
        monte_carlo=monte_carlo,
        damage=resolved.damage,
        curve_sets=curve_sets,
    )

    priced = table.copy()
    priced["damage"] = point.per_building
    path = write_vector(out, priced, config=resolved)

    unit = resolved.damage.currency
    low, high = interval.count_interval
    lo_q, hi_q = interval.quantiles
    conditional = " (conditional on this extent)" if interval.count_interval_conditional else ""
    typer.echo(
        f"wrote {path} ({point.n_damaged:,} of {point.n_buildings:,} buildings damaged"
        f", {low:,}-{high:,} across the interval{conditional})"
    )
    typer.echo(
        f"{unit} {point.total:,.0f} on {family.value} curves"
        f" — {int(lo_q * 100)}-{int(hi_q * 100)}% interval {unit} {interval.lower:,.0f}"
        f" to {unit} {interval.upper:,.0f} over {interval.n_samples:,} draws"
        f" (loss ratio {point.loss_ratio:.1%} of {unit} {point.exposed_value_total:,.0f} exposed)"
    )
    for name, amount in point.by_class.items():
        typer.echo(f"  {name:<12} {unit} {amount:>15,.0f}")
    if not interval.curves_verified:
        typer.secho(
            "warning: the bundled curve constants carry the shape of each published family "
            "but their digits have not been checked against the source tables. Building "
            "counts and loss ratios stand; do not quote the currency totals. Pass --curves "
            "with transcribed tables to clear this.",
            fg=typer.colors.YELLOW,
            err=True,
        )


@app.command("fetch-curves")
def fetch_curves(
    cache: Annotated[Path, typer.Option(help="Where to keep it.")] = Path("data/cache"),
) -> None:
    """Download the USACE depth-damage curve library (about half a megabyte).

    51 HAZUS occupancy types, structure and contents curves for each, from the USACE
    Economic Guidance Memoranda by way of github.com/USACE/go-consequences (MIT).
    These are the published values, so damage totals computed with them lose the
    "unverified" warning that the bundled approximations carry.
    """
    from floodline.damage.usace import ensure_usace_curves, load_usace_curves

    path = ensure_usace_curves(cache_dir=cache, download=True)
    curves = load_usace_curves(path)
    typer.echo(
        f"cached {path} ({path.stat().st_size / 1e3:,.0f} kB) — "
        f"{len(curves.codes)} occupancy types, "
        f"{len(curves.contents.curves)} with contents curves"
    )


@app.command("fetch-population")
def fetch_population(
    product: Annotated[
        str, typer.Option(help="worldpop_constrained, worldpop_unconstrained or ghs_pop.")
    ] = "worldpop_constrained",
    iso3: Annotated[str, typer.Option(help="Three-letter country code.")] = "USA",
    year: Annotated[int, typer.Option(help="Product year.")] = 2020,
    cache: Annotated[Path, typer.Option(help="Where to keep it.")] = Path("data/cache"),
) -> None:
    """Download a national population raster once, so windows can be read locally.

    Neither WorldPop nor GHS-POP can be windowed over HTTP: WorldPop advertises
    `Accept-Ranges: bytes` and then ignores the Range header, and GHS-POP is a zip
    whose directory sits at the end of a multi-gigabyte file. WorldPop USA 2020
    constrained is 494 MB and this is a one-time cost.
    """
    from floodline.io.population import PopulationProduct, ensure_population_raster

    try:
        chosen = PopulationProduct(product)
    except ValueError as exc:
        typer.secho(
            f"error: unknown product {product!r}; known: "
            f"{', '.join(p.value for p in PopulationProduct)}",
            fg=typer.colors.RED,
            err=True,
        )
        raise typer.Exit(code=2) from exc

    typer.echo(f"fetching {chosen.value} for {iso3} {year} — this is hundreds of megabytes")
    path = ensure_population_raster(chosen, iso3=iso3, year=year, cache_dir=cache, download=True)
    typer.echo(f"cached {path} ({path.stat().st_size / 1e6:,.0f} MB)")


@app.command()
def assess(
    huc: Annotated[str, typer.Argument(help="Hydrologic unit code, e.g. 1204010403.")],
    out: Annotated[
        Path | None, typer.Option(help="Write the priced building table here as GeoParquet.")
    ] = None,
    resolution: Annotated[int, typer.Option(help="Cell size in metres.")] = 30,
    discharge_cms: Annotated[
        float | None, typer.Option(help="Override the gauge peak. Label it a scenario if you do.")
    ] = None,
    samples: Annotated[int | None, typer.Option(help="Monte Carlo draws.")] = None,
    buildings: Annotated[bool, typer.Option(help="Fetch Overture footprints.")] = True,
    population: Annotated[bool, typer.Option(help="Read a population grid.")] = True,
    download_population: Annotated[
        bool,
        typer.Option(help="Allow the one-time 494 MB national population download."),
    ] = False,
    inventory: Annotated[
        str,
        typer.Option(help="nsi (values per structure) or overture (geometry only)."),
    ] = "nsi",
    download_curves: Annotated[
        bool, typer.Option(help="Allow the USACE curve library download if not cached.")
    ] = False,
    config: ConfigOption = None,
) -> None:
    """Run the whole chain for one watershed on live data: terrain to damage.

    Reads 3DEP over HTTP, finds the watershed's own gauge, places the modelled
    discharge in that gauge's record, pulls Overture footprints and a population
    grid, and prices the result with a Monte Carlo band. Every stage that cannot
    reach its data is reported as a gap rather than filled with a default.

    The Overture read is the slow part — minutes, not seconds — and is cached under
    `data/cache` per release and bounding box.
    """
    from floodline.assess import NoDischargeError, assess_watershed, buildings_geoparquet
    from floodline.compute import watershed_by_huc
    from floodline.io.vector import write_vector

    resolved = load_config(config)
    unit, resolved = watershed_by_huc(huc, config=resolved)
    typer.echo(
        f"{unit.name} — HUC-{len(unit.huc)} {unit.huc} — {unit.area_km2:,.0f} km2 — "
        f"{resolved.crs.analysis.to_string()}"
    )

    try:
        result = assess_watershed(
            unit,
            config=resolved,
            resolution_m=float(resolution),
            discharge_cms=discharge_cms,
            samples=samples,
            with_buildings=buildings,
            with_population=population,
            download_population=download_population,
            inventory=inventory,
            download_curves=download_curves,
        )
    except NoDischargeError as exc:
        typer.secho(f"error: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1) from exc

    source = "observed at a gauge" if result.gauged else "supplied, not observed"
    typer.echo(f"\ndischarge {result.discharge_cms:,.0f} m3/s ({source})")
    if result.history is not None:
        typer.echo(f"  {result.history.summary()}")
        for peak in result.history.larger_floods[:3]:
            typer.echo(
                f"    larger on record: {peak.water_year}  "
                f"{peak.discharge_cms:,.0f} m3/s  ({peak.date})"
            )
    typer.echo(
        f"\nflooded {result.flooded_km2:,.1f} km2 at {resolution} m, "
        f"max depth {result.max_depth_m:.1f} m"
    )

    if result.marks is not None:
        typer.echo(f"marks      {result.marks.summary()}")
    elif result.n_marks_available == 0:
        typer.echo("marks      none surveyed inside this watershed")

    if result.buildings is not None:
        exposed = result.buildings
        typer.echo(
            f"buildings  {exposed.n_inundated:,} above finished floor of "
            f"{len(exposed.buildings):,} ({result.inventory}) "
            f"({exposed.n_wet_ground:,} with water on the ground)"
        )
    if result.night_population is not None:
        typer.echo(
            f"residents  {result.night_population:,.0f} overnight in flooded structures, "
            f"{result.day_population:,.0f} present by day (NSI, per structure)"
        )
    if result.people is not None:
        typer.echo(
            f"people     {result.people.people_affected:,.0f} of "
            f"{result.people.people_total:,.0f} in the window "
            f"({result.people.share_affected:.1%}) — one grid, one estimate"
        )

    if result.damage is not None and result.interval is not None:
        unit_name = resolved.damage.currency
        low, high = result.interval.count_interval
        lo_q, hi_q = result.interval.quantiles
        typer.echo(
            f"damage     {unit_name} {result.damage.total:,.0f} on "
            f"{result.damage.family.value} curves, "
            f"{int(lo_q * 100)}-{int(hi_q * 100)}% {unit_name} "
            f"{result.interval.lower:,.0f} to {result.interval.upper:,.0f} "
            f"({low:,}-{high:,} buildings)"
        )
        typer.echo(
            f"           loss ratio {result.damage.loss_ratio:.1%} of "
            f"{unit_name} {result.damage.exposed_value_total:,.0f} exposed"
        )
        if result.contents_damage:
            structure_only = result.damage.total - result.contents_damage
            typer.echo(
                f"           structure {unit_name} {structure_only:,.0f} + "
                f"contents {unit_name} {result.contents_damage:,.0f}"
            )
        if not result.interval.curves_verified:
            typer.secho(
                "warning: curve constants are unverified — counts and ratios stand, "
                "currency totals do not.",
                fg=typer.colors.YELLOW,
                err=True,
            )

    if result.ladder is not None and max(result.ladder.damage) > 0:
        unit_name = resolved.damage.currency
        typer.echo("\ndamage against discharge")
        step = max(1, len(result.ladder.multipliers) // 6)
        for i in range(0, len(result.ladder.multipliers), step):
            typer.echo(
                f"  {result.ladder.multipliers[i]:>4.2f}x "
                f"{result.ladder.discharge_cms[i]:>8,.0f} m3/s  "
                f"{result.ladder.inundated[i]:>8,} buildings  "
                f"{unit_name} {result.ladder.damage[i] / 1e9:>6.2f} bn"
            )

    for gap in result.gaps:
        typer.secho(f"gap: {gap}", fg=typer.colors.YELLOW, err=True)
    for note in result.warnings:
        typer.secho(f"warning: {note}", fg=typer.colors.YELLOW, err=True)

    if out is not None:
        frame = buildings_geoparquet(result)
        if frame is None:
            typer.secho(
                "nothing to write: exposure or damage did not run.",
                fg=typer.colors.YELLOW,
                err=True,
            )
        else:
            typer.echo(f"wrote {write_vector(out, frame, config=resolved)}")

    typer.echo("\ntimings: " + ", ".join(f"{k} {v:.1f}s" for k, v in result.seconds.items()))


@app.command()
def validate(
    modelled: Annotated[
        Path, typer.Argument(exists=True, dir_okay=False, help="Modelled depth raster.")
    ],
    reference: Annotated[
        Path, typer.Argument(exists=True, dir_okay=False, help="Observed wet mask, same grid.")
    ],
    min_depth: Annotated[
        float, typer.Option(help="Depth above which a modelled cell counts as wet.")
    ] = 0.0,
    config: ConfigOption = None,
) -> None:
    """Score a modelled extent against an observed wet mask.

    Reports CSI, hit rate, false alarm ratio and bias together, never one alone: a
    model that floods the whole watershed scores a perfect hit rate. Cells where the
    reference has no data are excluded rather than counted dry, since a swath edge
    would otherwise contribute correct negatives that flatter every ratio.

    floodline has no observed extent of its own — the Sentinel-1 route was dropped for
    want of credentials — so this takes one you supply. Validation against surveyed
    high-water marks, which is what the project actually reports, runs inside
    `floodline assess` and on the map.
    """
    import numpy as np

    from floodline.io.raster import read_raster
    from floodline.validate.metrics import extent_metrics

    resolved = load_config(config)
    depth = read_raster(modelled, config=resolved)
    observed = read_raster(reference, config=resolved)

    if depth.data.shape != observed.data.shape:
        typer.secho(
            f"error: modelled raster is {depth.data.shape} but the reference is "
            f"{observed.data.shape}; they must be on the same grid.",
            fg=typer.colors.RED,
            err=True,
        )
        raise typer.Exit(code=1)

    wet_model = np.isfinite(depth.data) & (depth.data > min_depth)
    # A reference cell is wet where it is positive, and invalid where it is nodata:
    # "no observation here" is not "no water here".
    reference_data = np.asarray(observed.data, dtype=np.float64)
    valid = np.isfinite(reference_data)
    if observed.nodata is not None:
        valid &= reference_data != observed.nodata
    wet_reference = valid & (reference_data > 0)

    result = extent_metrics(wet_model, wet_reference, valid=valid)
    typer.echo(result.summary())
    typer.echo(
        f"  hits {result.hits:,} · misses {result.misses:,} · "
        f"false alarms {result.false_alarms:,} · scored cells "
        f"{result.hits + result.misses + result.false_alarms + result.correct_negatives:,}"
    )
    if result.bias > 1.0:
        typer.echo(f"  the model floods {result.bias:.2f}x the observed area")
    elif result.bias < 1.0:
        typer.echo(f"  the model floods {result.bias:.2f}x the observed area (under)")


@app.command()
def report(
    huc: Annotated[str, typer.Argument(help="Hydrologic unit code.")],
    out: Annotated[Path, typer.Argument(help="Output HTML report.")],
    resolution: Annotated[int, typer.Option(help="Cell size in metres.")] = 30,
    samples: Annotated[int | None, typer.Option(help="Monte Carlo draws.")] = None,
    buildings: Annotated[bool, typer.Option(help="Include exposure and damage.")] = True,
    config: ConfigOption = None,
) -> None:
    """Render a self-contained HTML report for one watershed.

    Runs the same chain as `assess` and writes the result as a standing page: limits
    first, then figures, then the depth map, with the images inlined so the file can be
    moved or sent without breaking. Read it as the thing you hand someone; the map is
    better for exploring.
    """
    from floodline.assess import NoDischargeError, assess_watershed
    from floodline.compute import watershed_by_huc
    from floodline.report.render import ReportInputs, render_report

    resolved = load_config(config)
    unit, resolved = watershed_by_huc(huc, config=resolved)
    typer.echo(f"{unit.name} — HUC-{len(unit.huc)} {unit.huc} — assessing…")

    try:
        result = assess_watershed(
            unit,
            config=resolved,
            resolution_m=float(resolution),
            samples=samples,
            with_buildings=buildings,
        )
    except NoDischargeError as exc:
        typer.secho(f"error: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1) from exc

    path = render_report(ReportInputs(assessment=result), out)
    size_kb = path.stat().st_size / 1024
    typer.echo(f"wrote {path} ({size_kb:,.0f} kB)")
    for gap in result.gaps:
        typer.secho(f"gap: {gap}", fg=typer.colors.YELLOW, err=True)


if __name__ == "__main__":  # pragma: no cover
    app()


@app.command()
def reproduce(
    target: Annotated[
        list[str] | None,
        typer.Option(help="Target name; repeat for several. Omit to run all."),
    ] = None,
    write: Annotated[
        bool, typer.Option(help="Record what was produced in docs/RESULTS.json.")
    ] = False,
) -> None:
    """Regenerate published results and report any that have moved.

    The repository's rule is that no result stands in it that was not actually
    produced. That is only enforceable if the results can be produced again, so every
    published table is a named target with the code that makes it and the value it
    last made.

    Targets recompute from source data - real elevation, real gauges, real claims -
    rather than reading a cached summary, because a reproduction that reads its own
    output proves nothing. A full run is therefore slow and needs network. Nothing is
    written unless `--write` is passed, so a run can be inspected before it becomes
    the new record.
    """
    from floodline.reproduce import TARGETS, format_report, write_results
    from floodline.reproduce import reproduce as run_targets

    known = {t.name for t in TARGETS}
    wanted = list(target) if target else None
    if wanted:
        unknown = sorted(set(wanted) - known)
        if unknown:
            typer.echo(f"unknown target(s): {', '.join(unknown)}", err=True)
            typer.echo(f"known: {', '.join(sorted(known))}", err=True)
            raise typer.Exit(2)

    for entry in TARGETS:
        if wanted is None or entry.name in wanted:
            typer.echo(f"  {entry.name:<20} {entry.describes}")
    typer.echo("")

    results = run_targets(wanted)
    typer.echo(format_report(results))
    if write:
        write_results(results)
        typer.echo("\nwrote docs/RESULTS.json")

    failed = [r for r in results if r.error]
    moved = [r for r in results if r.drift]
    if failed:
        typer.echo(f"\n{len(failed)} target(s) could not run.", err=True)
        raise typer.Exit(1)
    if moved:
        typer.echo(
            f"\n{len(moved)} target(s) moved. Either the code changed a published "
            "number or the upstream data did; both are worth reading before "
            "accepting them with --write.",
            err=True,
        )
        raise typer.Exit(1)
    typer.echo("\nevery target reproduced.")


@app.command()
def warm(
    huc: Annotated[list[str] | None, typer.Option(help="HUC code; repeat for several.")] = None,
    resolution: Annotated[float, typer.Option(help="Cell size in metres.")] = 30.0,
    cache: Annotated[Path, typer.Option(help="Where computed watersheds are kept.")] = Path(
        "outputs/cache"
    ),
    exposure: Annotated[bool, typer.Option(help="Also value the buildings.")] = True,
    config: ConfigOption = None,
) -> None:
    """Compute watersheds ahead of time, so a demo does not wait on anyone else.

    Every request served cold depends on USGS, USACE and FEMA being up at that moment.
    Over the course of building this, all three have been down or rate-limiting at
    different times, and a first impression should not be a timeout someone else
    caused. Run this before showing the map.

    Drives the service's own routes rather than rebuilding their payloads. An earlier
    version assembled the cache files itself and got the format wrong within an hour;
    going through the routes means a warmed entry is byte-identical to a served one
    because it *is* a served one.
    """
    from fastapi.testclient import TestClient

    from floodline.service import create_app

    resolved = load_config(config)
    cache.mkdir(parents=True, exist_ok=True)
    codes = list(huc) if huc else ["1204010403"]

    # No rate limit against ourselves: this is the operator, not a visitor.
    application = create_app(config=resolved, cache_dir=cache, rate_per_minute=10_000)
    with TestClient(application) as http:
        for code in codes:
            typer.echo(f"{code}: computing at {resolution:g} m...")
            response = http.get(f"/api/compute/{code}", params={"resolution": resolution})
            if response.status_code != 200:
                typer.echo(
                    f"  depth failed: {response.status_code} {response.text[:120]}", err=True
                )
                continue
            typer.echo(f"  depth cached ({response.json().get('name', code)})")
            if not exposure:
                continue
            response = http.get(f"/api/exposure/{code}", params={"resolution": resolution})
            if response.status_code != 200:
                typer.echo(
                    f"  exposure failed: {response.status_code} {response.text[:120]}", err=True
                )
                continue
            stats = response.json().get("stats", {})
            typer.echo(f"  exposure cached ({stats.get('structures', 0):,} structures)")
    typer.echo("\nwarmed. The server will serve these from disk.")
