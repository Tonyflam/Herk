"""Score-truthful marking (#4): on-chain balance reads, the book-vs-chain
reconciliation contract (live scoring is from chain, so HELM marks from chain),
and route/liquidity pre-validation that keeps the agent out of unroutable names.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from helm.agent import Agent
from helm.data import onchain, routes


# ----------------------------------------------------------- on-chain decode
def test_pad_addr_is_32_bytes():
    word = onchain._pad_addr("0x" + "ab" * 20)
    assert len(word) == 64
    assert word.endswith("ab")
    assert word.startswith("0")


def test_token_meta_prefers_config_override(settings):
    settings.execution.token_addresses = {"foo": "0xCONTRACTfoo"}
    try:
        addr, dec = onchain.token_meta(settings, "FOO")
        assert addr == "0xCONTRACTfoo"
        assert dec is None  # override carries no decimals hint; confirmed on chain
    finally:
        settings.execution.token_addresses = {}


def test_token_meta_uses_builtin_registry(settings):
    meta = onchain.token_meta(settings, "USDT")
    assert meta is not None
    addr, dec = meta
    assert addr.lower().startswith("0x55d398")  # canonical BSC-USD
    assert dec == 18


def test_token_meta_unknown_symbol_is_none(settings):
    assert onchain.token_meta(settings, "NOSUCHTOKEN") is None


def test_resolve_wallet_prefers_config(settings):
    settings.execution.wallet_address = "0xMyWallet"
    try:
        assert onchain.resolve_wallet(settings) == "0xMyWallet"
    finally:
        settings.execution.wallet_address = ""


def test_wallet_holdings_empty_without_wallet(settings):
    assert onchain.wallet_holdings(settings, "", ["USDT"]) == {}


# --------------------------------------------------------- route validation
class _StubMarket:
    """Minimal MarketData stand-in returning canned quotes."""

    def __init__(self, quotes):
        self._q = quotes

    def get_quote(self, sym):
        return self._q.get(sym, SimpleNamespace(
            price=0.0, volume_24h_usd=0.0, provenance=SimpleNamespace(ok=False)))

    def close(self):
        pass


def _q(price, vol):
    return SimpleNamespace(price=price, volume_24h_usd=vol,
                           provenance=SimpleNamespace(ok=True))


def test_routes_classify_ok_thin_dead(settings):
    market = _StubMarket({
        "ETH": _q(1800.0, 800_000_000),   # deep -> ok
        "TWT": _q(0.42, 120_000),          # below min_liquidity -> thin
        # "GHOST" absent -> no quote -> dead
    })
    book = {"ETH", "TWT", "GHOST"}
    checks = routes.validate_routes(settings, ["ETH", "TWT", "GHOST"], market, tradeable=book)
    by = {c.symbol: c for c in checks}
    assert by["ETH"].status == "ok"
    assert by["TWT"].status == "thin"
    assert by["GHOST"].status == "dead"
    assert by["GHOST"].in_scope is False  # GHOST is not an eligible symbol


def test_routes_stable_is_always_ok(settings):
    checks = routes.validate_routes(settings, ["USDT"], _StubMarket({}), tradeable=set())
    assert checks[0].status == "ok"
    assert "stable" in checks[0].note


def test_routes_summary_counts(settings):
    market = _StubMarket({"ETH": _q(1800.0, 800_000_000), "TWT": _q(0.42, 120_000)})
    checks = routes.validate_routes(settings, ["ETH", "TWT", "GHOST"], market,
                                    tradeable={"ETH", "TWT", "GHOST"})
    sm = routes.summarize(checks)
    assert sm["total"] == 3
    assert sm["ok"] == 1 and sm["thin"] == 1 and sm["dead"] == 1
    assert sm["tradeable_bad"] == 2  # TWT thin + GHOST dead


# --------------------------------------------------- on-chain reconciliation
def _fresh_agent(tmp_path) -> Agent:
    return Agent(state_path=tmp_path / "state.json", ledger_path=tmp_path / "audit.jsonl")


def test_reconcile_noop_in_paper(tmp_path, monkeypatch):
    """Paper mode must never touch on-chain or mutate the book."""
    agent = _fresh_agent(tmp_path)
    try:
        called = {"n": 0}
        monkeypatch.setattr(onchain, "wallet_holdings",
                            lambda *a, **k: called.__setitem__("n", called["n"] + 1) or {})
        agent.portfolio.cash = 100.0
        agent._reconcile_onchain({}, [])
        assert called["n"] == 0          # not live -> not called
        assert agent.portfolio.cash == 100.0
    finally:
        agent.close()


def test_reconcile_marks_book_to_chain(tmp_path, monkeypatch):
    """When live + mark_from_onchain, the book is corrected to actual balances
    and the drift is logged — this is what makes HELM's score match the judges'."""
    agent = _fresh_agent(tmp_path)
    try:
        agent.settings.mode = "live"
        agent.settings.scoring.mark_from_onchain = True
        agent.settings.scoring.onchain_drift_alert_pct = 2.0
        monkeypatch.setattr(type(agent.settings), "is_live", property(lambda self: True))
        monkeypatch.setattr(onchain, "resolve_wallet", lambda s: "0xWALLET")

        # Book says cash=50 + 10 UNI; chain says cash=47.5 + 9.8 UNI (gas + slippage).
        from helm.portfolio import Position
        agent.portfolio.cash = 50.0
        agent.portfolio.positions["UNI"] = Position(
            symbol="UNI", qty=10.0, avg_entry=5.0, stop_price=4.0,
            take_profit_price=7.0, stop_distance=1.0, highest_price=5.0, entry_ts="t")

        def fake_holdings(s, wallet, symbols, client=None):
            return {
                "USDT": onchain.Holding("USDT", units=47.5, raw=0, decimals=18, ok=True),
                "UNI": onchain.Holding("UNI", units=9.8, raw=0, decimals=18, ok=True),
            }
        monkeypatch.setattr(onchain, "wallet_holdings", fake_holdings)

        actions: list = []
        agent._reconcile_onchain({"UNI": 5.0}, actions)

        assert agent.portfolio.cash == 47.5          # marked to chain
        assert agent.portfolio.positions["UNI"].qty == 9.8
        assert any(a.kind == "reconcile" for a in actions)
        assert any(r.get("type") == "reconcile" for r in agent.ledger.tail(5))
    finally:
        agent.close()


def test_reconcile_drops_dust_position(tmp_path, monkeypatch):
    """A position the chain no longer holds (sold/dusted) is removed on marking."""
    agent = _fresh_agent(tmp_path)
    try:
        agent.settings.mode = "live"
        agent.settings.scoring.mark_from_onchain = True
        monkeypatch.setattr(type(agent.settings), "is_live", property(lambda self: True))
        monkeypatch.setattr(onchain, "resolve_wallet", lambda s: "0xWALLET")

        from helm.portfolio import Position
        agent.portfolio.positions["CAKE"] = Position(
            symbol="CAKE", qty=3.0, avg_entry=2.0, stop_price=1.5,
            take_profit_price=3.0, stop_distance=0.5, highest_price=2.0, entry_ts="t")

        monkeypatch.setattr(onchain, "wallet_holdings", lambda *a, **k: {
            "CAKE": onchain.Holding("CAKE", units=0.0, raw=0, decimals=18, ok=True)})

        agent._reconcile_onchain({"CAKE": 2.0}, [])
        assert "CAKE" not in agent.portfolio.positions
    finally:
        agent.close()


def test_reconcile_skips_unreadable_leg(tmp_path, monkeypatch):
    """A failed balance read must NOT zero the book — keep the booked mark."""
    agent = _fresh_agent(tmp_path)
    try:
        agent.settings.mode = "live"
        agent.settings.scoring.mark_from_onchain = True
        monkeypatch.setattr(type(agent.settings), "is_live", property(lambda self: True))
        monkeypatch.setattr(onchain, "resolve_wallet", lambda s: "0xWALLET")

        from helm.portfolio import Position
        agent.portfolio.positions["INJ"] = Position(
            symbol="INJ", qty=4.0, avg_entry=6.0, stop_price=5.0,
            take_profit_price=8.0, stop_distance=1.0, highest_price=6.0, entry_ts="t")

        # ok=False -> unreadable; reconcile must leave the booked qty intact.
        monkeypatch.setattr(onchain, "wallet_holdings", lambda *a, **k: {
            "INJ": onchain.Holding("INJ", ok=False, note="read failed")})

        agent._reconcile_onchain({"INJ": 6.0}, [])
        assert agent.portfolio.positions["INJ"].qty == 4.0
    finally:
        agent.close()


def test_reconcile_adopts_unbooked_onchain_holding(tmp_path, monkeypatch):
    """A real holding the book lost track of (e.g. a redeploy dropped it from
    saved state) is re-adopted from chain, so equity is not under-reported and the
    drawdown halt cannot false-trip."""
    agent = _fresh_agent(tmp_path)
    try:
        agent.settings.mode = "live"
        agent.settings.scoring.mark_from_onchain = True
        monkeypatch.setattr(type(agent.settings), "is_live", property(lambda self: True))
        monkeypatch.setattr(onchain, "resolve_wallet", lambda s: "0xWALLET")

        # Book holds only cash; chain holds cash + 0.7 AAVE the book forgot.
        agent.portfolio.cash = 32.0
        agent.portfolio.positions.clear()

        def fake_holdings(s, wallet, symbols, client=None):
            return {
                "USDT": onchain.Holding("USDT", units=32.0, raw=0, decimals=18, ok=True),
                "AAVE": onchain.Holding("AAVE", units=0.7, raw=0, decimals=18, ok=True),
            }
        monkeypatch.setattr(onchain, "wallet_holdings", fake_holdings)

        actions: list = []
        agent._reconcile_onchain({"AAVE": 85.0}, actions)

        assert "AAVE" in agent.portfolio.positions
        assert abs(agent.portfolio.positions["AAVE"].qty - 0.7) < 1e-9
        assert agent.portfolio.positions["AAVE"].avg_entry == 85.0
        # Wide protective stop (not a tight one that would dump the adopted name).
        assert agent.portfolio.positions["AAVE"].stop_price < 85.0 * 0.85
        assert any(a.kind == "reconcile" and "adopted" in a.detail for a in actions)
    finally:
        agent.close()


def test_reconcile_does_not_adopt_dust(tmp_path, monkeypatch):
    """A sub-dust on-chain balance is not adopted (gas-inefficient noise)."""
    agent = _fresh_agent(tmp_path)
    try:
        agent.settings.mode = "live"
        agent.settings.scoring.mark_from_onchain = True
        monkeypatch.setattr(type(agent.settings), "is_live", property(lambda self: True))
        monkeypatch.setattr(onchain, "resolve_wallet", lambda s: "0xWALLET")

        agent.portfolio.cash = 10.0
        agent.portfolio.positions.clear()

        def fake_holdings(s, wallet, symbols, client=None):
            return {
                "USDT": onchain.Holding("USDT", units=10.0, raw=0, decimals=18, ok=True),
                "AAVE": onchain.Holding("AAVE", units=0.000001, raw=0, decimals=18, ok=True),
            }
        monkeypatch.setattr(onchain, "wallet_holdings", fake_holdings)

        agent._reconcile_onchain({"AAVE": 85.0}, [])
        assert "AAVE" not in agent.portfolio.positions   # ~$0.00009 — left as dust
    finally:
        agent.close()


# ------------------------------------------------- volatility (grid-fuel) tilt
def _balanced_settings():
    """Load the balanced profile deterministically, regardless of any ambient
    ``HELM_PROFILE`` pinned in ``.env`` (the live contest stages ``max``)."""
    import os

    from helm.config import load_settings

    prev = os.environ.get("HELM_PROFILE")
    os.environ["HELM_PROFILE"] = "balanced"
    try:
        return load_settings()
    finally:
        if prev is None:
            os.environ.pop("HELM_PROFILE", None)
        else:
            os.environ["HELM_PROFILE"] = prev


class _CandleMarket:
    """MarketData stand-in feeding the SignalEngine canned candles/quotes/regime."""

    def __init__(self, series, quotes, regime):
        self._series = series      # {sym: Candles}
        self._quotes = quotes      # {sym: quote-like}
        self._regime = regime

    def get_candles(self, sym, interval="1h", limit=200):
        return self._series[sym]

    def get_quote(self, sym):
        return self._quotes[sym]

    def get_regime(self):
        return self._regime

    def close(self):
        pass


def _candle_series(sym, closes, range_frac):
    """Synthetic 1h candles: smooth closes wrapped in a fixed intrabar range so
    the realized high/low (hence ATR%) is controlled independently of drift."""
    from helm.data.sources import Candles, Provenance
    rows = []
    for i, c in enumerate(closes):
        hi = c * (1.0 + range_frac / 2.0)
        lo = c * (1.0 - range_frac / 2.0)
        rows.append((i * 3_600_000, c, hi, lo, c, 1_000.0))
    return Candles(symbol=sym, interval="1h", rows=rows,
                   provenance=Provenance("test", ok=True))


def test_vol_tilt_promotes_the_volatile_grid_fuel_name():
    """The harvester is a grid: it earns from price *range*, yet vol-adjusted
    momentum alone ranks the calm, lower-range name ahead of the volatile mover.
    ``vol_tilt_weight`` adds a z-scored ATR% term so the high-range name (best
    grid fuel) is promoted — and can overtake the calm leader. Live the tilt is
    0.6 against a 17-name cross-section where the mover (XPL) is a +3σ ATR%
    outlier; this 2-name unit pins the *mechanism* with a larger weight.
    """
    import math
    from dataclasses import replace

    from helm.signals.engine import SignalEngine

    s = _balanced_settings()  # fresh instance — safe to mutate (not the shared fixture)

    n = 200
    # CALM: higher drift (+12%), razor-thin intrabar range -> low ATR%.
    # VOLA: lower drift (+6%), wide intrabar range -> high ATR%. A faint shared
    # wiggle keeps close-to-close vol strictly positive for both.
    calm = [100.0 * (1 + 0.12 * i / (n - 1)) + 0.05 * math.sin(i / 2.0) for i in range(n)]
    vola = [100.0 * (1 + 0.06 * i / (n - 1)) + 0.05 * math.sin(i / 2.0) for i in range(n)]
    series = {"CALM": _candle_series("CALM", calm, 0.004),
              "VOLA": _candle_series("VOLA", vola, 0.06)}
    quotes = {
        "CALM": SimpleNamespace(price=calm[-1], volume_24h_usd=1e8,
                                provenance=SimpleNamespace(ok=True, source="test")),
        "VOLA": SimpleNamespace(price=vola[-1], volume_24h_usd=1e8,
                                provenance=SimpleNamespace(ok=True, source="test")),
    }
    regime = SimpleNamespace(fear_greed=50, btc_dominance=50.0,
                             funding_annual=None, sources={})
    market = _CandleMarket(series, quotes, regime)

    # --- tilt OFF: vol-adjusted momentum favours the calm, higher-drift name ---
    s.signals = replace(s.signals, vol_tilt_weight=0.0)
    base = {r.symbol: r for r in SignalEngine(s, market).compute(["CALM", "VOLA"]).signals}
    assert base["VOLA"].atr_pct > base["CALM"].atr_pct          # VOLA is the volatile one
    assert base["CALM"].composite > base["VOLA"].composite      # the bug: calm name wins

    # --- tilt ON: the high-range grid-fuel name is promoted and overtakes ---
    s.signals = replace(s.signals, vol_tilt_weight=1.5)
    tilt = {r.symbol: r for r in SignalEngine(s, market).compute(["CALM", "VOLA"]).signals}
    assert tilt["VOLA"].composite > tilt["CALM"].composite      # tilt flips the leader
    # and the tilt strictly widens VOLA's standing vs the untilted baseline
    base_edge = base["VOLA"].composite - base["CALM"].composite
    tilt_edge = tilt["VOLA"].composite - tilt["CALM"].composite
    assert tilt_edge > base_edge


def test_vol_tilt_zero_leaves_composite_unchanged():
    """Default ``vol_tilt_weight=0`` must not perturb the composite at all
    (keeps every existing backtest and the balanced profile byte-identical)."""
    import math

    from helm.signals.engine import SignalEngine

    s = _balanced_settings()
    assert s.signals.vol_tilt_weight == 0.0  # default off (balanced/backtests)

    n = 200
    a = [100.0 * (1 + 0.10 * i / (n - 1)) + 0.05 * math.sin(i / 2.0) for i in range(n)]
    b = [100.0 * (1 + 0.04 * i / (n - 1)) + 0.05 * math.sin(i / 3.0) for i in range(n)]
    series = {"A": _candle_series("A", a, 0.01), "B": _candle_series("B", b, 0.05)}
    quotes = {
        "A": SimpleNamespace(price=a[-1], volume_24h_usd=1e8,
                             provenance=SimpleNamespace(ok=True, source="test")),
        "B": SimpleNamespace(price=b[-1], volume_24h_usd=1e8,
                             provenance=SimpleNamespace(ok=True, source="test")),
    }
    regime = SimpleNamespace(fear_greed=50, btc_dominance=50.0,
                             funding_annual=None, sources={})
    market = _CandleMarket(series, quotes, regime)

    rows = {r.symbol: r for r in SignalEngine(s, market).compute(["A", "B"]).signals}
    # With tilt off, composite is purely the cross-sectional momentum z-blend:
    # two names => exact ±0.707 split, no ATR% contribution.
    assert rows["A"].composite == pytest.approx(-rows["B"].composite)
    assert abs(rows["A"].composite) > 0.0
