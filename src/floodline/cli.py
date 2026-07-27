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
def hand(
    dem: Annotated[Path, typer.Argument(exists=True, dir_okay=False, help="Conditioned DEM.")],
    out: Annotated[Path, typer.Argument(help="Output HAND raster.")],
    config: ConfigOption = None,
) -> None:
    """Compute height above nearest drainage."""
    _not_implemented("hand", 1)


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
