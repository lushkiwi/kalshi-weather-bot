from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from kalshi_weather_bot.config import (
    AlertsConfig,
    AppConfig,
    EdgeMin,
    KalshiConfig,
    NwsConfig,
    OpenMeteoConfig,
    RecorderConfig,
    RiskConfig,
    TradingConfig,
    WeatherConfig,
)
from kalshi_weather_bot.edge.detector import Candidate
from kalshi_weather_bot.execution.paper import PaperBroker
from kalshi_weather_bot.execution.router import route
from kalshi_weather_bot.execution.sizing import flat_size
from kalshi_weather_bot.kalshi.models import Market
from kalshi_weather_bot.recorder.db import Recorder
from kalshi_weather_bot.risk.limits import PortfolioState


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


def _cfg(flat: int = 10) -> AppConfig:
    return AppConfig(
        mode="paper",
        kalshi=KalshiConfig(env="demo"),
        weather=WeatherConfig(
            openmeteo=OpenMeteoConfig(),
            nws=NwsConfig(user_agent="test"),
        ),
        series=["KXHIGHNY"],
        cities={},
        trading=TradingConfig(edge_min=EdgeMin(), flat_size=flat),
        risk=RiskConfig(
            max_contracts_per_market=100,
            max_notional_per_event=200,
            max_total_notional=1000,
            max_daily_loss_usd=100,
        ),
        alerts=AlertsConfig(),
        recorder=RecorderConfig(),
    )


def test_flat_size_clamps_to_max_allowed():
    assert flat_size(TradingConfig(flat_size=10), max_allowed=3) == 3
    assert flat_size(TradingConfig(flat_size=10), max_allowed=100) == 10
    assert flat_size(TradingConfig(flat_size=10), max_allowed=0) == 0
    assert flat_size(TradingConfig(flat_size=10), max_allowed=-5) == 0


@pytest.mark.asyncio
async def test_paper_taker_buy_yes_records_order_and_fill(tmp_path: Path) -> None:
    async with Recorder(tmp_path / "rec.sqlite3") as rec:
        broker = PaperBroker(rec)
        fill = await broker.taker_buy(
            candidate=_cand("buy_yes"),
            market=_mkt(),
            size=10,
            tick_id="tick1",
            now=NOW,
        )
        assert fill is not None
        assert fill.side == "yes"
        assert fill.count == 10
        assert fill.yes_price_cents == 46
        assert fill.fee_cents > 0

        orders = await rec.fetchall("SELECT ticker, count, status FROM orders")
        fills = await rec.fetchall("SELECT ticker, count FROM fills")
        assert orders == [("KXHIGHNY-26APR15-T80", 10, "filled")]
        assert fills == [("KXHIGHNY-26APR15-T80", 10)]


@pytest.mark.asyncio
async def test_paper_taker_buy_no_uses_100_minus_bid(tmp_path: Path) -> None:
    async with Recorder(tmp_path / "rec.sqlite3") as rec:
        broker = PaperBroker(rec)
        fill = await broker.taker_buy(
            candidate=_cand("buy_no"),
            market=_mkt(yes_bid=40, yes_ask=46),
            size=5,
            tick_id="tick1",
            now=NOW,
        )
        assert fill is not None
        assert fill.side == "no"
        assert fill.yes_price_cents == 60


@pytest.mark.asyncio
async def test_paper_taker_buy_yes_skipped_when_no_ask(tmp_path: Path) -> None:
    async with Recorder(tmp_path / "rec.sqlite3") as rec:
        broker = PaperBroker(rec)
        fill = await broker.taker_buy(
            candidate=_cand("buy_yes"),
            market=_mkt(yes_ask=None),
            size=5,
            tick_id="tick1",
            now=NOW,
        )
        assert fill is None


@pytest.mark.asyncio
async def test_load_portfolio_sums_fills(tmp_path: Path) -> None:
    async with Recorder(tmp_path / "rec.sqlite3") as rec:
        broker = PaperBroker(rec)
        await broker.taker_buy(
            candidate=_cand("buy_yes"),
            market=_mkt(ticker="KXHIGHNY-26APR15-T80"),
            size=10,
            tick_id="tick1",
            now=NOW,
        )
        await broker.taker_buy(
            candidate=_cand("buy_yes"),
            market=_mkt(ticker="KXHIGHNY-26APR15-T85"),
            size=7,
            tick_id="tick2",
            now=NOW,
        )
        state = await broker.load_portfolio()
        assert state.contracts_per_ticker == {
            "KXHIGHNY-26APR15-T80": 10,
            "KXHIGHNY-26APR15-T85": 7,
        }
        assert state.contracts_per_event == {"KXHIGHNY-26APR15": 17}
        assert state.total_contracts == 17


@pytest.mark.asyncio
async def test_router_happy_path_records_decision_and_fill(tmp_path: Path) -> None:
    async with Recorder(tmp_path / "rec.sqlite3") as rec:
        broker = PaperBroker(rec)
        state = PortfolioState()
        outcome = await route(
            candidate=_cand("buy_yes"),
            market=_mkt(),
            event_ticker="KXHIGHNY-26APR15",
            cfg=_cfg(),
            broker=broker,
            recorder=rec,
            state=state,
            tick_id="tick1",
            now=NOW,
        )
        assert outcome.action == "buy_yes"
        assert outcome.size == 10
        assert outcome.fill is not None
        assert state.contracts_per_ticker["KXHIGHNY-26APR15-T80"] == 10

        decisions = await rec.fetchall("SELECT action, size FROM decisions")
        assert decisions == [("buy_yes", 10)]


@pytest.mark.asyncio
async def test_router_skips_when_risk_rejects(tmp_path: Path) -> None:
    async with Recorder(tmp_path / "rec.sqlite3") as rec:
        broker = PaperBroker(rec)
        state = PortfolioState(
            contracts_per_ticker={"KXHIGHNY-26APR15-T80": 100}
        )
        outcome = await route(
            candidate=_cand("buy_yes"),
            market=_mkt(),
            event_ticker="KXHIGHNY-26APR15",
            cfg=_cfg(),
            broker=broker,
            recorder=rec,
            state=state,
            tick_id="tick1",
            now=NOW,
        )
        assert outcome.action == "skip_per_market_cap_reached"
        assert outcome.fill is None
        orders = await rec.fetchall("SELECT count(*) FROM orders")
        assert orders == [(0,)]


@pytest.mark.asyncio
async def test_router_skips_when_no_quote(tmp_path: Path) -> None:
    async with Recorder(tmp_path / "rec.sqlite3") as rec:
        broker = PaperBroker(rec)
        state = PortfolioState()
        outcome = await route(
            candidate=_cand("buy_yes"),
            market=_mkt(yes_ask=None, yes_bid=None),
            event_ticker="KXHIGHNY-26APR15",
            cfg=_cfg(),
            broker=broker,
            recorder=rec,
            state=state,
            tick_id="tick1",
            now=NOW,
        )
        assert outcome.action == "skip_no_quote"
        assert state.total_contracts == 0


@pytest.mark.asyncio
async def test_router_clamps_size_to_remaining_room(tmp_path: Path) -> None:
    async with Recorder(tmp_path / "rec.sqlite3") as rec:
        broker = PaperBroker(rec)
        state = PortfolioState(
            contracts_per_ticker={"KXHIGHNY-26APR15-T80": 95}
        )
        outcome = await route(
            candidate=_cand("buy_yes"),
            market=_mkt(),
            event_ticker="KXHIGHNY-26APR15",
            cfg=_cfg(flat=10),
            broker=broker,
            recorder=rec,
            state=state,
            tick_id="tick1",
            now=NOW,
        )
        assert outcome.action == "buy_yes"
        assert outcome.size == 5
        assert state.contracts_per_ticker["KXHIGHNY-26APR15-T80"] == 100
