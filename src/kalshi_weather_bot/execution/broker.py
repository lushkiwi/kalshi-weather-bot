"""Shared broker types.

``BrokerFill`` is the dataclass emitted by every broker (paper + Kalshi)
after a successful ``taker_buy``. The ``Broker`` protocol is what the
router and scheduler depend on — either concrete broker plugs in.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from kalshi_weather_bot.edge.detector import Candidate
from kalshi_weather_bot.kalshi.models import Market
from kalshi_weather_bot.risk.limits import PortfolioState


@dataclass(slots=True)
class BrokerFill:
    client_order_id: str
    kalshi_order_id: str
    ticker: str
    side: str                 # 'yes' | 'no'
    count: int
    yes_price_cents: int      # price paid on the YES side; for side='no' this is (100 - yes_bid)
    fee_cents: int
    filled_ts: int


class Broker(Protocol):
    async def taker_buy(
        self,
        *,
        candidate: Candidate,
        market: Market,
        size: int,
        tick_id: str,
        now: datetime,
    ) -> BrokerFill | None: ...

    async def load_portfolio(self) -> PortfolioState: ...


def client_order_id(tick_id: str, ticker: str, side: str) -> str:
    return hashlib.sha256(f"{tick_id}|{ticker}|{side}".encode()).hexdigest()[:24]


def event_from_ticker(ticker: str) -> str:
    """Weather tickers are ``<event>-<suffix>``; drop the suffix."""
    return ticker.rsplit("-", 1)[0]


__all__ = ["Broker", "BrokerFill", "client_order_id", "event_from_ticker"]
