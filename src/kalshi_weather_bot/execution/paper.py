"""Simulated taker broker for paper trading.

Mirrors the subset of the live Kalshi order client the router needs, but
fills instantly at the quoted ask (for buy YES) or ``100 − bid`` (for buy
NO). Sitting in queue as a maker is not simulated: we have no realistic
way to model queue position against a live book, and PLAN.md §5 already
defaults ``aggressive=false`` to "cancel and re-evaluate next tick" for
unfilled makers — so taker-only is behaviourally close.

Every order and fill is written through the recorder so the DB remains the
single source of truth for paper P&L.
"""

from __future__ import annotations

import hashlib
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime

from kalshi_weather_bot.edge.detector import Candidate
from kalshi_weather_bot.edge.fees import taker_fee_cents
from kalshi_weather_bot.kalshi.models import Market
from kalshi_weather_bot.recorder.db import Recorder
from kalshi_weather_bot.risk.limits import PortfolioState


def _client_order_id(tick_id: str, ticker: str, side: str) -> str:
    return hashlib.sha256(f"{tick_id}|{ticker}|{side}".encode()).hexdigest()[:24]


def _event_from_ticker(ticker: str) -> str:
    """Weather tickers are ``<event>-<suffix>``; drop the suffix."""
    return ticker.rsplit("-", 1)[0]


@dataclass(slots=True)
class PaperFill:
    client_order_id: str
    kalshi_order_id: str
    ticker: str
    side: str                 # 'yes' | 'no'
    count: int
    yes_price_cents: int      # price paid on the YES side; for side='no' this is (100 - yes_bid)
    fee_cents: int
    filled_ts: int


class PaperBroker:
    """Immediate-fill taker broker backed by the recorder DB."""

    def __init__(self, recorder: Recorder, mode: str = "paper") -> None:
        self._rec = recorder
        self._mode = mode

    async def taker_buy(
        self,
        *,
        candidate: Candidate,
        market: Market,
        size: int,
        tick_id: str,
        now: datetime,
    ) -> PaperFill | None:
        """Instantly fill a buy order at the opposite side of the book.

        Returns ``None`` if the required quote is missing — the caller
        should record a skip rather than treat this as a failure.
        """
        if size <= 0:
            return None
        if candidate.side == "buy_yes":
            if market.yes_ask is None:
                return None
            fill_price, side = market.yes_ask, "yes"
        elif candidate.side == "buy_no":
            if market.yes_bid is None:
                return None
            fill_price, side = 100 - market.yes_bid, "no"
        else:
            return None

        coid = _client_order_id(tick_id, market.ticker, candidate.side)
        kalshi_id = f"paper-{coid}"
        fee = taker_fee_cents(size, fill_price / 100.0)
        ts = int(now.timestamp())

        await self._rec.execute(
            "INSERT OR REPLACE INTO orders (client_order_id, kalshi_order_id, ticker, "
            "side, action, count, yes_price, status, created_ts, resolved_ts, mode) "
            "VALUES (?, ?, ?, ?, 'buy', ?, ?, 'filled', ?, ?, ?)",
            (coid, kalshi_id, market.ticker, side, size, fill_price, ts, ts, self._mode),
        )
        await self._rec.execute(
            "INSERT INTO fills (kalshi_order_id, ticker, count, yes_price, fee_cents, "
            "filled_ts, mode) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (kalshi_id, market.ticker, size, fill_price, fee, ts, self._mode),
        )
        await self._rec.commit()

        return PaperFill(
            client_order_id=coid,
            kalshi_order_id=kalshi_id,
            ticker=market.ticker,
            side=side,
            count=size,
            yes_price_cents=fill_price,
            fee_cents=fee,
            filled_ts=ts,
        )

    async def load_portfolio(self) -> PortfolioState:
        """Reconstruct positions from persisted fills."""
        rows = await self._rec.fetchall(
            "SELECT ticker, count FROM fills WHERE mode = ?", (self._mode,)
        )
        state = PortfolioState()
        events: dict[str, int] = defaultdict(int)
        for ticker, count in rows:
            state.contracts_per_ticker[ticker] = (
                state.contracts_per_ticker.get(ticker, 0) + int(count)
            )
            events[_event_from_ticker(ticker)] += int(count)
            state.total_contracts += int(count)
        state.contracts_per_event = dict(events)
        return state


__all__ = ["PaperBroker", "PaperFill"]
