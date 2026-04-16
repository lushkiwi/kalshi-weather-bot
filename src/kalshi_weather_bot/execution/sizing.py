"""Position sizing.

Flat sizing through M4/M5: ``min(config.flat_size, risk_cap_remaining)``.
Fractional-Kelly variant arrives in M6 once calibration is available.
"""

from __future__ import annotations

from kalshi_weather_bot.config import TradingConfig


def flat_size(cfg: TradingConfig, max_allowed: int) -> int:
    """Return the number of contracts to request; never exceeds ``max_allowed``."""
    if max_allowed <= 0:
        return 0
    return min(cfg.flat_size, max_allowed)


__all__ = ["flat_size"]
