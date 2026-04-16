"""inspect-edges orchestration: fetch markets + forecasts, print a raw edge table.

M2 scope: no fees, no sizing, no trading. Shows fair vs market for a human
sanity check. M3 extends this with fee-aware net edges.
"""

from __future__ import annotations

import asyncio
import re
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timezone

from kalshi_weather_bot.config import AppConfig, Secrets
from kalshi_weather_bot.edge.detector import Candidate, evaluate
from kalshi_weather_bot.edge.implied import MarketImplied, implied_from_market
from kalshi_weather_bot.kalshi.client import KalshiClient
from kalshi_weather_bot.kalshi.markets import list_active_weather_markets
from kalshi_weather_bot.kalshi.models import Market
from kalshi_weather_bot.logging_setup import get_logger
from kalshi_weather_bot.probability.threshold import (
    FairProbability,
    ThresholdError,
    contract_probability,
    event_coherence_error,
)
from kalshi_weather_bot.weather.models import EnsembleForecast
from kalshi_weather_bot.weather.openmeteo import OpenMeteoClient
from kalshi_weather_bot.weather.stations import STATIONS, Station, by_series


log = get_logger("edge.inspect")


# Event ticker tail encodes the target date, e.g. "KXHIGHNY-26APR15".
_MONTHS = {
    "JAN": 1, "FEB": 2, "MAR": 3, "APR": 4, "MAY": 5, "JUN": 6,
    "JUL": 7, "AUG": 8, "SEP": 9, "OCT": 10, "NOV": 11, "DEC": 12,
}
_DATE_RE = re.compile(r"(\d{2})([A-Z]{3})(\d{2})$")


def parse_event_date(event_ticker: str) -> date | None:
    tail = event_ticker.rsplit("-", 1)[-1]
    m = _DATE_RE.match(tail)
    if not m:
        return None
    yy, mon, dd = m.group(1), m.group(2), m.group(3)
    if mon not in _MONTHS:
        return None
    return date(2000 + int(yy), _MONTHS[mon], int(dd))


def _series_from_event(event_ticker: str) -> str:
    """Event tickers are `<SERIES>-<DATE>`; pull the series prefix."""
    return event_ticker.split("-", 1)[0]


@dataclass(slots=True)
class EdgeRow:
    ticker: str
    event_ticker: str
    city: str
    strike_desc: str
    target_date: date | None
    hours_to_close: float | None
    market_yes_bid: int | None
    market_yes_ask: int | None
    p_market_mid: float | None
    p_fair: float | None
    edge_buy_yes: float | None          # gross (no fees)
    edge_buy_no: float | None
    net_edge_buy_yes: float | None      # fee-adjusted
    net_edge_buy_no: float | None
    effective_edge_min: float | None
    flagged_side: str | None            # 'buy_yes' | 'buy_no' | None
    n_samples: int
    note: str = ""


def _strike_desc(m: Market) -> str:
    if m.strike_type == "greater":
        return f"> {m.floor_strike}"
    if m.strike_type == "less":
        # V2 populates cap_strike; legacy payloads populated floor_strike.
        threshold = m.cap_strike if m.cap_strike is not None else m.floor_strike
        return f"< {threshold}"
    if m.strike_type == "between":
        return f"{m.floor_strike}–{m.cap_strike}"
    return m.strike_type or "?"


def _ensembles_by_city_date(
    forecasts: dict[str, list[EnsembleForecast]],
) -> dict[tuple[str, date], EnsembleForecast]:
    out: dict[tuple[str, date], EnsembleForecast] = {}
    for city, ef_list in forecasts.items():
        for ef in ef_list:
            out[(city, ef.target_date)] = ef
    return out


async def _fetch_forecasts(
    cfg: AppConfig, secrets: Secrets, cities: list[Station]
) -> dict[str, list[EnsembleForecast]]:
    fetched_at = datetime.now(tz=timezone.utc)
    out: dict[str, list[EnsembleForecast]] = {}
    async with OpenMeteoClient(
        api_key=secrets.openmeteo_api_key,
        models=cfg.weather.openmeteo.models,
        forecast_days=cfg.weather.openmeteo.forecast_days,
    ) as om:
        for st in cities:
            raw = await om.fetch_raw(st)
            out[st.city_code] = om.parse_daily_max(raw, st, fetched_at)
    return out


def _hours_to_close(m: Market, now: datetime) -> float | None:
    when = m.close_time or m.expiration_time
    if when is None:
        return None
    return max(0.0, (when - now).total_seconds() / 3600.0)


def _build_rows(
    markets: list[Market],
    by_event_date: dict[tuple[str, date], EnsembleForecast],
    *,
    edge_min: float,
    decay_hours: float,
    now: datetime | None = None,
) -> list[EdgeRow]:
    now = now or datetime.now(tz=timezone.utc)
    # Group markets by event so we can log coherence errors.
    by_event: dict[str, list[Market]] = defaultdict(list)
    for m in markets:
        by_event[m.event_ticker].append(m)

    rows: list[EdgeRow] = []
    for event_ticker, event_markets in by_event.items():
        target = parse_event_date(event_ticker)
        # Kalshi's /markets payload doesn't reliably populate series_ticker on each
        # Market row, but the event_ticker always starts with it.
        series = event_markets[0].series_ticker or _series_from_event(event_ticker)
        station = by_series(series)
        ef: EnsembleForecast | None = None
        if station is not None and target is not None:
            ef = by_event_date.get((station.city_code, target))
            if ef is None:
                log.debug(
                    "forecast_miss",
                    event_ticker=event_ticker,
                    city=station.city_code,
                    target=target.isoformat(),
                    have_dates=sorted(
                        d.isoformat() for (c, d) in by_event_date if c == station.city_code
                    ),
                )

        event_probs: list[FairProbability] = []

        for m in event_markets:
            implied: MarketImplied = implied_from_market(m)
            city = station.city_code if station else "?"
            note = ""
            p_fair: float | None = None
            n_samples = 0
            if ef is None:
                note = "no forecast for event"
            else:
                samples = ef.values()
                n_samples = len(samples)
                try:
                    fair = contract_probability(m, samples)
                    p_fair = fair.p_fair
                    event_probs.append(fair)
                except ThresholdError as e:
                    note = str(e)

            edge_buy_yes: float | None = None
            edge_buy_no: float | None = None
            net_yes: float | None = None
            net_no: float | None = None
            effective_min: float | None = None
            flagged_side: str | None = None
            htc = _hours_to_close(m, now)

            if p_fair is not None and htc is not None:
                cands: list[Candidate] = evaluate(
                    p_fair,
                    implied,
                    edge_min=edge_min,
                    hours_to_close=htc,
                    decay_hours=decay_hours,
                )
                for c in cands:
                    if c.side == "buy_yes":
                        edge_buy_yes = c.gross_edge
                        net_yes = c.net_edge
                    else:
                        edge_buy_no = c.gross_edge
                        net_no = c.net_edge
                    effective_min = c.effective_edge_min
                flagged = [c for c in cands if c.flagged]
                if flagged:
                    flagged_side = max(flagged, key=lambda c: c.net_edge).side

            rows.append(
                EdgeRow(
                    ticker=m.ticker,
                    event_ticker=event_ticker,
                    city=city,
                    strike_desc=_strike_desc(m),
                    target_date=target,
                    hours_to_close=htc,
                    market_yes_bid=m.yes_bid,
                    market_yes_ask=m.yes_ask,
                    p_market_mid=implied.mid,
                    p_fair=p_fair,
                    edge_buy_yes=edge_buy_yes,
                    edge_buy_no=edge_buy_no,
                    net_edge_buy_yes=net_yes,
                    net_edge_buy_no=net_no,
                    effective_edge_min=effective_min,
                    flagged_side=flagged_side,
                    n_samples=n_samples,
                    note=note,
                )
            )

        if len(event_probs) >= 2:
            err = event_coherence_error(event_probs)
            # Only overshoot is a real red flag: Kalshi routinely leaves
            # brackets unlisted, so undershoots are expected.
            if err > 0.05:
                log.warning(
                    "event_overshoot",
                    event_ticker=event_ticker,
                    sum_minus_one=round(err, 4),
                )

    return rows


def format_table(rows: list[EdgeRow]) -> str:
    header = (
        f"{'ticker':<28} {'date':<10} {'strike':<14} {'bid/ask':<10} "
        f"{'h':>5} {'p_mkt':>7} {'p_fair':>7} "
        f"{'net_Y':>7} {'net_N':>7} {'min':>6} {'flag':<8} {'n':>4} note"
    )
    sep = "-" * len(header)
    lines = [header, sep]
    for r in sorted(rows, key=lambda r: (r.event_ticker, r.ticker)):
        bid = "-" if r.market_yes_bid is None else str(r.market_yes_bid)
        ask = "-" if r.market_yes_ask is None else str(r.market_yes_ask)
        date_str = r.target_date.isoformat() if r.target_date else "-"

        def fmt(v: float | None, decimals: int = 3, width: int = 7) -> str:
            return f"{'':>{width}}" if v is None else f"{v:>{width}.{decimals}f}"

        flag = ""
        if r.flagged_side == "buy_yes":
            flag = "BUY_YES"
        elif r.flagged_side == "buy_no":
            flag = "BUY_NO"

        lines.append(
            f"{r.ticker:<28} {date_str:<10} {r.strike_desc:<14} "
            f"{bid + '/' + ask:<10} "
            f"{fmt(r.hours_to_close, 1, 5)} "
            f"{fmt(r.p_market_mid, 3)} {fmt(r.p_fair, 3)} "
            f"{fmt(r.net_edge_buy_yes, 3, 7)} {fmt(r.net_edge_buy_no, 3, 7)} "
            f"{fmt(r.effective_edge_min, 3, 6)} {flag:<8} "
            f"{r.n_samples:>4} {r.note}"
        )
    return "\n".join(lines)


async def fetch_inputs(
    cfg: AppConfig, secrets: Secrets
) -> tuple[list[Market], dict[tuple[str, date], EnsembleForecast]]:
    """Pull live markets + ensembles. Shared by inspect-edges and the trading tick."""
    cities = [st for st in STATIONS.values() if st.series_ticker in cfg.series]
    key_id, pem = secrets.kalshi_credentials(cfg.kalshi.env)

    async with KalshiClient(
        key_id,
        pem,
        env=cfg.kalshi.env,
        rate_limit_per_sec=cfg.kalshi.rate_limit_per_sec,
        timeout_sec=cfg.kalshi.request_timeout_sec,
    ) as kc:
        markets = await list_active_weather_markets(kc, cfg.series)

    forecasts = await _fetch_forecasts(cfg, secrets, cities)
    by_event_date = _ensembles_by_city_date(forecasts)
    return markets, by_event_date


async def run_inspect(cfg: AppConfig, secrets: Secrets) -> list[EdgeRow]:
    """Fetch markets + ensembles, compute fair probs + raw edges, return rows."""
    markets, by_event_date = await fetch_inputs(cfg, secrets)
    return _build_rows(
        markets,
        by_event_date,
        edge_min=cfg.trading.edge_min.default,
        decay_hours=cfg.trading.close_decay_hours,
    )


def build_rows(
    markets: list[Market],
    by_event_date: dict[tuple[str, date], EnsembleForecast],
    *,
    edge_min: float,
    decay_hours: float,
    now: datetime | None = None,
) -> list[EdgeRow]:
    """Public facade for ``_build_rows`` — tick loop uses the same row shape."""
    return _build_rows(
        markets, by_event_date, edge_min=edge_min, decay_hours=decay_hours, now=now
    )


def run_inspect_sync(cfg: AppConfig, secrets: Secrets) -> list[EdgeRow]:
    return asyncio.run(run_inspect(cfg, secrets))
