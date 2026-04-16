"""Candidate → order pipeline: size, risk-gate, submit, record.

Any decision (trade or skip) is written to the ``decisions`` table so the
dry-run audit trail is always complete. Fills are written by the broker;
the router only mutates ``state`` in-memory so downstream candidates in the
same tick see the new position before it lands in the DB.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from kalshi_weather_bot.config import AppConfig
from kalshi_weather_bot.edge.detector import Candidate
from kalshi_weather_bot.execution.broker import Broker, BrokerFill
from kalshi_weather_bot.execution.sizing import flat_size
from kalshi_weather_bot.kalshi.models import Market
from kalshi_weather_bot.recorder.db import Recorder
from kalshi_weather_bot.risk.limits import (
    PortfolioState,
    apply_fill,
    approve_order,
)


@dataclass(slots=True)
class RouteOutcome:
    action: str                     # 'buy_yes' | 'buy_no' | 'skip_<reason>'
    size: int
    limit_price_cents: int
    reason: str
    fill: BrokerFill | None


def _limit_price(candidate: Candidate, market: Market) -> int:
    if candidate.side == "buy_yes":
        return market.yes_ask or 0
    return 100 - (market.yes_bid or 0)


async def _record_decision(
    recorder: Recorder,
    *,
    ticker: str,
    action: str,
    size: int,
    limit_price: int,
    reason: str,
    mode: str,
    now: datetime,
) -> None:
    await recorder.execute(
        "INSERT INTO decisions (snapshot_ts, ticker, action, size, limit_price, "
        "reason, mode) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (int(now.timestamp()), ticker, action, size, limit_price, reason, mode),
    )
    await recorder.commit()


async def route(
    *,
    candidate: Candidate,
    market: Market,
    event_ticker: str,
    cfg: AppConfig,
    broker: Broker,
    recorder: Recorder,
    state: PortfolioState,
    tick_id: str,
    now: datetime,
) -> RouteOutcome:
    requested = flat_size(cfg.trading, max_allowed=cfg.risk.max_contracts_per_market)
    decision = approve_order(
        event_ticker=event_ticker,
        ticker=market.ticker,
        requested_size=requested,
        state=state,
        cfg=cfg.risk,
    )
    limit_price = _limit_price(candidate, market)

    if decision.approved_size == 0:
        await _record_decision(
            recorder,
            ticker=market.ticker,
            action=f"skip_{decision.reason}",
            size=0,
            limit_price=limit_price,
            reason=decision.reason,
            mode=cfg.mode,
            now=now,
        )
        return RouteOutcome(
            action=f"skip_{decision.reason}",
            size=0,
            limit_price_cents=limit_price,
            reason=decision.reason,
            fill=None,
        )

    fill = await broker.taker_buy(
        candidate=candidate,
        market=market,
        size=decision.approved_size,
        tick_id=tick_id,
        now=now,
    )

    if fill is None:
        reason = "no_quote"
        await _record_decision(
            recorder,
            ticker=market.ticker,
            action=f"skip_{reason}",
            size=0,
            limit_price=limit_price,
            reason=reason,
            mode=cfg.mode,
            now=now,
        )
        return RouteOutcome(
            action=f"skip_{reason}",
            size=0,
            limit_price_cents=limit_price,
            reason=reason,
            fill=None,
        )

    apply_fill(
        state,
        event_ticker=event_ticker,
        ticker=market.ticker,
        size=fill.count,
    )
    await _record_decision(
        recorder,
        ticker=market.ticker,
        action=candidate.side,
        size=fill.count,
        limit_price=fill.yes_price_cents,
        reason=decision.reason,
        mode=cfg.mode,
        now=now,
    )
    return RouteOutcome(
        action=candidate.side,
        size=fill.count,
        limit_price_cents=fill.yes_price_cents,
        reason=decision.reason,
        fill=fill,
    )


__all__ = ["RouteOutcome", "route"]
