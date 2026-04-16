"""Read-only soak dashboard — Starlette app + HTML template."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from string import Template

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import HTMLResponse
from starlette.routing import Route

from kalshi_weather_bot.dashboard.queries import (
    esc,
    minutes_ago,
    read_alerts,
    read_db_stats,
    read_latest_scan,
    read_pnl,
    read_tick_log,
    ts_to_str,
)
from kalshi_weather_bot.risk.killswitch import is_killed, read_reason

_TEMPLATE_PATH = Path(__file__).with_name("template.html")
PAGE_TEMPLATE = Template(_TEMPLATE_PATH.read_text())


def _build_trades_table(fills: list) -> str:
    if not fills:
        return '<div class="empty">No trades yet</div>'
    rows = []
    for filled_ts, ticker, side, count, price, fee, mode in fills:
        side = side or "?"
        badge_cls = "yes" if side == "yes" else "no"
        short_ticker = esc(ticker.split("-", 1)[-1]) if "-" in ticker else esc(ticker)
        rows.append(
            f"<tr>"
            f"<td>{ts_to_str(filled_ts)}</td>"
            f"<td>{short_ticker}</td>"
            f'<td><span class="badge {badge_cls}">{esc(side).upper()}</span></td>'
            f"<td>{count}</td>"
            f"<td>{price}&cent;</td>"
            f"<td>{fee}&cent;</td>"
            f"<td>{esc(mode)}</td>"
            f"</tr>"
        )
    return (
        "<table>"
        "<tr><th>Time</th><th>Market</th><th>Side</th>"
        "<th>Qty</th><th>Price</th><th>Fee</th><th>Mode</th></tr>"
        + "".join(rows)
        + "</table>"
    )


def _build_positions_table(positions: list[dict]) -> str:
    if not positions:
        return '<div class="empty">No positions yet</div>'
    rows = []
    for p in positions:
        short = esc(p["ticker"].split("-", 1)[-1]) if "-" in p["ticker"] else esc(p["ticker"])
        side_cls = "yes" if p["side"] == "yes" else "no"
        payout = p.get("payout_cents", 0)
        payout_str = f"${payout / 100:.2f}"
        if p["settled"]:
            result_cls = "yes" if p["result"] == "yes" else "no"
            result_html = f'<span class="badge {result_cls}">{esc(p["result"]).upper()}</span>'
            pnl = p["pnl_cents"]
            pnl_color = "var(--green)" if pnl >= 0 else "var(--red)"
            pnl_str = f'<span style="color:{pnl_color}">{pnl / 100:+.2f}</span>'
        else:
            result_html = '<span class="badge info">OPEN</span>'
            pnl_str = "-"
        rows.append(
            f"<tr>"
            f"<td>{short}</td>"
            f'<td><span class="badge {side_cls}">{esc(p["side"]).upper()}</span></td>'
            f"<td>{p['count']}</td>"
            f"<td>{p['price']}&cent;</td>"
            f"<td>{p['fee']}&cent;</td>"
            f"<td>{payout_str}</td>"
            f"<td>{result_html}</td>"
            f"<td>{pnl_str}</td>"
            f"</tr>"
        )
    return (
        "<table>"
        "<tr><th>Market</th><th>Side</th><th>Qty</th>"
        "<th>Price</th><th>Fee</th><th>Payout</th><th>Result</th><th>P&amp;L</th></tr>"
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
            f"<td>{esc(a['time'])}</td>"
            f'<td><span class="badge {badge}">{esc(a["level"]).upper()}</span></td>'
            f"<td>{esc(a['event'])}</td>"
            f'<td style="color:var(--muted);font-size:0.8rem">{esc(a["detail"])}</td>'
            f"</tr>"
        )
    return (
        "<table>"
        "<tr><th>Time</th><th>Level</th><th>Event</th><th>Details</th></tr>"
        + "".join(rows)
        + "</table>"
    )


def _build_scan_table(scan_rows: list[tuple]) -> str:
    if not scan_rows:
        return '<div class="empty">No scan data yet</div>'
    rows = []
    for (city, ticker, strike, target_date, bid, ask,
         p_fair, p_market, net_yes, net_no, flagged, n_samples) in scan_rows:
        short = esc(ticker.split("-", 1)[-1]) if "-" in ticker else esc(ticker)
        ba = f"{bid or '-'}/{ask or '-'}"
        pf = f"{p_fair:.1%}" if p_fair is not None else "-"
        pm = f"{p_market:.1%}" if p_market is not None else "-"
        ney = f"{net_yes:+.1%}" if net_yes is not None else "-"
        nen = f"{net_no:+.1%}" if net_no is not None else "-"
        if flagged:
            badge = "yes" if flagged == "buy_yes" else "no"
            flag_html = f'<span class="badge {badge}">{esc(flagged).upper()}</span>'
        else:
            flag_html = ""
        rows.append(
            f"<tr>"
            f"<td>{esc(city)}</td>"
            f"<td>{short}</td>"
            f"<td>{esc(strike)}</td>"
            f"<td>{esc(target_date or '-')}</td>"
            f"<td>{ba}</td>"
            f"<td>{pf}</td><td>{pm}</td>"
            f"<td>{ney}</td><td>{nen}</td>"
            f"<td>{flag_html}</td>"
            f"<td>{n_samples}</td>"
            f"</tr>"
        )
    return (
        "<table>"
        "<tr><th>City</th><th>Contract</th><th>Strike</th><th>Date</th>"
        "<th>Bid/Ask</th><th>Fair</th><th>Market</th>"
        "<th>Edge Y</th><th>Edge N</th><th>Flag</th><th>N</th></tr>"
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
        stats = read_db_stats(db_path)
        tick_log = read_tick_log(log_path)
        scan_meta, scan_rows = read_latest_scan(db_path)
        pnl = read_pnl(db_path)

        kl = Path(kill_lock)
        if is_killed(kl):
            reason = esc(read_reason(kl) or "unknown")
            kill_html = f'<span class="dot red"></span>ARMED &mdash; {reason}'
        else:
            kill_html = '<span class="dot green"></span>Clear'

        last_ts = tick_log["last_tick_ts"] or stats["last_tick_ts"]
        ticks_today = max(tick_log["ticks_today_log"], stats["ticks_today"])
        if last_ts:
            mins = minutes_ago(last_ts)
            last_tick = f"{ts_to_str(last_ts)} ({mins}m ago)"
            tick_dot = "green" if mins <= 10 else ("yellow" if mins <= 30 else "red")
        else:
            last_tick = "no ticks yet"
            tick_dot = "yellow"

        now_utc = datetime.now(tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

        rpnl = pnl["realized_pnl_cents"]
        pnl_color = "var(--green)" if rpnl >= 0 else "var(--red)"
        pnl_display = f"{rpnl / 100:+.2f}" if pnl["settled_trades"] > 0 else "n/a"

        html = PAGE_TEMPLATE.safe_substitute(
            mode=esc(mode),
            kill_html=kill_html,
            last_tick=last_tick,
            tick_dot=tick_dot,
            ticks_today=ticks_today,
            pnl_color=pnl_color,
            pnl_display=pnl_display,
            settled_trades=pnl["settled_trades"],
            open_trades=pnl["open_trades"],
            open_cost=f"{pnl['open_cost_cents'] / 100:.2f}",
            positions_table=_build_positions_table(pnl["positions"]),
            scan_meta=scan_meta,
            scan_table=_build_scan_table(scan_rows),
            alerts_table=_build_alerts_table(read_alerts(log_path)),
            now_utc=now_utc,
        )
        return HTMLResponse(html)

    return Starlette(routes=[Route("/", homepage)])


__all__ = ["create_app"]
