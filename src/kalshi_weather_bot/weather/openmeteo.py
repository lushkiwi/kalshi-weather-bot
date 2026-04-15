from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Any

import httpx
from tenacity import AsyncRetrying, retry_if_exception_type, stop_after_attempt, wait_exponential

from kalshi_weather_bot.logging_setup import get_logger
from kalshi_weather_bot.weather.models import EnsembleForecast, ForecastSample
from kalshi_weather_bot.weather.stations import Station


FREE_BASE = "https://ensemble-api.open-meteo.com/v1"
PAID_BASE = "https://customer-ensemble-api.open-meteo.com/v1"


class OpenMeteoClient:
    """Async client for Open-Meteo Ensemble API.

    Uses the free endpoint unless ``api_key`` is provided, in which case it
    routes to the paid ``customer-ensemble-api`` host and appends the key.
    """

    def __init__(
        self,
        *,
        api_key: str | None = None,
        models: list[str] | None = None,
        forecast_days: int = 3,
        timeout_sec: float = 30.0,
    ) -> None:
        base = PAID_BASE if api_key else FREE_BASE
        self._api_key = api_key
        self._models = models or ["gfs025", "ecmwf_ifs025", "icon_seamless"]
        self._forecast_days = forecast_days
        self._client = httpx.AsyncClient(base_url=base, timeout=timeout_sec)
        self._log = get_logger("weather.openmeteo").bind(paid=bool(api_key))

    async def aclose(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> "OpenMeteoClient":
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    async def fetch_raw(self, station: Station) -> dict[str, Any]:
        params: dict[str, Any] = {
            "latitude": station.lat,
            "longitude": station.lon,
            "hourly": "temperature_2m",
            "models": ",".join(self._models),
            "forecast_days": self._forecast_days,
            "timezone": station.tz,
            "temperature_unit": "fahrenheit",
        }
        if self._api_key:
            params["apikey"] = self._api_key

        async for attempt in AsyncRetrying(
            stop=stop_after_attempt(4),
            wait=wait_exponential(multiplier=0.5, min=0.5, max=8),
            retry=retry_if_exception_type((httpx.TransportError,)),
            reraise=True,
        ):
            with attempt:
                r = await self._client.get("/ensemble", params=params)
                r.raise_for_status()
                return r.json()
        raise RuntimeError("unreachable")

    def parse_daily_max(
        self, raw: dict[str, Any], station: Station, fetched_at: datetime
    ) -> list[EnsembleForecast]:
        """Group per-member hourly temps into daily max per target_date."""
        hourly = raw.get("hourly", {})
        times: list[str] = hourly.get("time", [])
        if not times:
            return []

        # Open-Meteo column naming:
        #   temperature_2m                         -> plain control, no model tag
        #   temperature_2m_<model>                 -> deterministic run of <model>
        #   temperature_2m_memberNN_<model>        -> perturbed member NN of <model>
        member_cols: list[tuple[str, int | None, str]] = []  # (col_name, member, source)
        for key in hourly:
            if not key.startswith("temperature_2m"):
                continue
            if key == "temperature_2m":
                member_cols.append((key, 0, "mixed"))
                continue
            body = key[len("temperature_2m_") :]
            first, _, rest = body.partition("_")
            if first.startswith("member") and rest:
                try:
                    mem = int(first.removeprefix("member"))
                except ValueError:
                    mem = None
                member_cols.append((key, mem, rest))
            else:
                member_cols.append((key, 0, body))

        # Parse timestamps (ISO local to station.tz) and bucket by local date.
        parsed_times: list[datetime] = [datetime.fromisoformat(t) for t in times]

        run_time = _parse_runtime(raw) or fetched_at

        buckets: dict[tuple[date, str], EnsembleForecast] = {}
        for col_name, member, source in member_cols:
            series = hourly[col_name]
            daily: dict[date, float] = {}
            for t, v in zip(parsed_times, series):
                if v is None:
                    continue
                d = t.date()
                if d not in daily or v > daily[d]:
                    daily[d] = float(v)
            for d, v in daily.items():
                key = (d, source)
                ef = buckets.get(key)
                if ef is None:
                    ef = EnsembleForecast(
                        city=station.city_code, target_date=d, variable="tmax_f"
                    )
                    buckets[key] = ef
                ef.samples.append(
                    ForecastSample(
                        city=station.city_code,
                        target_date=d,
                        source=source,
                        member=member,
                        variable="tmax_f",
                        value=v,
                        fetched_at=fetched_at,
                        run_time=run_time,
                    )
                )

        # Merge same (date) across sources into a single EnsembleForecast so that
        # downstream consumers see one unified sample set per contract.
        merged: dict[date, EnsembleForecast] = {}
        for (d, _source), ef in buckets.items():
            if d not in merged:
                merged[d] = EnsembleForecast(city=station.city_code, target_date=d, variable="tmax_f")
            merged[d].samples.extend(ef.samples)

        return sorted(merged.values(), key=lambda e: e.target_date)


def _parse_runtime(raw: dict[str, Any]) -> datetime | None:
    # Open-Meteo returns 'generationtime_ms' and sometimes 'current' meta, but no explicit run time
    # in the free response. We treat fetched_at as a fallback; keep hook for future accuracy.
    gen_iso = raw.get("generationtime")
    if isinstance(gen_iso, str):
        try:
            return datetime.fromisoformat(gen_iso).replace(tzinfo=timezone.utc)
        except ValueError:
            return None
    return None
