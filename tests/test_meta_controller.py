"""Meta-controller: HELM's edge. These tests pin the contest-game behavior:
survival dominates, leads are protected late, and being behind buys *bounded*
extra variance — never enough to breach the halt line.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from helm.contest.meta_controller import MetaController


def _at(mc: MetaController, frac: float):
    """A datetime at `frac` through the contest window."""
    return mc.start + timedelta(seconds=(mc.end - mc.start).total_seconds() * frac)


def test_build_phase_runs_full_budget(settings):
    mc = MetaController(settings)
    p = mc.assess(now=_at(mc, 0.10), equity=100, peak_equity=100, initial_equity=100)
    assert p.phase == "build"
    assert p.posture == "build"
    assert p.halt_new_risk is False
    # No drawdown, early, neutral regime → full gross cap.
    assert p.max_gross_pct == pytest.approx(settings.risk.max_gross_exposure, rel=1e-6)
    assert p.drawdown_budget_left == pytest.approx(1.0, abs=1e-9)


def test_halt_when_drawdown_breaches_line(settings):
    mc = MetaController(settings)
    halt = settings.contest.halt_drawdown_pct
    equity = 100.0 * (1.0 - (halt + 1.0) / 100.0)  # past the halt line
    p = mc.assess(now=_at(mc, 0.5), equity=equity, peak_equity=100, initial_equity=100)
    assert p.halt_new_risk is True
    assert p.posture == "halt"
    assert p.max_gross_pct == 0.0
    assert p.exposure_scale == 0.0
    assert p.aggression_scale == 0.0


def test_drawdown_taper_is_convex_and_derisks(settings):
    mc = MetaController(settings)
    halt = settings.contest.halt_drawdown_pct
    # Drawdown at half the halt budget → de-risked but not halted.
    equity = 100.0 * (1.0 - (halt * 0.5) / 100.0)
    p = mc.assess(now=_at(mc, 0.10), equity=equity, peak_equity=100, initial_equity=100)
    assert p.halt_new_risk is False
    assert p.drawdown_budget_left == pytest.approx(0.5, abs=0.02)
    # Convex taper (budget**1.3) keeps exposure strictly below the linear 0.5.
    assert 0.0 < p.exposure_scale < 0.5 * settings.risk.max_gross_exposure


def test_protect_lead_in_endgame_sheds_variance(settings):
    mc = MetaController(settings)
    lead = settings.contest.protect_lead_return_pct
    equity = 100.0 * (1.0 + (lead + 5.0) / 100.0)
    p = mc.assess(now=_at(mc, 0.90), equity=equity, peak_equity=equity, initial_equity=100)
    assert p.posture == "protect_lead"
    # Aggression cut hard relative to baseline.
    assert p.aggression_scale < settings.risk.per_trade_risk_pct
    assert p.per_trade_risk_pct < settings.risk.per_trade_risk_pct


def test_catch_up_adds_bounded_variance(settings):
    mc = MetaController(settings)
    behind = settings.contest.catchup_behind_return_pct
    equity = 100.0 * (1.0 + (behind - 1.0) / 100.0)  # below the catch-up threshold
    p = mc.assess(now=_at(mc, 0.90), equity=equity, peak_equity=equity, initial_equity=100)
    assert p.posture == "catch_up"
    # Extra aggression, but bounded by the global clamp (<= 1.5).
    assert p.aggression_scale > 1.0
    assert p.aggression_scale <= 1.5


def test_survival_caps_catch_up(settings):
    # Even "behind late", a deep drawdown must dominate and cut risk.
    mc = MetaController(settings)
    halt = settings.contest.halt_drawdown_pct
    equity = 100.0 * (1.0 - (halt * 0.8) / 100.0)  # deep but not halted
    p = mc.assess(now=_at(mc, 0.90), equity=equity, peak_equity=100, initial_equity=100)
    # Drawdown factor (~0.2**1.3) crushes aggression well below the catch-up bump.
    assert p.aggression_scale < 1.0


def test_external_rank_one_forces_protect(settings):
    mc = MetaController(settings)
    # Flat return but ranked #1 → still protect.
    p = mc.assess(now=_at(mc, 0.90), equity=100, peak_equity=100,
                  initial_equity=100, external_rank=1)
    assert p.posture == "protect_lead"
