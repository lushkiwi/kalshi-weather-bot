"""Single trading-tick orchestrator."""

from __future__ import annotations

from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import AsyncIterator
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
from kalshi_weather_bot.execution.broker import Broker
from kalshi_weather_bot.execution.paper import PaperBroker
from kalshi_weather_bot.execution.router import RouteOutcome, route
from kalshi_weather_bot.kalshi.client import KalshiClient
from kalshi_weather_bot.kalshi.models import Market
from kalshi_weather_bot.kalshi.orders import KalshiBroker
from kalshi_weather_bot.logging_setup import get_logger
from kalshi_weather_bot.recorder.db import Recorder
from kalshi_weather_bot.recorder.settlements import check_settlements
from kalshi_weather_bot.risk.health import reconcile_positions
from kalshi_weather_bot.risk.killswitch import activate, is_killed, read_reason

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
    reconciliation_drift: dict[str, tuple[int, int]] = field(default_factory=dict)
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
        if self.reconciliation_drift:
            lines.append("  position drift:")
            for ticker, (db_val, live_val) in sorted(
                self.reconciliation_drift.items()
            ):
                lines.append(f"    {ticker}: db={db_val} live={live_val}")
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


async def _persist_scan(
    rec: Recorder, tick_id: str, now: datetime, rows: list[EdgeRow]
) -> None:
    ts = int(now.timestamp())
    tuples = [
        (
            tick_id,
            ts,
            r.city,
            r.ticker,
            r.strike_desc,
            r.target_date.isoformat() if r.target_date else None,
            r.market_yes_bid,
            r.market_yes_ask,
            r.p_fair,
            r.p_market_mid,
            r.net_edge_buy_yes,
            r.net_edge_buy_no,
            r.flagged_side,
            r.n_samples,
        )
        for r in rows
    ]
    await rec.executemany(
        "INSERT INTO tick_scans "
        "(tick_id,ts,city,ticker,strike,target_date,bid,ask,"
        "p_fair,p_market,net_edge_yes,net_edge_no,flagged,n_samples) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        tuples,
    )
    await rec.commit()


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


@asynccontextmanager
async def _broker_for_mode(
    cfg: AppConfig, secrets: Secrets, recorder: Recorder
) -> AsyncIterator[tuple[Broker, KalshiClient | None]]:
    """Yield the right broker for ``cfg.mode`` along with the live client (if any).

    Paper mode needs no network client; demo/live share the same client
    setup so reconciliation and order submission hit the same session.
    """
    if cfg.mode == "paper":
        yield PaperBroker(recorder, mode="paper"), None
        return

    key_id, pem = secrets.kalshi_credentials(cfg.kalshi.env)
    async with KalshiClient(
        key_id,
        pem,
        env=cfg.kalshi.env,
        rate_limit_per_sec=cfg.kalshi.rate_limit_per_sec,
        timeout_sec=cfg.kalshi.request_timeout_sec,
    ) as kc:
        yield KalshiBroker(kc, recorder, mode=cfg.mode), kc


async def _reconcile_with_live(
    broker: PaperBroker, client: KalshiClient, mode: str
) -> dict[str, tuple[int, int]]:
    """Compare DB-derived paper positions to live Kalshi positions.

    Only exercised in modes that talk to the real API. Returns a drift map
    keyed by ticker — empty if everything matches.
    """
    db_state = await broker.load_portfolio()
    try:
        live_resp = await client.get_positions()
    except Exception as e:
        log.bind(mode=mode).warning("reconcile_fetch_failed", error=str(e))
        return {}
    live_positions: dict[str, int] = {}
    for pos in live_resp.get("market_positions") or live_resp.get("positions") or []:
        ticker = pos.get("ticker")
        count = int(pos.get("position", 0))
        if ticker and count != 0:
            live_positions[ticker] = abs(count)
    report = reconcile_positions(db_state.contracts_per_ticker, live_positions)
    return report.drifted_tickers


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

    markets, by_event_date, nws_highs = await fetch_inputs(cfg, secrets)
    summary.n_markets = len(markets)
    markets_by_ticker = {m.ticker: m for m in markets}

    rows = build_rows(
        markets,
        by_event_date,
        edge_min=cfg.trading.edge_min.default,
        decay_hours=cfg.trading.close_decay_hours,
        now=now,
        nws_highs=nws_highs,
    )
    summary.n_rows = len(rows)
    summary.n_flagged = sum(1 for r in rows if r.flagged_side is not None)

    picks = _pick_per_event_best(rows, markets_by_ticker)

    async with Recorder(cfg.recorder.db_path) as rec:
        await _persist_scan(rec, tick_id, now, rows)
        async with _broker_for_mode(cfg, secrets, rec) as (broker, client):
            # Paper reconciliation is meaningless (DB is the source of truth);
            # for demo/live we diff our DB view against Kalshi. A mismatch
            # halts trading for this tick — next tick will retry.
            if client is not None:
                paper_view = PaperBroker(rec, mode=cfg.mode)
                drift = await _reconcile_with_live(paper_view, client, cfg.mode)
                if drift:
                    summary.reconciliation_drift = drift
                    log_.warning("position_drift_halt", drift=drift)
                    activate(
                        kill_lock,
                        f"position_drift tick={tick_id} tickers={sorted(drift)[:3]}",
                    )
                    if notifier is not None:
                        await notifier.send(
                            "critical",
                            f"[{cfg.mode}] position drift — kill switch armed",
                            "\n".join(
                                f"{t}: db={d} live={l}"
                                for t, (d, l) in sorted(drift.items())
                            ),
                        )
                    return summary

            state = await broker.load_portfolio()
            if not picks:
                log_.info("tick_no_edges", rows=len(rows))
                return summary

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

        # Check for newly settled markets (works in all modes since we
        # always read from production Kalshi).
        try:
            key_id, pem = secrets.kalshi_credentials(cfg.kalshi.env)
            async with KalshiClient(
                key_id, pem,
                env=cfg.kalshi.env,
                rate_limit_per_sec=cfg.kalshi.rate_limit_per_sec,
                timeout_sec=cfg.kalshi.request_timeout_sec,
            ) as settle_client:
                settled = await check_settlements(settle_client, rec)
                if settled:
                    log_.info(
                        "settlements_recorded",
                        count=len(settled),
                        tickers=[t for t, _ in settled],
                    )
        except Exception as exc:
            log_.debug("settlement_check_failed", error=str(exc))

    log_.info(
        "tick_done",
        markets=summary.n_markets,
        flagged=summary.n_flagged,
        fills=summary.n_fills,
    )
    return summary


__all__ = ["DEFAULT_KILL_LOCK", "TickSummary", "run_tick"]
