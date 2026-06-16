"""Momentum primitives: per-symbol lookback returns, vol-adjusted, then ranked
cross-sectionally so the score is *relative strength*, not absolute drift.

Pure functions; the engine wires these across the universe.
"""

from __future__ import annotations

import math

import numpy as np


def lookback_return(closes, hours: int) -> float | None:
    """Simple return over ``hours`` bars (assumes 1h candles)."""
    a = np.asarray(closes, dtype=float)
    if a.size < hours + 1:
        return None
    base = a[-1 - hours]
    if base <= 0:
        return None
    return float(a[-1] / base - 1.0)


def vol_adjusted_return(r: float | None, vol_hourly: float, hours: int) -> float | None:
    """Scale a raw return by its horizon vol → an information-ratio-like number."""
    if r is None:
        return None
    denom = vol_hourly * math.sqrt(hours)
    if denom <= 0:
        return 0.0
    return r / denom


def zscore(values: list[float]) -> np.ndarray:
    """Cross-sectional z-score (population spread), zeros if degenerate."""
    arr = np.asarray(values, dtype=float)
    if arr.size == 0:
        return arr
    mu = float(np.nanmean(arr))
    sd = float(np.nanstd(arr, ddof=1)) if arr.size > 1 else 0.0
    if sd <= 0 or math.isnan(sd):
        return np.zeros_like(arr)
    return (arr - mu) / sd
