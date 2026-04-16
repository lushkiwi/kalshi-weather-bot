"""Long-running scheduler that fires ``run_tick`` on a cron schedule.

A single tick crash must not kill the daemon — the whole point of the soak
is observing the bot over days without hand-holding. We catch per-tick
exceptions, record them, and arm the kill switch after ``max_consecutive_failures``
back-to-back errors so a systemic problem halts trading instead of
repeatedly blasting bad orders.

SIGINT / SIGTERM trigger a graceful drain: APScheduler stops dispatching,
any in-flight tick completes, then the process exits.
"""

from __future__ import annotations

import asyncio
import signal
from pathlib import Path

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from kalshi_weather_bot.alerts.notifier import Notifier
from kalshi_weather_bot.config import AppConfig, Secrets
from kalshi_weather_bot.logging_setup import get_logger
from kalshi_weather_bot.risk.killswitch import activate, is_killed
from kalshi_weather_bot.scheduler.loop import DEFAULT_KILL_LOCK, run_tick

log = get_logger("scheduler.daemon")

MAX_CONSECUTIVE_FAILURES = 3


class _TickRunner:
    """Stateful wrapper: owns failure counter + notifier + kill lock."""

    def __init__(
        self,
        cfg: AppConfig,
        secrets: Secrets,
        *,
        kill_lock: Path,
        notifier: Notifier | None,
    ) -> None:
        self._cfg = cfg
        self._secrets = secrets
        self._kill_lock = kill_lock
        self._notifier = notifier
        self._failures = 0
        self._running = False

    async def __call__(self) -> None:
        # Overlap guard: if the previous tick is still running (e.g. a slow
        # API call), skip this firing rather than stack ticks on top of each other.
        if self._running:
            log.warning("tick_overlap_skipped")
            return
        if is_killed(self._kill_lock):
            log.info("tick_skipped_killed_lock")
            return

        self._running = True
        try:
            summary = await run_tick(
                self._cfg,
                self._secrets,
                kill_lock=self._kill_lock,
                notifier=self._notifier,
            )
            self._failures = 0
            log.info(
                "tick_complete",
                tick_id=summary.tick_id,
                fills=summary.n_fills,
                flagged=summary.n_flagged,
                killed=summary.killed,
            )
        except Exception as e:
            self._failures += 1
            log.exception(
                "tick_failed",
                failures=self._failures,
                error_type=type(e).__name__,
            )
            if self._notifier is not None:
                await self._notifier.send(
                    "error",
                    f"[{self._cfg.mode}] tick {self._failures}× failed",
                    f"{type(e).__name__}: {e}",
                )
            if self._failures >= MAX_CONSECUTIVE_FAILURES:
                reason = f"consecutive_failures={self._failures} last={type(e).__name__}"
                activate(self._kill_lock, reason)
                log.critical("kill_switch_armed_after_failures", reason=reason)
                if self._notifier is not None:
                    await self._notifier.send(
                        "critical",
                        f"[{self._cfg.mode}] kill switch armed",
                        reason,
                    )
        finally:
            self._running = False


async def run_scheduler(
    cfg: AppConfig,
    secrets: Secrets,
    *,
    cron: str = "*/5 * * * *",
    kill_lock: Path = DEFAULT_KILL_LOCK,
    notifier: Notifier | None = None,
) -> None:
    """Block forever, firing ``run_tick`` on ``cron``. Returns on SIGINT/SIGTERM."""
    runner = _TickRunner(cfg, secrets, kill_lock=kill_lock, notifier=notifier)
    scheduler = AsyncIOScheduler(timezone="UTC")
    scheduler.add_job(
        runner,
        CronTrigger.from_crontab(cron, timezone="UTC"),
        id="trading_tick",
        max_instances=1,          # defense-in-depth alongside the overlap guard
        coalesce=True,
        misfire_grace_time=60,
    )
    scheduler.start()
    log.info("scheduler_started", cron=cron, mode=cfg.mode, kill_lock=str(kill_lock))

    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop_event.set)
        except NotImplementedError:
            # Windows doesn't support add_signal_handler; fall back to default.
            pass

    if notifier is not None:
        await notifier.send(
            "info",
            f"[{cfg.mode}] scheduler up",
            f"cron={cron} kill_lock={kill_lock}",
        )

    try:
        await stop_event.wait()
    finally:
        log.info("scheduler_stopping")
        scheduler.shutdown(wait=True)
        if notifier is not None:
            await notifier.send("info", f"[{cfg.mode}] scheduler stopped", "graceful shutdown")


__all__ = ["MAX_CONSECUTIVE_FAILURES", "run_scheduler"]
