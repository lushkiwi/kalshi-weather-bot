from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Annotated

import typer

from datetime import date

from kalshi_weather_bot.config import Secrets, load_config
from kalshi_weather_bot.logging_setup import get_logger, setup_logging

app = typer.Typer(add_completion=False, help="Kalshi weather trading bot.")
log = get_logger("cli")

LIVE_CONFIRM_SENTINEL = "yes-i-mean-it"


def _bootstrap(config_path: Path, log_level: str) -> tuple:
    setup_logging(level=log_level)
    cfg = load_config(config_path)
    secrets = Secrets.from_env()
    return cfg, secrets


def _require_live_confirmation(secrets: Secrets) -> None:
    """Two-factor guard before any code path submits real-money orders."""
    if secrets.kalshi_live_confirm != LIVE_CONFIRM_SENTINEL:
        typer.echo(
            "refusing to run in live mode: set "
            f"KALSHI_LIVE_CONFIRM={LIVE_CONFIRM_SENTINEL} to arm live trading.",
            err=True,
        )
        raise typer.Exit(code=2)
    today = date.today().isoformat()
    expected = f"LIVE {today}"
    entered = typer.prompt(
        f"Type exactly '{expected}' to confirm live trading for today"
    ).strip()
    if entered != expected:
        typer.echo("confirmation mismatch — aborting.", err=True)
        raise typer.Exit(code=2)


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
def tick(
    config: Annotated[Path, typer.Option("--config", "-c")] = Path("config.yaml"),
    kill_lock: Annotated[Path, typer.Option("--kill-lock")] = Path("kill.lock"),
    log_level: Annotated[str, typer.Option("--log-level")] = "INFO",
) -> None:
    """Run a single trading tick end-to-end (paper mode by default)."""
    from kalshi_weather_bot.alerts.notifier import Notifier
    from kalshi_weather_bot.scheduler.loop import run_tick

    cfg, secrets = _bootstrap(config, log_level)
    if cfg.mode == "live":
        _require_live_confirmation(secrets)
    notifier = Notifier(cfg.alerts, token=secrets.ntfy_token)
    summary = asyncio.run(run_tick(cfg, secrets, kill_lock=kill_lock, notifier=notifier))
    typer.echo(summary.pretty())


@app.command()
def run(
    config: Annotated[Path, typer.Option("--config", "-c")] = Path("config.yaml"),
    cron: Annotated[str, typer.Option("--cron", help="Cron schedule (UTC) for the trading tick.")] = "*/5 * * * *",
    kill_lock: Annotated[Path, typer.Option("--kill-lock")] = Path("kill.lock"),
    log_level: Annotated[str, typer.Option("--log-level")] = "INFO",
) -> None:
    """Start the trading scheduler (paper / demo / live per config)."""
    from kalshi_weather_bot.alerts.notifier import Notifier
    from kalshi_weather_bot.scheduler.daemon import run_scheduler

    cfg, secrets = _bootstrap(config, log_level)
    if cfg.mode == "live":
        _require_live_confirmation(secrets)
    notifier = Notifier(cfg.alerts, token=secrets.ntfy_token)
    asyncio.run(
        run_scheduler(
            cfg, secrets, cron=cron, kill_lock=kill_lock, notifier=notifier
        )
    )


@app.command()
def kill(
    reason: Annotated[str, typer.Option("--reason", "-r")] = "manual",
    kill_lock: Annotated[Path, typer.Option("--kill-lock")] = Path("kill.lock"),
) -> None:
    """Write kill.lock so the scheduler halts before placing any more orders."""
    from kalshi_weather_bot.risk.killswitch import activate, is_killed, read_reason

    if is_killed(kill_lock):
        typer.echo(f"already killed: {read_reason(kill_lock)}")
        raise typer.Exit(code=0)
    activate(kill_lock, reason)
    typer.echo(f"kill switch armed at {kill_lock} (reason={reason!r})")


@app.command()
def unkill(
    kill_lock: Annotated[Path, typer.Option("--kill-lock")] = Path("kill.lock"),
) -> None:
    """Remove kill.lock so the scheduler can resume."""
    from kalshi_weather_bot.risk.killswitch import deactivate

    if deactivate(kill_lock):
        typer.echo(f"kill switch cleared at {kill_lock}")
    else:
        typer.echo(f"no kill lock at {kill_lock}")


@app.command()
def status(
    config: Annotated[Path, typer.Option("--config", "-c")] = Path("config.yaml"),
    limit: Annotated[int, typer.Option("--limit", "-n")] = 10,
    kill_lock: Annotated[Path, typer.Option("--kill-lock")] = Path("kill.lock"),
) -> None:
    """Print kill-switch state plus the most recent decisions and fills."""
    from kalshi_weather_bot.recorder.db import Recorder
    from kalshi_weather_bot.risk.killswitch import is_killed, read_reason

    cfg, _ = _bootstrap(config, "WARNING")

    if is_killed(kill_lock):
        typer.echo(f"KILLED: {read_reason(kill_lock)}")
    else:
        typer.echo("kill switch: clear")

    async def _dump() -> None:
        async with Recorder(cfg.recorder.db_path) as rec:
            decisions = await rec.fetchall(
                "SELECT snapshot_ts, ticker, action, size, limit_price, mode, reason "
                "FROM decisions ORDER BY id DESC LIMIT ?",
                (limit,),
            )
            fills = await rec.fetchall(
                "SELECT filled_ts, ticker, count, yes_price, fee_cents, mode "
                "FROM fills ORDER BY id DESC LIMIT ?",
                (limit,),
            )

        typer.echo(f"\nrecent decisions ({len(decisions)}):")
        for ts, ticker, action, size, limit_price, mode, reason in decisions:
            typer.echo(
                f"  ts={ts} {mode} {ticker} {action} "
                f"size={size} limit={limit_price} reason={reason}"
            )
        typer.echo(f"\nrecent fills ({len(fills)}):")
        for ts, ticker, count, price, fee, mode in fills:
            typer.echo(
                f"  ts={ts} {mode} {ticker} count={count} @ {price}¢ fee={fee}¢"
            )

    asyncio.run(_dump())


@app.command()
def dashboard(
    config: Annotated[Path, typer.Option("--config", "-c")] = Path("config.yaml"),
    host: Annotated[str, typer.Option("--host")] = "0.0.0.0",
    port: Annotated[int, typer.Option("--port")] = 8050,
    kill_lock: Annotated[Path, typer.Option("--kill-lock")] = Path("kill.lock"),
) -> None:
    """Start a read-only web dashboard for monitoring the soak."""
    import uvicorn

    from kalshi_weather_bot.dashboard.app import create_app

    cfg, _ = _bootstrap(config, "WARNING")
    web = create_app(
        db_path=cfg.recorder.db_path,
        log_path="logs/bot.log",
        kill_lock=str(kill_lock),
        mode=cfg.mode,
    )
    typer.echo(f"dashboard at http://{host}:{port}")
    uvicorn.run(web, host=host, port=port, log_level="warning")


if __name__ == "__main__":
    app()
