"""Data-staleness health gate and broker/position reconciliation.

Refuses to trade when our inputs are older than their configured TTL. NWS is
advisory-only per PLAN.md §6: missing NWS widens ``edge_min`` (not handled
here) but does not block trading on its own.

Reconciliation compares our DB-derived contract counts to what Kalshi says
we hold. Any divergence is a signal to halt — it usually means an out-of-
band fill or a bug in our accounting.
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


@dataclass(slots=True)
class ReconciliationReport:
    matches: bool
    drifted_tickers: dict[str, tuple[int, int]] = field(default_factory=dict)  # ticker -> (db, live)

    def pretty(self) -> str:
        if self.matches:
            return "positions match"
        lines = ["position drift:"]
        for ticker, (db_val, live_val) in sorted(self.drifted_tickers.items()):
            lines.append(f"  {ticker}: db={db_val} live={live_val}")
        return "\n".join(lines)


def reconcile_positions(
    db_positions: dict[str, int], live_positions: dict[str, int]
) -> ReconciliationReport:
    """Diff two ticker->count maps. Any nonzero delta is a drift."""
    drifted: dict[str, tuple[int, int]] = {}
    for ticker in set(db_positions) | set(live_positions):
        db_val = int(db_positions.get(ticker, 0))
        live_val = int(live_positions.get(ticker, 0))
        if db_val != live_val:
            drifted[ticker] = (db_val, live_val)
    return ReconciliationReport(matches=not drifted, drifted_tickers=drifted)


__all__ = [
    "HealthStatus",
    "ReconciliationReport",
    "check_health",
    "reconcile_positions",
]
