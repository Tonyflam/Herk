"""Volatility primitives: realized vol (for vol-targeting) and ATR (for stops).

Pure functions over close/high/low arrays. No I/O, no config — easy to unit-test.
"""

from __future__ import annotations

import math

import numpy as np

HOURS_PER_YEAR = 24 * 365


def log_returns(closes) -> np.ndarray:
    a = np.asarray(closes, dtype=float)
    if a.size < 2 or np.any(a <= 0):
        a = a[a > 0]
        if a.size < 2:
            return np.array([])
    return np.diff(np.log(a))


def realized_vol_hourly(closes) -> float:
    """Std-dev of hourly log returns (per-hour vol)."""
    lr = log_returns(closes)
    if lr.size < 2:
        return 0.0
    return float(np.std(lr, ddof=1))


def realized_vol_annual(closes) -> float:
    """Annualized realized vol from hourly candles (for vol-targeting)."""
    return realized_vol_hourly(closes) * math.sqrt(HOURS_PER_YEAR)


def atr(highs, lows, closes, period: int = 14) -> float:
    """Average True Range in price units (Wilder TR, simple mean of last N)."""
    h = np.asarray(highs, dtype=float)
    l = np.asarray(lows, dtype=float)
    c = np.asarray(closes, dtype=float)
    n = min(h.size, l.size, c.size)
    if n == 0:
        return 0.0
    if n < period + 1:
        return float(np.mean(h[:n] - l[:n]))  # coarse fallback
    h, l, c = h[-(period + 1):], l[-(period + 1):], c[-(period + 1):]
    prev_c = c[:-1]
    tr = np.maximum(
        h[1:] - l[1:],
        np.maximum(np.abs(h[1:] - prev_c), np.abs(l[1:] - prev_c)),
    )
    return float(np.mean(tr))
