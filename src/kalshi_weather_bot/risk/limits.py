"""Pre-trade risk gates.

The router calls ``approve_order`` for every candidate trade and honours the
returned ``approved_size`` (which may be clamped below the requested size).
Units are contracts throughout — a Kalshi binary contract pays at most $1,
so "100 contracts in a market" and "$100 notional in that market" are the
same thing for our purposes.

Daily loss is tracked in cents. The gate fires when the absolute realised
loss reaches ``max_daily_loss_usd``; unrealised P&L is out of scope until
we have a mark-to-market feed (M5+).
"""

from __future__ import annotations

from dataclasses import dataclass, field

from kalshi_weather_bot.config import RiskConfig


@dataclass(slots=True)
class PortfolioState:
    """Snapshot of current positions + daily realised P&L, used for gating."""

    contracts_per_ticker: dict[str, int] = field(default_factory=dict)
    contracts_per_event: dict[str, int] = field(default_factory=dict)
    total_contracts: int = 0
    daily_realized_pnl_cents: int = 0      # negative = loss


@dataclass(slots=True)
class LimitDecision:
    approved_size: int
    reason: str


def approve_order(
    *,
    event_ticker: str,
    ticker: str,
    requested_size: int,
    state: PortfolioState,
    cfg: RiskConfig,
) -> LimitDecision:
    if requested_size <= 0:
        return LimitDecision(0, "non_positive_size")

    max_loss_cents = cfg.max_daily_loss_usd * 100
    if -state.daily_realized_pnl_cents >= max_loss_cents:
        return LimitDecision(0, "daily_loss_breached")

    cur_mkt = state.contracts_per_ticker.get(ticker, 0)
    if cur_mkt >= requested_size:
        return LimitDecision(0, "position_filled")
    room_mkt = cfg.max_contracts_per_market - cur_mkt
    if room_mkt <= 0:
        return LimitDecision(0, "per_market_cap_reached")

    cur_evt = state.contracts_per_event.get(event_ticker, 0)
    room_evt = cfg.max_notional_per_event - cur_evt
    if room_evt <= 0:
        return LimitDecision(0, "per_event_cap_reached")

    room_total = cfg.max_total_notional - state.total_contracts
    if room_total <= 0:
        return LimitDecision(0, "total_notional_reached")

    approved = min(requested_size, room_mkt, room_evt, room_total)
    reason = "approved" if approved == requested_size else "clamped"
    return LimitDecision(approved, reason)


def apply_fill(
    state: PortfolioState, *, event_ticker: str, ticker: str, size: int
) -> None:
    """Mutate ``state`` to reflect a new long position of ``size`` contracts."""
    state.contracts_per_ticker[ticker] = state.contracts_per_ticker.get(ticker, 0) + size
    state.contracts_per_event[event_ticker] = (
        state.contracts_per_event.get(event_ticker, 0) + size
    )
    state.total_contracts += size


__all__ = ["LimitDecision", "PortfolioState", "apply_fill", "approve_order"]
