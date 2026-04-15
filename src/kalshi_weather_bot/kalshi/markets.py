from __future__ import annotations

from typing import Any

from kalshi_weather_bot.kalshi.client import KalshiClient
from kalshi_weather_bot.kalshi.models import Market, Orderbook


async def list_markets_for_series(client: KalshiClient, series_ticker: str) -> list[Market]:
    markets: list[Market] = []
    cursor: str | None = None
    while True:
        page = await client.get_markets(series_ticker=series_ticker, status="open", cursor=cursor)
        for raw in page.get("markets", []):
            markets.append(Market.model_validate(raw))
        cursor = page.get("cursor") or None
        if not cursor:
            break
    return markets


async def list_active_weather_markets(
    client: KalshiClient, series_tickers: list[str]
) -> list[Market]:
    out: list[Market] = []
    for series in series_tickers:
        out.extend(await list_markets_for_series(client, series))
    return out


async def fetch_orderbook(client: KalshiClient, ticker: str, depth: int = 5) -> Orderbook:
    raw = await client.get_orderbook(ticker, depth=depth)
    book = raw.get("orderbook", raw)
    return _parse_orderbook(ticker, book)


def _parse_orderbook(ticker: str, raw: dict[str, Any]) -> Orderbook:
    def levels(side: list[Any] | None) -> list[dict[str, int]]:
        if not side:
            return []
        # Kalshi returns pairs like [price, size] per level.
        return [{"price": int(p), "size": int(s)} for p, s in side]

    return Orderbook.model_validate(
        {"ticker": ticker, "yes": levels(raw.get("yes")), "no": levels(raw.get("no"))}
    )


def parse_weather_ticker(ticker: str) -> dict[str, str | float] | None:
    """Parse a KXHIGH{CITY}-YYMMMDD-{T|B}{strike} ticker into its components.

    Examples:
        KXHIGHNY-26APR15-T90    -> {series: KXHIGHNY, city: NY, date: 26APR15, kind: T, strike: 90}
        KXHIGHCHI-26APR15-B79.5 -> {series: KXHIGHCHI, city: CHI, date: 26APR15, kind: B, strike: 79.5}
    """
    parts = ticker.split("-")
    if len(parts) != 3:
        return None
    series, date_str, tail = parts
    if not series.startswith("KXHIGH") or not tail:
        return None
    kind = tail[0]
    if kind not in ("T", "B"):
        return None
    try:
        strike = float(tail[1:])
    except ValueError:
        return None
    return {
        "series": series,
        "city": series.removeprefix("KXHIGH"),
        "date": date_str,
        "kind": kind,
        "strike": strike,
    }
