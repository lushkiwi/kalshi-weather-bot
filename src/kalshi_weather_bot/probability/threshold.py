"""Map a Kalshi contract onto an ensemble-derived fair probability.

Handles the three strike_type values Kalshi returns for weather markets:
  - "greater": P(X > floor_strike)
  - "less":    P(X < floor_strike)
  - "between": P(floor_strike <= X <= cap_strike)

Strike values are integer Fahrenheit, but Kalshi uses half-step strikes like
T89.5 to disambiguate boundary cases ("high > 89.5" = "high >= 90" since
observed highs are integer). We pass the strike through unchanged.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from kalshi_weather_bot.kalshi.models import Market
from kalshi_weather_bot.probability.distributions import (
    prob_between,
    prob_greater,
    prob_less,
)


@dataclass(slots=True)
class FairProbability:
    ticker: str
    p_fair: float
    method: str           # 'greater' | 'less' | 'between'
    sigma_inflation: float
    n_samples: int


class ThresholdError(ValueError):
    """Raised when a Market lacks the strike metadata needed to price it."""


def contract_probability(
    market: Market,
    samples: Sequence[float],
    *,
    sigma_inflation: float = 1.0,
) -> FairProbability:
    n = len(samples)
    st = market.strike_type
    if st == "greater":
        if market.floor_strike is None:
            raise ThresholdError(f"{market.ticker} has strike_type=greater but no floor_strike")
        p = prob_greater(samples, market.floor_strike, sigma_inflation=sigma_inflation)
        return FairProbability(market.ticker, p, "greater", sigma_inflation, n)

    if st == "less":
        if market.floor_strike is None:
            raise ThresholdError(f"{market.ticker} has strike_type=less but no floor_strike")
        p = prob_less(samples, market.floor_strike, sigma_inflation=sigma_inflation)
        return FairProbability(market.ticker, p, "less", sigma_inflation, n)

    if st == "between":
        if market.floor_strike is None or market.cap_strike is None:
            raise ThresholdError(
                f"{market.ticker} has strike_type=between but missing floor/cap strike"
            )
        p = prob_between(
            samples, market.floor_strike, market.cap_strike, sigma_inflation=sigma_inflation
        )
        return FairProbability(market.ticker, p, "between", sigma_inflation, n)

    raise ThresholdError(f"{market.ticker}: unsupported strike_type={st!r}")


def event_coherence_error(probs: Sequence[FairProbability]) -> float:
    """Signed deviation from 1.0 of the sum of p_fair over an event.

    For a fully-tiled event ladder this should be ≈ 0. The caller decides
    whether to warn and skip the event (PLAN.md §3 uses |err| < 0.02).
    """
    return sum(p.p_fair for p in probs) - 1.0


__all__ = ["FairProbability", "ThresholdError", "contract_probability", "event_coherence_error"]
