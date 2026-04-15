from __future__ import annotations

import math

import pytest

from kalshi_weather_bot.edge.fees import (
    maker_fee_cents,
    maker_fee_rate,
    taker_fee_cents,
    taker_fee_rate,
)


def test_taker_fee_peak_single_contract():
    # At P=0.5, C=1: 0.07 * 1 * 0.25 * 100 = 1.75 → ceil = 2 cents.
    assert taker_fee_cents(1, 0.5) == 2


def test_taker_fee_peak_100_contracts():
    # 0.07 * 100 * 0.25 * 100 = 175 → ceil = 175.
    assert taker_fee_cents(100, 0.5) == 175


def test_taker_fee_ceiling_per_order_not_per_contract():
    # 10 contracts at P=0.2: 0.07 * 10 * 0.2 * 0.8 * 100 = 11.2 → ceil = 12.
    assert taker_fee_cents(10, 0.2) == 12
    # Per-contract then summed (wrong) would be: ceil(0.07*1*0.16*100)=2, *10=20.
    # Per-order ceiling is strictly cheaper.
    assert taker_fee_cents(10, 0.2) < 10 * taker_fee_cents(1, 0.2)


def test_taker_fee_zero_at_endpoints():
    # P=0 or P=1 → P*(1-P)=0 → no fee.
    assert taker_fee_cents(100, 0.0) == 0
    assert taker_fee_cents(100, 1.0) == 0


def test_maker_fee_is_quarter_of_taker():
    # 0.0175 / 0.07 = 0.25. At high C, rounding vanishes and ratio holds.
    assert maker_fee_cents(1000, 0.5) == math.ceil(0.0175 * 1000 * 0.25 * 100)


def test_maker_fee_peak_single_contract():
    # 0.0175 * 1 * 0.25 * 100 = 0.4375 → ceil = 1 cent.
    assert maker_fee_cents(1, 0.5) == 1


def test_taker_fee_rate_is_fraction():
    assert taker_fee_rate(0.5) == 0.02
    assert taker_fee_rate(0.0) == 0.0
    assert taker_fee_rate(1.0) == 0.0


def test_maker_fee_rate_is_fraction():
    assert maker_fee_rate(0.5) == 0.01


def test_taker_fee_symmetric_in_price():
    for p in (0.1, 0.2, 0.3, 0.4):
        assert taker_fee_cents(100, p) == taker_fee_cents(100, 1.0 - p)


def test_taker_fee_zero_contracts():
    assert taker_fee_cents(0, 0.5) == 0
    assert taker_fee_cents(-5, 0.5) == 0


def test_invalid_price_raises():
    with pytest.raises(ValueError):
        taker_fee_cents(1, -0.1)
    with pytest.raises(ValueError):
        taker_fee_cents(1, 1.5)
    with pytest.raises(ValueError):
        taker_fee_rate(2.0)
