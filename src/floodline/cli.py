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
    from floodline.synthetic import make_synthetic_catchment

    resolved = load_config(config)
    catchment = make_synthetic_catchment(
        rows=rows,
        cols=cols,
        cellsize=cellsize,
        n_pits=pits,
        seed=seed,
        nodata=resolved.raster.nodata,
        epsg=resolved.crs.analysis.to_epsg() or 7856,
    )
    path = write_cog(out, catchment.as_raster(), config=resolved)
    typer.echo(f"wrote {path} ({rows}x{cols}, {len(catchment.pits)} pits)")


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
    hand_raster: Annotated[Path, typer.Argument(exists=True, dir_okay=False, help="HAND raster.")],
    stage_m: Annotated[float, typer.Argument(help="Gauge stage in metres.")],
    out: Annotated[Path, typer.Argument(help="Output depth raster.")],
    config: ConfigOption = None,
) -> None:
    """Turn a stage into an inundation extent and depth raster."""
    _not_implemented("inundate", 2)


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
