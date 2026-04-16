"""Single trading-tick orchestrator.

Callable from the CLI (``tick``) for one-shot runs and from APScheduler
(M4 later) for the recurring loop. Everything the tick needs — fetch,
risk-gate, route, record — is composed here so ``run_tick`` stays readable.

Per-event policy (PLAN.md §4): at most one order per event per tick, picking
the flagged candidate with the highest net edge.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from kalshi_weather_bot.alerts.notifier import Notifier
from kalshi_weather_bot.config import AppConfig, Secrets
from kalshi_weather_bot.edge.detector import Candidate, evaluate
from kalshi_weather_bot.edge.implied import implied_from_market
from kalshi_weather_bot.edge.inspect import (
    EdgeRow,
    build_rows,
    fetch_inputs,
)
from kalshi_weather_bot.execution.paper import PaperBroker
from kalshi_weather_bot.execution.router import RouteOutcome, route
from kalshi_weather_bot.kalshi.models import Market
from kalshi_weather_bot.logging_setup import get_logger
from kalshi_weather_bot.recorder.db import Recorder
from kalshi_weather_bot.risk.killswitch import is_killed, read_reason

log = get_logger("scheduler.loop")

DEFAULT_KILL_LOCK = Path("kill.lock")


@dataclass(slots=True)
class TickSummary:
    tick_id: str
    killed: bool = False
    kill_reason: str | None = None
    n_markets: int = 0
    n_rows: int = 0
    n_flagged: int = 0
    outcomes: list[RouteOutcome] = field(default_factory=list)

    @property
    def n_fills(self) -> int:
        return sum(1 for o in self.outcomes if o.fill is not None)

    def pretty(self) -> str:
        lines = [
            f"tick {self.tick_id}",
            f"  killed={self.killed}"
            + (f" reason={self.kill_reason!r}" if self.killed else ""),
            f"  markets={self.n_markets} rows={self.n_rows} flagged={self.n_flagged}",
            f"  outcomes={len(self.outcomes)} fills={self.n_fills}",
        ]
        for o in self.outcomes:
            suffix = ""
            if o.fill:
                suffix = (
                    f" → fill {o.fill.count} @ {o.fill.yes_price_cents}¢"
                    f" fee={o.fill.fee_cents}¢"
                )
            lines.append(
                f"    {o.action:<28} size={o.size:>4} limit={o.limit_price_cents:>3} "
                f"reason={o.reason}{suffix}"
            )
        return "\n".join(lines)


def _pick_per_event_best(rows: list[EdgeRow], markets_by_ticker: dict[str, Market]) -> list[tuple[EdgeRow, Candidate, Market]]:
    """For each event, return the single flagged candidate with the highest net edge."""
    per_event: dict[str, tuple[EdgeRow, Candidate, Market]] = {}
    for row in rows:
        if row.flagged_side is None or row.p_fair is None:
            continue
        m = markets_by_ticker.get(row.ticker)
        if m is None:
            continue
        # Recompute the full candidate list so we have the Candidate object (not just the row).
        cands = evaluate(
            row.p_fair,
            implied_from_market(m),
            edge_min=(row.effective_edge_min or 0.0),
            hours_to_close=row.hours_to_close or 0.0,
            decay_hours=0.0,      # edge_min already at its effective level
        )
        best = max((c for c in cands if c.flagged), key=lambda c: c.net_edge, default=None)
        if best is None:
            continue
        cur = per_event.get(row.event_ticker)
        if cur is None or best.net_edge > cur[1].net_edge:
            per_event[row.event_ticker] = (row, best, m)
    return list(per_event.values())


async def run_tick(
    cfg: AppConfig,
    secrets: Secrets,
    *,
    kill_lock: Path = DEFAULT_KILL_LOCK,
    notifier: Notifier | None = None,
) -> TickSummary:
    tick_id = uuid4().hex[:12]
    now = datetime.now(tz=timezone.utc)
    summary = TickSummary(tick_id=tick_id)
    log_ = log.bind(tick_id=tick_id, mode=cfg.mode)

    if is_killed(kill_lock):
        summary.killed = True
        summary.kill_reason = read_reason(kill_lock)
        log_.warning("tick_skipped_killed", reason=summary.kill_reason)
        return summary

    markets, by_event_date = await fetch_inputs(cfg, secrets)
    summary.n_markets = len(markets)
    markets_by_ticker = {m.ticker: m for m in markets}

    rows = build_rows(
        markets,
        by_event_date,
        edge_min=cfg.trading.edge_min.default,
        decay_hours=cfg.trading.close_decay_hours,
        now=now,
    )
    summary.n_rows = len(rows)
    summary.n_flagged = sum(1 for r in rows if r.flagged_side is not None)

    picks = _pick_per_event_best(rows, markets_by_ticker)
    if not picks:
        log_.info("tick_no_edges", rows=len(rows))
        return summary

    async with Recorder(cfg.recorder.db_path) as rec:
        broker = PaperBroker(rec, mode=cfg.mode)
        state = await broker.load_portfolio()
        for row, candidate, market in picks:
            outcome = await route(
                candidate=candidate,
                market=market,
                event_ticker=row.event_ticker,
                cfg=cfg,
                broker=broker,
                recorder=rec,
                state=state,
                tick_id=tick_id,
                now=now,
            )
            summary.outcomes.append(outcome)
            if outcome.fill and notifier is not None:
                if outcome.fill.count >= cfg.alerts.fill_alert_min_size:
                    await notifier.send(
                        "info",
                        f"[{cfg.mode}] fill {outcome.fill.ticker}",
                        f"{outcome.action} {outcome.fill.count} @ "
                        f"{outcome.fill.yes_price_cents}¢ fee={outcome.fill.fee_cents}¢",
                    )

    log_.info(
        "tick_done",
        markets=summary.n_markets,
        flagged=summary.n_flagged,
        fills=summary.n_fills,
    )
    return summary


__all__ = ["DEFAULT_KILL_LOCK", "TickSummary", "run_tick"]
