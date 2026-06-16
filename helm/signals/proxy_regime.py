"""No-lookahead Fear & Greed proxy reconstructed from price history.

The live stack reads the Fear & Greed Index from alternative.me / CMC. That feed
has no historical replay through our fetchers, so the walk-forward backtest has
always run the regime overlay *neutral* — meaning HELM's live de-risking lever
was never validated against history. That is a silent risk: an overlay that
de-risks into a V-recovery would "survive" yet lose the return race.

This module reconstructs a faithful proxy from OHLCV alone (no lookahead) so the
overlay can be replayed and stress-tested. It is built only from components that
are honestly reconstructable from public candles:

  * momentum   — price vs its own trailing 7-day mean (greed above, fear below)
  * volatility — recent realized vol vs a longer baseline (fear when elevated)
  * breadth    — share of the universe trading above its own 7-day mean

These mirror the dominant, price-derived inputs of the published index
(momentum + volatility) plus breadth. The survey/social/dominance sub-indices
are deliberately omitted — they cannot be honestly reconstructed from candles.

The same function doubles as a live fallback: if the F&G feed is unavailable,
HELM can still form a regime view from price action instead of defaulting blind.
"""

from __future__ import annotations

import math

_DAY = 24
_WEEK = 168

# momentum premium/discount vs the 7d mean that spans the full 0-100 range
_MOM_FULL_SCALE = 0.15


def classify(fg: int) -> str:
    """Map a 0-100 score to alternative.me-style bands."""
    if fg <= 24:
        return "Extreme Fear"
    if fg <= 44:
        return "Fear"
    if fg <= 55:
        return "Neutral"
    if fg <= 75:
        return "Greed"
    return "Extreme Greed"


def _realized_vol(closes: list[float]) -> float:
    """Std-dev of hourly log returns over the supplied window (no annualizing)."""
    if len(closes) < 3:
        return 0.0
    rets = [
        math.log(closes[i] / closes[i - 1])
        for i in range(1, len(closes))
        if closes[i - 1] > 0 and closes[i] > 0
    ]
    if len(rets) < 2:
        return 0.0
    mean = sum(rets) / len(rets)
    var = sum((r - mean) ** 2 for r in rets) / (len(rets) - 1)
    return math.sqrt(var)


def _clamp(x: float, lo: float = 0.0, hi: float = 100.0) -> float:
    return max(lo, min(hi, x))


def proxy_fear_greed(
    btc_closes: list[float], breadth_frac: float | None = None
) -> tuple[int, str]:
    """Reconstruct a 0-100 Fear & Greed proxy from BTC closes (+ optional breadth).

    Uses only data up to the final element — strictly no lookahead. ``breadth_frac``
    is the share (0..1) of the tradeable universe trading above its own trailing
    7-day mean at the same instant; pass ``None`` to fall back to neutral breadth.

    Returns ``(value, classification)``.
    """
    if len(btc_closes) < _WEEK:
        return 50, "Neutral"
    last = btc_closes[-1]

    # --- momentum: close vs trailing 7d mean (±15% spans the full range) -----
    sma_week = sum(btc_closes[-_WEEK:]) / _WEEK
    mom = (last / sma_week - 1.0) if sma_week > 0 else 0.0
    mom_score = 50.0 + (mom / _MOM_FULL_SCALE) * 50.0

    # --- volatility: recent 24h realized vol vs trailing 7d baseline ---------
    recent_vol = _realized_vol(btc_closes[-_DAY:])
    base_vol = _realized_vol(btc_closes[-_WEEK:])
    ratio = (recent_vol / base_vol) if base_vol > 1e-9 else 1.0
    # calm (ratio < 1) → greed; spiking (ratio > 1) → fear.
    vol_score = 50.0 - (ratio - 1.0) * 50.0

    # --- breadth: share of universe above its own 7d mean --------------------
    breadth_score = (breadth_frac * 100.0) if breadth_frac is not None else 50.0

    fg = (
        0.40 * _clamp(mom_score)
        + 0.35 * _clamp(vol_score)
        + 0.25 * _clamp(breadth_score)
    )
    fg_i = int(round(_clamp(fg)))
    return fg_i, classify(fg_i)
