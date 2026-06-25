"""Leader rotation: recycle dead-weight capital into the engine's current #1.

Momentum decays. A name bought days ago can fall out of the ranked shortlist
while still hogging most of the book, so a nearly-fully-invested agent can never
fund the live leader from cash. ``Agent._run_rotation`` closes that gap: it sells
the worst stale holding (one the engine no longer ranks, dominated by the leader
by a clear margin) so the same cycle's entry step funds the leader.

These tests pin the hysteresis that keeps it from thrashing or chasing noise:
  • fires on a large stale chunk OR when capital-constrained,
  • never rotates a fresh position (min-hold), a thin one (min size), or one the
    leader doesn't clearly beat (composite edge),
  • never rotates when no ranked leader needs funding,
  • dry-run is observe-only, and the feature is killable from config.
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
def _agent(tmp_path, settings=None) -> Agent:
    """A hermetic paper agent on a throwaway state/ledger (never the live book).

    Pins the *balanced* profile when none is supplied so the test is reproducible
    even when the live contest profile (``max``) is staged in ``.env``.
    """
    if settings is None:
        prev = os.environ.get("HELM_PROFILE")
        os.environ["HELM_PROFILE"] = "balanced"
        try:
            settings = load_settings()
        finally:
            if prev is None:
                os.environ.pop("HELM_PROFILE", None)
            else:
                os.environ["HELM_PROFILE"] = prev
    return Agent(settings=settings, state_path=tmp_path / "state.json",
                 ledger_path=tmp_path / "audit.jsonl")


def _sig(symbol: str, composite: float, price: float = 100.0) -> SymbolSignal:
    return SymbolSignal(symbol=symbol, price=price, composite=composite,
                        liquidity_usd=5e7, passes_liquidity=True,
                        passes_quality=True, passes_cost=True)


def _snap(ranked: list[SymbolSignal], signals: list[SymbolSignal]) -> SignalSnapshot:
    # _run_rotation never reads snap.regime, so a bare snapshot is sufficient.
    return SignalSnapshot(regime=None, signals=signals, ranked=ranked)


def _pos(symbol: str, qty: float, price: float, age_h: float) -> Position:
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
    # Minimal posture stub: only the fields _run_entries + Sentinel actually read.
    return SimpleNamespace(max_gross_pct=max_gross, per_trade_risk_pct=risk_pct,
                           halt_new_risk=False, posture="build")


# ------------------------------------------------------------------- the path
def test_rotation_sells_stale_into_leader(tmp_path):
    """A large, old, no-longer-ranked holding is sold so the leader can be funded."""
    agent = _agent(tmp_path)
    try:
        _book(agent, cash=1.0, positions=[_pos("AVAX", qty=0.7, price=100.0, age_h=24)])
        prices = {"AVAX": 100.0, "AAVE": 200.0}
        snap = _snap(ranked=[_sig("AAVE", 2.7, 200.0)],
                     signals=[_sig("AAVE", 2.7, 200.0), _sig("AVAX", 0.1, 100.0)])
        actions: list = []
        agent._run_rotation(snap, prices, posture=None, actions=actions,
                            dry_run=False, now=_NOW)

        assert "AVAX" not in agent.portfolio.positions          # dead weight sold
        assert agent.portfolio.cash > 60.0                      # ~$70 freed (minus costs)
        assert any(a.kind == "exit" and "rotate->AAVE" in a.detail for a in actions)
        assert any(r.get("type") == "trade"
                   and r.get("data", {}).get("reason") == "rotation"
                   and r.get("data", {}).get("into") == "AAVE"
                   for r in agent.ledger.tail(5))
    finally:
        agent.close()


def test_rotation_fires_when_cash_constrained_even_if_not_big(tmp_path):
    """A mid-size stale name still rotates when we have no deployable cash."""
    agent = _agent(tmp_path)
    try:
        # AVAX is only ~30% of equity (below big-holding frac) but cash is dust,
        # so the capital-constrained trigger carries it.
        _book(agent, cash=1.0, positions=[
            _pos("AVAX", qty=0.3, price=100.0, age_h=24),   # $30
            _pos("AAVE", qty=0.35, price=200.0, age_h=24),  # $70 (held leader, funded)
        ])
        prices = {"AVAX": 100.0, "AAVE": 200.0, "LINK": 20.0}
        # LINK is the unheld leader that needs funding.
        snap = _snap(ranked=[_sig("LINK", 2.9, 20.0)],
                     signals=[_sig("LINK", 2.9, 20.0), _sig("AVAX", 0.2, 100.0),
                              _sig("AAVE", 0.3, 200.0)])
        actions: list = []
        agent._run_rotation(snap, prices, None, actions, dry_run=False, now=_NOW)
        assert "AVAX" not in agent.portfolio.positions
        assert any(a.kind == "exit" and "rotate->LINK" in a.detail for a in actions)
    finally:
        agent.close()


# --------------------------------------------------------------- the brakes
def test_rotation_respects_min_hold(tmp_path):
    """A freshly opened position is never rotated (anti-whipsaw)."""
    agent = _agent(tmp_path)
    try:
        _book(agent, cash=1.0, positions=[_pos("AVAX", qty=0.7, price=100.0, age_h=1.0)])
        prices = {"AVAX": 100.0, "AAVE": 200.0}
        snap = _snap(ranked=[_sig("AAVE", 2.7, 200.0)],
                     signals=[_sig("AAVE", 2.7, 200.0), _sig("AVAX", 0.1, 100.0)])
        actions: list = []
        agent._run_rotation(snap, prices, None, actions, dry_run=False, now=_NOW)
        assert "AVAX" in agent.portfolio.positions
        assert actions == []
    finally:
        agent.close()


def test_rotation_requires_composite_edge(tmp_path):
    """No rotation when the leader doesn't clearly beat the stale name."""
    agent = _agent(tmp_path)
    try:
        _book(agent, cash=1.0, positions=[_pos("AVAX", qty=0.7, price=100.0, age_h=24)])
        prices = {"AVAX": 100.0, "AAVE": 200.0}
        # edge = 0.30 - 0.20 = 0.10 < rotation_min_edge (0.40)
        snap = _snap(ranked=[_sig("AAVE", 0.30, 200.0)],
                     signals=[_sig("AAVE", 0.30, 200.0), _sig("AVAX", 0.20, 100.0)])
        actions: list = []
        agent._run_rotation(snap, prices, None, actions, dry_run=False, now=_NOW)
        assert "AVAX" in agent.portfolio.positions
        assert actions == []
    finally:
        agent.close()


def test_rotation_skips_tiny_holdings(tmp_path):
    """A holding below the min-stale size isn't worth the gas to rotate."""
    agent = _agent(tmp_path)
    try:
        # $5 AVAX < rotation_min_stale_usd ($10); cash low; leader unheld.
        _book(agent, cash=1.0, positions=[_pos("AVAX", qty=0.05, price=100.0, age_h=24)])
        prices = {"AVAX": 100.0, "AAVE": 200.0}
        snap = _snap(ranked=[_sig("AAVE", 2.7, 200.0)],
                     signals=[_sig("AAVE", 2.7, 200.0), _sig("AVAX", 0.1, 100.0)])
        actions: list = []
        agent._run_rotation(snap, prices, None, actions, dry_run=False, now=_NOW)
        assert "AVAX" in agent.portfolio.positions
        assert actions == []
    finally:
        agent.close()


def test_rotation_skips_when_leader_already_funded(tmp_path):
    """No ranked leader needs funding -> nothing is sold."""
    agent = _agent(tmp_path, settings=None)
    try:
        # Balanced target = 20% of equity. AAVE held above the top-up threshold,
        # so it is not "underfunded" and there is no other ranked name.
        _book(agent, cash=1.0, positions=[
            _pos("AAVE", qty=0.2, price=200.0, age_h=24),   # $40 (funded leader)
            _pos("AVAX", qty=0.7, price=100.0, age_h=24),   # $70 stale
        ])
        prices = {"AVAX": 100.0, "AAVE": 200.0}
        snap = _snap(ranked=[_sig("AAVE", 2.7, 200.0)],
                     signals=[_sig("AAVE", 2.7, 200.0), _sig("AVAX", 0.1, 100.0)])
        actions: list = []
        agent._run_rotation(snap, prices, None, actions, dry_run=False, now=_NOW)
        assert "AVAX" in agent.portfolio.positions
        assert actions == []
    finally:
        agent.close()


def test_rotation_dry_run_is_observe_only(tmp_path):
    """Dry-run reports the intended rotation but never mutates the book."""
    agent = _agent(tmp_path)
    try:
        _book(agent, cash=1.0, positions=[_pos("AVAX", qty=0.7, price=100.0, age_h=24)])
        prices = {"AVAX": 100.0, "AAVE": 200.0}
        snap = _snap(ranked=[_sig("AAVE", 2.7, 200.0)],
                     signals=[_sig("AAVE", 2.7, 200.0), _sig("AVAX", 0.1, 100.0)])
        actions: list = []
        agent._run_rotation(snap, prices, None, actions, dry_run=True, now=_NOW)
        assert "AVAX" in agent.portfolio.positions
        assert agent.portfolio.cash == 1.0
        assert any(a.kind == "dry_run" and "would rotate" in a.detail for a in actions)
    finally:
        agent.close()


def test_rotation_can_be_disabled(tmp_path):
    """rotation_enabled=false is a hard off switch."""
    s = load_settings()
    s.risk = replace(s.risk, rotation_enabled=False)
    agent = _agent(tmp_path, settings=s)
    try:
        _book(agent, cash=1.0, positions=[_pos("AVAX", qty=0.7, price=100.0, age_h=24)])
        prices = {"AVAX": 100.0, "AAVE": 200.0}
        snap = _snap(ranked=[_sig("AAVE", 2.7, 200.0)],
                     signals=[_sig("AAVE", 2.7, 200.0), _sig("AVAX", 0.1, 100.0)])
        actions: list = []
        agent._run_rotation(snap, prices, None, actions, dry_run=False, now=_NOW)
        assert "AVAX" in agent.portfolio.positions
        assert actions == []
    finally:
        agent.close()


# ----------------------------------------------- entries pyramid into leader
def test_entries_top_up_funds_held_leader(tmp_path):
    """Freed cash pyramids into a held leader up to max_position_pct, so rotation
    proceeds don't sit idle just because the leader is already a position."""
    agent = _agent(tmp_path)        # balanced -> max_position_pct 0.20
    try:
        # AAVE $5 held, $145 cash, equity $150 -> target 20% = $30, room $25 (> $20 floor).
        _book(agent, cash=145.0, positions=[_pos("AAVE", qty=0.025, price=200.0, age_h=24)])
        prices = {"AAVE": 200.0}
        snap = _snap(ranked=[_sig("AAVE", 2.7, 200.0)], signals=[_sig("AAVE", 2.7, 200.0)])
        before_qty = agent.portfolio.positions["AAVE"].qty
        actions: list = []
        agent._run_entries(snap, prices, _posture(), actions, dry_run=False, now=_NOW)

        pos = agent.portfolio.positions["AAVE"]
        assert pos.qty > before_qty                                  # topped up
        assert pos.qty * 200.0 <= 0.20 * agent.portfolio.equity(prices) + 1.0  # never past cap
        assert agent.portfolio.cash < 145.0
        assert any(a.kind == "entry" and a.symbol == "AAVE" for a in actions)
    finally:
        agent.close()


def test_entries_leave_leader_at_target(tmp_path):
    """A leader already at its weight cap is left alone (no churn / gas bleed)."""
    agent = _agent(tmp_path)
    try:
        # AAVE $10 held vastly exceeds 20% of a $12 equity -> no top-up room.
        _book(agent, cash=2.0, positions=[_pos("AAVE", qty=0.05, price=200.0, age_h=24)])
        prices = {"AAVE": 200.0}
        snap = _snap(ranked=[_sig("AAVE", 2.7, 200.0)], signals=[_sig("AAVE", 2.7, 200.0)])
        before_qty = agent.portfolio.positions["AAVE"].qty
        actions: list = []
        agent._run_entries(snap, prices, _posture(), actions, dry_run=False, now=_NOW)
        assert agent.portfolio.positions["AAVE"].qty == before_qty   # untouched
    finally:
        agent.close()
