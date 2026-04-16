"""Data-staleness health gate.

Refuses to trade when our inputs are older than their configured TTL. NWS is
advisory-only per PLAN.md §6: missing NWS widens ``edge_min`` (not handled
here) but does not block trading on its own.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from kalshi_weather_bot.config import RiskConfig


@dataclass(slots=True)
class HealthStatus:
    healthy: bool
    reasons: list[str] = field(default_factory=list)
    nws_stale: bool = False                # advisory flag; does not fail health


def _is_stale(last_ts: float | None, now_ts: float, ttl_seconds: int) -> bool:
    return last_ts is None or (now_ts - last_ts) > ttl_seconds


def check_health(
    *,
    kalshi_last_fetch_ts: float | None,
    openmeteo_last_fetch_ts: float | None,
    nws_last_fetch_ts: float | None,
    now_ts: float,
    cfg: RiskConfig,
) -> HealthStatus:
    reasons: list[str] = []
    if _is_stale(kalshi_last_fetch_ts, now_ts, cfg.kalshi_stale_seconds):
        reasons.append("kalshi_stale")
    if _is_stale(openmeteo_last_fetch_ts, now_ts, cfg.openmeteo_stale_seconds):
        reasons.append("openmeteo_stale")
    nws_stale = _is_stale(nws_last_fetch_ts, now_ts, cfg.nws_stale_seconds)
    return HealthStatus(healthy=not reasons, reasons=reasons, nws_stale=nws_stale)


__all__ = ["HealthStatus", "check_health"]
