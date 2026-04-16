from __future__ import annotations

from kalshi_weather_bot.config import RiskConfig
from kalshi_weather_bot.risk.limits import (
    PortfolioState,
    apply_fill,
    approve_order,
)


def _cfg(**overrides: int) -> RiskConfig:
    base = dict(
        max_contracts_per_market=100,
        max_notional_per_event=200,
        max_total_notional=1000,
        max_daily_loss_usd=100,
        kalshi_stale_seconds=60,
        openmeteo_stale_seconds=1200,
        nws_stale_seconds=3600,
    )
    base.update(overrides)
    return RiskConfig(**base)


def test_approve_empty_state_approves_in_full():
    d = approve_order(
        event_ticker="KXHIGHNY-26APR15",
        ticker="KXHIGHNY-26APR15-T80",
        requested_size=10,
        state=PortfolioState(),
        cfg=_cfg(),
    )
    assert d.approved_size == 10
    assert d.reason == "approved"


def test_non_positive_size_rejected():
    d = approve_order(
        event_ticker="E", ticker="T", requested_size=0,
        state=PortfolioState(), cfg=_cfg(),
    )
    assert d.approved_size == 0
    assert d.reason == "non_positive_size"


def test_per_market_clamp():
    state = PortfolioState(contracts_per_ticker={"T": 95})
    d = approve_order(
        event_ticker="E", ticker="T", requested_size=10,
        state=state, cfg=_cfg(),
    )
    assert d.approved_size == 5
    assert d.reason == "clamped"


def test_per_market_full_rejects():
    state = PortfolioState(contracts_per_ticker={"T": 100})
    d = approve_order(
        event_ticker="E", ticker="T", requested_size=10,
        state=state, cfg=_cfg(),
    )
    assert d.approved_size == 0
    assert d.reason == "per_market_cap_reached"


def test_per_event_clamp():
    state = PortfolioState(contracts_per_event={"E": 195})
    d = approve_order(
        event_ticker="E", ticker="T", requested_size=20,
        state=state, cfg=_cfg(),
    )
    assert d.approved_size == 5


def test_per_event_full_rejects():
    state = PortfolioState(contracts_per_event={"E": 200})
    d = approve_order(
        event_ticker="E", ticker="T", requested_size=20,
        state=state, cfg=_cfg(),
    )
    assert d.approved_size == 0
    assert d.reason == "per_event_cap_reached"


def test_total_notional_clamp():
    state = PortfolioState(total_contracts=998)
    d = approve_order(
        event_ticker="E", ticker="T", requested_size=10,
        state=state, cfg=_cfg(),
    )
    assert d.approved_size == 2


def test_daily_loss_blocks_at_limit():
    # -$100 = -10000 cents. At the limit, trading halts.
    state = PortfolioState(daily_realized_pnl_cents=-10000)
    d = approve_order(
        event_ticker="E", ticker="T", requested_size=10,
        state=state, cfg=_cfg(),
    )
    assert d.approved_size == 0
    assert d.reason == "daily_loss_breached"


def test_daily_loss_below_limit_allows_trade():
    state = PortfolioState(daily_realized_pnl_cents=-9999)
    d = approve_order(
        event_ticker="E", ticker="T", requested_size=10,
        state=state, cfg=_cfg(),
    )
    assert d.approved_size == 10


def test_apply_fill_updates_all_counters():
    state = PortfolioState()
    apply_fill(state, event_ticker="E", ticker="T", size=7)
    apply_fill(state, event_ticker="E", ticker="T", size=3)
    apply_fill(state, event_ticker="E", ticker="U", size=5)
    assert state.contracts_per_ticker == {"T": 10, "U": 5}
    assert state.contracts_per_event == {"E": 15}
    assert state.total_contracts == 15
