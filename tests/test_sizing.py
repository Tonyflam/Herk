"""Sizing: a single name can never blow up the book. Size is the MIN of four
independent budgets (risk / vol-target / position-cap / gross-headroom).
"""

from __future__ import annotations

import pytest

from helm.risk.sizing import plan_position

_BASE = dict(
    stop_atr_mult=2.5,
    take_profit_atr_mult=4.0,
    max_position_pct=0.20,
)


def test_risk_budget_binds_with_wide_stop():
    # ATR stop = 5*2.5 = 12.5; risk$ = 1000*1.5% = 15; qty = 1.2; notional = 120.
    p = plan_position(symbol="X", price=100, atr=5, equity=1000,
                      per_trade_risk_pct=1.5, gross_headroom_usd=1e9, **_BASE)
    assert p.binding_constraint == "risk"
    assert p.notional_usd == pytest.approx(120.0, rel=1e-6)
    assert p.stop_price == pytest.approx(87.5)
    assert p.take_profit_price == pytest.approx(120.0)  # 100 + 5*4.0
    assert p.ok


def test_position_cap_binds_with_tight_stop():
    # Tiny ATR → huge risk budget → per-position cap (20%) binds.
    p = plan_position(symbol="X", price=100, atr=0.1, equity=1000,
                      per_trade_risk_pct=1.5, gross_headroom_usd=1e9, **_BASE)
    assert p.binding_constraint == "position_cap"
    assert p.notional_usd == pytest.approx(200.0)
    assert p.pct_of_equity == pytest.approx(0.20)


def test_gross_headroom_binds():
    p = plan_position(symbol="X", price=100, atr=5, equity=1000,
                      per_trade_risk_pct=1.5, gross_headroom_usd=40.0, **_BASE)
    assert p.binding_constraint == "gross_headroom"
    assert p.notional_usd == pytest.approx(40.0)


def test_vol_target_binds_for_hot_names():
    # realized vol 4.0 vs target 0.45 → vol notional = 1000*0.45/4 = 112.5 < risk 120.
    p = plan_position(symbol="X", price=100, atr=5, equity=1000,
                      per_trade_risk_pct=1.5, gross_headroom_usd=1e9,
                      realized_vol_annual=4.0, target_vol_annual=0.45, **_BASE)
    assert p.binding_constraint == "vol_target"
    assert p.notional_usd == pytest.approx(112.5)


def test_fallback_stop_when_atr_missing():
    # No ATR → 6% fallback stop distance → stop at 94.
    p = plan_position(symbol="X", price=100, atr=0, equity=1000,
                      per_trade_risk_pct=1.5, gross_headroom_usd=1e9, **_BASE)
    assert p.stop_price == pytest.approx(94.0)
    assert p.stop_distance == pytest.approx(6.0)


def test_invalid_inputs_return_zero_plan():
    p = plan_position(symbol="X", price=0, atr=5, equity=1000,
                      per_trade_risk_pct=1.5, gross_headroom_usd=100, **_BASE)
    assert p.ok is False
    assert p.binding_constraint == "invalid"
    assert p.notional_usd == 0


def test_size_never_exceeds_any_budget():
    p = plan_position(symbol="X", price=37.5, atr=1.2, equity=523,
                      per_trade_risk_pct=1.5, gross_headroom_usd=15.0,
                      realized_vol_annual=0.9, target_vol_annual=0.45, **_BASE)
    risk_budget = 523 * 0.015 / (1.2 * 2.5) * 37.5
    vol_budget = 523 * (0.45 / 0.9)
    cap_budget = 523 * 0.20
    gross_budget = 15.0
    assert p.notional_usd <= min(risk_budget, vol_budget, cap_budget, gross_budget) + 1e-9
