from __future__ import annotations

import asyncio
import json
from typing import Any

from kalshi_weather_bot.config import AppConfig, Secrets
from kalshi_weather_bot.kalshi.client import KalshiClient
from kalshi_weather_bot.kalshi.markets import fetch_orderbook, list_active_weather_markets
from kalshi_weather_bot.kalshi.models import Market
from kalshi_weather_bot.logging_setup import get_logger
from kalshi_weather_bot.recorder.db import Recorder
from kalshi_weather_bot.util.time_utils import utcnow_ts
from kalshi_weather_bot.weather.nws import NwsClient
from kalshi_weather_bot.weather.openmeteo import OpenMeteoClient
from kalshi_weather_bot.weather.stations import Station, STATIONS


log = get_logger("recorder.snapshot")


async def _save_raw(
    rec: Recorder, source: str, endpoint: str, params: dict[str, Any], body: Any, status: int = 200
) -> None:
    await rec.execute(
        "INSERT INTO raw_responses (source, endpoint, params_json, response_json, fetched_at, status_code) VALUES (?, ?, ?, ?, ?, ?)",
        (source, endpoint, json.dumps(params, default=str), json.dumps(body, default=str), utcnow_ts(), status),
    )


async def _save_market(rec: Recorder, m: Market, snapshot_ts: int) -> None:
    await rec.execute(
        """INSERT OR REPLACE INTO market_snapshots
           (ticker, event_ticker, series_ticker, snapshot_ts, yes_bid, yes_ask, no_bid, no_ask,
            last_price, volume, floor_strike, cap_strike, strike_type, expiration_ts)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            m.ticker,
            m.event_ticker,
            m.series_ticker,
            snapshot_ts,
            m.yes_bid,
            m.yes_ask,
            m.no_bid,
            m.no_ask,
            m.last_price,
            m.volume,
            m.floor_strike,
            m.cap_strike,
            m.strike_type,
            int(m.expiration_time.timestamp()) if m.expiration_time else None,
        ),
    )


async def _save_forecast_samples(rec: Recorder, forecasts: list, fetched_ts: int) -> None:
    rows: list[tuple[Any, ...]] = []
    for ef in forecasts:
        for s in ef.samples:
            rows.append(
                (
                    s.city,
                    s.target_date.isoformat(),
                    s.source,
                    s.member if s.member is not None else 0,
                    s.variable,
                    float(s.value),
                    int(s.fetched_at.timestamp()) if hasattr(s.fetched_at, "timestamp") else fetched_ts,
                    int(s.run_time.timestamp()) if hasattr(s.run_time, "timestamp") else fetched_ts,
                )
            )
    if rows:
        await rec.executemany(
            """INSERT OR REPLACE INTO forecast_samples
               (city, target_date, source, member, variable, value, fetched_at, run_time)
               VALUES (?,?,?,?,?,?,?,?)""",
            rows,
        )


async def _verify_settlement_sources(markets: list[Market]) -> None:
    """M1 station-mapping audit. Logs warnings if a market's rules text doesn't
    mention the expected NWS station/airport. No hard failure."""
    from kalshi_weather_bot.weather.stations import by_series

    for m in markets:
        st = by_series(m.series_ticker or "")
        if st is None:
            continue
        rules = (m.rules_primary or "") + " " + (m.settlement_source or "")
        if st.station_id not in rules:
            log.warning(
                "settlement_source_mismatch",
                ticker=m.ticker,
                expected_station=st.station_id,
                excerpt=rules[:200],
            )


async def _pull_kalshi(
    rec: Recorder, client: KalshiClient, series: list[str], snapshot_ts: int
) -> list[Market]:
    # List markets
    all_markets: list[Market] = []
    for s in series:
        raw = await client.get_markets(series_ticker=s, status="open", limit=200)
        await _save_raw(rec, "kalshi_markets", "/markets", {"series_ticker": s}, raw)
        for m_raw in raw.get("markets", []):
            m = Market.model_validate(m_raw)
            all_markets.append(m)
            await _save_market(rec, m, snapshot_ts)

    # Orderbooks (sequential to respect the client's rate limiter)
    for m in all_markets:
        ob_raw = await client.get_orderbook(m.ticker, depth=5)
        await _save_raw(rec, "kalshi_orderbook", f"/markets/{m.ticker}/orderbook", {"depth": 5}, ob_raw)

    return all_markets


async def _pull_weather(rec: Recorder, cfg: AppConfig, secrets: Secrets) -> None:
    cities: list[Station] = list(STATIONS.values())

    from datetime import datetime, timezone

    fetched_at = datetime.now(tz=timezone.utc)
    fetched_ts = int(fetched_at.timestamp())

    async with OpenMeteoClient(
        api_key=secrets.openmeteo_api_key,
        models=cfg.weather.openmeteo.models,
        forecast_days=cfg.weather.openmeteo.forecast_days,
    ) as om:
        for st in cities:
            raw = await om.fetch_raw(st)
            await _save_raw(rec, "openmeteo", "/ensemble", {"city": st.city_code}, raw)
            forecasts = om.parse_daily_max(raw, st, fetched_at)
            await _save_forecast_samples(rec, forecasts, fetched_ts)

    async with NwsClient(user_agent=cfg.weather.nws.user_agent) as nws:
        for st in cities:
            try:
                raw = await nws.point_forecast(st)
            except Exception as e:  # NWS can be flaky; don't block M1 on it
                log.warning("nws_point_failed", city=st.city_code, error=str(e))
                continue
            await _save_raw(rec, "nws_point", "/gridpoints/forecast", {"city": st.city_code}, raw)
            highs = nws.extract_daily_highs_f(raw)
            rows: list[tuple[Any, ...]] = []
            for day, high in highs.items():
                rows.append(
                    (
                        st.city_code,
                        day,
                        "nws_point",
                        0,
                        "tmax_f",
                        float(high),
                        fetched_ts,
                        fetched_ts,
                    )
                )
            if rows:
                await rec.executemany(
                    """INSERT OR REPLACE INTO forecast_samples
                       (city, target_date, source, member, variable, value, fetched_at, run_time)
                       VALUES (?,?,?,?,?,?,?,?)""",
                    rows,
                )


async def run_backfill(cfg: AppConfig, secrets: Secrets, *, days: int = 1) -> None:
    """Single-pass backfill: Kalshi markets + orderbooks + weather ensembles, persisted to SQLite."""
    # `days` is reserved for future multi-pass historical rebuilds; for M1 we take one snapshot now.
    del days

    snapshot_ts = utcnow_ts()
    key_id, pem = secrets.kalshi_credentials(cfg.kalshi.env)

    async with Recorder(cfg.recorder.db_path) as rec:
        async with KalshiClient(
            key_id,
            pem,
            env=cfg.kalshi.env,
            rate_limit_per_sec=cfg.kalshi.rate_limit_per_sec,
            timeout_sec=cfg.kalshi.request_timeout_sec,
        ) as kc:
            markets = await _pull_kalshi(rec, kc, cfg.series, snapshot_ts)
            await _verify_settlement_sources(markets)
            await rec.commit()

        await _pull_weather(rec, cfg, secrets)
        await rec.commit()

        log.info(
            "backfill_done",
            snapshot_ts=snapshot_ts,
            markets=len(markets),
            db=cfg.recorder.db_path,
        )


def run_backfill_sync(cfg: AppConfig, secrets: Secrets, *, days: int = 1) -> None:
    asyncio.run(run_backfill(cfg, secrets, days=days))
