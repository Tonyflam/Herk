"""Long/short perps engine: signed-direction accounting, inverted stops/levels,
direction-aware exits, the ccxt reduceOnly close flag, and the autonomous short
entry path.

These pin the contract that makes shorting safe and correct:
  • a short PROFITS as price falls and LOSES as it rises (mirror of a long),
  • short bookkeeping reduces EXACTLY to the long path when direction = +1,
  • stops sit ABOVE entry and take-profits BELOW for a short,
  • exits close a short by BUYING back (reduce_only), a long by SELLING,
  • the ccxt adapter sizes every leg correctly and only flags reduceOnly on swaps,
  • the autonomous short entry is a HARD no-op unless long/short + a swap market,
  • the long-only secondary mechanisms never touch a short position.
"""

from __future__ import annotations

import math
import os
from dataclasses import replace
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from helm.agent import Agent
from helm.config import load_settings
from helm.execution.base import Fill, Order
from helm.execution.ccxt_adapter import CcxtAdapter
from helm.portfolio import Portfolio
from helm.risk.sizing import liquidation_price, max_safe_leverage, plan_position
from helm.signals.engine import SignalSnapshot, SymbolSignal
from helm.universe import rank_full_market

_NOW = datetime(2026, 6, 25, 12, 0, tzinfo=timezone.utc)


# --------------------------------------------------------------- helpers
def _fill(symbol, side, qty, price, fee=0.0, gas=0.0) -> Fill:
    return Fill(symbol, side, qty, price, qty * price, fee, 0.0, "ts", "test", True, "", gas)


def _posture(max_gross: float = 1.20, risk_pct: float = 4.0) -> SimpleNamespace:
    return SimpleNamespace(max_gross_pct=max_gross, per_trade_risk_pct=risk_pct,
                           halt_new_risk=False, posture="build")


def _short_sig(symbol: str, price: float = 100.0, atr: float = 4.0) -> SymbolSignal:
    return SymbolSignal(symbol=symbol, price=price, composite=-2.0, atr=atr,
                        realized_vol_annual=0.8, liquidity_usd=5e7,
                        passes_liquidity=True, passes_quality_short=True,
                        passes_cost_short=True)


def _snap_short(shorts: list[SymbolSignal]) -> SignalSnapshot:
    return SignalSnapshot(regime=None, signals=shorts, ranked=[], ranked_short=shorts)


def _ls_settings():
    """Balanced profile with long/short perps armed (paper executor stays)."""
    prev = os.environ.get("HELM_PROFILE")
    os.environ["HELM_PROFILE"] = "balanced"
    try:
        s = load_settings()
    finally:
        if prev is None:
            os.environ.pop("HELM_PROFILE", None)
        else:
            os.environ["HELM_PROFILE"] = prev
    s.execution = replace(s.execution, long_short_enabled=True,
                          market_type="swap", max_shorts=3)
    return s


def _agent(tmp_path, settings) -> Agent:
    return Agent(settings=settings, state_path=tmp_path / "state.json",
                 ledger_path=tmp_path / "audit.jsonl")


# --------------------------------------------------------- portfolio math
def test_short_profits_when_price_falls():
    p = Portfolio.new(1000.0)
    p.apply_open(_fill("X", "sell", 1.0, 100.0), direction=-1,
                 stop_price=110.0, take_profit_price=90.0, stop_distance=10.0)
    pos = p.positions["X"]
    assert pos.direction == -1 and pos.qty == 1.0
    assert p.cash == pytest.approx(900.0)               # notional reserved as margin
    assert p.equity({"X": 100.0}) == pytest.approx(1000.0)   # flat at entry
    assert p.equity({"X": 90.0}) == pytest.approx(1010.0)    # +10 as price falls
    assert pos.unrealized_pnl(90.0) == pytest.approx(10.0)
    realized = p.apply_close(_fill("X", "buy", 1.0, 90.0))
    assert realized == pytest.approx(10.0)
    assert "X" not in p.positions
    assert p.cash == pytest.approx(1010.0)


def test_short_loses_when_price_rises():
    p = Portfolio.new(1000.0)
    p.apply_open(_fill("X", "sell", 1.0, 100.0), -1, 110.0, 90.0, 10.0)
    assert p.equity({"X": 110.0}) == pytest.approx(990.0)
    realized = p.apply_close(_fill("X", "buy", 1.0, 110.0))
    assert realized == pytest.approx(-10.0)
    assert p.cash == pytest.approx(990.0)


def test_gross_usd_uses_abs_notional_for_short():
    p = Portfolio.new(1000.0)
    p.apply_open(_fill("X", "sell", 2.0, 100.0), -1, 110.0, 90.0, 10.0)
    # Exposure magnitude is qty*price regardless of side (not margin+pnl).
    assert p.gross_usd({"X": 100.0}) == pytest.approx(200.0)


def test_long_open_close_matches_legacy_buy_sell():
    a = Portfolio.new(1000.0)
    b = Portfolio.new(1000.0)
    a.apply_buy(_fill("X", "buy", 2.0, 50.0), 45.0, 60.0, 5.0)
    b.apply_open(_fill("X", "buy", 2.0, 50.0), 1, 45.0, 60.0, 5.0)
    assert a.cash == pytest.approx(b.cash)
    assert a.equity({"X": 55.0}) == pytest.approx(b.equity({"X": 55.0}))
    ra = a.apply_sell(_fill("X", "sell", 2.0, 55.0))
    rb = b.apply_close(_fill("X", "sell", 2.0, 55.0))
    assert ra == pytest.approx(rb)
    assert a.cash == pytest.approx(b.cash)


def test_exits_short_stop_tp_and_trailing():
    p = Portfolio.new(1000.0)
    p.apply_open(_fill("X", "sell", 1.0, 100.0), -1, stop_price=110.0,
                 take_profit_price=90.0, stop_distance=8.0)
    assert p.exits_to_run({"X": 100.0}, trailing=False) == []
    assert p.exits_to_run({"X": 110.0}, trailing=False)[0].reason == "stop"        # rise -> stop
    assert p.exits_to_run({"X": 90.0}, trailing=False)[0].reason == "take_profit"  # fall -> TP
    p.update_marks({"X": 95.0})                      # trough now 95; trail = 95+8 = 103
    ex = p.exits_to_run({"X": 103.5}, trailing=True)
    assert ex and ex[0].reason == "trailing_stop"


def test_plan_position_inverts_levels_for_short():
    common = dict(symbol="X", price=100.0, atr=4.0, equity=1000.0,
                  per_trade_risk_pct=2.0, stop_atr_mult=2.0, take_profit_atr_mult=3.0,
                  max_position_pct=0.5, gross_headroom_usd=1000.0)
    sh = plan_position(**common, direction=-1)
    lo = plan_position(**common, direction=1)
    assert sh.ok and lo.ok
    assert sh.stop_price > 100.0 and sh.take_profit_price < 100.0   # short: stop up, TP down
    assert lo.stop_price < 100.0 and lo.take_profit_price > 100.0   # long: stop down, TP up


# ------------------------------------------------------------- ccxt adapter
class _FakeEx:
    def __init__(self, fill_price: float = 100.0):
        self.fill_price = fill_price
        self.calls: list[dict] = []
        self.levs: list[tuple] = []

    def load_markets(self):
        return {}

    def set_leverage(self, lev, market):
        self.levs.append((lev, market))

    def create_order(self, symbol, type_, side, amount, price=None, params=None):
        self.calls.append({"symbol": symbol, "side": side,
                           "amount": amount, "params": params or {}})
        notional = amount * self.fill_price
        return {"average": self.fill_price, "filled": amount, "amount": amount,
                "cost": notional, "fee": {"cost": notional * 0.001, "currency": "USDT"}}


def _ccxt_settings(market_type: str = "swap"):
    s = load_settings()
    s.execution.adapter = "ccxt"
    s.execution.exchange = "bybit"
    s.execution.market_type = market_type
    s.execution.quote_currency = "USDT"
    s.execution.testnet = True
    s.secrets.ccxt_api_key = "PUBKEY"
    s.secrets.ccxt_secret = "SECRET"
    return s


def test_ccxt_short_open_sizes_by_qty_no_reduceonly():
    fake = _FakeEx(100.0)
    a = CcxtAdapter(_ccxt_settings("swap"), client=fake)
    fill = a.execute(Order("INJ", "sell", ref_price=100.0, qty=2.0, reduce_only=False))
    assert fill.ok
    assert fake.calls[0]["side"] == "sell"
    assert fake.calls[0]["amount"] == pytest.approx(2.0)        # exact base qty
    assert "reduceOnly" not in fake.calls[0]["params"]


def test_ccxt_short_close_passes_reduceonly_and_qty():
    fake = _FakeEx(100.0)
    a = CcxtAdapter(_ccxt_settings("swap"), client=fake)
    fill = a.execute(Order("INJ", "buy", ref_price=100.0, qty=2.0,
                           notional_usd=200.0, reduce_only=True))
    assert fill.ok
    assert fake.calls[0]["side"] == "buy"
    assert fake.calls[0]["amount"] == pytest.approx(2.0)        # qty, NOT notional/ref
    assert fake.calls[0]["params"].get("reduceOnly") is True


def test_ccxt_spot_never_flags_reduceonly():
    fake = _FakeEx(100.0)
    a = CcxtAdapter(_ccxt_settings("spot"), client=fake)
    a.execute(Order("AAVE", "sell", ref_price=100.0, qty=1.0, reduce_only=True))
    assert "reduceOnly" not in fake.calls[0]["params"]         # perps-only flag


# ------------------------------------------------- venue universe discovery
class _FakeVenue:
    """Fake ccxt client exposing markets + tickers for universe discovery."""

    def __init__(self, markets: dict, tickers: dict):
        self.markets: dict = {}
        self._markets = markets
        self._tickers = tickers

    def load_markets(self):
        self.markets = self._markets
        return self._markets

    def fetch_tickers(self):
        return self._tickers

    def set_sandbox_mode(self, on):
        pass


def _discovery_venue() -> _FakeVenue:
    markets = {
        "BTC/USDT:USDT": {"swap": True, "linear": True, "active": True,
                          "quote": "USDT", "settle": "USDT", "base": "BTC"},
        "ETH/USDT:USDT": {"swap": True, "linear": True, "active": True,
                          "quote": "USDT", "settle": "USDT", "base": "ETH"},
        "INJ/USDT:USDT": {"swap": True, "linear": True, "active": True,
                          "quote": "USDT", "settle": "USDT", "base": "INJ"},
        "DOGE/USDT:USDT": {"swap": True, "linear": True, "active": False,
                           "quote": "USDT", "settle": "USDT", "base": "DOGE"},
        "BTC/USD:BTC": {"swap": True, "linear": False, "inverse": True,
                        "active": True, "quote": "USD", "settle": "BTC", "base": "BTC"},
        "SOL/USDT": {"swap": False, "spot": True, "active": True,
                     "quote": "USDT", "base": "SOL"},
        "USDC/USDT:USDT": {"swap": True, "linear": True, "active": True,
                           "quote": "USDT", "settle": "USDT", "base": "USDC"},
    }
    tickers = {
        "BTC/USDT:USDT": {"quoteVolume": 9e9},
        "ETH/USDT:USDT": {"quoteVolume": 5e9},
        "INJ/USDT:USDT": {"baseVolume": 1e6, "last": 200.0},   # derived = 2e8
        "USDC/USDT:USDT": {"quoteVolume": 1e9},
    }
    return _FakeVenue(markets, tickers)


def test_perp_ticker_rows_only_active_linear_usdt_perps():
    a = CcxtAdapter(_ccxt_settings("swap"), client=_discovery_venue())
    rows = a.perp_ticker_rows()
    syms = {r["symbol"] for r in rows}
    # inactive (DOGE), inverse (BTC/USD), and spot (SOL) are all excluded
    assert syms == {"BTCUSDT", "ETHUSDT", "INJUSDT", "USDCUSDT"}
    inj = next(r for r in rows if r["symbol"] == "INJUSDT")
    assert inj["quoteVolume"] == pytest.approx(2e8)            # baseVolume*last fallback


def test_perp_rows_feed_rank_full_market_drops_stable_and_sorts():
    a = CcxtAdapter(_ccxt_settings("swap"), client=_discovery_venue())
    ranked = rank_full_market(a.perp_ticker_rows(), quote="USDT",
                              top_n=10, min_vol_usd=5e6)
    assert ranked[:3] == ["BTC", "ETH", "INJ"]                 # by 24h volume desc
    assert "USDC" not in ranked                               # stable base dropped


def test_perp_ticker_rows_empty_for_spot_market():
    a = CcxtAdapter(_ccxt_settings("spot"), client=_discovery_venue())
    assert a.perp_ticker_rows() == []                         # perps-only discovery


# ----------------------------------------------- liquidation-distance buffer
def test_liquidation_price_direction_and_leverage():
    # 5x: long liquidates ~19.5% below, short ~19.5% above (incl. maint margin).
    assert liquidation_price(100.0, 1, 5.0) == pytest.approx(80.5, abs=1e-6)
    assert liquidation_price(100.0, -1, 5.0) == pytest.approx(119.5, abs=1e-6)
    # 1x (or less) is fully collateralised -> far, non-binding bounds.
    assert liquidation_price(100.0, 1, 1.0) == 0.0
    assert liquidation_price(100.0, -1, 1.0) == pytest.approx(200.0)


def test_max_safe_leverage_tightens_with_wider_stops():
    # Tight stop (2%) -> safe leverage pinned at the hard ceiling.
    assert max_safe_leverage(100.0, 2.0, hard_cap=10.0) == pytest.approx(10.0)
    # Medium stop (8%) -> dialled below the ceiling.
    mid = max_safe_leverage(100.0, 8.0, hard_cap=10.0)
    assert 1.0 < mid < 10.0
    # Wide stop (40%) -> close to 1x (barely any leverage is survivable).
    wide = max_safe_leverage(100.0, 40.0, hard_cap=10.0)
    assert wide < mid
    assert wide >= 1.0


def test_safe_leverage_keeps_stop_inside_liquidation():
    # At the derived safe leverage the stop must sit strictly inside liquidation.
    entry, stop_dist = 100.0, 8.0
    lev = max_safe_leverage(entry, stop_dist, hard_cap=20.0)
    liq_long = liquidation_price(entry, 1, lev)
    liq_short = liquidation_price(entry, -1, lev)
    assert (entry - stop_dist) > liq_long                     # long stop above liq
    assert (entry + stop_dist) < liq_short                    # short stop below liq


def test_ccxt_clamps_per_trade_leverage():
    s = _ccxt_settings("swap")
    s.execution.leverage_enabled = True
    s.execution.max_leverage = 10.0
    fake = _FakeEx(100.0)
    a = CcxtAdapter(s, client=fake)
    a.execute(Order("INJ", "sell", ref_price=100.0, qty=1.0, leverage=3.0))
    assert fake.levs[-1][0] == pytest.approx(3.0)             # honoured (<= ceiling)
    a.execute(Order("INJ", "sell", ref_price=100.0, qty=1.0, leverage=20.0))
    assert fake.levs[-1][0] == pytest.approx(10.0)            # clamped to ceiling
    a.execute(Order("INJ", "sell", ref_price=100.0, qty=1.0, leverage=0.0))
    assert fake.levs[-1][0] == pytest.approx(10.0)            # 0 -> use ceiling


def test_ccxt_no_leverage_when_disabled():
    fake = _FakeEx(100.0)
    a = CcxtAdapter(_ccxt_settings("swap"), client=fake)   # leverage_enabled False
    a.execute(Order("INJ", "sell", ref_price=100.0, qty=1.0, leverage=5.0))
    assert fake.levs == []                                    # set_leverage never called

# ------------------------------------------------- venue lot-size precision
class _FakePrecEx(_FakeEx):
    """Fake venue with a 0.001 lot step (rounds DOWN like a real exchange)."""

    def amount_to_precision(self, symbol, amount):
        return f"{math.floor(float(amount) * 1000) / 1000:.3f}"


def test_ccxt_rounds_amount_to_venue_lot_precision():
    fake = _FakePrecEx(99.0)
    a = CcxtAdapter(_ccxt_settings("swap"), client=fake)
    # 233 / 99 = 2.35353... -> floored to the 0.001 lot step = 2.353
    fill = a.execute(Order("INJ", "buy", ref_price=99.0, notional_usd=233.0))
    assert fill.ok
    assert fake.calls[0]["amount"] == pytest.approx(2.353)


def test_ccxt_rejects_amount_below_lot_size():
    fake = _FakePrecEx(100.0)
    a = CcxtAdapter(_ccxt_settings("swap"), client=fake)
    # 0.05 / 100 = 0.0005 -> rounds to 0.000 -> below the venue lot size
    fill = a.execute(Order("INJ", "buy", ref_price=100.0, notional_usd=0.05))
    assert not fill.ok
    assert fake.calls == []                                   # never sent to venue


# ------------------------------------------------- async market-fill settle
class _FakeAsyncEx(_FakeEx):
    """Create ACKs with filled=0 (like Bybit); the fill only appears on a
    follow-up fetch_order — the adapter must re-fetch to book it correctly."""

    def create_order(self, symbol, type_, side, amount, price=None, params=None):
        self.calls.append({"symbol": symbol, "side": side,
                           "amount": amount, "params": params or {}})
        oid = "oid%d" % len(self.calls)
        self._pending = getattr(self, "_pending", {})
        self._pending[oid] = (amount,)
        return {"id": oid, "filled": 0, "average": None, "info": {"orderId": oid}}

    def fetch_order(self, oid, symbol=None, params=None):
        (amount,) = self._pending[oid]
        notional = amount * self.fill_price
        return {"id": oid, "filled": amount, "amount": amount,
                "average": self.fill_price, "cost": notional,
                "fee": {"cost": notional * 0.001, "currency": "USDT"}, "status": "closed"}


def test_ccxt_settles_async_fill_via_fetch_order():
    fake = _FakeAsyncEx(0.0726)
    a = CcxtAdapter(_ccxt_settings("swap"), client=fake)
    fill = a.execute(Order("DOGE", "buy", ref_price=0.0726, notional_usd=7.0))
    assert fill.ok                                       # settled, NOT 'unfilled'
    assert fill.qty == pytest.approx(7.0 / 0.0726, rel=1e-6)
    assert fill.price == pytest.approx(0.0726)

# ------------------------------------------------------- agent integration
def test_agent_opens_short_via_engine(tmp_path):
    agent = _agent(tmp_path, _ls_settings())
    try:
        agent.portfolio = Portfolio.new(1000.0)
        snap = _snap_short([_short_sig("INJ", 100.0)])
        actions: list = []
        agent._run_short_entries(snap, {"INJ": 100.0}, _posture(), actions, False, _NOW)
        pos = agent.portfolio.positions.get("INJ")
        assert pos is not None
        assert pos.direction == -1 and pos.qty > 0
        assert pos.stop_price > 100.0 and pos.take_profit_price < 100.0
        assert agent.portfolio.cash < 1000.0               # margin reserved
        assert any(a.kind == "short" for a in actions)
    finally:
        agent.close()


def test_agent_short_take_profit_closes_by_buying(tmp_path):
    agent = _agent(tmp_path, _ls_settings())
    try:
        p = Portfolio.new(1000.0)
        p.apply_open(_fill("INJ", "sell", 2.0, 100.0), -1,
                     stop_price=110.0, take_profit_price=90.0, stop_distance=8.0)
        agent.portfolio = p
        actions: list = []
        agent._run_exits({"INJ": 89.0}, actions, dry_run=False)   # below TP -> cover
        assert "INJ" not in agent.portfolio.positions
        assert agent.portfolio.realized_pnl > 0
        assert any(a.kind == "exit" for a in actions)
    finally:
        agent.close()


def test_agent_short_stop_closes_with_loss(tmp_path):
    agent = _agent(tmp_path, _ls_settings())
    try:
        p = Portfolio.new(1000.0)
        p.apply_open(_fill("INJ", "sell", 2.0, 100.0), -1,
                     stop_price=110.0, take_profit_price=90.0, stop_distance=8.0)
        agent.portfolio = p
        agent._run_exits({"INJ": 111.0}, [], dry_run=False)       # above stop -> cover
        assert "INJ" not in agent.portfolio.positions
        assert agent.portfolio.realized_pnl < 0
    finally:
        agent.close()


def test_short_entry_is_noop_when_disabled(tmp_path):
    prev = os.environ.get("HELM_PROFILE")
    os.environ["HELM_PROFILE"] = "balanced"
    try:
        s = load_settings()                # defaults: spot + long_short disabled
    finally:
        if prev is None:
            os.environ.pop("HELM_PROFILE", None)
        else:
            os.environ["HELM_PROFILE"] = prev
    agent = _agent(tmp_path, s)
    try:
        agent.portfolio = Portfolio.new(1000.0)
        snap = _snap_short([_short_sig("INJ", 100.0)])
        agent._run_short_entries(snap, {"INJ": 100.0}, _posture(), [], False, _NOW)
        assert not agent.portfolio.positions       # hard no-op
    finally:
        agent.close()


def test_trail_guard_leaves_shorts_untouched(tmp_path):
    s = _ls_settings()
    s.risk = replace(s.risk, harvest_trail_giveback_pct=0.03, swing_symbol="")
    agent = _agent(tmp_path, s)
    try:
        p = Portfolio.new(1000.0)
        # A short showing a paper loss (price above entry) with a fresh peak — the
        # long-only trail-lock must NEVER bank it (that would add to the short).
        p.apply_open(_fill("INJ", "sell", 2.0, 100.0), -1,
                     stop_price=120.0, take_profit_price=80.0, stop_distance=20.0)
        p.positions["INJ"].highest_price = 115.0
        agent.portfolio = p
        agent._run_trail_guard({"INJ": 110.0}, [], dry_run=False)
        assert "INJ" in agent.portfolio.positions
        assert agent.portfolio.positions["INJ"].qty == pytest.approx(2.0)
    finally:
        agent.close()
