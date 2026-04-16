"""Read-only soak dashboard.

Reads the recorder SQLite DB + log file to show a simple status page.
Runs as a separate process; never writes to the DB or interferes with
the trading daemon.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from string import Template

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import HTMLResponse
from starlette.routing import Route

from kalshi_weather_bot.risk.killswitch import is_killed, read_reason

PAGE_TEMPLATE = Template("""\
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="refresh" content="60">
<title>Kalshi Weather Bot</title>
<style>
  :root {
    --bg: #0f1117; --card: #1a1d27; --border: #2a2d3a;
    --text: #e1e4ec; --muted: #8b8fa3; --accent: #6c7ee1;
    --green: #34d399; --yellow: #fbbf24; --red: #f87171;
  }
  * { margin: 0; padding: 0; box-sizing: border-box; }
  body {
    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', system-ui, sans-serif;
    background: var(--bg); color: var(--text);
    padding: 24px; max-width: 960px; margin: 0 auto;
  }
  h1 { font-size: 1.5rem; font-weight: 600; margin-bottom: 20px; }
  h2 { font-size: 1rem; font-weight: 600; color: var(--muted); text-transform: uppercase;
       letter-spacing: 0.05em; margin-bottom: 12px; }
  .grid { display: grid; grid-template-columns: 1fr 1fr; gap: 16px; margin-bottom: 24px; }
  @media (max-width: 640px) { .grid { grid-template-columns: 1fr; } }
  .card {
    background: var(--card); border: 1px solid var(--border);
    border-radius: 12px; padding: 20px;
  }
  .card.full { grid-column: 1 / -1; }
  .stat-row { display: flex; justify-content: space-between; padding: 6px 0;
              border-bottom: 1px solid var(--border); }
  .stat-row:last-child { border-bottom: none; }
  .stat-label { color: var(--muted); font-size: 0.875rem; }
  .stat-value { font-weight: 600; font-size: 0.95rem; font-variant-numeric: tabular-nums; }
  .dot { display: inline-block; width: 10px; height: 10px; border-radius: 50%;
         margin-right: 6px; vertical-align: middle; }
  .dot.green { background: var(--green); }
  .dot.yellow { background: var(--yellow); }
  .dot.red { background: var(--red); }
  table { width: 100%; border-collapse: collapse; font-size: 0.85rem; }
  th { text-align: left; color: var(--muted); font-weight: 500; padding: 8px 6px;
       border-bottom: 1px solid var(--border); }
  td { padding: 8px 6px; border-bottom: 1px solid var(--border); font-variant-numeric: tabular-nums; }
  tr:last-child td { border-bottom: none; }
  .badge { display: inline-block; padding: 2px 8px; border-radius: 4px;
           font-size: 0.75rem; font-weight: 600; }
  .badge.yes { background: rgba(52,211,153,0.15); color: var(--green); }
  .badge.no { background: rgba(251,191,36,0.15); color: var(--yellow); }
  .badge.warn { background: rgba(251,191,36,0.15); color: var(--yellow); }
  .badge.error { background: rgba(248,113,113,0.15); color: var(--red); }
  .badge.critical { background: rgba(248,113,113,0.25); color: var(--red); }
  .badge.info { background: rgba(108,126,225,0.15); color: var(--accent); }
  .empty { color: var(--muted); font-style: italic; padding: 16px 0; text-align: center; }
  .footer { color: var(--muted); font-size: 0.75rem; text-align: center; margin-top: 24px; }
</style>
</head>
<body>

<h1>Kalshi Weather Bot</h1>

<div class="grid">

  <div class="card">
    <h2>Bot Status</h2>
    <div class="stat-row">
      <span class="stat-label">Mode</span>
      <span class="stat-value">$mode</span>
    </div>
    <div class="stat-row">
      <span class="stat-label">Kill Switch</span>
      <span class="stat-value">$kill_html</span>
    </div>
    <div class="stat-row">
      <span class="stat-label">Last Tick</span>
      <span class="stat-value"><span class="dot $tick_dot"></span>$last_tick</span>
    </div>
    <div class="stat-row">
      <span class="stat-label">Ticks Today</span>
      <span class="stat-value">$ticks_today</span>
    </div>
  </div>

  <div class="card">
    <h2>Today's Trading</h2>
    <div class="stat-row">
      <span class="stat-label">Trades Placed</span>
      <span class="stat-value">$trades_today</span>
    </div>
    <div class="stat-row">
      <span class="stat-label">Contracts</span>
      <span class="stat-value">$contracts_today</span>
    </div>
    <div class="stat-row">
      <span class="stat-label">Cost</span>
      <span class="stat-value">$$$cost_today</span>
    </div>
    <div class="stat-row">
      <span class="stat-label">Fees</span>
      <span class="stat-value">$$$fees_today</span>
    </div>
  </div>

  <div class="card full">
    <h2>Recent Trades</h2>
    $trades_table
  </div>

  <div class="card full">
    <h2>Alerts &amp; Errors</h2>
    $alerts_table
  </div>

</div>

<div class="footer">
  auto-refreshes every 60s &middot; read-only view &middot; $now_utc UTC
</div>

</body>
</html>
""")


def _ts_to_str(unix_ts: int | float) -> str:
    return datetime.fromtimestamp(unix_ts, tz=timezone.utc).strftime("%b %d %H:%M UTC")


def _minutes_ago(unix_ts: int | float) -> int:
    return int((datetime.now(tz=timezone.utc).timestamp() - unix_ts) / 60)


def _read_db_stats(db_path: str) -> dict:
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


def _read_alerts(log_path: str, limit: int = 20) -> list[dict]:
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
            alerts.append({
                "time": entry.get("timestamp", "")[:19].replace("T", " "),
                "level": level,
                "event": entry.get("event", ""),
                "detail": _alert_detail(entry),
            })
    return alerts


def _alert_detail(entry: dict) -> str:
    skip = {"event", "level", "timestamp"}
    parts = []
    for k, v in entry.items():
        if k in skip:
            continue
        parts.append(f"{k}={v}")
    return " ".join(parts[:4])


def _esc(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _build_trades_table(fills: list) -> str:
    if not fills:
        return '<div class="empty">No trades yet</div>'
    rows = []
    for filled_ts, ticker, side, count, price, fee, mode in fills:
        side = side or "?"
        badge_cls = "yes" if side == "yes" else "no"
        short_ticker = _esc(ticker.split("-", 1)[-1]) if "-" in ticker else _esc(ticker)
        rows.append(
            f"<tr>"
            f"<td>{_ts_to_str(filled_ts)}</td>"
            f"<td>{short_ticker}</td>"
            f'<td><span class="badge {badge_cls}">{_esc(side).upper()}</span></td>'
            f"<td>{count}</td>"
            f"<td>{price}&cent;</td>"
            f"<td>{fee}&cent;</td>"
            f"<td>{_esc(mode)}</td>"
            f"</tr>"
        )
    return (
        "<table>"
        "<tr><th>Time</th><th>Market</th><th>Side</th>"
        "<th>Qty</th><th>Price</th><th>Fee</th><th>Mode</th></tr>"
        + "".join(rows)
        + "</table>"
    )


def _build_alerts_table(alerts: list[dict]) -> str:
    if not alerts:
        return '<div class="empty">No alerts &mdash; all clear</div>'
    rows = []
    for a in alerts:
        badge = a["level"]
        if badge == "warning":
            badge = "warn"
        rows.append(
            f"<tr>"
            f"<td>{_esc(a['time'])}</td>"
            f'<td><span class="badge {badge}">{_esc(a["level"]).upper()}</span></td>'
            f"<td>{_esc(a['event'])}</td>"
            f'<td style="color:var(--muted);font-size:0.8rem">{_esc(a["detail"])}</td>'
            f"</tr>"
        )
    return (
        "<table>"
        "<tr><th>Time</th><th>Level</th><th>Event</th><th>Details</th></tr>"
        + "".join(rows)
        + "</table>"
    )


def create_app(
    *,
    db_path: str = "data/recorder.sqlite3",
    log_path: str = "logs/bot.log",
    kill_lock: str = "kill.lock",
    mode: str = "paper",
) -> Starlette:

    async def homepage(request: Request) -> HTMLResponse:
        stats = _read_db_stats(db_path)

        kl = Path(kill_lock)
        if is_killed(kl):
            reason = _esc(read_reason(kl) or "unknown")
            kill_html = f'<span class="dot red"></span>ARMED &mdash; {reason}'
        else:
            kill_html = '<span class="dot green"></span>Clear'

        last_ts = stats["last_tick_ts"]
        if last_ts:
            mins = _minutes_ago(last_ts)
            last_tick = f"{_ts_to_str(last_ts)} ({mins}m ago)"
            tick_dot = "green" if mins <= 10 else ("yellow" if mins <= 30 else "red")
        else:
            last_tick = "no ticks yet"
            tick_dot = "yellow"

        now_utc = datetime.now(tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

        html = PAGE_TEMPLATE.safe_substitute(
            mode=_esc(mode),
            kill_html=kill_html,
            last_tick=last_tick,
            tick_dot=tick_dot,
            ticks_today=stats["ticks_today"],
            trades_today=stats["trades_today"],
            contracts_today=stats["contracts_today"],
            cost_today=f"{stats['cost_cents'] / 100:.2f}",
            fees_today=f"{stats['fees_cents'] / 100:.2f}",
            trades_table=_build_trades_table(stats["recent_fills"]),
            alerts_table=_build_alerts_table(_read_alerts(log_path)),
            now_utc=now_utc,
        )
        return HTMLResponse(html)

    return Starlette(routes=[Route("/", homepage)])


__all__ = ["create_app"]
