"""``flask leeuwenhoek ...``"""

from __future__ import annotations

import click
from flask import current_app
from flask.cli import AppGroup, with_appcontext


def _lw():
    lw = current_app.extensions.get("leeuwenhoek")
    if lw is None:
        raise click.ClickException("Leeuwenhoek is not initialized in this app.")
    return lw


cli = AppGroup("leeuwenhoek", help="Leeuwenhoek tools.")


@cli.command()
@with_appcontext
def install() -> None:
    """Create the tables in LEEUWENHOEK_DATABASE_URI."""
    _lw().storage.install()
    click.echo("Tables created.")


@cli.command()
@click.option(
    "--hours", type=float, default=None, help="Retention (defaults to LEEUWENHOEK_PRUNE_HOURS)."
)
@with_appcontext
def prune(hours: float | None) -> None:
    """Delete old entries."""
    removed = _lw().prune(hours)
    click.echo(f"{removed} entries deleted.")


@cli.command()
@with_appcontext
def clear() -> None:
    """Delete everything."""
    click.echo(f"{_lw().storage.clear()} entries deleted.")


@cli.command()
@with_appcontext
def pause() -> None:
    """Pause recording."""
    _lw().set_recording(False)
    click.echo("Recording paused.")


@cli.command()
@with_appcontext
def resume() -> None:
    """Resume recording."""
    _lw().set_recording(True)
    click.echo("Recording resumed.")
