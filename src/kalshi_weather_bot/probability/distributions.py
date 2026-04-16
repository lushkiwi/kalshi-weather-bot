"""Sample-based tail probabilities for ensemble forecasts.

Regime selection per PLAN.md §3:
  - Far from threshold (|median - strike| > 2σ): Laplace-smoothed ECDF.
  - Near threshold: Gaussian KDE with Silverman's rule, then analytic CDF.

Time-horizon inflation ``k(h)`` scales σ before KDE fitting. Default is 1.0
until calibration (M6) provides per-horizon factors.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Sequence

import numpy as np
from scipy.stats import norm


# Boundary between ECDF regime and KDE regime, in units of σ.
NEAR_THRESHOLD_SIGMA = 2.0


def _sigma(samples: np.ndarray) -> float:
    if samples.size < 2:
        return 0.0
    return float(np.std(samples, ddof=1))


def _median(samples: np.ndarray) -> float:
    return float(np.median(samples))


def ecdf_prob_greater(samples: np.ndarray, strike: float) -> float:
    """P(X > strike) with Laplace smoothing: (#>strike + 0.5) / (N + 1)."""
    n = samples.size
    if n == 0:
        return 0.5
    gt = int(np.sum(samples > strike))
    return (gt + 0.5) / (n + 1)


def kde_prob_greater(samples: np.ndarray, strike: float, sigma_inflation: float = 1.0) -> float:
    """Gaussian KDE tail: P(X > strike) ≈ 1 - Σ_i Φ((strike - x_i) / h) / N.

    ``sigma_inflation`` multiplies the base Silverman bandwidth — used to
    inject the time-horizon correction factor k(h) learned during calibration.
    """
    n = samples.size
    if n == 0:
        return 0.5
    base = _silverman_bandwidth(samples)
    h = max(base * sigma_inflation, 1e-9)
    z = (strike - samples) / h
    cdf_at_strike = float(np.mean(norm.cdf(z)))
    return max(0.0, min(1.0, 1.0 - cdf_at_strike))


def kde_prob_between(
    samples: np.ndarray, low: float, high: float, sigma_inflation: float = 1.0
) -> float:
    """P(low <= X <= high) via Gaussian KDE."""
    if high < low:
        low, high = high, low
    n = samples.size
    if n == 0:
        return 0.0
    base = _silverman_bandwidth(samples)
    h = max(base * sigma_inflation, 1e-9)
    upper = float(np.mean(norm.cdf((high - samples) / h)))
    lower = float(np.mean(norm.cdf((low - samples) / h)))
    return max(0.0, min(1.0, upper - lower))


def _silverman_bandwidth(samples: np.ndarray) -> float:
    """Silverman's rule with IQR correction for peaked/non-Gaussian distributions.

    Uses min(std, IQR/1.34) as the scale estimate — the standard textbook
    improvement that avoids oversmoothing when the data is more concentrated
    than a Gaussian.
    """
    n = samples.size
    if n < 2:
        return 1.0
    std = _sigma(samples)
    if std <= 0:
        return 0.5
    iqr = float(np.percentile(samples, 75) - np.percentile(samples, 25))
    scale = min(std, iqr / 1.34) if iqr > 0 else std
    return 0.9 * scale * n ** (-1 / 5)


def prob_greater(
    samples: Sequence[float] | np.ndarray,
    strike: float,
    *,
    sigma_inflation: float = 1.0,
) -> float:
    """Public entry: P(X > strike) with automatic regime selection."""
    arr = np.asarray(samples, dtype=float)
    if arr.size == 0:
        return 0.5
    sigma = _sigma(arr)
    if sigma > 0 and abs(_median(arr) - strike) > NEAR_THRESHOLD_SIGMA * sigma:
        return ecdf_prob_greater(arr, strike)
    return kde_prob_greater(arr, strike, sigma_inflation=sigma_inflation)


def prob_less(
    samples: Sequence[float] | np.ndarray,
    strike: float,
    *,
    sigma_inflation: float = 1.0,
) -> float:
    """P(X < strike) — complement of prob_greater (with the strike itself a measure-zero set)."""
    return 1.0 - prob_greater(samples, strike, sigma_inflation=sigma_inflation)


def prob_between(
    samples: Sequence[float] | np.ndarray,
    low: float,
    high: float,
    *,
    sigma_inflation: float = 1.0,
) -> float:
    """P(low <= X <= high). Uses KDE near threshold, ECDF far away.

    "Near" here means either endpoint is within 2σ of the median.
    """
    arr = np.asarray(samples, dtype=float)
    if arr.size == 0:
        return 0.0
    sigma = _sigma(arr)
    med = _median(arr)
    far_from_both = sigma > 0 and min(abs(med - low), abs(med - high)) > NEAR_THRESHOLD_SIGMA * sigma
    if far_from_both:
        n = arr.size
        inside = int(np.sum((arr >= low) & (arr <= high)))
        return (inside + 0.5) / (n + 1)
    return kde_prob_between(arr, low, high, sigma_inflation=sigma_inflation)


# -------------------- Time-horizon inflation scaffolding --------------------


InflationFn = Callable[[float], float]


def default_inflation(_hours_to_close: float) -> float:
    return 1.0


def piecewise_inflation(table: dict[float, float]) -> InflationFn:
    """Step function: largest key <= h wins. ``table`` maps horizon-in-hours to k.

    Example: {0: 1.0, 24: 1.1, 72: 1.3} → 1.0 for 0<=h<24, 1.1 for 24<=h<72, 1.3 beyond.
    """
    keys = sorted(table)

    def fn(h: float) -> float:
        chosen = keys[0]
        for k in keys:
            if k <= h:
                chosen = k
            else:
                break
        return table[chosen]

    return fn


def hours_between(later_ts: float, earlier_ts: float) -> float:
    return max(0.0, (later_ts - earlier_ts) / 3600.0)


__all__ = [
    "NEAR_THRESHOLD_SIGMA",
    "default_inflation",
    "ecdf_prob_greater",
    "hours_between",
    "kde_prob_between",
    "kde_prob_greater",
    "piecewise_inflation",
    "prob_between",
    "prob_greater",
    "prob_less",
]
