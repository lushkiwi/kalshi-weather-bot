"""Turn (fair probability, market quotes) into concrete trade candidates.

For each contract we consider both directions as a taker:
  buy YES at ``yes_ask``: edge = p_fair − (yes_ask/100) − fee
  buy NO  at ``100 − yes_bid``: edge = (1 − p_fair) − ((100 − yes_bid)/100) − fee

Fees use the worst-case C=1 rate from ``edge/fees.py``; per-order ceiling
only gets cheaper as size grows, so we never over-trade on a paper edge.

An edge threshold with linear time-decay filters these down to flagged
candidates: the threshold ramps from ``edge_min`` at ``h ≥ decay_hours`` to
``2·edge_min`` at the close (``h = 0``), per PLAN.md §4.
"""

from __future__ import annotations

from dataclasses import dataclass

from kalshi_weather_bot.edge.fees import taker_fee_rate
from kalshi_weather_bot.edge.implied import MarketImplied


def effective_edge_min(base: float, hours_to_close: float, decay_hours: float) -> float:
    """Linear ramp: ``base`` at h ≥ decay_hours, ``2·base`` at h = 0."""
    if decay_hours <= 0:
        return base
    ramp = max(0.0, min(1.0, 1.0 - hours_to_close / decay_hours))
    return base * (1.0 + ramp)


@dataclass(slots=True)
class Candidate:
    ticker: str
    side: str                 # 'buy_yes' | 'buy_no'
    p_fair: float
    p_market_cost: float      # what you pay to enter, as a probability
    gross_edge: float
    fee_rate: float
    net_edge: float
    effective_edge_min: float
    flagged: bool


def _candidate(
    ticker: str,
    side: str,
    p_win: float,
    cost: float,
    edge_min_eff: float,
) -> Candidate:
    fee = taker_fee_rate(cost)
    gross = p_win - cost
    net = gross - fee
    return Candidate(
        ticker=ticker,
        side=side,
        p_fair=p_win if side == "buy_yes" else 1.0 - p_win,
        p_market_cost=cost,
        gross_edge=gross,
        fee_rate=fee,
        net_edge=net,
        effective_edge_min=edge_min_eff,
        flagged=net > edge_min_eff,
    )


def evaluate(
    p_fair: float,
    implied: MarketImplied,
    *,
    edge_min: float,
    hours_to_close: float,
    decay_hours: float,
) -> list[Candidate]:
    """Return the two taker candidates (buy YES, buy NO) where the market side has a quote."""
    edge_min_eff = effective_edge_min(edge_min, hours_to_close, decay_hours)
    out: list[Candidate] = []

    cost_yes = implied.p_buy_yes()
    if cost_yes is not None:
        out.append(_candidate(implied.ticker, "buy_yes", p_fair, cost_yes, edge_min_eff))

    cost_no = implied.p_buy_no()
    if cost_no is not None:
        out.append(_candidate(implied.ticker, "buy_no", 1.0 - p_fair, cost_no, edge_min_eff))

    return out


def best_candidate(cands: list[Candidate]) -> Candidate | None:
    """Return the candidate with the highest net edge, or None if none flagged."""
    flagged = [c for c in cands if c.flagged]
    if not flagged:
        return None
    return max(flagged, key=lambda c: c.net_edge)


__all__ = ["Candidate", "best_candidate", "effective_edge_min", "evaluate"]
