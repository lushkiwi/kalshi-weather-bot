from __future__ import annotations

import asyncio
from typing import Any

from kalshi_weather_bot.kalshi.client import KalshiClient
from kalshi_weather_bot.kalshi.models import Market, Orderbook
from kalshi_weather_bot.logging_setup import get_logger

log = get_logger("kalshi.markets")


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
    return _parse_orderbook(ticker, raw)


def _parse_orderbook(ticker: str, raw: dict[str, Any]) -> Orderbook:
    # Kalshi API returns either legacy "orderbook" (integer cents pairs)
    # or current "orderbook_fp" (decimal dollar-string pairs).
    fp = raw.get("orderbook_fp")
    if fp is not None:
        return _parse_fp_book(ticker, fp)
    book = raw.get("orderbook", raw)
    return _parse_legacy_book(ticker, book)


def _parse_fp_book(ticker: str, fp: dict[str, Any]) -> Orderbook:
    """Parse ``orderbook_fp`` format: ``yes_dollars``/``no_dollars`` with [price_str, size_str] pairs."""

    def levels(side: list[Any] | None) -> list[dict[str, int]]:
        if not side:
            return []
        out = []
        for price_str, size_str in side:
            cents = round(float(price_str) * 100)
            size = int(float(size_str))
            out.append({"price": cents, "size": size})
        return out

    return Orderbook.model_validate({
        "ticker": ticker,
        "yes": levels(fp.get("yes_dollars")),
        "no": levels(fp.get("no_dollars")),
    })


def _parse_legacy_book(ticker: str, book: dict[str, Any]) -> Orderbook:
    """Parse legacy ``orderbook`` format: ``yes``/``no`` with [price_cents, size] pairs."""

    def levels(side: list[Any] | None) -> list[dict[str, int]]:
        if not side:
            return []
        return [{"price": int(p), "size": int(s)} for p, s in side]

    return Orderbook.model_validate({
        "ticker": ticker,
        "yes": levels(book.get("yes")),
        "no": levels(book.get("no")),
    })


async def enrich_with_orderbooks(
    client: KalshiClient, markets: list[Market]
) -> None:
    """Fetch orderbooks and overlay bid/ask onto Market objects.

    The bulk GET /markets endpoint often returns null for yes_bid/yes_ask;
    the per-ticker orderbook endpoint has the real quotes.
    """

    async def _fetch_one(m: Market) -> None:
        try:
            book = await fetch_orderbook(client, m.ticker, depth=1)
            if m.yes_bid is None:
                m.yes_bid = book.best_yes_bid()
            if m.yes_ask is None:
                m.yes_ask = book.best_yes_ask()
        except Exception as exc:
            log.debug("orderbook_fetch_failed", ticker=m.ticker, error=str(exc))

    await asyncio.gather(*[_fetch_one(m) for m in markets])


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
