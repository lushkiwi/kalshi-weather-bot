from __future__ import annotations

import asyncio
import time
from typing import Any

import httpx
from tenacity import AsyncRetrying, retry_if_exception_type, stop_after_attempt, wait_exponential

from kalshi_weather_bot.kalshi.auth import build_auth_headers, load_private_key
from kalshi_weather_bot.logging_setup import get_logger


PROD_BASE = "https://api.elections.kalshi.com/trade-api/v2"
DEMO_BASE = "https://demo-api.kalshi.co/trade-api/v2"


class _TokenBucket:
    """Simple async token bucket for req/sec throttling."""

    def __init__(self, rate_per_sec: float, burst: int | None = None) -> None:
        self._rate = rate_per_sec
        self._capacity = burst if burst is not None else max(1, int(rate_per_sec))
        self._tokens = float(self._capacity)
        self._last = time.monotonic()
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        async with self._lock:
            while True:
                now = time.monotonic()
                elapsed = now - self._last
                self._last = now
                self._tokens = min(self._capacity, self._tokens + elapsed * self._rate)
                if self._tokens >= 1:
                    self._tokens -= 1
                    return
                needed = (1 - self._tokens) / self._rate
                await asyncio.sleep(needed)


class KalshiClient:
    """Async Kalshi v2 client. Signs every request; respects rate limit; retries transient errors."""

    def __init__(
        self,
        key_id: str,
        private_key_pem: str,
        *,
        env: str = "demo",
        rate_limit_per_sec: float = 8.0,
        timeout_sec: float = 15.0,
    ) -> None:
        self._base = DEMO_BASE if env == "demo" else PROD_BASE
        self._key_id = key_id
        self._private_key = load_private_key(private_key_pem)
        self._bucket = _TokenBucket(rate_limit_per_sec)
        self._client = httpx.AsyncClient(
            base_url=self._base,
            timeout=timeout_sec,
            http2=True,
        )
        self._log = get_logger("kalshi.client").bind(env=env)

    @property
    def base_url(self) -> str:
        return self._base

    async def aclose(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> "KalshiClient":
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    def _path_for_sign(self, url_path: str) -> str:
        # The signed path must include the /trade-api/v2 prefix (full path from host).
        base_path = self._base.split("//", 1)[1].split("/", 1)[1]
        return f"/{base_path}{url_path}"

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        await self._bucket.acquire()
        sign_path = self._path_for_sign(path)
        headers = build_auth_headers(self._key_id, self._private_key, method, sign_path)

        async for attempt in AsyncRetrying(
            stop=stop_after_attempt(4),
            wait=wait_exponential(multiplier=0.5, min=0.5, max=8),
            retry=retry_if_exception_type((httpx.TransportError,)),
            reraise=True,
        ):
            with attempt:
                response = await self._client.request(
                    method, path, params=params, json=json, headers=headers
                )
                if response.status_code >= 500:
                    self._log.warning(
                        "kalshi_5xx", status=response.status_code, path=path, body=response.text[:500]
                    )
                    response.raise_for_status()
                if response.status_code >= 400:
                    raise httpx.HTTPStatusError(
                        f"{response.status_code} for {path}: {response.text[:500]}",
                        request=response.request,
                        response=response,
                    )
                return response.json()
        raise RuntimeError("unreachable")

    # ----------------------------- Markets ---------------------------------

    async def get_markets(
        self,
        *,
        series_ticker: str | None = None,
        event_ticker: str | None = None,
        status: str | None = "open",
        limit: int = 200,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {"limit": limit}
        if series_ticker:
            params["series_ticker"] = series_ticker
        if event_ticker:
            params["event_ticker"] = event_ticker
        if status:
            params["status"] = status
        if cursor:
            params["cursor"] = cursor
        return await self._request("GET", "/markets", params=params)

    async def get_market(self, ticker: str) -> dict[str, Any]:
        return await self._request("GET", f"/markets/{ticker}")

    async def get_orderbook(self, ticker: str, depth: int = 5) -> dict[str, Any]:
        return await self._request("GET", f"/markets/{ticker}/orderbook", params={"depth": depth})

    async def get_events(
        self, *, series_ticker: str | None = None, status: str | None = "open", limit: int = 200
    ) -> dict[str, Any]:
        params: dict[str, Any] = {"limit": limit}
        if series_ticker:
            params["series_ticker"] = series_ticker
        if status:
            params["status"] = status
        return await self._request("GET", "/events", params=params)

    # ----------------------------- Portfolio --------------------------------

    async def get_positions(self) -> dict[str, Any]:
        return await self._request("GET", "/portfolio/positions")

    async def get_fills(
        self,
        *,
        min_ts: int | None = None,
        order_id: str | None = None,
        ticker: str | None = None,
        limit: int = 500,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {"limit": limit}
        if min_ts is not None:
            params["min_ts"] = min_ts
        if order_id is not None:
            params["order_id"] = order_id
        if ticker is not None:
            params["ticker"] = ticker
        return await self._request("GET", "/portfolio/fills", params=params)

    async def post_order(self, payload: dict[str, Any]) -> dict[str, Any]:
        return await self._request("POST", "/portfolio/orders", json=payload)

    async def cancel_order(self, order_id: str) -> dict[str, Any]:
        return await self._request("DELETE", f"/portfolio/orders/{order_id}")

    async def get_order(self, order_id: str) -> dict[str, Any]:
        return await self._request("GET", f"/portfolio/orders/{order_id}")
