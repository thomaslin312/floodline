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
    reading: Annotated[float, typer.Argument(help="Gauge reading, relative to gauge zero.")],
    out: Annotated[Path, typer.Argument(help="Output depth raster.")],
    gauge_row: Annotated[int, typer.Option(help="Row of the gauge's channel cell.")],
    gauge_col: Annotated[int, typer.Option(help="Column of the gauge's channel cell.")],
    threshold: Annotated[
        int | None, typer.Option(help="Accumulation threshold in cells; overrides config.")
    ] = None,
    config: ConfigOption = None,
) -> None:
    """Turn a gauge reading into an inundation extent and depth raster.

    Requires `hydraulics.gauge_datum_offset_m` in the config: a gauge reading is
    relative to that gauge's own zero, and there is no safe default.
    """
    from floodline.hydraulics.inundate import inundate as flood
    from floodline.hydraulics.stage import resolve_gauge, stage_field
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

    gauge = resolve_gauge(reading, chain.filled, (gauge_row, gauge_col), config=resolved)
    if not chain.streams[gauge_row, gauge_col]:
        typer.secho(
            f"warning: gauge cell ({gauge_row}, {gauge_col}) is not a stream cell. "
            "Its bed elevation is a hillslope, not a channel, so the derived depth "
            "is meaningless. Snap the gauge to the network first.",
            fg=typer.colors.YELLOW,
            err=True,
        )

    stage = stage_field(
        chain.filled, chain.flowdir, gauge, config=resolved, cellsize=raster.cellsize
    )
    result = flood(
        chain.hand.hand,
        stage,
        streams=chain.streams,
        config=resolved,
        cell_area_m2=raster.cell_area_m2,
    )
    path = write_cog(out, raster.with_data(result.depth), config=resolved, dtype="float32")
    typer.echo(
        f"wrote {path} (stage {gauge.reading}"
        f"{resolved.hydraulics.require_gauge_reading_unit().value} = "
        f"{gauge.datum_elevation_m:.2f} m on datum = "
        f"{gauge.depth_m:.2f} m above bed; {result.n_wet} cells wet, "
        f"{result.area_m2 / 1e6:.3f} km2, max depth {result.max_depth_m:.2f} m; "
        f"{result.n_removed_by_connectivity} cells dropped as disconnected)"
    )


@app.command()
def exposure(
    depth: Annotated[Path, typer.Argument(exists=True, dir_okay=False, help="Depth raster.")],
    buildings: Annotated[Path, typer.Argument(exists=True, help="Building footprints.")],
    out: Annotated[Path, typer.Argument(help="Output GeoParquet.")],
    config: ConfigOption = None,
) -> None:
    """Intersect the depth raster with buildings and population."""
    _not_implemented("exposure", 2)


@app.command()
def damage(
    exposed: Annotated[Path, typer.Argument(exists=True, dir_okay=False, help="Exposure table.")],
    out: Annotated[Path, typer.Argument(help="Output GeoParquet.")],
    config: ConfigOption = None,
) -> None:
    """Apply depth-damage curves and Monte Carlo uncertainty."""
    _not_implemented("damage", 3)


@app.command()
def validate(
    modelled: Annotated[Path, typer.Argument(exists=True, dir_okay=False, help="Modelled extent.")],
    reference: Annotated[Path, typer.Argument(exists=True, dir_okay=False, help="SAR extent.")],
    config: ConfigOption = None,
) -> None:
    """Compare modelled extent against Sentinel-1 and published figures."""
    _not_implemented("validate", 4)


@app.command()
def report(
    out: Annotated[Path, typer.Argument(help="Output HTML report.")],
    config: ConfigOption = None,
) -> None:
    """Render the static report."""
    _not_implemented("report", 5)


if __name__ == "__main__":  # pragma: no cover
    app()
