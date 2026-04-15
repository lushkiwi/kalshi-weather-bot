from __future__ import annotations

import numpy as np
from hypothesis import given, settings
from hypothesis import strategies as st

from kalshi_weather_bot.probability.distributions import (
    ecdf_prob_greater,
    kde_prob_between,
    kde_prob_greater,
    piecewise_inflation,
    prob_between,
    prob_greater,
    prob_less,
)


def test_prob_greater_far_above_threshold():
    samples = [90.0, 91.0, 92.0, 93.0, 94.0]
    # Strike far below median → ECDF path, should be very high.
    assert prob_greater(samples, 70.0) > 0.9


def test_prob_greater_far_below_threshold():
    samples = [70.0, 71.0, 72.0, 73.0, 74.0]
    assert prob_greater(samples, 90.0) < 0.1


def test_prob_greater_at_median_is_near_half():
    rng = np.random.default_rng(0)
    samples = rng.normal(loc=80.0, scale=3.0, size=200)
    p = prob_greater(samples, 80.0)
    assert 0.4 < p < 0.6


def test_prob_less_is_complement():
    samples = [70.0, 75.0, 80.0, 85.0, 90.0, 95.0]
    for strike in (72.5, 80.0, 88.0):
        assert abs(prob_greater(samples, strike) + prob_less(samples, strike) - 1.0) < 1e-9


def test_prob_between_bounds():
    samples = list(range(70, 91))
    p = prob_between(samples, 75.0, 85.0)
    assert 0.0 <= p <= 1.0
    # Should be roughly (85-75)/(90-70) = 0.5 ish.
    assert 0.3 < p < 0.7


def test_prob_between_swapped_endpoints():
    samples = [70.0, 75.0, 80.0, 85.0, 90.0]
    a = kde_prob_between(np.asarray(samples), 75.0, 85.0)
    b = kde_prob_between(np.asarray(samples), 85.0, 75.0)
    assert abs(a - b) < 1e-9


def test_ecdf_laplace_smoothing_never_zero_or_one():
    samples = np.array([70.0, 71.0, 72.0])
    assert 0.0 < ecdf_prob_greater(samples, 100.0) < 1.0
    assert 0.0 < ecdf_prob_greater(samples, 0.0) < 1.0


def test_empty_samples_graceful():
    assert prob_greater([], 80.0) == 0.5
    assert prob_less([], 80.0) == 0.5
    assert prob_between([], 70.0, 90.0) == 0.0


def test_kde_monotonic_in_strike():
    rng = np.random.default_rng(1)
    samples = rng.normal(loc=80.0, scale=3.0, size=100)
    strikes = [75.0, 78.0, 80.0, 82.0, 85.0]
    probs = [kde_prob_greater(samples, s) for s in strikes]
    for a, b in zip(probs, probs[1:]):
        assert a >= b - 1e-9


@given(
    samples=st.lists(st.floats(min_value=-50.0, max_value=150.0), min_size=5, max_size=50),
    strike=st.floats(min_value=-50.0, max_value=150.0),
)
@settings(max_examples=100, deadline=None)
def test_prob_greater_in_unit_interval(samples, strike):
    p = prob_greater(samples, strike)
    assert 0.0 <= p <= 1.0


@given(
    samples=st.lists(st.floats(min_value=-50.0, max_value=150.0), min_size=5, max_size=50),
    low=st.floats(min_value=-50.0, max_value=50.0),
    width=st.floats(min_value=0.1, max_value=50.0),
)
@settings(max_examples=100, deadline=None)
def test_prob_between_in_unit_interval(samples, low, width):
    p = prob_between(samples, low, low + width)
    assert 0.0 <= p <= 1.0


def test_piecewise_inflation():
    fn = piecewise_inflation({0.0: 1.0, 24.0: 1.1, 72.0: 1.3})
    assert fn(0.0) == 1.0
    assert fn(12.0) == 1.0
    assert fn(24.0) == 1.1
    assert fn(48.0) == 1.1
    assert fn(72.0) == 1.3
    assert fn(200.0) == 1.3
