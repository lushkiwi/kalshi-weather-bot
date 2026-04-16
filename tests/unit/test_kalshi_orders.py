from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from kalshi_weather_bot.edge.detector import Candidate
from kalshi_weather_bot.kalshi.models import Market
from kalshi_weather_bot.kalshi.orders import KalshiBroker
from kalshi_weather_bot.recorder.db import Recorder


NOW = datetime(2026, 4, 15, 12, 0, tzinfo=timezone.utc)


def _mkt(ticker: str = "KXHIGHNY-26APR15-T80", yes_bid: int | None = 40, yes_ask: int | None = 46) -> Market:
    return Market(
        ticker=ticker,
        event_ticker="KXHIGHNY-26APR15",
        series_ticker="KXHIGHNY",
        status="open",
        yes_bid=yes_bid,
        yes_ask=yes_ask,
        strike_type="greater",
        floor_strike=80.0,
    )


def _cand(side: str = "buy_yes") -> Candidate:
    return Candidate(
        ticker="KXHIGHNY-26APR15-T80",
        side=side,
        p_fair=0.55 if side == "buy_yes" else 0.45,
        p_market_cost=0.46 if side == "buy_yes" else 0.60,
        gross_edge=0.09,
        fee_rate=0.02,
        net_edge=0.07,
        effective_edge_min=0.04,
        flagged=True,
    )


class FakeKalshiClient:
    """Stand-in for KalshiClient. Records calls and returns scripted responses."""

    def __init__(
        self,
        *,
        order_id: str = "ord-1",
        fills: list[dict[str, Any]] | None = None,
        positions: list[dict[str, Any]] | None = None,
        post_raises: Exception | None = None,
    ) -> None:
        self.order_id = order_id
        self.fills = fills or []
        self.positions = positions or []
        self.posted: list[dict[str, Any]] = []
        self.canceled: list[str] = []
        self.fills_queries: list[dict[str, Any]] = []
        self._post_raises = post_raises

    async def post_order(self, payload: dict[str, Any]) -> dict[str, Any]:
        if self._post_raises is not None:
            raise self._post_raises
        self.posted.append(payload)
        return {"order": {"order_id": self.order_id, "status": "executed"}}

    async def get_fills(self, *, order_id: str | None = None, **kw: Any) -> dict[str, Any]:
        self.fills_queries.append({"order_id": order_id, **kw})
        return {"fills": self.fills}

    async def cancel_order(self, order_id: str) -> dict[str, Any]:
        self.canceled.append(order_id)
        return {}

    async def get_positions(self) -> dict[str, Any]:
        return {"market_positions": self.positions}


@pytest.mark.asyncio
async def test_kalshi_broker_taker_buy_yes_fully_filled(tmp_path: Path) -> None:
    client = FakeKalshiClient(
        fills=[{"count": 10, "yes_price": 46, "side": "yes"}],
    )
    async with Recorder(tmp_path / "rec.sqlite3") as rec:
        broker = KalshiBroker(client, rec, mode="demo")
        fill = await broker.taker_buy(
            candidate=_cand("buy_yes"), market=_mkt(), size=10, tick_id="t1", now=NOW
        )
        assert fill is not None
        assert fill.count == 10
        assert fill.yes_price_cents == 46
        assert fill.side == "yes"
        assert fill.kalshi_order_id == "ord-1"
        assert client.posted[0]["ticker"] == "KXHIGHNY-26APR15-T80"
        assert client.posted[0]["type"] == "limit"
        assert client.posted[0]["action"] == "buy"
        assert client.posted[0]["side"] == "yes"
        assert client.posted[0]["yes_price"] == 46
        assert client.posted[0]["count"] == 10
        assert client.canceled == []

        rows = await rec.fetchall("SELECT status, count FROM orders")
        fills = await rec.fetchall("SELECT ticker, count FROM fills")
        assert rows == [("filled", 10)]
        assert fills == [("KXHIGHNY-26APR15-T80", 10)]


@pytest.mark.asyncio
async def test_kalshi_broker_taker_buy_no_sends_opposite_yes_price(tmp_path: Path) -> None:
    client = FakeKalshiClient(
        fills=[{"count": 5, "no_price": 60, "side": "no"}],
    )
    async with Recorder(tmp_path / "rec.sqlite3") as rec:
        broker = KalshiBroker(client, rec, mode="demo")
        fill = await broker.taker_buy(
            candidate=_cand("buy_no"), market=_mkt(), size=5, tick_id="t1", now=NOW
        )
        assert fill is not None
        assert fill.side == "no"
        assert fill.count == 5
        assert fill.yes_price_cents == 60
        # Buying NO at 60 = yes_price 40 on the wire (100 - 60).
        assert client.posted[0]["side"] == "no"
        assert client.posted[0]["yes_price"] == 40


@pytest.mark.asyncio
async def test_kalshi_broker_partial_fill_cancels_remainder(tmp_path: Path) -> None:
    client = FakeKalshiClient(fills=[{"count": 3, "yes_price": 46, "side": "yes"}])
    async with Recorder(tmp_path / "rec.sqlite3") as rec:
        broker = KalshiBroker(client, rec, mode="demo")
        fill = await broker.taker_buy(
            candidate=_cand("buy_yes"), market=_mkt(), size=10, tick_id="t1", now=NOW
        )
        assert fill is not None
        assert fill.count == 3
        assert client.canceled == ["ord-1"]
        statuses = await rec.fetchall("SELECT status FROM orders")
        assert statuses == [("partial",)]


@pytest.mark.asyncio
async def test_kalshi_broker_no_fill_cancels_and_returns_none(tmp_path: Path) -> None:
    client = FakeKalshiClient(fills=[])
    async with Recorder(tmp_path / "rec.sqlite3") as rec:
        broker = KalshiBroker(client, rec, mode="demo")
        fill = await broker.taker_buy(
            candidate=_cand("buy_yes"), market=_mkt(), size=10, tick_id="t1", now=NOW
        )
        assert fill is None
        assert client.canceled == ["ord-1"]
        statuses = await rec.fetchall("SELECT status FROM orders")
        assert statuses == [("canceled",)]


@pytest.mark.asyncio
async def test_kalshi_broker_skips_when_quote_missing(tmp_path: Path) -> None:
    client = FakeKalshiClient()
    async with Recorder(tmp_path / "rec.sqlite3") as rec:
        broker = KalshiBroker(client, rec, mode="demo")
        fill = await broker.taker_buy(
            candidate=_cand("buy_yes"),
            market=_mkt(yes_ask=None),
            size=10,
            tick_id="t1",
            now=NOW,
        )
        assert fill is None
        assert client.posted == []


@pytest.mark.asyncio
async def test_kalshi_broker_records_rejected_on_post_failure(tmp_path: Path) -> None:
    client = FakeKalshiClient(post_raises=RuntimeError("boom"))
    async with Recorder(tmp_path / "rec.sqlite3") as rec:
        broker = KalshiBroker(client, rec, mode="demo")
        fill = await broker.taker_buy(
            candidate=_cand("buy_yes"), market=_mkt(), size=10, tick_id="t1", now=NOW
        )
        assert fill is None
        statuses = await rec.fetchall("SELECT status FROM orders")
        assert statuses == [("rejected",)]


@pytest.mark.asyncio
async def test_kalshi_broker_load_portfolio_from_positions(tmp_path: Path) -> None:
    client = FakeKalshiClient(
        positions=[
            {"ticker": "KXHIGHNY-26APR15-T80", "position": 10},
            {"ticker": "KXHIGHNY-26APR15-T85", "position": -4},  # short NO
            {"ticker": "KXHIGHCHI-26APR15-T70", "position": 0},  # ignored
        ],
    )
    async with Recorder(tmp_path / "rec.sqlite3") as rec:
        broker = KalshiBroker(client, rec, mode="demo")
        state = await broker.load_portfolio()
        assert state.contracts_per_ticker == {
            "KXHIGHNY-26APR15-T80": 10,
            "KXHIGHNY-26APR15-T85": 4,
        }
        assert state.contracts_per_event == {"KXHIGHNY-26APR15": 14}
        assert state.total_contracts == 14
