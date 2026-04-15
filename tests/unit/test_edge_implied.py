from __future__ import annotations

from datetime import datetime, timezone

from kalshi_weather_bot.edge.implied import implied_from_market
from kalshi_weather_bot.kalshi.models import Market


def _mkt(yes_bid: int | None, yes_ask: int | None) -> Market:
    return Market(
        ticker="KXHIGHNY-26APR15-T80",
        event_ticker="KXHIGHNY-26APR15",
        series_ticker="KXHIGHNY",
        status="open",
        yes_bid=yes_bid,
        yes_ask=yes_ask,
        strike_type="greater",
        floor_strike=80.0,
        expiration_time=datetime(2026, 4, 16, tzinfo=timezone.utc),
    )


def test_implied_midpoint_and_sides():
    imp = implied_from_market(_mkt(40, 46))
    assert imp.yes_bid == 40
    assert imp.yes_ask == 46
    assert imp.mid == 0.43  # (40+46)/200
    assert imp.p_buy_yes() == 0.46
    assert imp.p_buy_no() == 0.60


def test_implied_one_sided_book_has_no_mid():
    imp = implied_from_market(_mkt(None, 46))
    assert imp.mid is None
    assert imp.p_buy_yes() == 0.46
    assert imp.p_buy_no() is None


def test_implied_empty_book():
    imp = implied_from_market(_mkt(None, None))
    assert imp.mid is None
    assert imp.p_buy_yes() is None
    assert imp.p_buy_no() is None
