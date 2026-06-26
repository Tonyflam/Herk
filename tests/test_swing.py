"""Manual swing control: operator-directed take-profit + automatic dip rebuy.

A human can fire a one-shot SELL of ``swing_symbol`` to cash via the
``HELM_SWING_CMD`` env var (``verb#token``); the agent then "arms" and rebuys
that name once it dips ``swing_rebuy_drop`` below the realized sell price. The
feature is OFF by default and every existing guardrail still applies.

These tests pin the contract that keeps it safe:
  • a ``sell`` command liquidates the whole position and arms the rebuy,
  • the command is idempotent on its token (a restart never re-fires it),
  • while armed, neither ``_run_entries`` nor ``_run_rotation`` may re-buy the
    name (the manual exit is never undone underneath the operator),
  • the dip rebuy fires only once price has fallen far enough, not before,
  • ``off`` disarms, the swing state survives a restart, and the whole thing is
    a hard no-op when disabled.
"""

from __future__ import annotations

import os
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from helm.agent import Agent
from helm.config import load_settings
from helm.portfolio import Portfolio, Position
from helm.signals.engine import SignalSnapshot, SymbolSignal


# A fixed "now" so position ages are deterministic regardless of the wall clock.
_NOW = datetime(2026, 6, 25, 12, 0, tzinfo=timezone.utc)


# --------------------------------------------------------------- test helpers
def _settings(**risk_overrides):
    """Balanced profile with manual swing enabled on AAVE (overridable)."""
    prev = os.environ.get("HELM_PROFILE")
    os.environ["HELM_PROFILE"] = "balanced"
    try:
        s = load_settings()
    finally:
        if prev is None:
            os.environ.pop("HELM_PROFILE", None)
        else:
            os.environ["HELM_PROFILE"] = prev
    base = dict(swing_enabled=True, swing_symbol="AAVE", swing_rebuy_drop=0.02)
    base.update(risk_overrides)
    s.risk = replace(s.risk, **base)
    return s


def _agent(tmp_path, settings=None) -> Agent:
    if settings is None:
        settings = _settings()
    return Agent(settings=settings, state_path=tmp_path / "state.json",
                 ledger_path=tmp_path / "audit.jsonl")


def _sig(symbol: str, composite: float, price: float = 100.0) -> SymbolSignal:
    return SymbolSignal(symbol=symbol, price=price, composite=composite,
                        liquidity_usd=5e7, passes_liquidity=True,
                        passes_quality=True, passes_cost=True)


def _snap(ranked: list[SymbolSignal], signals: list[SymbolSignal] | None = None) -> SignalSnapshot:
    return SignalSnapshot(regime=None, signals=signals if signals is not None else ranked,
                          ranked=ranked)


def _pos(symbol: str, qty: float, price: float, age_h: float = 24.0) -> Position:
    ts = (_NOW - timedelta(hours=age_h)).isoformat()
    return Position(symbol=symbol, qty=qty, avg_entry=price, stop_price=price * 0.5,
                    take_profit_price=price * 5.0, stop_distance=price * 0.5,
                    highest_price=price, entry_ts=ts)


def _book(agent: Agent, cash: float, positions: list[Position]) -> None:
    p = Portfolio.new(0.0)
    p.cash = cash
    for pos in positions:
        p.positions[pos.symbol] = pos
    agent.portfolio = p


def _posture(max_gross: float = 1.20, risk_pct: float = 4.0) -> SimpleNamespace:
    return SimpleNamespace(max_gross_pct=max_gross, per_trade_risk_pct=risk_pct,
                           halt_new_risk=False, posture="build")


# ------------------------------------------------------------------ the path
def test_swing_sell_arms_and_liquidates(tmp_path, monkeypatch):
    """A ``sell`` command closes the whole position and arms the dip rebuy."""
    monkeypatch.setenv("HELM_SWING_CMD", "sell#1")
    agent = _agent(tmp_path)
    try:
        _book(agent, cash=1.0, positions=[_pos("AAVE", qty=1.0, price=100.0)])
        prices = {"AAVE": 100.0}
        snap = _snap(ranked=[_sig("AAVE", 2.0, 100.0)])
        actions: list = []
        agent._run_swing(snap, prices, actions, dry_run=False, now=_NOW)

        assert "AAVE" not in agent.portfolio.positions     # fully liquidated
        assert agent.portfolio.cash > 90.0                 # ~$100 freed (minus costs)
        assert agent.portfolio.swing_armed is True
        assert agent.portfolio.swing_sell_px > 0
        assert agent.portfolio.swing_token == "1"
        assert any(a.kind == "exit" and "manual-sell" in a.detail for a in actions)
    finally:
        agent.close()


def test_swing_command_is_idempotent_on_token(tmp_path, monkeypatch):
    """An already-consumed token never re-fires (a routine restart is safe)."""
    monkeypatch.setenv("HELM_SWING_CMD", "sell#1")
    agent = _agent(tmp_path)
    try:
        _book(agent, cash=1.0, positions=[_pos("AAVE", qty=1.0, price=100.0)])
        agent.portfolio.swing_token = "1"          # token 1 already consumed
        prices = {"AAVE": 100.0}
        snap = _snap(ranked=[_sig("AAVE", 2.0, 100.0)])
        actions: list = []
        agent._run_swing(snap, prices, actions, dry_run=False, now=_NOW)

        assert "AAVE" in agent.portfolio.positions  # untouched
        assert agent.portfolio.swing_armed is False
        assert actions == []
    finally:
        agent.close()


def test_swing_armed_blocks_auto_rebuy_in_entries(tmp_path, monkeypatch):
    """While armed, the normal entry loop must not re-buy the parked name."""
    monkeypatch.delenv("HELM_SWING_CMD", raising=False)
    agent = _agent(tmp_path)
    try:
        _book(agent, cash=80.0, positions=[])       # in cash, waiting for the dip
        agent.portfolio.swing_armed = True
        agent.portfolio.swing_sell_px = 100.0
        prices = {"AAVE": 100.0}
        snap = _snap(ranked=[_sig("AAVE", 2.7, 100.0)])
        actions: list = []
        agent._run_entries(snap, prices, _posture(), actions, dry_run=False, now=_NOW)

        assert "AAVE" not in agent.portfolio.positions   # entry loop left it alone
        assert agent.portfolio.cash == 80.0
        assert not any(a.kind == "entry" for a in actions)
    finally:
        agent.close()


def test_swing_armed_blocks_rotation_into_parked_name(tmp_path, monkeypatch):
    """Rotation must not steer freed capital into the parked swing name either."""
    monkeypatch.delenv("HELM_SWING_CMD", raising=False)
    agent = _agent(tmp_path)
    try:
        # We still hold a stale AVAX chunk; AAVE is the only ranked leader, but it
        # is parked for a manual rebuy, so rotation has no eligible leader.
        _book(agent, cash=1.0, positions=[_pos("AVAX", qty=0.7, price=100.0)])
        agent.portfolio.swing_armed = True
        agent.portfolio.swing_sell_px = 200.0
        prices = {"AVAX": 100.0, "AAVE": 200.0}
        snap = _snap(ranked=[_sig("AAVE", 2.7, 200.0)],
                     signals=[_sig("AAVE", 2.7, 200.0), _sig("AVAX", 0.1, 100.0)])
        actions: list = []
        agent._run_rotation(snap, prices, None, actions, dry_run=False, now=_NOW)

        assert "AVAX" in agent.portfolio.positions   # nothing rotated
        assert actions == []
    finally:
        agent.close()


def test_swing_dip_rebuy_triggers_at_threshold(tmp_path, monkeypatch):
    """Once price dips >= swing_rebuy_drop below the sell, cash is redeployed."""
    monkeypatch.delenv("HELM_SWING_CMD", raising=False)
    agent = _agent(tmp_path)
    try:
        _book(agent, cash=50.0, positions=[])
        agent.portfolio.swing_armed = True
        agent.portfolio.swing_sell_px = 100.0
        prices = {"AAVE": 98.0}                       # exactly -2%
        snap = _snap(ranked=[_sig("AAVE", 2.7, 98.0)])
        actions: list = []
        agent._run_swing(snap, prices, actions, dry_run=False, now=_NOW)

        assert "AAVE" in agent.portfolio.positions    # rebought
        assert agent.portfolio.swing_armed is False    # disarmed after the rebuy
        assert agent.portfolio.cash < 5.0              # cash deployed (gas reserve only)
        assert any(a.kind == "entry" and "swing-rebuy" in a.detail for a in actions)
    finally:
        agent.close()


def test_swing_dip_rebuy_waits_above_threshold(tmp_path, monkeypatch):
    """A shallow dip (< drop) does NOT rebuy — we keep waiting in cash."""
    monkeypatch.delenv("HELM_SWING_CMD", raising=False)
    agent = _agent(tmp_path)
    try:
        _book(agent, cash=50.0, positions=[])
        agent.portfolio.swing_armed = True
        agent.portfolio.swing_sell_px = 100.0
        prices = {"AAVE": 99.0}                       # only -1%
        snap = _snap(ranked=[_sig("AAVE", 2.7, 99.0)])
        actions: list = []
        agent._run_swing(snap, prices, actions, dry_run=False, now=_NOW)

        assert "AAVE" not in agent.portfolio.positions
        assert agent.portfolio.swing_armed is True     # still waiting
        assert agent.portfolio.cash == 50.0
    finally:
        agent.close()


def test_swing_absolute_target_overrides_percentage(tmp_path, monkeypatch):
    """HELM_SWING_REBUY_PX sets an absolute rebuy trigger above the -2% default."""
    monkeypatch.delenv("HELM_SWING_CMD", raising=False)
    monkeypatch.setenv("HELM_SWING_REBUY_PX", "83.9")
    agent = _agent(tmp_path)
    try:
        _book(agent, cash=50.0, positions=[])
        agent.portfolio.swing_armed = True
        agent.portfolio.swing_sell_px = 85.39          # -2% default would be ~83.68
        prices = {"AAVE": 83.9}                         # at the absolute target
        snap = _snap(ranked=[_sig("AAVE", 2.7, 83.9)])
        actions: list = []
        agent._run_swing(snap, prices, actions, dry_run=False, now=_NOW)

        assert "AAVE" in agent.portfolio.positions      # rebought at the absolute target
        assert agent.portfolio.swing_armed is False
        assert any(a.kind == "entry" and "swing-rebuy" in a.detail for a in actions)
    finally:
        agent.close()


def test_swing_absolute_target_waits_above_level(tmp_path, monkeypatch):
    """With an absolute target set, a price still above it does NOT rebuy."""
    monkeypatch.delenv("HELM_SWING_CMD", raising=False)
    monkeypatch.setenv("HELM_SWING_REBUY_PX", "83.9")
    agent = _agent(tmp_path)
    try:
        _book(agent, cash=50.0, positions=[])
        agent.portfolio.swing_armed = True
        agent.portfolio.swing_sell_px = 85.39
        prices = {"AAVE": 84.5}                         # above the 83.9 target
        snap = _snap(ranked=[_sig("AAVE", 2.7, 84.5)])
        actions: list = []
        agent._run_swing(snap, prices, actions, dry_run=False, now=_NOW)

        assert "AAVE" not in agent.portfolio.positions
        assert agent.portfolio.swing_armed is True      # still waiting for 83.9
        assert agent.portfolio.cash == 50.0
    finally:
        agent.close()


def test_swing_garbage_target_falls_back_to_percentage(tmp_path, monkeypatch):
    """A non-numeric HELM_SWING_REBUY_PX is ignored; the -2% default still works."""
    monkeypatch.delenv("HELM_SWING_CMD", raising=False)
    monkeypatch.setenv("HELM_SWING_REBUY_PX", "not-a-number")
    agent = _agent(tmp_path)
    try:
        _book(agent, cash=50.0, positions=[])
        agent.portfolio.swing_armed = True
        agent.portfolio.swing_sell_px = 100.0
        prices = {"AAVE": 98.0}                         # exactly -2% default
        snap = _snap(ranked=[_sig("AAVE", 2.7, 98.0)])
        actions: list = []
        agent._run_swing(snap, prices, actions, dry_run=False, now=_NOW)

        assert "AAVE" in agent.portfolio.positions      # default percentage still fired
        assert agent.portfolio.swing_armed is False
    finally:
        agent.close()


def test_swing_off_command_disarms(tmp_path, monkeypatch):
    """``off`` clears the armed state without trading."""
    monkeypatch.setenv("HELM_SWING_CMD", "off#2")
    agent = _agent(tmp_path)
    try:
        _book(agent, cash=50.0, positions=[])
        agent.portfolio.swing_armed = True
        agent.portfolio.swing_sell_px = 100.0
        prices = {"AAVE": 90.0}                       # below threshold, but disarmed first
        snap = _snap(ranked=[_sig("AAVE", 2.7, 90.0)])
        actions: list = []
        agent._run_swing(snap, prices, actions, dry_run=False, now=_NOW)

        assert agent.portfolio.swing_armed is False
        assert "AAVE" not in agent.portfolio.positions  # no rebuy after disarm
        assert agent.portfolio.swing_token == "2"
    finally:
        agent.close()


def test_swing_disabled_is_a_no_op(tmp_path, monkeypatch):
    """With swing_enabled=False a command is completely ignored."""
    monkeypatch.setenv("HELM_SWING_CMD", "sell#1")
    agent = _agent(tmp_path, settings=_settings(swing_enabled=False))
    try:
        _book(agent, cash=1.0, positions=[_pos("AAVE", qty=1.0, price=100.0)])
        prices = {"AAVE": 100.0}
        snap = _snap(ranked=[_sig("AAVE", 2.0, 100.0)])
        actions: list = []
        agent._run_swing(snap, prices, actions, dry_run=False, now=_NOW)

        assert "AAVE" in agent.portfolio.positions      # untouched
        assert agent.portfolio.swing_armed is False
        assert actions == []
    finally:
        agent.close()


def test_swing_dry_run_is_observe_only(tmp_path, monkeypatch):
    """Dry-run reports the intended sell but never mutates the book."""
    monkeypatch.setenv("HELM_SWING_CMD", "sell#1")
    agent = _agent(tmp_path)
    try:
        _book(agent, cash=1.0, positions=[_pos("AAVE", qty=1.0, price=100.0)])
        prices = {"AAVE": 100.0}
        snap = _snap(ranked=[_sig("AAVE", 2.0, 100.0)])
        actions: list = []
        agent._run_swing(snap, prices, actions, dry_run=True, now=_NOW)

        assert "AAVE" in agent.portfolio.positions      # not sold
        assert agent.portfolio.swing_armed is False
        assert any(a.kind == "dry_run" and "manual-sell" in a.detail for a in actions)
    finally:
        agent.close()


def test_swing_state_survives_restart(tmp_path, monkeypatch):
    """Armed state, sell price, and consumed token persist across a reload."""
    monkeypatch.delenv("HELM_SWING_CMD", raising=False)
    agent = _agent(tmp_path)
    try:
        _book(agent, cash=20.0, positions=[_pos("BTC", qty=0.01, price=60000.0)])
        agent.portfolio.swing_armed = True
        agent.portfolio.swing_sell_px = 83.51
        agent.portfolio.swing_token = "3"
        agent._save_state()
    finally:
        agent.close()

    restored = _agent(tmp_path)
    try:
        assert restored.portfolio.swing_armed is True
        assert restored.portfolio.swing_sell_px == 83.51
        assert restored.portfolio.swing_token == "3"
    finally:
        restored.close()
