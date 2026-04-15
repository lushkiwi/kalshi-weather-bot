"""Kalshi trading fees.

Per Kalshi's published fee schedule:
  taker fee (dollars) = ceil_to_cent(0.07 · C · P · (1 − P))
  maker fee (dollars) = ceil_to_cent(0.0175 · C · P · (1 − P))

where C is the number of contracts in the order and P is the trade price in
dollars (0..1). The ceiling is applied to the whole order, not per contract —
a 10-contract order pays a single rounded fee, not 10 separate roundings.

We compute fees in integer cents throughout to keep accounting exact.
"""

from __future__ import annotations

from decimal import ROUND_CEILING, Decimal

TAKER_RATE = Decimal("0.07")
MAKER_RATE = Decimal("0.0175")
_ONE = Decimal(1)
_HUNDRED = Decimal(100)
_CENT = Decimal(1)


def _fee_cents(rate: Decimal, contracts: int, price: float) -> int:
    if contracts <= 0:
        return 0
    if not 0.0 <= price <= 1.0:
        raise ValueError(f"price must be in [0,1]; got {price}")
    p = Decimal(str(price))
    cents = rate * Decimal(contracts) * p * (_ONE - p) * _HUNDRED
    return int(cents.quantize(_CENT, rounding=ROUND_CEILING))


def taker_fee_cents(contracts: int, price: float) -> int:
    """Taker (crossing) fee for the whole order, in cents."""
    return _fee_cents(TAKER_RATE, contracts, price)


def maker_fee_cents(contracts: int, price: float) -> int:
    """Maker (resting) fee for the whole order, in cents."""
    return _fee_cents(MAKER_RATE, contracts, price)


def taker_fee_rate(price: float) -> float:
    """Worst-case per-contract taker fee as a probability-fraction (C=1).

    Used in edge evaluation where we want a conservative per-contract rate:
    because the ceiling is applied per order, C=1 gives the largest rounding
    penalty and therefore the most pessimistic edge.
    """
    if not 0.0 <= price <= 1.0:
        raise ValueError(f"price must be in [0,1]; got {price}")
    return taker_fee_cents(1, price) / 100.0


def maker_fee_rate(price: float) -> float:
    """Worst-case per-contract maker fee as a probability-fraction (C=1)."""
    if not 0.0 <= price <= 1.0:
        raise ValueError(f"price must be in [0,1]; got {price}")
    return maker_fee_cents(1, price) / 100.0


__all__ = [
    "MAKER_RATE",
    "TAKER_RATE",
    "maker_fee_cents",
    "maker_fee_rate",
    "taker_fee_cents",
    "taker_fee_rate",
]
