from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

from kalshi_weather_bot.edge.inspect import (
    EdgeRow,
    _build_rows,
    _ensembles_by_city_date,
    format_table,
    parse_event_date,
)
from kalshi_weather_bot.kalshi.models import Market
from kalshi_weather_bot.weather.models import EnsembleForecast, ForecastSample


def test_parse_event_date():
    assert parse_event_date("KXHIGHNY-26APR15") == date(2026, 4, 15)
    assert parse_event_date("KXHIGHCHI-26DEC01") == date(2026, 12, 1)
    assert parse_event_date("not-a-ticker") is None
    assert parse_event_date("KXHIGHNY-26ZZZ15") is None


NOW = datetime(2026, 4, 15, 12, 0, tzinfo=timezone.utc)


def _mkt(
    ticker: str,
    yes_bid: int,
    yes_ask: int,
    strike: float,
    *,
    series_ticker: str | None = "KXHIGHNY",
) -> Market:
    return Market(
        ticker=ticker,
        event_ticker="KXHIGHNY-26APR15",
        series_ticker=series_ticker,
        status="open",
        yes_bid=yes_bid,
        yes_ask=yes_ask,
        floor_strike=strike,
        strike_type="greater",
        expiration_time=NOW + timedelta(hours=24),
        close_time=NOW + timedelta(hours=24),
    )


def _forecast_for_ny_apr15(values: list[float]) -> EnsembleForecast:
    fetched = datetime(2026, 4, 14, tzinfo=timezone.utc)
    samples = [
        ForecastSample(
            city="NY",
            target_date=date(2026, 4, 15),
            source="gfs025",
            member=i,
            variable="tmax_f",
            value=v,
            fetched_at=fetched,
            run_time=fetched,
        )
        for i, v in enumerate(values)
    ]
    return EnsembleForecast(
        city="NY",
        target_date=date(2026, 4, 15),
        variable="tmax_f",
        samples=samples,
    )


def test_build_rows_computes_edges():
    markets = [_mkt("KXHIGHNY-26APR15-T80", yes_bid=40, yes_ask=46, strike=80.0)]
    ef = _forecast_for_ny_apr15([78.0, 80.0, 82.0, 84.0, 86.0] * 20)
    by_event_date = _ensembles_by_city_date({"NY": [ef]})
    rows = _build_rows(markets, by_event_date, edge_min=0.04, decay_hours=6.0, now=NOW)
    assert len(rows) == 1
    r = rows[0]
    assert r.ticker == "KXHIGHNY-26APR15-T80"
    assert r.target_date == date(2026, 4, 15)
    assert r.city == "NY"
    assert r.p_fair is not None
    assert r.edge_buy_yes is not None
    assert r.edge_buy_no is not None
    assert r.net_edge_buy_yes is not None
    assert r.effective_edge_min == 0.04  # h=24 is past decay window
    assert r.note == ""


def test_build_rows_missing_forecast_sets_note():
    markets = [_mkt("KXHIGHNY-26APR15-T80", yes_bid=40, yes_ask=46, strike=80.0)]
    rows = _build_rows(markets, {}, edge_min=0.04, decay_hours=6.0, now=NOW)
    assert len(rows) == 1
    assert rows[0].p_fair is None
    assert "no forecast" in rows[0].note


def test_build_rows_matches_forecast_when_series_ticker_missing():
    # Kalshi's /markets response sometimes omits series_ticker on the Market.
    # The event_ticker prefix ("KXHIGHNY-26APR15") should be the fallback.
    markets = [_mkt("KXHIGHNY-26APR15-T80", 40, 46, 80.0, series_ticker=None)]
    ef = _forecast_for_ny_apr15([78.0, 80.0, 82.0, 84.0, 86.0] * 20)
    by_event_date = _ensembles_by_city_date({"NY": [ef]})
    rows = _build_rows(markets, by_event_date, edge_min=0.04, decay_hours=6.0, now=NOW)
    assert rows[0].p_fair is not None
    assert rows[0].note == ""


def test_build_rows_flags_side_with_clear_edge():
    # Samples centered at 86 → P(X>80) is very high. Market yes_ask=46 → huge buy_yes edge.
    markets = [_mkt("KXHIGHNY-26APR15-T80", yes_bid=40, yes_ask=46, strike=80.0)]
    ef = _forecast_for_ny_apr15([84.0, 85.0, 86.0, 87.0, 88.0] * 20)
    by_event_date = _ensembles_by_city_date({"NY": [ef]})
    rows = _build_rows(markets, by_event_date, edge_min=0.04, decay_hours=6.0, now=NOW)
    assert rows[0].flagged_side == "buy_yes"


def test_format_table_renders_header_and_rows():
    rows = [
        EdgeRow(
            ticker="KXHIGHNY-26APR15-T80",
            event_ticker="KXHIGHNY-26APR15",
            city="NY",
            strike_desc="> 80.0",
            target_date=date(2026, 4, 15),
            hours_to_close=24.0,
            market_yes_bid=40,
            market_yes_ask=46,
            p_market_mid=0.43,
            p_fair=0.55,
            edge_buy_yes=0.09,
            edge_buy_no=-0.15,
            net_edge_buy_yes=0.07,
            net_edge_buy_no=-0.17,
            effective_edge_min=0.04,
            flagged_side="buy_yes",
            n_samples=100,
        )
    ]
    text = format_table(rows)
    assert "ticker" in text
    assert "KXHIGHNY-26APR15-T80" in text
    assert "2026-04-15" in text
    assert "BUY_YES" in text
