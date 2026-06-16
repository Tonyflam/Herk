"""Sentinel: the deterministic pre-trade gate. The LLM never reaches trading
code — every order must pass these checks first. Each test forces exactly one
violation and asserts it is caught.
"""

from __future__ import annotations

from helm.contest.meta_controller import ContestPosture
from helm.risk.sentinel import BookState, SecurityChecklist, Sentinel
from helm.risk.sizing import SizePlan

_NO_KILL = "/tmp/helm.NONEXISTENT.kill.switch"


def _posture(max_gross_pct=0.90, halt=False, posture="build"):
    return ContestPosture(
        phase="build", posture=("halt" if halt else posture),
        elapsed_frac=0.1, time_left_frac=0.9, drawdown_pct=0.0,
        drawdown_budget_left=1.0, our_return_pct=0.0, halt_new_risk=halt,
        exposure_scale=1.0, aggression_scale=1.0,
        max_gross_pct=max_gross_pct, per_trade_risk_pct=1.5,
    )


def _plan(notional=15.0, pct=0.15):
    return SizePlan(
        symbol="UNI", price=100.0, atr=5.0, stop_price=87.5,
        take_profit_price=120.0, stop_distance=12.5, risk_budget_usd=1.5,
        notional_usd=notional, qty=notional / 100.0, pct_of_equity=pct,
        binding_constraint="risk",
    )


def _book(equity=100.0, gross=0.0, positions=0, day_start=100.0, holds=False):
    return BookState(
        equity=equity, peak_equity=100.0, day_start_equity=day_start,
        gross_usd=gross, open_positions=positions, holds_symbol=holds,
    )


def _decide(settings, **over):
    s = Sentinel(settings, kill_switch_path=over.pop("kill", _NO_KILL))
    kwargs = dict(
        symbol="UNI", plan=_plan(), book=_book(), posture=_posture(),
        liquidity_usd=5_000_000.0, est_slippage_bps=20.0, security=None,
    )
    kwargs.update(over)
    return s.pre_trade(**kwargs)


def test_approves_a_clean_trade(settings):
    d = _decide(settings)
    assert d.approved is True, d.reason
    assert d.failed_checks == []


def test_blocks_on_gross_cap(settings):
    # cap = 0.90 * 100 = 90; existing gross 80 + notional 20 = 100 > 90.
    d = _decide(settings, plan=_plan(notional=20.0, pct=0.20), book=_book(gross=80.0))
    assert d.approved is False
    assert "gross_cap" in d.failed_checks


def test_blocks_on_position_cap(settings):
    d = _decide(settings, plan=_plan(notional=30.0, pct=0.30))
    assert d.approved is False
    assert "position_cap" in d.failed_checks


def test_blocks_on_thin_liquidity(settings):
    d = _decide(settings, liquidity_usd=1_000.0)
    assert d.approved is False
    assert "liquidity" in d.failed_checks


def test_blocks_on_excess_slippage(settings):
    d = _decide(settings, est_slippage_bps=500.0)
    assert d.approved is False
    assert "slippage" in d.failed_checks


def test_blocks_on_daily_loss_limit(settings):
    # day_start 100, equity 90 → -10% < -daily_loss_limit (default 8%).
    d = _decide(settings, book=_book(equity=90.0, day_start=100.0))
    assert d.approved is False
    assert "daily_loss_limit" in d.failed_checks


def test_blocks_on_contest_halt(settings):
    d = _decide(settings, posture=_posture(halt=True))
    assert d.approved is False
    assert "contest_halt" in d.failed_checks


def test_blocks_on_dust(settings):
    d = _decide(settings, plan=_plan(notional=0.5, pct=0.005))
    assert d.approved is False
    assert "dust_floor" in d.failed_checks


def test_blocks_on_full_position_slots(settings):
    d = _decide(settings, book=_book(positions=settings.risk.max_open_positions))
    assert d.approved is False
    assert "position_slots" in d.failed_checks


def test_kill_switch_blocks_everything(settings, tmp_path):
    ks = tmp_path / "helm.STOP"
    ks.write_text("halt")
    d = _decide(settings, kill=str(ks))
    assert d.approved is False
    assert "kill_switch" in d.failed_checks


def test_security_checklist_failure_blocks(settings):
    sec = SecurityChecklist(honeypot_safe=False, contract_verified=True)
    d = _decide(settings, security=sec)
    assert d.approved is False
    assert "sec:honeypot" in d.failed_checks


def test_security_checklist_none_is_skipped(settings):
    # Paper mode: all on-chain checks report as skipped/pass.
    d = _decide(settings, security=SecurityChecklist())
    assert d.approved is True, d.reason


def test_pre_exit_always_allows_valid_close(settings):
    s = Sentinel(settings, kill_switch_path=_NO_KILL)
    d = s.pre_exit(symbol="UNI", qty=5.0, held_qty=10.0)
    assert d.approved is True


def test_pre_exit_rejects_when_nothing_held(settings):
    s = Sentinel(settings, kill_switch_path=_NO_KILL)
    d = s.pre_exit(symbol="UNI", qty=5.0, held_qty=0.0)
    assert d.approved is False
