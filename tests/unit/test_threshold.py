from __future__ import annotations

from datetime import datetime, timezone

import numpy as np
import pytest

from kalshi_weather_bot.kalshi.models import Market
from kalshi_weather_bot.probability.threshold import (
    ThresholdError,
    contract_probability,
    event_coherence_error,
)


def _mkt(ticker: str, strike_type: str, floor: float | None, cap: float | None = None) -> Market:
    return Market(
        ticker=ticker,
        event_ticker=ticker.rsplit("-", 1)[0],
        series_ticker="KXHIGHNY",
        status="open",
        floor_strike=floor,
        cap_strike=cap,
        strike_type=strike_type,  # type: ignore[arg-type]
        expiration_time=datetime(2026, 4, 16, tzinfo=timezone.utc),
    )


def test_contract_probability_greater():
    samples = [70.0, 75.0, 80.0, 85.0, 90.0] * 20
    m = _mkt("KXHIGHNY-26APR15-T79.5", "greater", 79.5)
    fair = contract_probability(m, samples)
    assert fair.method == "greater"
    assert fair.n_samples == 100
    assert 0.5 < fair.p_fair < 0.7


def test_contract_probability_less_is_complement_of_greater():
    samples = [70.0, 75.0, 80.0, 85.0, 90.0] * 20
    m_g = _mkt("KXHIGHNY-26APR15-T79.5", "greater", 79.5)
    m_l = _mkt("KXHIGHNY-26APR15-L79.5", "less", 79.5)
    p_g = contract_probability(m_g, samples).p_fair
    p_l = contract_probability(m_l, samples).p_fair
    assert abs((p_g + p_l) - 1.0) < 1e-9


def test_contract_probability_between():
    samples = list(range(70, 91)) * 5
    m = _mkt("KXHIGHNY-26APR15-B79.5", "between", 75.0, 85.0)
    fair = contract_probability(m, samples)
    assert fair.method == "between"
    assert 0.0 <= fair.p_fair <= 1.0


def test_missing_floor_raises():
    m = _mkt("KXHIGHNY-26APR15-T", "greater", None)
    with pytest.raises(ThresholdError):
        contract_probability(m, [70.0, 75.0, 80.0])


def test_between_missing_cap_raises():
    m = _mkt("KXHIGHNY-26APR15-B", "between", 75.0, None)
    with pytest.raises(ThresholdError):
        contract_probability(m, [70.0, 75.0, 80.0])


def test_unsupported_strike_type_raises():
    m = _mkt("KXHIGHNY-26APR15-X", "structured", 75.0)
    with pytest.raises(ThresholdError):
        contract_probability(m, [70.0, 75.0, 80.0])


def test_ladder_sums_to_one():
    # Build a complete tiled ladder: (-inf, 80), [80, 85], [85, 90], (90, inf)
    rng = np.random.default_rng(42)
    samples = list(rng.normal(loc=83.0, scale=4.0, size=200))

    mkts = [
        _mkt("KXHIGHNY-26APR15-L80", "less", 80.0),
        _mkt("KXHIGHNY-26APR15-B82.5", "between", 80.0, 85.0),
        _mkt("KXHIGHNY-26APR15-B87.5", "between", 85.0, 90.0),
        _mkt("KXHIGHNY-26APR15-T90", "greater", 90.0),
    ]
    probs = [contract_probability(m, samples) for m in mkts]
    err = event_coherence_error(probs)
    # Tiling should be coherent within a small tolerance (KDE vs ECDF mixing).
    assert abs(err) < 0.05


def test_greater_monotonic_in_strike():
    samples = list(range(70, 91))
    strikes = [75.0, 80.0, 85.0, 89.0]
    probs = [
        contract_probability(_mkt(f"T{s}", "greater", s), samples).p_fair for s in strikes
    ]
    for a, b in zip(probs, probs[1:]):
        assert a >= b - 1e-9
