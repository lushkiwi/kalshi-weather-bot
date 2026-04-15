from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Annotated

import typer

from kalshi_weather_bot.config import Secrets, load_config
from kalshi_weather_bot.logging_setup import get_logger, setup_logging

app = typer.Typer(add_completion=False, help="Kalshi weather trading bot.")
log = get_logger("cli")


def _bootstrap(config_path: Path, log_level: str) -> tuple:
    setup_logging(level=log_level)
    cfg = load_config(config_path)
    secrets = Secrets.from_env()
    return cfg, secrets


@app.command()
def backfill(
    days: Annotated[int, typer.Option(help="How many days of markets + forecasts to snapshot.")] = 1,
    config: Annotated[Path, typer.Option("--config", "-c")] = Path("config.yaml"),
    log_level: Annotated[str, typer.Option("--log-level")] = "INFO",
) -> None:
    """Fetch current markets + forecasts once and write to SQLite."""
    from kalshi_weather_bot.recorder.snapshot import run_backfill

    cfg, secrets = _bootstrap(config, log_level)
    asyncio.run(run_backfill(cfg, secrets, days=days))


@app.command("inspect-edges")
def inspect_edges(
    config: Annotated[Path, typer.Option("--config", "-c")] = Path("config.yaml"),
    log_level: Annotated[str, typer.Option("--log-level")] = "INFO",
) -> None:
    """Dry-run: compute fair probabilities and edges, print a table, place no trades."""
    from kalshi_weather_bot.edge.inspect import format_table, run_inspect_sync

    cfg, secrets = _bootstrap(config, log_level)
    rows = run_inspect_sync(cfg, secrets)
    typer.echo(format_table(rows))


@app.command()
def run(
    config: Annotated[Path, typer.Option("--config", "-c")] = Path("config.yaml"),
    log_level: Annotated[str, typer.Option("--log-level")] = "INFO",
) -> None:
    """Start the trading scheduler (paper / demo / live per config)."""
    _bootstrap(config, log_level)
    typer.echo("run is implemented in Milestone 4.")
    raise typer.Exit(code=2)


@app.command()
def kill(
    config: Annotated[Path, typer.Option("--config", "-c")] = Path("config.yaml"),
) -> None:
    """Cancel all open orders and halt the scheduler via kill.lock."""
    _bootstrap(config, "INFO")
    typer.echo("kill is implemented in Milestone 4.")
    raise typer.Exit(code=2)


@app.command()
def status(
    config: Annotated[Path, typer.Option("--config", "-c")] = Path("config.yaml"),
) -> None:
    """Print recent activity summary from the recorder DB."""
    _bootstrap(config, "INFO")
    typer.echo("status is implemented in Milestone 4.")
    raise typer.Exit(code=2)


if __name__ == "__main__":
    app()
