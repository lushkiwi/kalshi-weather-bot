from __future__ import annotations

import os

import pytest

from kalshi_weather_bot.weather.openmeteo import OpenMeteoClient
from kalshi_weather_bot.weather.stations import STATIONS


pytestmark = pytest.mark.integration


@pytest.mark.asyncio
async def test_openmeteo_live_fetch_returns_ensemble_data() -> None:
    async with OpenMeteoClient() as om:
        raw = await om.fetch_raw(STATIONS["NY"])
    hourly = raw.get("hourly", {})
    keys = [k for k in hourly if k.startswith("temperature_2m")]
    assert keys, "expected at least one temperature_2m series"
    assert hourly.get("time"), "expected hourly time array"


@pytest.mark.asyncio
async def test_kalshi_demo_markets_fetch_if_creds_set() -> None:
    key_id = os.getenv("KALSHI_DEMO_API_KEY_ID")
    pem = os.getenv("KALSHI_DEMO_PRIVATE_KEY_PEM")
    if not key_id or not pem:
        pytest.skip("KALSHI_DEMO_* env not set")
    from kalshi_weather_bot.kalshi.client import KalshiClient

    async with KalshiClient(key_id, pem, env="demo") as c:
        page = await c.get_markets(series_ticker="KXHIGHNY", status="open", limit=5)
    assert "markets" in page
