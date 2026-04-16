"""Real-money broker that submits orders to Kalshi.

Taker-only for v1: posts a limit at the opposite-side BBO with a
client-generated idempotency key. Kalshi's match engine executes
immediately when the price crosses the book; anything left resting
is canceled before we return. Keeping the broker surface area
identical to ``PaperBroker`` means the router doesn't know or care
which one it has.

Fills and orders are mirrored into the recorder DB so reconciliation
against ``GET /portfolio/positions`` stays auditable.
"""

from __future__ import annotations

import asyncio
from collections import defaultdict
from datetime import datetime
from typing import Any

from kalshi_weather_bot.edge.detector import Candidate
from kalshi_weather_bot.edge.fees import taker_fee_cents
from kalshi_weather_bot.execution.broker import (
    BrokerFill,
    client_order_id,
    event_from_ticker,
)
from kalshi_weather_bot.kalshi.client import KalshiClient
from kalshi_weather_bot.kalshi.models import Market
from kalshi_weather_bot.logging_setup import get_logger
from kalshi_weather_bot.recorder.db import Recorder
from kalshi_weather_bot.risk.limits import PortfolioState


log = get_logger("kalshi.orders")

# Poll budget while waiting for a crossing limit to fully execute. Kalshi's
# demo engine usually fills immediately; anything past this is aborted.
_FILL_POLL_ATTEMPTS = 3
_FILL_POLL_INTERVAL_SEC = 0.6


class KalshiBroker:
    """Taker-only broker backed by Kalshi's ``/portfolio/orders`` endpoint."""

    def __init__(
        self, client: KalshiClient, recorder: Recorder, *, mode: str = "demo"
    ) -> None:
        self._client = client
        self._rec = recorder
        self._mode = mode
        self._log = log.bind(mode=mode)

    async def taker_buy(
        self,
        *,
        candidate: Candidate,
        market: Market,
        size: int,
        tick_id: str,
        now: datetime,
    ) -> BrokerFill | None:
        if size <= 0:
            return None
        if candidate.side == "buy_yes":
            if market.yes_ask is None:
                return None
            yes_price, side = market.yes_ask, "yes"
        elif candidate.side == "buy_no":
            if market.yes_bid is None:
                return None
            yes_price, side = 100 - market.yes_bid, "no"
        else:
            return None

        coid = client_order_id(tick_id, market.ticker, candidate.side)
        payload: dict[str, Any] = {
            "ticker": market.ticker,
            "client_order_id": coid,
            "type": "limit",
            "action": "buy",
            "side": side,
            "count": size,
        }
        # Kalshi accepts yes_price for both sides on a buy; buying NO at 40
        # means yes_price=60 (the implied opposite-side price).
        payload["yes_price"] = yes_price if side == "yes" else 100 - yes_price
        ts = int(now.timestamp())

        # Record the intent before the network round-trip so a crash still
        # leaves us able to audit what we tried to do.
        await self._rec.execute(
            "INSERT OR REPLACE INTO orders (client_order_id, ticker, side, action, "
            "count, yes_price, status, created_ts, mode) "
            "VALUES (?, ?, ?, ?, ?, ?, 'submitted', ?, ?)",
            (coid, market.ticker, side, "buy", size, yes_price, ts, self._mode),
        )
        await self._rec.commit()

        try:
            response = await self._client.post_order(payload)
        except Exception as e:
            self._log.error(
                "order_post_failed", ticker=market.ticker, coid=coid, error=str(e)
            )
            await self._rec.execute(
                "UPDATE orders SET status = 'rejected', resolved_ts = ? "
                "WHERE client_order_id = ?",
                (ts, coid),
            )
            await self._rec.commit()
            return None

        order = response.get("order") or {}
        order_id = order.get("order_id")
        if not order_id:
            self._log.warning("order_post_no_id", response=response)
            return None

        filled, fill_price = await self._await_fill(
            order_id=order_id, requested=size, fallback_price=yes_price
        )

        # Anything unfilled: cancel so we don't leave a resting limit behind.
        if filled < size:
            try:
                await self._client.cancel_order(order_id)
            except Exception as e:
                self._log.warning(
                    "order_cancel_failed", order_id=order_id, error=str(e)
                )

        if filled == 0:
            await self._rec.execute(
                "UPDATE orders SET status = 'canceled', kalshi_order_id = ?, "
                "resolved_ts = ? WHERE client_order_id = ?",
                (order_id, ts, coid),
            )
            await self._rec.commit()
            return None

        status = "filled" if filled == size else "partial"
        fee = taker_fee_cents(filled, fill_price / 100.0)
        await self._rec.execute(
            "UPDATE orders SET kalshi_order_id = ?, count = ?, yes_price = ?, "
            "status = ?, resolved_ts = ? WHERE client_order_id = ?",
            (order_id, filled, fill_price, status, ts, coid),
        )
        await self._rec.execute(
            "INSERT INTO fills (kalshi_order_id, ticker, count, yes_price, fee_cents, "
            "filled_ts, mode) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (order_id, market.ticker, filled, fill_price, fee, ts, self._mode),
        )
        await self._rec.commit()

        return BrokerFill(
            client_order_id=coid,
            kalshi_order_id=order_id,
            ticker=market.ticker,
            side=side,
            count=filled,
            yes_price_cents=fill_price,
            fee_cents=fee,
            filled_ts=ts,
        )

    async def _await_fill(
        self, *, order_id: str, requested: int, fallback_price: int
    ) -> tuple[int, int]:
        """Poll /portfolio/fills for ``order_id`` until fully filled or budget exhausted.

        Returns ``(filled_count, avg_yes_price_cents)``. The YES-side price
        is the per-contract cost in cents; for a NO buy we store
        ``100 − yes_price`` so the accounting stays uniform with the
        paper broker.
        """
        best_filled = 0
        last_price = fallback_price
        for _ in range(_FILL_POLL_ATTEMPTS):
            try:
                resp = await self._client.get_fills(order_id=order_id, limit=50)
            except Exception as e:
                self._log.warning(
                    "fills_poll_failed", order_id=order_id, error=str(e)
                )
                await asyncio.sleep(_FILL_POLL_INTERVAL_SEC)
                continue

            fills = resp.get("fills") or []
            total_count = 0
            weighted_price = 0
            for f in fills:
                c = int(f.get("count", 0))
                if c <= 0:
                    continue
                # Kalshi reports the per-contract fill price on the side
                # the order was for. A NO buy pays (100 - yes_price).
                if f.get("side") == "no":
                    price = int(f.get("no_price", 100 - fallback_price))
                else:
                    price = int(f.get("yes_price", fallback_price))
                weighted_price += price * c
                total_count += c

            if total_count > 0:
                best_filled = total_count
                last_price = weighted_price // total_count
                if total_count >= requested:
                    return total_count, last_price
            await asyncio.sleep(_FILL_POLL_INTERVAL_SEC)
        return best_filled, last_price

    async def load_portfolio(self) -> PortfolioState:
        """Build PortfolioState from Kalshi's source-of-truth ``/portfolio/positions``."""
        try:
            resp = await self._client.get_positions()
        except Exception as e:
            self._log.error("positions_fetch_failed", error=str(e))
            raise

        state = PortfolioState()
        events: dict[str, int] = defaultdict(int)
        for pos in resp.get("market_positions") or resp.get("positions") or []:
            ticker = pos.get("ticker")
            count = int(pos.get("position", 0))
            if not ticker or count == 0:
                continue
            # Kalshi reports signed contracts; long YES is positive, long NO
            # is negative. For the per-market cap we only care about gross.
            gross = abs(count)
            state.contracts_per_ticker[ticker] = (
                state.contracts_per_ticker.get(ticker, 0) + gross
            )
            events[event_from_ticker(ticker)] += gross
            state.total_contracts += gross

        state.contracts_per_event = dict(events)
        return state


__all__ = ["KalshiBroker"]
