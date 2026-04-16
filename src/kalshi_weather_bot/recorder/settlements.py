"""Settlement ingestion and realized P&L computation."""

from __future__ import annotations

from datetime import datetime, timezone

from kalshi_weather_bot.kalshi.client import KalshiClient
from kalshi_weather_bot.kalshi.models import Market
from kalshi_weather_bot.logging_setup import get_logger
from kalshi_weather_bot.recorder.db import Recorder

log = get_logger("recorder.settlements")


async def _unsettled_tickers(rec: Recorder) -> list[str]:
    """Return tickers that have fills but no settlement record yet."""
    rows = await rec.fetchall(
        "SELECT DISTINCT f.ticker FROM fills f "
        "LEFT JOIN market_settlements ms ON f.ticker = ms.ticker "
        "WHERE ms.ticker IS NULL"
    )
    return [r[0] for r in rows]


async def check_settlements(
    client: KalshiClient, rec: Recorder
) -> list[tuple[str, str]]:
    """Query Kalshi for settlement results on our open positions.

    Returns list of (ticker, result) pairs that were newly recorded.
    """
    tickers = await _unsettled_tickers(rec)
    if not tickers:
        return []

    newly_settled: list[tuple[str, str]] = []
    for ticker in tickers:
        try:
            raw = await client.get_market(ticker)
            m = Market.model_validate(raw.get("market", raw))
        except Exception as exc:
            log.debug("settlement_check_failed", ticker=ticker, error=str(exc))
            continue

        if m.status not in ("settled", "finalized") or m.result is None:
            continue

        ts = int(datetime.now(tz=timezone.utc).timestamp())
        await rec.execute(
            "INSERT OR IGNORE INTO market_settlements "
            "(ticker, event_ticker, result, expiration_value, settled_ts) "
            "VALUES (?, ?, ?, ?, ?)",
            (ticker, m.event_ticker, m.result, m.expiration_value, ts),
        )
        newly_settled.append((ticker, m.result))
        log.info(
            "settlement_recorded",
            ticker=ticker,
            result=m.result,
            expiration_value=m.expiration_value,
        )

    if newly_settled:
        await rec.commit()
    return newly_settled


async def compute_realized_pnl(rec: Recorder) -> list[dict]:
    """Compute per-fill realized P&L for all settled positions.

    Returns a list of dicts with keys:
        ticker, side, count, yes_price, fee_cents, result, pnl_cents
    """
    rows = await rec.fetchall(
        "SELECT f.ticker, "
        "(SELECT o.side FROM orders o WHERE o.kalshi_order_id = f.kalshi_order_id LIMIT 1), "
        "f.count, f.yes_price, f.fee_cents, ms.result, f.filled_ts "
        "FROM fills f "
        "JOIN market_settlements ms ON f.ticker = ms.ticker "
        "ORDER BY f.filled_ts"
    )
    out: list[dict] = []
    for ticker, side, count, yes_price, fee_cents, result, filled_ts in rows:
        if side == "yes":
            if result == "yes":
                pnl = (100 - yes_price) * count - fee_cents
            else:
                pnl = -yes_price * count - fee_cents
        else:
            if result == "no":
                pnl = yes_price * count - fee_cents
            else:
                pnl = -(100 - yes_price) * count - fee_cents
        out.append({
            "ticker": ticker,
            "side": side or "?",
            "count": count,
            "yes_price": yes_price,
            "fee_cents": fee_cents,
            "result": result,
            "pnl_cents": pnl,
            "filled_ts": filled_ts,
        })
    return out


__all__ = ["check_settlements", "compute_realized_pnl"]
