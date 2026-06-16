"""Proxy Fear & Greed reconstruction — pins the no-lookahead regime proxy used
to validate (and, live, to back up) HELM's de-risking overlay.
"""

from __future__ import annotations

import math

from helm.signals.proxy_regime import classify, proxy_fear_greed


def test_short_series_is_neutral():
    # Fewer than a week of hourly closes → no honest read → neutral.
    assert proxy_fear_greed([100.0] * 50) == (50, "Neutral")


def test_classify_bands():
    assert classify(10) == "Extreme Fear"
    assert classify(35) == "Fear"
    assert classify(50) == "Neutral"
    assert classify(65) == "Greed"
    assert classify(90) == "Extreme Greed"


def test_steady_uptrend_reads_greedy():
    closes = [100.0 * (1.002 ** i) for i in range(200)]
    fg, _ = proxy_fear_greed(closes, breadth_frac=0.8)
    assert fg >= 56  # greed band


def test_volatile_selloff_reads_fearful():
    closes = [100.0] * 160
    v = 100.0
    for i in range(40):  # sharp, choppy decline → elevated recent vol + down momentum
        v *= 0.97 if i % 2 == 0 else 1.01
        closes.append(v)
    fg, _ = proxy_fear_greed(closes, breadth_frac=0.2)
    assert fg <= 44  # fear band


def test_breadth_raises_score():
    closes = [100.0 + math.sin(i / 10.0) for i in range(200)]  # flat-ish trend
    low = proxy_fear_greed(closes, breadth_frac=0.1)[0]
    high = proxy_fear_greed(closes, breadth_frac=0.9)[0]
    assert high > low


def test_value_is_bounded():
    for closes in ([100.0 * (1.05 ** i) for i in range(200)],
                   [100.0 * (0.95 ** i) for i in range(200)]):
        fg, _ = proxy_fear_greed(closes, breadth_frac=0.5)
        assert 0 <= fg <= 100
