from __future__ import annotations

import json
from pathlib import Path

from kalshi_weather_bot.kalshi.markets import _parse_orderbook, parse_weather_ticker
from kalshi_weather_bot.kalshi.models import Market


def test_parse_weather_ticker_greater() -> None:
    result = parse_weather_ticker("KXHIGHNY-26APR15-T90")
    assert result == {
        "series": "KXHIGHNY",
        "city": "NY",
        "date": "26APR15",
        "kind": "T",
        "strike": 90.0,
    }


def test_parse_weather_ticker_between() -> None:
    result = parse_weather_ticker("KXHIGHCHI-26APR15-B79.5")
    assert result is not None
    assert result["city"] == "CHI"
    assert result["kind"] == "B"
    assert result["strike"] == 79.5


def test_parse_weather_ticker_invalid() -> None:
    assert parse_weather_ticker("NOTAKALSHIWEATHER") is None
    assert parse_weather_ticker("KXHIGHNY-26APR15") is None
    assert parse_weather_ticker("KXHIGHNY-26APR15-X90") is None


def test_parse_orderbook_best_prices() -> None:
    raw = {"yes": [[40, 100], [39, 50]], "no": [[55, 200], [54, 75]]}
    book = _parse_orderbook("KXHIGHNY-26APR15-T90", raw)
    assert book.best_yes_bid() == 40
    assert book.best_no_bid() == 55
    assert book.best_yes_ask() == 45  # 100 - best_no_bid


def test_parse_orderbook_fp_format() -> None:
    raw = {
        "orderbook_fp": {
            "yes_dollars": [["0.0500", "426.00"], ["0.0600", "66.00"]],
            "no_dollars": [["0.9000", "13.00"], ["0.9100", "250.00"]],
        }
    }
    book = _parse_orderbook("KXHIGHNY-26APR16-T91", raw)
    assert book.best_yes_bid() == 6
    assert book.best_no_bid() == 91
    assert book.best_yes_ask() == 9  # 100 - 91


def test_market_models_validate_fixture(fixtures_dir: Path) -> None:
    data = json.loads((fixtures_dir / "kalshi_markets.json").read_text())
    markets = [Market.model_validate(m) for m in data["markets"]]
    assert len(markets) == 2
    assert markets[0].ticker == "KXHIGHNY-26APR15-T90"
    assert markets[0].strike_type == "greater"
    assert markets[0].floor_strike == 90
    assert markets[1].strike_type == "between"
    assert markets[1].floor_strike == 79
    assert markets[1].cap_strike == 80
