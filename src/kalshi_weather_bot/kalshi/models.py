from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


StrikeType = Literal["greater", "less", "between", "structured"]
MarketStatus = Literal["open", "active", "closed", "settled", "finalized", "unopened"]


class Market(BaseModel):
    """A single Kalshi binary contract (one rung of a temperature ladder)."""

    model_config = ConfigDict(extra="ignore")

    ticker: str
    event_ticker: str
    series_ticker: str | None = None
    status: MarketStatus
    yes_bid: int | None = None                 # cents
    yes_ask: int | None = None
    no_bid: int | None = None
    no_ask: int | None = None
    last_price: int | None = None
    volume: int = 0
    open_interest: int = 0
    floor_strike: float | None = None
    cap_strike: float | None = None
    strike_type: StrikeType | None = None
    expiration_time: datetime | None = None
    close_time: datetime | None = None
    result: str | None = None                 # 'yes' | 'no' (populated after settlement)
    expiration_value: str | None = None        # observed value e.g. "87.00"
    rules_primary: str | None = None
    settlement_source: str | None = None
    title: str | None = None
    subtitle: str | None = None


class Event(BaseModel):
    model_config = ConfigDict(extra="ignore")

    event_ticker: str
    series_ticker: str
    title: str | None = None
    sub_title: str | None = None
    markets: list[Market] = Field(default_factory=list)


class OrderbookLevel(BaseModel):
    price: int
    size: int


class Orderbook(BaseModel):
    model_config = ConfigDict(extra="ignore")

    ticker: str
    yes: list[OrderbookLevel] = Field(default_factory=list)
    no: list[OrderbookLevel] = Field(default_factory=list)

    def best_yes_bid(self) -> int | None:
        return max((level.price for level in self.yes), default=None)

    def best_no_bid(self) -> int | None:
        return max((level.price for level in self.no), default=None)

    def best_yes_ask(self) -> int | None:
        """Best ask for YES derived from NO side: if someone will buy NO for 60, you can sell NO (= buy YES) at 40."""
        best_no = self.best_no_bid()
        return None if best_no is None else 100 - best_no


class Position(BaseModel):
    model_config = ConfigDict(extra="ignore")

    ticker: str
    position: int = 0                          # signed contracts
    market_exposure: int = 0                   # cents
    realized_pnl: int = 0
    total_traded: int = 0


class Fill(BaseModel):
    model_config = ConfigDict(extra="ignore")

    trade_id: str
    ticker: str
    order_id: str
    side: Literal["yes", "no"]
    action: Literal["buy", "sell"]
    count: int
    yes_price: int
    no_price: int | None = None
    created_time: datetime


class Order(BaseModel):
    model_config = ConfigDict(extra="ignore")

    order_id: str
    client_order_id: str | None = None
    ticker: str
    side: Literal["yes", "no"]
    action: Literal["buy", "sell"]
    count: int
    yes_price: int | None = None
    no_price: int | None = None
    status: str
    created_time: datetime | None = None
