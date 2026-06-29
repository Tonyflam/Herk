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


def test_max_gross_env_lever_opens_and_survival_still_dominates(settings, monkeypatch):
    """``HELM_MAX_GROSS`` overrides the profile gross base live (scaling the SAME
    survival-gated pipeline). It opens deployment when we choose to lean in, is
    clamped so it can't be fat-fingered into reckless leverage, and can NEVER
    deploy through the halt line — survival still dominates."""
    mc = MetaController(settings)
    # Full-budget point (early, no drawdown, neutral regime) → exposure_scale == 1,
    # so max_gross_pct reads the resolved base directly.
    monkeypatch.setenv("HELM_MAX_GROSS", "1.9")
    p = mc.assess(now=_at(mc, 0.10), equity=100, peak_equity=100, initial_equity=100)
    assert p.max_gross_pct == pytest.approx(1.9, rel=1e-6)
    # Survival dominates: past the halt line the lever cannot deploy anything.
    halt = settings.contest.halt_drawdown_pct
    eq = 100.0 * (1.0 - (halt + 1.0) / 100.0)
    p2 = mc.assess(now=_at(mc, 0.5), equity=eq, peak_equity=100, initial_equity=100)
    assert p2.halt_new_risk is True
    assert p2.max_gross_pct == 0.0
    # Clamp: an absurd value is bounded, never uncapped leverage.
    monkeypatch.setenv("HELM_MAX_GROSS", "99")
    p3 = mc.assess(now=_at(mc, 0.10), equity=100, peak_equity=100, initial_equity=100)
    assert p3.max_gross_pct == pytest.approx(2.5, rel=1e-6)
    # Garbage falls back to the profile default.
    monkeypatch.setenv("HELM_MAX_GROSS", "not-a-number")
    p4 = mc.assess(now=_at(mc, 0.10), equity=100, peak_equity=100, initial_equity=100)
    assert p4.max_gross_pct == pytest.approx(settings.risk.max_gross_exposure, rel=1e-6)


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


# --------------------------------------------------------------------------- #
# Codified endgame escalation (#5): the engine escalates catch-up risk by a
# pre-committed rule (lateness × surviving DD budget × rank), never a human.
# --------------------------------------------------------------------------- #
def test_codified_escalation_scales_with_budget(settings):
    mc = MetaController(settings)
    c = settings.contest
    full = mc._catchup_risk_mult(elapsed=1.0, budget_left=1.0, external_rank=None)
    thin = mc._catchup_risk_mult(elapsed=1.0, budget_left=0.6, external_rank=None)
    below = mc._catchup_risk_mult(elapsed=1.0, budget_left=0.4, external_rank=None)
    # Full budget at the very end → ceiling; partial budget → strictly between;
    # below the survival floor → baseline only (no escalation).
    assert full == pytest.approx(c.catchup_max_risk_mult)
    assert c.catchup_risk_mult < thin < c.catchup_max_risk_mult
    assert below == pytest.approx(c.catchup_risk_mult)


def test_escalation_dormant_outside_endgame(settings):
    mc = MetaController(settings)
    # Mid-phase: lateness clamps to zero → baseline catch-up bump, no escalation.
    m = mc._catchup_risk_mult(elapsed=0.5, budget_left=1.0, external_rank=None)
    assert m == pytest.approx(settings.contest.catchup_risk_mult)


def test_deeper_rank_escalates_at_least_as_much(settings):
    mc = MetaController(settings)
    near = mc._catchup_risk_mult(elapsed=0.95, budget_left=1.0, external_rank=4)
    deep = mc._catchup_risk_mult(elapsed=0.95, budget_left=1.0, external_rank=6)
    assert deep >= near


def test_escalation_still_capped_by_survival(settings):
    # Behind + very late but DEEP drawdown → escalation cannot lift risk through
    # the convex survival taper (the gate stays the binding constraint).
    mc = MetaController(settings)
    halt = settings.contest.halt_drawdown_pct
    equity = 100.0 * (1.0 - (halt * 0.8) / 100.0)
    p = mc.assess(now=_at(mc, 0.97), equity=equity, peak_equity=100, initial_equity=100)
    assert p.posture == "catch_up"
    assert p.aggression_scale < 1.0


# --------------------------------------------------------------------------- #
# Survival-gated regime overlay (#2): with ample DD budget the regime cut is
# dampened (stay deployed through fear); it ramps to full strength as budget thins.
# --------------------------------------------------------------------------- #
def test_regime_overlay_is_survival_gated(settings):
    mc = MetaController(settings)
    # Early, no drawdown → full budget. A risk-off regime cut is dampened.
    p = mc.assess(now=_at(mc, 0.10), equity=100, peak_equity=100,
                  initial_equity=100, regime_gross_scale=0.5)
    floor = settings.regime.overlay_dd_gate_floor
    expected = 1.0 - (1.0 - 0.5) * floor          # gated regime scale at full budget
    assert p.exposure_scale == pytest.approx(expected, rel=1e-6)
    assert p.exposure_scale > 0.5                  # strictly more deployed than ungated


def test_regime_gate_tightens_as_budget_thins(settings):
    mc = MetaController(settings)
    # Same risk-off regime, shallow vs deeper drawdown. The gate applies MORE of
    # the cut as budget thins, so the regime's share of exposure shrinks.
    shallow = mc.assess(now=_at(mc, 0.10), equity=98, peak_equity=100,
                        initial_equity=100, regime_gross_scale=0.5)
    deeper = mc.assess(now=_at(mc, 0.10), equity=90, peak_equity=100,
                       initial_equity=100, regime_gross_scale=0.5)
    # Effective regime multiplier = exposure_scale / (dd_factor * time_factor).
    # Compare the gated regime share directly via the documented formula.
    floor = settings.regime.overlay_dd_gate_floor

    def gated(budget_left: float) -> float:
        gate = floor + (1.0 - floor) * (1.0 - budget_left)
        return 1.0 - (1.0 - 0.5) * gate

    halt = settings.contest.halt_drawdown_pct
    g_shallow = gated(1.0 - (2.0 / halt))
    g_deep = gated(1.0 - (10.0 / halt))
    assert g_deep < g_shallow                      # thinner budget → bigger cut
    assert deeper.exposure_scale < shallow.exposure_scale


# --------------------------------------------------------------------------- #
# Personal / always-on mode (contest.enabled = False): the tournament brain is
# OFF. No phase clock, no week-long time-taper, no protect-lead / catch-up rank
# games — the agent just grows the stack. The convex survival taper and the
# regime overlay remain the ONLY throttles, so a deep drawdown still halts.
# --------------------------------------------------------------------------- #
def _personal(settings):
    """Same settings as the fixture but with the contest brain disabled."""
    from dataclasses import replace
    return replace(settings, contest=replace(settings.contest, enabled=False))


def test_personal_mode_has_no_contest_phases_or_taper(settings):
    mc = MetaController(_personal(settings))
    # No phase clock: every point in time reads "live", and the week-long
    # aggression taper is flat at 1.0 (there's no fixed window to taper across).
    assert mc._phase(0.10) == "live"
    assert mc._phase(0.95) == "live"
    assert mc._time_factor(0.10) == pytest.approx(1.0)
    assert mc._time_factor(0.95) == pytest.approx(1.0)


def test_personal_mode_ignores_lead_and_rank(settings):
    mc = MetaController(_personal(settings))
    lead = settings.contest.protect_lead_return_pct
    equity = 100.0 * (1.0 + (lead + 5.0) / 100.0)
    # Big lead, late, ranked #1 — in contest mode this forces protect_lead; in
    # personal mode there is no lead to protect, so we keep building at full size.
    p = mc.assess(now=_at(mc, 0.90), equity=equity, peak_equity=equity,
                  initial_equity=100, external_rank=1)
    assert p.phase == "live"
    assert p.posture == "build"
    assert p.per_trade_risk_pct == pytest.approx(settings.risk.per_trade_risk_pct, rel=1e-6)


def test_personal_mode_ignores_being_behind(settings):
    mc = MetaController(_personal(settings))
    behind = settings.contest.catchup_behind_return_pct
    equity = 100.0 * (1.0 + (behind - 1.0) / 100.0)  # below the catch-up threshold
    # Behind and late — contest mode would add catch-up variance; personal mode
    # does not chase a rank, so aggression stays at baseline (no >1.0 bump).
    p = mc.assess(now=_at(mc, 0.95), equity=equity, peak_equity=equity, initial_equity=100)
    assert p.posture == "build"
    assert p.aggression_scale == pytest.approx(1.0, rel=1e-6)


def test_personal_mode_still_halts_on_deep_drawdown(settings):
    """Survival DNA is NOT optional: the convex taper + halt line stay fully on in
    personal mode (the halt is now a personal circuit-breaker, not a DQ gate).
    """
    mc = MetaController(_personal(settings))
    halt = settings.contest.halt_drawdown_pct
    equity = 100.0 * (1.0 - (halt + 1.0) / 100.0)  # past the halt line
    p = mc.assess(now=_at(mc, 0.50), equity=equity, peak_equity=100, initial_equity=100)
    assert p.halt_new_risk is True
    assert p.posture == "halt"
    assert p.max_gross_pct == 0.0
    # A mid-budget drawdown still de-risks convexly (survival dominates).
    eq2 = 100.0 * (1.0 - (halt * 0.5) / 100.0)
    p2 = mc.assess(now=_at(mc, 0.10), equity=eq2, peak_equity=100, initial_equity=100)
    assert 0.0 < p2.exposure_scale < 0.5 * settings.risk.max_gross_exposure


def test_personal_profile_loads_with_contest_disabled():
    """The shipped `personal` profile wires contest.enabled=False + a 25% personal
    circuit-breaker, without disturbing the default (balanced) profile."""
    import os
    from helm.config import load_settings
    prev = os.environ.get("HELM_PROFILE")
    os.environ["HELM_PROFILE"] = "personal"
    try:
        s = load_settings()
    finally:
        if prev is None:
            os.environ.pop("HELM_PROFILE", None)
        else:
            os.environ["HELM_PROFILE"] = prev
    assert s.contest.enabled is False
    assert s.contest.halt_drawdown_pct == pytest.approx(25.0)
    assert s.signals.top_n == 2
