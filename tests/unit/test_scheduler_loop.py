from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
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
from kalshi_weather_bot.kalshi.models import Market
from kalshi_weather_bot.risk.killswitch import activate
from kalshi_weather_bot.scheduler.loop import run_tick
from kalshi_weather_bot.weather.models import EnsembleForecast, ForecastSample


NOW = datetime(2026, 4, 15, 12, 0, tzinfo=timezone.utc)
TARGET_DATE = date(2026, 4, 17)


def _mkt(ticker: str, bid: int | None, ask: int | None, strike: float) -> Market:
    return Market(
        ticker=ticker,
        event_ticker="KXHIGHNY-26APR17",
        series_ticker="KXHIGHNY",
        status="open",
        yes_bid=bid,
        yes_ask=ask,
        strike_type="greater",
        floor_strike=strike,
        close_time=NOW + timedelta(hours=24),
        expiration_time=NOW + timedelta(hours=24),
    )


def _forecast(values: list[float]) -> dict:
    samples = [
        ForecastSample(
            city="NY",
            target_date=TARGET_DATE,
            source="gfs025",
            member=i,
            variable="tmax_f",
            value=v,
            fetched_at=NOW,
            run_time=NOW,
        )
        for i, v in enumerate(values)
    ]
    ef = EnsembleForecast(
        city="NY", target_date=TARGET_DATE, variable="tmax_f", samples=samples
    )
    return {("NY", TARGET_DATE): ef}


def _cfg(tmp_path: Path) -> AppConfig:
    return AppConfig(
        mode="paper",
        kalshi=KalshiConfig(env="demo"),
        weather=WeatherConfig(
            openmeteo=OpenMeteoConfig(),
            nws=NwsConfig(user_agent="test"),
        ),
        series=["KXHIGHNY"],
        cities={},
        trading=TradingConfig(edge_min=EdgeMin(), flat_size=10, close_decay_hours=6.0),
        risk=RiskConfig(
            max_contracts_per_market=100,
            max_notional_per_event=200,
            max_total_notional=1000,
            max_daily_loss_usd=100,
        ),
        alerts=AlertsConfig(),
        recorder=RecorderConfig(db_path=str(tmp_path / "rec.sqlite3")),
    )


@pytest.mark.asyncio
async def test_run_tick_skipped_when_killed(tmp_path: Path, monkeypatch) -> None:
    lock = tmp_path / "kill.lock"
    activate(lock, "test")

    # fetch_inputs should never be called; raise if it is.
    from kalshi_weather_bot.scheduler import loop as loop_mod

    async def boom(*a, **kw):
        raise AssertionError("fetch_inputs should not be called when killed")

    monkeypatch.setattr(loop_mod, "fetch_inputs", boom)

    summary = await run_tick(_cfg(tmp_path), secrets=None, kill_lock=lock)  # type: ignore[arg-type]
    assert summary.killed
    assert summary.kill_reason is not None
    assert summary.n_markets == 0


@pytest.mark.asyncio
async def test_run_tick_end_to_end_places_paper_order(tmp_path: Path, monkeypatch) -> None:
    from kalshi_weather_bot.scheduler import loop as loop_mod

    markets = [_mkt("KXHIGHNY-26APR17-T80", bid=40, ask=46, strike=80.0)]
    by_event_date = _forecast([85.0, 86.0, 87.0, 88.0, 89.0] * 20)

    async def fake_fetch(*a, **kw):
        return markets, by_event_date, {}

    monkeypatch.setattr(loop_mod, "fetch_inputs", fake_fetch)

    lock = tmp_path / "kill.lock"
    summary = await run_tick(_cfg(tmp_path), secrets=None, kill_lock=lock)  # type: ignore[arg-type]

    assert summary.n_markets == 1
    assert summary.n_flagged >= 1
    assert summary.n_fills == 1
    fill = summary.outcomes[0].fill
    assert fill is not None
    assert fill.side == "yes"
    assert fill.count == 10


@pytest.mark.asyncio
async def test_run_tick_halts_on_position_drift(tmp_path: Path, monkeypatch) -> None:
    """Demo/live tick arms the kill switch and exits when DB vs Kalshi diverge."""
    from contextlib import asynccontextmanager

    from kalshi_weather_bot.execution.paper import PaperBroker
    from kalshi_weather_bot.scheduler import loop as loop_mod

    markets = [_mkt("KXHIGHNY-26APR17-T80", bid=40, ask=46, strike=80.0)]
    by_event_date = _forecast([85.0, 86.0, 87.0, 88.0, 89.0] * 20)

    async def fake_fetch(*a, **kw):
        return markets, by_event_date, {}

    monkeypatch.setattr(loop_mod, "fetch_inputs", fake_fetch)

    class FakeClient:
        async def get_positions(self):
            return {"market_positions": [{"ticker": "KXHIGHNY-26APR17-T80", "position": 5}]}

    @asynccontextmanager
    async def fake_broker(cfg, secrets, rec):
        yield PaperBroker(rec, mode="demo"), FakeClient()

    monkeypatch.setattr(loop_mod, "_broker_for_mode", fake_broker)

    cfg = _cfg(tmp_path)
    cfg = cfg.model_copy(update={"mode": "demo"})
    lock = tmp_path / "kill.lock"
    summary = await run_tick(cfg, secrets=None, kill_lock=lock)  # type: ignore[arg-type]

    assert summary.reconciliation_drift == {"KXHIGHNY-26APR17-T80": (0, 5)}
    assert summary.n_fills == 0
    assert lock.exists()                                      # kill switch armed


@pytest.mark.asyncio
async def test_run_tick_no_quotes_records_skip(tmp_path: Path, monkeypatch) -> None:
    from kalshi_weather_bot.scheduler import loop as loop_mod

    # No bid/ask → nothing flagged → no orders.
    markets = [_mkt("KXHIGHNY-26APR17-T80", bid=None, ask=None, strike=80.0)]
    by_event_date = _forecast([85.0] * 100)

    async def fake_fetch(*a, **kw):
        return markets, by_event_date, {}

    monkeypatch.setattr(loop_mod, "fetch_inputs", fake_fetch)

    summary = await run_tick(_cfg(tmp_path), secrets=None, kill_lock=tmp_path / "kill.lock")  # type: ignore[arg-type]
    assert summary.n_flagged == 0
    assert summary.n_fills == 0
