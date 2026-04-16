"""Data readers for the soak dashboard.

All functions are read-only: they open SQLite in ``?mode=ro`` and parse
log files without holding locks.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path


def ts_to_str(unix_ts: int | float) -> str:
    return datetime.fromtimestamp(unix_ts, tz=timezone.utc).strftime("%b %d %H:%M UTC")


def minutes_ago(unix_ts: int | float) -> int:
    return int((datetime.now(tz=timezone.utc).timestamp() - unix_ts) / 60)


def esc(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def read_db_stats(db_path: str) -> dict:
    out: dict = {
        "trades_today": 0,
        "contracts_today": 0,
        "cost_cents": 0,
        "fees_cents": 0,
        "ticks_today": 0,
        "recent_fills": [],
        "last_tick_ts": None,
    }
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    except Exception:
        return out
    try:
        today_start = int(
            datetime.now(tz=timezone.utc)
            .replace(hour=0, minute=0, second=0, microsecond=0)
            .timestamp()
        )
        row = conn.execute(
            "SELECT count(*), coalesce(sum(count),0), "
            "coalesce(sum(count * yes_price),0), coalesce(sum(fee_cents),0) "
            "FROM fills WHERE filled_ts >= ?",
            (today_start,),
        ).fetchone()
        if row:
            out["trades_today"] = row[0]
            out["contracts_today"] = row[1]
            out["cost_cents"] = row[2]
            out["fees_cents"] = row[3]

        ticks_row = conn.execute(
            "SELECT count(DISTINCT snapshot_ts) FROM decisions WHERE snapshot_ts >= ?",
            (today_start,),
        ).fetchone()
        out["ticks_today"] = ticks_row[0] if ticks_row else 0

        last_row = conn.execute("SELECT max(snapshot_ts) FROM decisions").fetchone()
        if last_row and last_row[0]:
            out["last_tick_ts"] = last_row[0]

        fills = conn.execute(
            "SELECT filled_ts, ticker, "
            "(SELECT side FROM orders WHERE kalshi_order_id = fills.kalshi_order_id LIMIT 1), "
            "count, yes_price, fee_cents, mode "
            "FROM fills ORDER BY filled_ts DESC LIMIT 15"
        ).fetchall()
        out["recent_fills"] = fills
    except Exception:
        pass
    finally:
        conn.close()
    return out


def read_tick_log(log_path: str) -> dict:
    tick_events = {"tick_no_edges", "tick_done", "tick_complete", "tick_skipped_killed"}
    last_ts: float | None = None
    ticks_today = 0
    today_str = datetime.now(tz=timezone.utc).strftime("%Y-%m-%d")

    try:
        lines = Path(log_path).read_text().splitlines()
    except Exception:
        return {"last_tick_ts": None, "ticks_today_log": 0}

    for line in reversed(lines[-1000:]):
        try:
            entry = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            continue
        event = entry.get("event", "")
        if event not in tick_events:
            continue
        ts_str = entry.get("timestamp", "")
        if not ts_str:
            continue
        try:
            ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00")).timestamp()
        except (ValueError, TypeError):
            continue
        if last_ts is None:
            last_ts = ts
        if ts_str.startswith(today_str):
            ticks_today += 1

    return {"last_tick_ts": last_ts, "ticks_today_log": ticks_today}


def read_alerts(log_path: str, limit: int = 20) -> list[dict]:
    alerts: list[dict] = []
    try:
        lines = Path(log_path).read_text().splitlines()
    except Exception:
        return alerts
    for line in reversed(lines[-500:]):
        if len(alerts) >= limit:
            break
        try:
            entry = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            continue
        level = entry.get("level", "")
        if level in ("warning", "error", "critical"):
            skip = {"event", "level", "timestamp"}
            parts = [f"{k}={v}" for k, v in entry.items() if k not in skip][:4]
            alerts.append({
                "time": entry.get("timestamp", "")[:19].replace("T", " "),
                "level": level,
                "event": entry.get("event", ""),
                "detail": " ".join(parts),
            })
    return alerts


def read_pnl(db_path: str) -> dict:
    """Read realized P&L and open position summaries."""
    out: dict = {
        "realized_pnl_cents": 0,
        "settled_trades": 0,
        "open_trades": 0,
        "open_cost_cents": 0,
        "positions": [],
    }
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    except Exception:
        return out
    try:
        # Realized P&L: fills joined with settlements
        settled = conn.execute(
            "SELECT f.ticker, "
            "(SELECT o.side FROM orders o "
            " WHERE o.kalshi_order_id = f.kalshi_order_id LIMIT 1), "
            "f.count, f.yes_price, f.fee_cents, ms.result, f.filled_ts "
            "FROM fills f "
            "JOIN market_settlements ms ON f.ticker = ms.ticker "
            "ORDER BY f.filled_ts DESC"
        ).fetchall()
        for ticker, side, count, yes_price, fee, result, filled_ts in settled:
            if side == "yes":
                pnl = ((100 - yes_price) if result == "yes" else -yes_price) * count - fee
            else:
                pnl = (yes_price if result == "no" else -(100 - yes_price)) * count - fee
            out["realized_pnl_cents"] += pnl
            if side == "yes":
                payout = (100 - yes_price) * count - fee
            else:
                payout = yes_price * count - fee
            out["positions"].append({
                "ticker": ticker,
                "side": side or "?",
                "count": count,
                "price": yes_price,
                "fee": fee,
                "result": result,
                "pnl_cents": pnl,
                "payout_cents": payout,
                "filled_ts": filled_ts,
                "settled": True,
            })
        out["settled_trades"] = len(settled)

        # Open positions: fills without settlement
        open_rows = conn.execute(
            "SELECT f.ticker, "
            "(SELECT o.side FROM orders o "
            " WHERE o.kalshi_order_id = f.kalshi_order_id LIMIT 1), "
            "f.count, f.yes_price, f.fee_cents, f.filled_ts "
            "FROM fills f "
            "LEFT JOIN market_settlements ms ON f.ticker = ms.ticker "
            "WHERE ms.ticker IS NULL "
            "ORDER BY f.filled_ts DESC"
        ).fetchall()
        for ticker, side, count, yes_price, fee, filled_ts in open_rows:
            if side == "yes":
                cost = yes_price * count + fee
            else:
                cost = (100 - yes_price) * count + fee
            out["open_cost_cents"] += cost
            if side == "yes":
                payout = (100 - yes_price) * count - fee
            else:
                payout = yes_price * count - fee
            out["positions"].append({
                "ticker": ticker,
                "side": side or "?",
                "count": count,
                "price": yes_price,
                "fee": fee,
                "result": None,
                "pnl_cents": None,
                "payout_cents": payout,
                "filled_ts": filled_ts,
                "settled": False,
            })
        out["open_trades"] = len(open_rows)
    except Exception:
        pass
    finally:
        conn.close()
    return out


def read_latest_scan(db_path: str) -> tuple[str, list[tuple]]:
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    except Exception:
        return ("", [])
    try:
        tid_row = conn.execute(
            "SELECT tick_id, ts FROM tick_scans ORDER BY ts DESC LIMIT 1"
        ).fetchone()
        if not tid_row:
            return ("", [])
        tick_id, ts = tid_row
        rows = conn.execute(
            "SELECT city, ticker, strike, target_date, bid, ask, "
            "p_fair, p_market, net_edge_yes, net_edge_no, flagged, n_samples "
            "FROM tick_scans WHERE tick_id = ? ORDER BY city, ticker",
            (tick_id,),
        ).fetchall()
        meta = f"tick {tick_id} &middot; {ts_to_str(ts)} ({minutes_ago(ts)}m ago)"
        return (meta, rows)
    except Exception:
        return ("", [])
    finally:
        conn.close()
