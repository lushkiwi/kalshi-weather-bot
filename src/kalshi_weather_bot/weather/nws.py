from __future__ import annotations

from datetime import datetime
from typing import Any

import httpx
from tenacity import AsyncRetrying, retry_if_exception_type, stop_after_attempt, wait_exponential

from kalshi_weather_bot.logging_setup import get_logger
from kalshi_weather_bot.weather.stations import Station


BASE = "https://api.weather.gov"


class NwsClient:
    """Minimal NWS API client. Used as settlement validator and future ground truth source."""

    def __init__(self, *, user_agent: str, timeout_sec: float = 20.0) -> None:
        self._client = httpx.AsyncClient(
            base_url=BASE,
            timeout=timeout_sec,
            headers={"User-Agent": user_agent, "Accept": "application/geo+json"},
        )
        self._log = get_logger("weather.nws")

    async def aclose(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> "NwsClient":
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    async def _get(self, path: str) -> dict[str, Any]:
        async for attempt in AsyncRetrying(
            stop=stop_after_attempt(3),
            wait=wait_exponential(multiplier=0.5, min=0.5, max=4),
            retry=retry_if_exception_type((httpx.TransportError,)),
            reraise=True,
        ):
            with attempt:
                r = await self._client.get(path)
                r.raise_for_status()
                return r.json()
        raise RuntimeError("unreachable")

    async def point_forecast(self, station: Station) -> dict[str, Any]:
        """Return the NWS gridpoint forecast JSON for a station's coordinates."""
        meta = await self._get(f"/points/{station.lat},{station.lon}")
        forecast_url = meta["properties"]["forecast"]
        # forecast is a full URL; use it relative to base by taking its path.
        path = forecast_url.split(BASE, 1)[-1]
        return await self._get(path)

    def extract_daily_highs_f(self, forecast_json: dict[str, Any]) -> dict[str, int]:
        """Pull {YYYY-MM-DD: high_f} from an NWS /gridpoints/.../forecast response."""
        out: dict[str, int] = {}
        for period in forecast_json.get("properties", {}).get("periods", []):
            if not period.get("isDaytime"):
                continue
            start = period.get("startTime")
            if not start:
                continue
            day = datetime.fromisoformat(start).date().isoformat()
            temp = period.get("temperature")
            unit = period.get("temperatureUnit", "F")
            if temp is None:
                continue
            if unit == "C":
                temp = int(round(temp * 9 / 5 + 32))
            out[day] = int(temp)
        return out
