from __future__ import annotations

from kalshi_weather_bot.config import RiskConfig
from kalshi_weather_bot.risk.health import check_health


def _cfg() -> RiskConfig:
    return RiskConfig(
        max_contracts_per_market=100,
        max_notional_per_event=200,
        max_total_notional=1000,
        max_daily_loss_usd=100,
        kalshi_stale_seconds=60,
        openmeteo_stale_seconds=1200,
        nws_stale_seconds=3600,
    )


def test_healthy_when_all_fresh():
    now = 1_000_000.0
    h = check_health(
        kalshi_last_fetch_ts=now - 10,
        openmeteo_last_fetch_ts=now - 60,
        nws_last_fetch_ts=now - 500,
        now_ts=now,
        cfg=_cfg(),
    )
    assert h.healthy
    assert h.reasons == []
    assert not h.nws_stale


def test_kalshi_stale_unhealthy():
    now = 1_000_000.0
    h = check_health(
        kalshi_last_fetch_ts=now - 120,
        openmeteo_last_fetch_ts=now - 10,
        nws_last_fetch_ts=now - 10,
        now_ts=now,
        cfg=_cfg(),
    )
    assert not h.healthy
    assert h.reasons == ["kalshi_stale"]


def test_nws_stale_does_not_fail_health():
    now = 1_000_000.0
    h = check_health(
        kalshi_last_fetch_ts=now - 10,
        openmeteo_last_fetch_ts=now - 10,
        nws_last_fetch_ts=now - 5000,
        now_ts=now,
        cfg=_cfg(),
    )
    assert h.healthy
    assert h.nws_stale


def test_missing_timestamps_count_as_stale():
    now = 1_000_000.0
    h = check_health(
        kalshi_last_fetch_ts=None,
        openmeteo_last_fetch_ts=None,
        nws_last_fetch_ts=None,
        now_ts=now,
        cfg=_cfg(),
    )
    assert not h.healthy
    assert set(h.reasons) == {"kalshi_stale", "openmeteo_stale"}
    assert h.nws_stale
