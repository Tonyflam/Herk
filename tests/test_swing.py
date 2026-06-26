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


# ----------------------------------------- standing buy-the-dip (add-on-dip)
def test_standing_target_buys_dip_when_already_holding(tmp_path, monkeypatch):
    """A standing HELM_SWING_REBUY_PX deploys idle cash into MORE of the name on
    the dip, even when not armed and already holding a position."""
    monkeypatch.delenv("HELM_SWING_CMD", raising=False)
    monkeypatch.setenv("HELM_SWING_REBUY_PX", "83.9")
    agent = _agent(tmp_path)
    try:
        # Already re-entered (0.7 AAVE) with leftover cash to add on the dip.
        _book(agent, cash=32.0, positions=[_pos("AAVE", qty=0.7, price=85.0)])
        assert agent.portfolio.swing_armed is False
        prices = {"AAVE": 83.9}                          # at the standing target
        snap = _snap(ranked=[_sig("AAVE", 2.7, 83.9)])
        actions: list = []
        agent._run_swing(snap, prices, actions, dry_run=False, now=_NOW)

        assert agent.portfolio.positions["AAVE"].qty > 0.7   # added to the position
        assert agent.portfolio.cash < 5.0                    # idle cash deployed
        assert any(a.kind == "entry" and "swing-rebuy" in a.detail for a in actions)
    finally:
        agent.close()


def test_standing_target_holds_cash_above_level(tmp_path, monkeypatch):
    """Above the standing target, idle cash is held (no add) — waiting for the dip."""
    monkeypatch.delenv("HELM_SWING_CMD", raising=False)
    monkeypatch.setenv("HELM_SWING_REBUY_PX", "83.9")
    agent = _agent(tmp_path)
    try:
        _book(agent, cash=32.0, positions=[_pos("AAVE", qty=0.7, price=85.0)])
        prices = {"AAVE": 85.39}                         # above 83.9
        snap = _snap(ranked=[_sig("AAVE", 2.7, 85.39)])
        actions: list = []
        agent._run_swing(snap, prices, actions, dry_run=False, now=_NOW)

        assert abs(agent.portfolio.positions["AAVE"].qty - 0.7) < 1e-9  # unchanged
        assert agent.portfolio.cash == 32.0                            # cash held
    finally:
        agent.close()


def test_standing_target_blocks_entries_from_spending_cash(tmp_path, monkeypatch):
    """While a standing target holds idle cash, the normal entry loop must not
    spend it on the swing symbol (the cash is earmarked for the dip)."""
    monkeypatch.delenv("HELM_SWING_CMD", raising=False)
    monkeypatch.setenv("HELM_SWING_REBUY_PX", "83.9")
    agent = _agent(tmp_path)
    try:
        _book(agent, cash=32.0, positions=[_pos("AAVE", qty=0.7, price=85.0)])
        prices = {"AAVE": 85.39}
        snap = _snap(ranked=[_sig("AAVE", 2.7, 85.39)])
        actions: list = []
        agent._run_entries(snap, prices, _posture(), actions, dry_run=False, now=_NOW)

        assert abs(agent.portfolio.positions["AAVE"].qty - 0.7) < 1e-9  # not topped up
        assert agent.portfolio.cash == 32.0
        assert not any(a.kind == "entry" for a in actions)
    finally:
        agent.close()


def test_block_symbol_helper_states(tmp_path, monkeypatch):
    """_swing_block_symbol: armed blocks unconditionally; standing target blocks
    only with idle cash; nothing pending returns empty."""
    monkeypatch.delenv("HELM_SWING_CMD", raising=False)
    monkeypatch.delenv("HELM_SWING_REBUY_PX", raising=False)
    agent = _agent(tmp_path)
    try:
        _book(agent, cash=0.5, positions=[_pos("AAVE", qty=0.7, price=85.0)])
        # Nothing pending: no block.
        assert agent._swing_block_symbol() == ""
        # Armed: blocks even with sub-dust cash.
        agent.portfolio.swing_armed = True
        assert agent._swing_block_symbol() == "AAVE"
        agent.portfolio.swing_armed = False
        # Standing target but no idle cash: no block.
        monkeypatch.setenv("HELM_SWING_REBUY_PX", "83.9")
        assert agent._swing_block_symbol() == ""
        # Standing target with idle cash: block.
        agent.portfolio.cash = 32.0
        assert agent._swing_block_symbol() == "AAVE"
    finally:
        agent.close()


# ============================================================================
# Volatility harvester: autonomous sell-the-rip / buy-the-dip fixed-slice grid.
#
# A symmetric grid off a moving anchor price. Once price rises a full
# ``harvest_step_pct`` it banks ``harvest_trade_frac`` of the position into USDT
# (locking realized profit -- the "profit guard" the leaders use); once price
# falls a full step it buys with that fraction of idle cash. A decaying core and
# a cash reserve always remain. These tests pin the contract that keeps it both
# effective and DQ-safe.
# ============================================================================
def _hsettings(**risk_overrides):
    """Balanced profile with the harvester enabled on AAVE (overridable)."""
    base = dict(harvest_enabled=True, harvest_step_pct=0.035,
                harvest_trade_frac=0.20, harvest_min_trade_usd=4.0)
    base.update(risk_overrides)
    return _settings(**base)


def _hagent(tmp_path, monkeypatch, **risk_overrides) -> Agent:
    """Harvester-enabled agent with all swing/harvest env knobs cleared."""
    for var in ("HELM_SWING_CMD", "HELM_SWING_REBUY_PX",
                "HELM_HARVEST_STEP", "HELM_HARVEST_FRAC"):
        monkeypatch.delenv(var, raising=False)
    return _agent(tmp_path, _hsettings(**risk_overrides))


def _halted_posture() -> SimpleNamespace:
    return SimpleNamespace(max_gross_pct=1.20, per_trade_risk_pct=4.0,
                           halt_new_risk=True, posture="halt")


def test_harvest_first_sight_arms_anchor_without_trading(tmp_path, monkeypatch):
    """The first cycle just records the anchor at the current price -- no trade,
    so the agent's upside is never reduced by merely turning the grid on."""
    agent = _hagent(tmp_path, monkeypatch)
    try:
        _book(agent, cash=0.30, positions=[_pos("AAVE", qty=1.0, price=86.0)])
        assert agent.portfolio.harvest_anchor_px == 0.0
        actions: list = []
        snap = _snap(ranked=[_sig("AAVE", 2.0, 86.0)])
        agent._run_harvest(snap, {"AAVE": 86.0}, _posture(), actions, dry_run=False, now=_NOW)

        assert agent.portfolio.harvest_anchor_px == 86.0       # armed at current px
        assert abs(agent.portfolio.positions["AAVE"].qty - 1.0) < 1e-9   # untouched
        assert actions == []                                   # no trade fired
    finally:
        agent.close()


def test_harvest_banks_a_slice_on_a_rip(tmp_path, monkeypatch):
    """A full up-step banks a fixed slice into USDT and locks realized profit."""
    agent = _hagent(tmp_path, monkeypatch)
    try:
        _book(agent, cash=0.30, positions=[_pos("AAVE", qty=1.0, price=86.0)])
        agent.portfolio.harvest_anchor_px = 86.0
        actions: list = []
        snap = _snap(ranked=[_sig("AAVE", 2.0, 90.0)])
        agent._run_harvest(snap, {"AAVE": 90.0}, _posture(), actions, dry_run=False, now=_NOW)

        assert agent.portfolio.harvest_anchor_px == 90.0          # anchor advanced
        assert abs(agent.portfolio.positions["AAVE"].qty - 0.80) < 0.02  # ~20% banked
        assert agent.portfolio.cash > 15.0                        # USDT proceeds in hand
        banks = [a for a in actions if a.kind == "exit" and "harvest-bank" in a.detail]
        assert banks and "pnl +" in banks[0].detail               # realized gain locked
    finally:
        agent.close()


def test_harvest_buys_a_slice_on_a_dip(tmp_path, monkeypatch):
    """A full down-step deploys a fixed slice of idle cash into more of the name."""
    agent = _hagent(tmp_path, monkeypatch)
    try:
        _book(agent, cash=40.0, positions=[_pos("AAVE", qty=0.5, price=86.0)])
        agent.portfolio.harvest_anchor_px = 86.0
        actions: list = []
        snap = _snap(ranked=[_sig("AAVE", 2.0, 82.0)])
        agent._run_harvest(snap, {"AAVE": 82.0}, _posture(), actions, dry_run=False, now=_NOW)

        assert agent.portfolio.harvest_anchor_px == 82.0
        assert agent.portfolio.positions["AAVE"].qty > 0.5        # accumulated cheaper
        assert agent.portfolio.cash < 35.0                        # idle cash deployed
        assert any(a.kind == "entry" and "harvest-dip" in a.detail for a in actions)
    finally:
        agent.close()


def test_harvest_holds_inside_the_band(tmp_path, monkeypatch):
    """A sub-step wiggle does nothing: no trade and the anchor stays put."""
    agent = _hagent(tmp_path, monkeypatch)
    try:
        _book(agent, cash=40.0, positions=[_pos("AAVE", qty=0.5, price=86.0)])
        agent.portfolio.harvest_anchor_px = 86.0
        actions: list = []
        snap = _snap(ranked=[_sig("AAVE", 2.0, 87.0)])          # +1.16% < 3.5% band
        agent._run_harvest(snap, {"AAVE": 87.0}, _posture(), actions, dry_run=False, now=_NOW)

        assert agent.portfolio.harvest_anchor_px == 86.0          # anchor unchanged
        assert abs(agent.portfolio.positions["AAVE"].qty - 0.5) < 1e-9
        assert actions == []
    finally:
        agent.close()


def test_harvest_disabled_is_a_no_op(tmp_path, monkeypatch):
    """With the harvester off, even a big move never touches the book."""
    agent = _hagent(tmp_path, monkeypatch, harvest_enabled=False)
    try:
        _book(agent, cash=0.30, positions=[_pos("AAVE", qty=1.0, price=86.0)])
        agent.portfolio.harvest_anchor_px = 86.0
        actions: list = []
        snap = _snap(ranked=[_sig("AAVE", 2.0, 95.0)])
        agent._run_harvest(snap, {"AAVE": 95.0}, _posture(), actions, dry_run=False, now=_NOW)

        assert abs(agent.portfolio.positions["AAVE"].qty - 1.0) < 1e-9
        assert actions == []
    finally:
        agent.close()


def test_harvest_banks_even_when_halted(tmp_path, monkeypatch):
    """Banking a rip is NOT gated by the drawdown halt -- gains are always locked
    in (selling only ever reduces risk)."""
    agent = _hagent(tmp_path, monkeypatch)
    try:
        _book(agent, cash=0.30, positions=[_pos("AAVE", qty=1.0, price=86.0)])
        agent.portfolio.harvest_anchor_px = 86.0
        actions: list = []
        snap = _snap(ranked=[_sig("AAVE", 2.0, 90.0)])
        agent._run_harvest(snap, {"AAVE": 90.0}, _halted_posture(), actions, dry_run=False, now=_NOW)

        assert agent.portfolio.positions["AAVE"].qty < 0.95       # still banked
        assert any(a.kind == "exit" and "harvest-bank" in a.detail for a in actions)
    finally:
        agent.close()


def test_harvest_dip_buy_blocked_when_halted(tmp_path, monkeypatch):
    """A dip-buy IS gated: under a halt no new risk is added (the anchor still
    advances so the grid resumes cleanly once the halt clears)."""
    agent = _hagent(tmp_path, monkeypatch)
    try:
        _book(agent, cash=40.0, positions=[_pos("AAVE", qty=0.5, price=86.0)])
        agent.portfolio.harvest_anchor_px = 86.0
        actions: list = []
        snap = _snap(ranked=[_sig("AAVE", 2.0, 82.0)])
        agent._run_harvest(snap, {"AAVE": 82.0}, _halted_posture(), actions, dry_run=False, now=_NOW)

        assert abs(agent.portfolio.positions["AAVE"].qty - 0.5) < 1e-9   # no buy
        assert agent.portfolio.cash == 40.0
        assert not any(a.kind == "entry" for a in actions)
    finally:
        agent.close()


def test_harvest_dip_buy_blocked_by_kill_switch(tmp_path, monkeypatch):
    """The kill-switch also blocks dip-buys (no new risk while tripped)."""
    agent = _hagent(tmp_path, monkeypatch)
    try:
        agent.sentinel.kill_switch_engaged = lambda: True  # type: ignore[assignment]
        _book(agent, cash=40.0, positions=[_pos("AAVE", qty=0.5, price=86.0)])
        agent.portfolio.harvest_anchor_px = 86.0
        actions: list = []
        snap = _snap(ranked=[_sig("AAVE", 2.0, 82.0)])
        agent._run_harvest(snap, {"AAVE": 82.0}, _posture(), actions, dry_run=False, now=_NOW)

        assert abs(agent.portfolio.positions["AAVE"].qty - 0.5) < 1e-9
        assert not any(a.kind == "entry" for a in actions)
    finally:
        agent.close()


def test_harvest_skips_trades_below_gas_floor(tmp_path, monkeypatch):
    """A slice worth less than ``harvest_min_trade_usd`` is skipped (gas economy),
    while the anchor still advances past the level."""
    agent = _hagent(tmp_path, monkeypatch)
    try:
        # 20% of 0.1 units @ ~90 ~= $1.8 -- below the $4 floor.
        _book(agent, cash=0.30, positions=[_pos("AAVE", qty=0.1, price=86.0)])
        agent.portfolio.harvest_anchor_px = 86.0
        actions: list = []
        snap = _snap(ranked=[_sig("AAVE", 2.0, 90.0)])
        agent._run_harvest(snap, {"AAVE": 90.0}, _posture(), actions, dry_run=False, now=_NOW)

        assert abs(agent.portfolio.positions["AAVE"].qty - 0.1) < 1e-9   # not sold
        assert agent.portfolio.harvest_anchor_px == 90.0                 # but advanced
        assert actions == []
    finally:
        agent.close()


def test_harvest_armed_swing_short_circuits(tmp_path, monkeypatch):
    """While a manual swing is armed the harvester stands down entirely (it never
    fights an operator-directed swing), leaving even the anchor untouched."""
    agent = _hagent(tmp_path, monkeypatch)
    try:
        _book(agent, cash=0.30, positions=[_pos("AAVE", qty=1.0, price=86.0)])
        agent.portfolio.harvest_anchor_px = 86.0
        agent.portfolio.swing_armed = True
        actions: list = []
        snap = _snap(ranked=[_sig("AAVE", 2.0, 90.0)])
        agent._run_harvest(snap, {"AAVE": 90.0}, _posture(), actions, dry_run=False, now=_NOW)

        assert abs(agent.portfolio.positions["AAVE"].qty - 1.0) < 1e-9
        assert agent.portfolio.harvest_anchor_px == 86.0
        assert actions == []
    finally:
        agent.close()


def test_harvest_anchor_persists_across_restart(tmp_path, monkeypatch):
    """The moving anchor survives a reload so the grid is continuous across the
    agent's frequent restarts."""
    monkeypatch.delenv("HELM_SWING_CMD", raising=False)
    agent = _hagent(tmp_path, monkeypatch)
    try:
        _book(agent, cash=10.0, positions=[_pos("AAVE", qty=0.5, price=86.0)])
        agent.portfolio.harvest_anchor_px = 87.53
        agent._save_state()
    finally:
        agent.close()

    restored = _hagent(tmp_path, monkeypatch)
    try:
        assert restored.portfolio.harvest_anchor_px == 87.53
    finally:
        restored.close()


def test_harvest_trail_locks_profit_on_a_giveback(tmp_path, monkeypatch):
    """With the trailing lock armed, a give-back from a fresh peak while the core
    is in profit banks a slice to cash AHEAD of a deeper dip -- without waiting
    for the full down-step. This is the "cash the top before it rolls over" leg."""
    agent = _hagent(tmp_path, monkeypatch, harvest_trail_giveback_pct=0.03)
    try:
        _book(agent, cash=0.30, positions=[_pos("AAVE", qty=1.0, price=86.0)])
        agent.portfolio.harvest_anchor_px = 95.0
        agent.portfolio.harvest_peak_px = 95.0
        actions: list = []
        # 92.0 is -3.2% off the 95 peak (trail fires) yet still inside the 3.5%
        # band off the anchor (no ordinary dip-buy), and well above the entry.
        snap = _snap(ranked=[_sig("AAVE", 2.0, 92.0)])
        agent._run_harvest(snap, {"AAVE": 92.0}, _posture(), actions, dry_run=False, now=_NOW)

        assert abs(agent.portfolio.positions["AAVE"].qty - 0.80) < 0.02   # ~20% banked
        assert agent.portfolio.cash > 15.0                               # USDT in hand
        locks = [a for a in actions if a.kind == "exit" and "trail-lock" in a.detail]
        assert locks and "pnl +" in locks[0].detail                      # realized gain
        assert agent.portfolio.harvest_peak_px == 92.0                   # re-armed at px
    finally:
        agent.close()


def test_harvest_trail_off_by_default_is_a_no_op(tmp_path, monkeypatch):
    """With the give-back at 0.0 (the default) the trail leg never fires: a small
    pullback inside the band does nothing -- existing behaviour is preserved."""
    agent = _hagent(tmp_path, monkeypatch)   # harvest_trail_giveback_pct defaults to 0.0
    try:
        _book(agent, cash=0.30, positions=[_pos("AAVE", qty=1.0, price=86.0)])
        agent.portfolio.harvest_anchor_px = 95.0
        agent.portfolio.harvest_peak_px = 95.0
        actions: list = []
        snap = _snap(ranked=[_sig("AAVE", 2.0, 92.0)])
        agent._run_harvest(snap, {"AAVE": 92.0}, _posture(), actions, dry_run=False, now=_NOW)

        assert abs(agent.portfolio.positions["AAVE"].qty - 1.0) < 1e-9    # untouched
        assert actions == []
    finally:
        agent.close()


def test_harvest_trail_holds_when_underwater(tmp_path, monkeypatch):
    """The trail leg never realizes a loss: a give-back from the peak while price
    is below the average entry does nothing (catastrophe cover is the stop's job,
    not the profit-lock's)."""
    agent = _hagent(tmp_path, monkeypatch, harvest_trail_giveback_pct=0.03)
    try:
        # Entry 93 > current 92 -> underwater; the trail must stand down.
        _book(agent, cash=0.30, positions=[_pos("AAVE", qty=1.0, price=93.0)])
        agent.portfolio.harvest_anchor_px = 95.0
        agent.portfolio.harvest_peak_px = 95.0
        actions: list = []
        snap = _snap(ranked=[_sig("AAVE", 2.0, 92.0)])
        agent._run_harvest(snap, {"AAVE": 92.0}, _posture(), actions, dry_run=False, now=_NOW)

        assert abs(agent.portfolio.positions["AAVE"].qty - 1.0) < 1e-9    # not sold
        assert not any(a.kind == "exit" for a in actions)
    finally:
        agent.close()


def test_harvest_trail_peak_persists_across_restart(tmp_path, monkeypatch):
    """The running peak survives a reload so the trailing lock is continuous
    across the agent's frequent restarts."""
    monkeypatch.delenv("HELM_SWING_CMD", raising=False)
    agent = _hagent(tmp_path, monkeypatch, harvest_trail_giveback_pct=0.03)
    try:
        _book(agent, cash=10.0, positions=[_pos("AAVE", qty=0.5, price=86.0)])
        agent.portfolio.harvest_peak_px = 98.42
        agent._save_state()
    finally:
        agent.close()

    restored = _hagent(tmp_path, monkeypatch, harvest_trail_giveback_pct=0.03)
    try:
        assert restored.portfolio.harvest_peak_px == 98.42
    finally:
        restored.close()


# ============================================================================
# TRAIL GUARD -- the trailing profit-lock extended to EVERY held name (not just
# the swing symbol). A large second holding (e.g. INJ) is otherwise protected
# only by its full ~8% trailing stop; this banks a slice as its top rolls over.
# ============================================================================
def _inj(qty: float, entry: float, peak: float, anchor: float = 0.0) -> Position:
    """A non-swing INJ position with an explicit peak (highest_price) + re-arm."""
    pos = _pos("INJ", qty=qty, price=entry)
    pos.highest_price = peak
    pos.trail_anchor = anchor
    return pos


def test_trail_guard_locks_profit_on_a_giveback(tmp_path, monkeypatch):
    """A non-swing holding in profit that gives back the give-back from a fresh
    peak banks a slice to USDT -- the same "cash the top" leg the harvester runs
    on the swing name, now covering the rest of the book (here INJ)."""
    agent = _hagent(tmp_path, monkeypatch, harvest_trail_giveback_pct=0.03)
    try:
        # Peak 5.00, now 4.85 = -3% off the high, still above the 4.50 entry.
        _book(agent, cash=0.30, positions=[_inj(qty=10.0, entry=4.50, peak=5.00)])
        actions: list = []
        agent._run_trail_guard({"INJ": 4.85}, actions, dry_run=False)

        assert abs(agent.portfolio.positions["INJ"].qty - 8.0) < 0.05      # ~20% banked
        assert agent.portfolio.cash > 9.0                                  # USDT in hand
        locks = [a for a in actions if a.kind == "exit" and "trail-lock" in a.detail]
        assert locks and "pnl +" in locks[0].detail                       # realized gain
        assert agent.portfolio.positions["INJ"].trail_anchor == 5.00       # re-armed at peak
    finally:
        agent.close()


def test_trail_guard_off_by_default_is_a_no_op(tmp_path, monkeypatch):
    """With the give-back at 0.0 (the default) the guard never fires -- existing
    behaviour across the whole book is byte-for-byte preserved."""
    agent = _hagent(tmp_path, monkeypatch)   # harvest_trail_giveback_pct defaults to 0.0
    try:
        _book(agent, cash=0.30, positions=[_inj(qty=10.0, entry=4.50, peak=5.00)])
        actions: list = []
        agent._run_trail_guard({"INJ": 4.85}, actions, dry_run=False)

        assert abs(agent.portfolio.positions["INJ"].qty - 10.0) < 1e-9     # untouched
        assert actions == []
    finally:
        agent.close()


def test_trail_guard_holds_when_underwater(tmp_path, monkeypatch):
    """The guard never realizes a loss: a give-back from the peak while price is
    below the average entry does nothing (that fall is the stop's job)."""
    agent = _hagent(tmp_path, monkeypatch, harvest_trail_giveback_pct=0.03)
    try:
        # Entry 5.00 > current 4.85 -> underwater; the guard must stand down.
        _book(agent, cash=0.30, positions=[_inj(qty=10.0, entry=5.00, peak=5.00)])
        actions: list = []
        agent._run_trail_guard({"INJ": 4.85}, actions, dry_run=False)

        assert abs(agent.portfolio.positions["INJ"].qty - 10.0) < 1e-9     # not sold
        assert not any(a.kind == "exit" for a in actions)
    finally:
        agent.close()


def test_trail_guard_fires_once_per_fresh_high(tmp_path, monkeypatch):
    """After locking once the guard re-arms to the peak: it will NOT fire again
    while price drifts under that same high, only after a genuinely NEW high."""
    agent = _hagent(tmp_path, monkeypatch, harvest_trail_giveback_pct=0.03)
    try:
        _book(agent, cash=0.30, positions=[_inj(qty=10.0, entry=4.50, peak=5.00)])
        actions: list = []
        agent._run_trail_guard({"INJ": 4.85}, actions, dry_run=False)   # fires, anchor->5.00
        qty_after_first = agent.portfolio.positions["INJ"].qty
        assert qty_after_first < 9.0

        # Same high, price drifts a touch lower -> no new high -> must NOT re-fire.
        actions2: list = []
        agent._run_trail_guard({"INJ": 4.80}, actions2, dry_run=False)
        assert abs(agent.portfolio.positions["INJ"].qty - qty_after_first) < 1e-9
        assert actions2 == []

        # A genuinely NEW high then a fresh give-back re-arms and fires again.
        agent.portfolio.positions["INJ"].highest_price = 5.40
        actions3: list = []
        agent._run_trail_guard({"INJ": 5.23}, actions3, dry_run=False)   # -3.1% off 5.40
        assert agent.portfolio.positions["INJ"].qty < qty_after_first
        assert any("trail-lock" in a.detail for a in actions3)
    finally:
        agent.close()


def test_trail_guard_leaves_the_swing_symbol_to_the_harvester(tmp_path, monkeypatch):
    """The swing symbol is the harvester's domain; the guard must never touch it
    (double-management would bank two slices on one rollover)."""
    agent = _hagent(tmp_path, monkeypatch, harvest_trail_giveback_pct=0.03)
    try:
        # AAVE is the configured swing symbol; give it a textbook give-back.
        aave = _pos("AAVE", qty=1.0, price=86.0)
        aave.highest_price = 95.0
        _book(agent, cash=0.30, positions=[aave])
        actions: list = []
        agent._run_trail_guard({"AAVE": 92.0}, actions, dry_run=False)

        assert abs(agent.portfolio.positions["AAVE"].qty - 1.0) < 1e-9    # untouched
        assert actions == []
    finally:
        agent.close()


def test_trail_anchor_persists_across_restart(tmp_path, monkeypatch):
    """The per-position re-arm anchor survives a reload so the guard stays
    continuous across the agent's frequent restarts."""
    monkeypatch.delenv("HELM_SWING_CMD", raising=False)
    agent = _hagent(tmp_path, monkeypatch, harvest_trail_giveback_pct=0.03)
    try:
        _book(agent, cash=10.0, positions=[_inj(qty=5.0, entry=4.50, peak=5.20, anchor=5.20)])
        agent._save_state()
    finally:
        agent.close()

    restored = _hagent(tmp_path, monkeypatch, harvest_trail_giveback_pct=0.03)
    try:
        assert restored.portfolio.positions["INJ"].trail_anchor == 5.20
    finally:
        restored.close()


# ============================================================================
# CASH OUT -- whole-book flatten (sellall) + hold-cash latch + dip rebuy.
# The operator's "close everything that's about to dip, wait, then rebuy" lever.
# ============================================================================
def test_sellall_flattens_whole_book_and_arms_rebuy(tmp_path, monkeypatch):
    """``sellall`` liquidates EVERY non-stable holding to cash -- the swing name
    via the armed swing-sell, every other name via a full flatten -- and latches
    ``swing_flat`` so the freed cash is held for the dip rebuy."""
    monkeypatch.setenv("HELM_SWING_CMD", "sellall#42")
    agent = _hagent(tmp_path, monkeypatch)
    monkeypatch.setenv("HELM_SWING_CMD", "sellall#42")   # _hagent cleared it; re-set
    try:
        _book(agent, cash=1.0, positions=[
            _pos("AAVE", qty=1.0, price=90.0),       # the swing symbol
            _inj(qty=10.0, entry=4.50, peak=5.00),   # a second, larger holding
        ])
        prices = {"AAVE": 95.0, "INJ": 4.85}
        snap = _snap(ranked=[_sig("AAVE", 4.0, 95.0), _sig("INJ", 2.0, 4.85)])
        actions: list = []
        agent._run_swing(snap, prices, actions, dry_run=False, now=_NOW)

        assert "AAVE" not in agent.portfolio.positions    # swing name flattened
        assert "INJ" not in agent.portfolio.positions     # other name flattened too
        assert agent.portfolio.cash > 130.0               # ~$95 + ~$48 freed to USDT
        assert agent.portfolio.swing_armed is True        # rebuy armed on the swing name
        assert agent.portfolio.swing_flat is True         # cash held for the dip
        assert sum(1 for a in actions if a.kind == "exit") == 2
    finally:
        agent.close()


def test_swing_flat_blocks_entries_and_rotation(tmp_path, monkeypatch):
    """While the cash-out latch is set, neither entries nor rotation may redeploy
    the held cash -- it waits for the dip rebuy."""
    agent = _hagent(tmp_path, monkeypatch)
    try:
        _book(agent, cash=80.0, positions=[])
        agent.portfolio.swing_flat = True
        prices = {"INJ": 4.85}
        snap = _snap(ranked=[_sig("INJ", 3.0, 4.85)])
        actions: list = []
        agent._run_entries(snap, prices, _posture(), actions, dry_run=False, now=_NOW)
        agent._run_rotation(snap, prices, _posture(), actions, dry_run=False, now=_NOW)

        assert agent.portfolio.positions == {}            # nothing bought
        assert agent.portfolio.cash == 80.0               # cash untouched
    finally:
        agent.close()


def test_swing_flat_clears_on_dip_rebuy(tmp_path, monkeypatch):
    """When the armed dip rebuy finally fires, the cash-out latch clears so the
    engine resumes normal allocation on later cycles."""
    monkeypatch.delenv("HELM_SWING_CMD", raising=False)
    agent = _hagent(tmp_path, monkeypatch)
    try:
        _book(agent, cash=80.0, positions=[])
        p = agent.portfolio
        p.swing_armed = True
        p.swing_flat = True
        p.swing_sell_px = 95.0                            # rebuy trigger = 95 * (1-0.02) = 93.1
        prices = {"AAVE": 92.0}                           # below the trigger -> fires
        snap = _snap(ranked=[_sig("AAVE", 4.0, 92.0)])
        actions: list = []
        agent._run_swing(snap, prices, actions, dry_run=False, now=_NOW)

        assert "AAVE" in agent.portfolio.positions        # redeployed on the dip
        assert agent.portfolio.swing_armed is False
        assert agent.portfolio.swing_flat is False        # latch cleared
    finally:
        agent.close()


def test_sellall_off_clears_the_latch(tmp_path, monkeypatch):
    """``off`` cancels a pending cash-out wait (disarms + unlatches)."""
    monkeypatch.setenv("HELM_SWING_CMD", "off#7")
    agent = _hagent(tmp_path, monkeypatch)
    monkeypatch.setenv("HELM_SWING_CMD", "off#7")
    try:
        _book(agent, cash=80.0, positions=[])
        agent.portfolio.swing_armed = True
        agent.portfolio.swing_flat = True
        prices = {"AAVE": 92.0}
        snap = _snap(ranked=[_sig("AAVE", 4.0, 92.0)])
        actions: list = []
        agent._run_swing(snap, prices, actions, dry_run=False, now=_NOW)

        assert agent.portfolio.swing_armed is False
        assert agent.portfolio.swing_flat is False
    finally:
        agent.close()


def test_swing_flat_persists_across_restart(tmp_path, monkeypatch):
    """The cash-out latch survives a reload so a restart never accidentally
    redeploys the held cash."""
    monkeypatch.delenv("HELM_SWING_CMD", raising=False)
    agent = _hagent(tmp_path, monkeypatch)
    try:
        _book(agent, cash=80.0, positions=[])
        agent.portfolio.swing_flat = True
        agent._save_state()
    finally:
        agent.close()

    restored = _hagent(tmp_path, monkeypatch)
    try:
        assert restored.portfolio.swing_flat is True
    finally:
        restored.close()


def test_harvest_owns_swing_symbol_in_block_helper(tmp_path, monkeypatch):
    """When harvesting, the symbol is blocked from the normal entry/rotation
    engine unconditionally -- the grid owns its whole inventory."""
    agent = _hagent(tmp_path, monkeypatch)
    try:
        # No swing armed, no standing target, sub-dust cash: only harvest ownership
        # can produce a block here.
        _book(agent, cash=0.20, positions=[_pos("AAVE", qty=0.5, price=86.0)])
        assert agent.portfolio.swing_armed is False
        assert agent._swing_block_symbol() == "AAVE"
    finally:
        agent.close()


def test_harvest_dry_run_only_previews(tmp_path, monkeypatch):
    """In dry-run mode the harvester reports intent but never fills."""
    agent = _hagent(tmp_path, monkeypatch)
    try:
        _book(agent, cash=0.30, positions=[_pos("AAVE", qty=1.0, price=86.0)])
        agent.portfolio.harvest_anchor_px = 86.0
        actions: list = []
        snap = _snap(ranked=[_sig("AAVE", 2.0, 90.0)])
        agent._run_harvest(snap, {"AAVE": 90.0}, _posture(), actions, dry_run=True, now=_NOW)

        assert abs(agent.portfolio.positions["AAVE"].qty - 1.0) < 1e-9   # no fill
        assert any(a.kind == "dry_run" and "would harvest-bank" in a.detail for a in actions)
    finally:
        agent.close()


def test_harvest_param_reads_and_clamps_env(tmp_path, monkeypatch):
    """Live env knobs override the profile default and are clamped to safe bounds;
    a bad value falls back to the default."""
    agent = _hagent(tmp_path, monkeypatch)
    try:
        # In-range override is honoured.
        monkeypatch.setenv("HELM_HARVEST_STEP", "0.10")
        assert abs(agent._harvest_param("HELM_HARVEST_STEP", 0.035, 0.005, 0.5) - 0.10) < 1e-9
        # Out-of-range is clamped.
        monkeypatch.setenv("HELM_HARVEST_STEP", "0.0001")
        assert abs(agent._harvest_param("HELM_HARVEST_STEP", 0.035, 0.005, 0.5) - 0.005) < 1e-9
        monkeypatch.setenv("HELM_HARVEST_FRAC", "5")
        assert abs(agent._harvest_param("HELM_HARVEST_FRAC", 0.20, 0.02, 0.90) - 0.90) < 1e-9
        # Garbage falls back to the default.
        monkeypatch.setenv("HELM_HARVEST_FRAC", "notanumber")
        assert abs(agent._harvest_param("HELM_HARVEST_FRAC", 0.20, 0.02, 0.90) - 0.20) < 1e-9
        # Unset uses the default.
        monkeypatch.delenv("HELM_HARVEST_FRAC", raising=False)
        assert abs(agent._harvest_param("HELM_HARVEST_FRAC", 0.20, 0.02, 0.90) - 0.20) < 1e-9
    finally:
        agent.close()


def test_harvest_frac_env_override_resizes_slice(tmp_path, monkeypatch):
    """A live HELM_HARVEST_FRAC change resizes the banked slice without a redeploy."""
    agent = _hagent(tmp_path, monkeypatch)
    try:
        monkeypatch.setenv("HELM_HARVEST_FRAC", "0.50")
        _book(agent, cash=0.30, positions=[_pos("AAVE", qty=1.0, price=86.0)])
        agent.portfolio.harvest_anchor_px = 86.0
        actions: list = []
        snap = _snap(ranked=[_sig("AAVE", 2.0, 90.0)])
        agent._run_harvest(snap, {"AAVE": 90.0}, _posture(), actions, dry_run=False, now=_NOW)

        # 50% slice banked -> roughly half the position sold.
        assert abs(agent.portfolio.positions["AAVE"].qty - 0.50) < 0.03
        assert any(a.kind == "exit" and "harvest-bank" in a.detail for a in actions)
    finally:
        agent.close()


# ============================================================================
# Harvester <-> rotation/entry coordination (endgame top_n=2 diversification).
#
# Live the harvester owns the swing symbol's core inventory. These invariants
# keep the rest of the engine coherent with it while still letting the agent
# diversify itself into the 2nd strongest momentum name (the AAVE+XPL book):
#   1. with rebalance OFF (default), rotation never sells the harvest-owned core
#      as "dead weight" -- only the harvester trims it;
#   2. with rebalance ON (contest 'max'), rotation may trim only the core's
#      EXCESS above an equal-weight target to seed a higher-ranked leader the
#      agent holds none of -- the autonomy primitive that breaks a 100%-one-name
#      corner without operator help; the core itself is never cut below target;
#   3. cash (banked by the harvester or freed by a rebalance) deploys into the
#      under-held ranked name via the normal entry loop, not back into the core.
# ============================================================================
def test_rotation_never_sells_harvest_owned_core(tmp_path, monkeypatch):
    """With rebalance OFF (the default), the harvest-owned core is fully
    protected from rotation: even as a big, old, cash-constrained holding the
    leader dominates by a wide edge, rotation leaves it untouched (only the
    harvester may trim that inventory)."""
    agent = _hagent(tmp_path, monkeypatch)      # balanced -> rotation_rebalance_enabled False
    try:
        # ~100% in AAVE with dust cash: big + capital-constrained + old, and the
        # unheld leader XPL beats AAVE by a huge composite edge -> absent the
        # harvest-core guard this is a textbook rotation sell.
        _book(agent, cash=0.30, positions=[_pos("AAVE", qty=1.0, price=86.0, age_h=24)])
        prices = {"AAVE": 86.0, "XPL": 1.20}
        snap = _snap(ranked=[_sig("XPL", 2.9, 1.20), _sig("AAVE", 0.10, 86.0)])
        actions: list = []
        agent._run_rotation(snap, prices, _posture(), actions, dry_run=False, now=_NOW)

        assert "AAVE" in agent.portfolio.positions                  # core untouched
        assert abs(agent.portfolio.positions["AAVE"].qty - 1.0) < 1e-9
        assert not any(a.kind == "exit" for a in actions)           # no rotation fired
    finally:
        agent.close()


def test_rotation_rebalances_excess_to_seed_leader(tmp_path, monkeypatch):
    """With rebalance ON, an over-concentrated agent seeds a higher-ranked leader
    it holds none of -- by itself. From 100%% AAVE with no cash and XPL ranked #1,
    rotation trims only AAVE's EXCESS above the equal-weight target (~half) into
    cash; the freed USDT then funds XPL via the entry loop. The AAVE core is
    preserved (never cut below target). This is the autonomy that ends the
    'dull, can't act on its own signal' corner -- no operator nudge required."""
    agent = _hagent(tmp_path, monkeypatch, rotation_rebalance_enabled=True)
    agent.settings.signals = replace(agent.settings.signals, top_n=2)
    try:
        _book(agent, cash=0.30, positions=[_pos("AAVE", qty=1.0, price=86.0, age_h=24)])
        prices = {"AAVE": 86.0, "XPL": 1.20}
        # XPL ranks #1 (unheld leader); AAVE #2 (over-weight, the funding source).
        snap = _snap(ranked=[_sig("XPL", 2.0, 1.20), _sig("AAVE", 1.8, 86.0)])
        actions: list = []
        agent._run_rotation(snap, prices, _posture(), actions, dry_run=False, now=_NOW)

        target_each = (86.0 + 0.30) / 2                   # equal-weight target per name
        aave = agent.portfolio.positions.get("AAVE")
        assert aave is not None and aave.qty < 1.0        # only the excess trimmed
        assert aave.qty * 86.0 >= target_each - 2.0       # core preserved at ~target
        assert agent.portfolio.cash >= target_each - 5.0  # ~half freed to seed XPL
        assert any(a.kind == "exit" and "rebalance->XPL" in a.detail for a in actions)
    finally:
        agent.close()


def test_rotation_rebalance_keeps_stronger_name_when_leader_weaker(tmp_path, monkeypatch):
    """Rebalance tilts only toward strength: if the unheld leader is WEAKER than
    the over-weight core, the agent does not trim the stronger name to fund it."""
    agent = _hagent(tmp_path, monkeypatch, rotation_rebalance_enabled=True)
    agent.settings.signals = replace(agent.settings.signals, top_n=2)
    try:
        _book(agent, cash=0.30, positions=[_pos("AAVE", qty=1.0, price=86.0, age_h=24)])
        prices = {"AAVE": 86.0, "XPL": 1.20}
        # XPL is ranked but WEAKER than the AAVE we hold -> no rebalance.
        snap = _snap(ranked=[_sig("AAVE", 2.4, 86.0), _sig("XPL", 1.1, 1.20)])
        actions: list = []
        agent._run_rotation(snap, prices, _posture(), actions, dry_run=False, now=_NOW)

        assert abs(agent.portfolio.positions["AAVE"].qty - 1.0) < 1e-9   # untouched
        assert not any(a.kind == "exit" for a in actions)
    finally:
        agent.close()


def test_entries_deploy_banked_cash_into_second_name(tmp_path, monkeypatch):
    """With top_n=2 and the harvester owning the core, USDT banked from a rip is
    deployed into the 2nd-ranked momentum name -- not back into the blocked core
    -- realizing the self-funding AAVE+XPL diversification."""
    # Endgame sizing: the live max profile lets a single name run to 80% so the
    # banked USDT clears the Sentinel's economic floor in one deploy.
    agent = _hagent(tmp_path, monkeypatch, max_position_pct=0.80)
    agent.settings.signals = replace(agent.settings.signals, top_n=2)
    try:
        # AAVE core ($43) + USDT the harvester has already banked ($60).
        _book(agent, cash=60.0, positions=[_pos("AAVE", qty=0.5, price=86.0)])
        prices = {"AAVE": 86.0, "XPL": 1.20}
        before_aave = agent.portfolio.positions["AAVE"].qty
        # AAVE ranks #1 (blocked from re-entry); XPL #2 is the deployable name.
        xpl = replace(_sig("XPL", 2.6, 1.20), atr=0.03)
        snap = _snap(ranked=[_sig("AAVE", 2.8, 86.0), xpl])
        actions: list = []
        agent._run_entries(snap, prices, _posture(), actions, dry_run=False, now=_NOW)

        assert "XPL" in agent.portfolio.positions                   # diversified in
        assert agent.portfolio.positions["XPL"].qty > 0
        assert abs(agent.portfolio.positions["AAVE"].qty - before_aave) < 1e-9  # core not re-bought
        assert agent.portfolio.cash < 60.0                          # banked USDT deployed
        assert any(a.kind == "entry" and a.symbol == "XPL" for a in actions)
    finally:
        agent.close()

