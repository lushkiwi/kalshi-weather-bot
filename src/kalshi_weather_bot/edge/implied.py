"""Convert Kalshi market quotes into market-implied probabilities.

Prices are stored in cents (0-99). ``yes_bid``/``yes_ask`` are the top-of-book
on the YES side; the symmetric NO-side prices are implied. See PLAN.md §4 for
how these feed the edge calculation.
"""

from __future__ import annotations

from dataclasses import dataclass

from kalshi_weather_bot.kalshi.models import Market


@dataclass(slots=True)
class MarketImplied:
    ticker: str
    yes_bid: int | None            # cents; price to sell YES (= buy NO)
    yes_ask: int | None            # cents; price to buy YES
    mid: float | None              # probability (0..1) midpoint, None if one side empty

    def p_buy_yes(self) -> float | None:
        """Probability implied by the YES ask (what you pay to buy YES)."""
        return None if self.yes_ask is None else self.yes_ask / 100.0

    def p_buy_no(self) -> float | None:
        """Probability implied by the NO ask (= 1 - yes_bid)."""
        return None if self.yes_bid is None else (100 - self.yes_bid) / 100.0


def implied_from_market(m: Market) -> MarketImplied:
    yb, ya = m.yes_bid, m.yes_ask
    if yb is not None and ya is not None:
        mid = (yb + ya) / 200.0
    else:
        mid = None
    return MarketImplied(ticker=m.ticker, yes_bid=yb, yes_ask=ya, mid=mid)


__all__ = ["MarketImplied", "implied_from_market"]
