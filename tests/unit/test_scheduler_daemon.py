from __future__ import annotations

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
from kalshi_weather_bot.scheduler.daemon import MAX_CONSECUTIVE_FAILURES, _TickRunner


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
        trading=TradingConfig(edge_min=EdgeMin(), flat_size=10),
        risk=RiskConfig(
            max_contracts_per_market=100,
            max_notional_per_event=200,
            max_total_notional=1000,
            max_daily_loss_usd=100,
        ),
        alerts=AlertsConfig(),
        recorder=RecorderConfig(db_path=str(tmp_path / "rec.sqlite3")),
    )


class _FakeSummary:
    def __init__(self) -> None:
        self.tick_id = "abc"
        self.n_fills = 0
        self.n_flagged = 0
        self.killed = False


@pytest.mark.asyncio
async def test_tick_failure_arms_kill_after_threshold(tmp_path: Path, monkeypatch) -> None:
    from kalshi_weather_bot.scheduler import daemon as dm

    async def always_fail(*a, **kw):
        raise RuntimeError("boom")

    monkeypatch.setattr(dm, "run_tick", always_fail)
    lock = tmp_path / "kill.lock"
    runner = _TickRunner(_cfg(tmp_path), secrets=None, kill_lock=lock, notifier=None)  # type: ignore[arg-type]

    for _ in range(MAX_CONSECUTIVE_FAILURES - 1):
        await runner()
        assert not lock.exists()
    await runner()
    assert lock.exists()
    assert "consecutive_failures=3" in lock.read_text()


@pytest.mark.asyncio
async def test_tick_success_resets_counter(tmp_path: Path, monkeypatch) -> None:
    from kalshi_weather_bot.scheduler import daemon as dm

    calls = {"n": 0}

    async def flaky(*a, **kw):
        calls["n"] += 1
        if calls["n"] < 2:
            raise RuntimeError("boom")
        return _FakeSummary()

    monkeypatch.setattr(dm, "run_tick", flaky)
    lock = tmp_path / "kill.lock"
    runner = _TickRunner(_cfg(tmp_path), secrets=None, kill_lock=lock, notifier=None)  # type: ignore[arg-type]

    await runner()    # fail #1
    await runner()    # success -> counter resets
    # Now two more failures should not trigger the kill switch.
    async def fail(*a, **kw):
        raise RuntimeError("boom")

    monkeypatch.setattr(dm, "run_tick", fail)
    await runner()
    await runner()
    assert not lock.exists()


@pytest.mark.asyncio
async def test_tick_skipped_when_locked(tmp_path: Path, monkeypatch) -> None:
    from kalshi_weather_bot.scheduler import daemon as dm

    called = {"n": 0}

    async def never(*a, **kw):
        called["n"] += 1
        return _FakeSummary()

    monkeypatch.setattr(dm, "run_tick", never)
    lock = tmp_path / "kill.lock"
    lock.write_text("manual")
    runner = _TickRunner(_cfg(tmp_path), secrets=None, kill_lock=lock, notifier=None)  # type: ignore[arg-type]
    await runner()
    assert called["n"] == 0
