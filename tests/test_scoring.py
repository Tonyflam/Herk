"""Score-truthful marking (#4): on-chain balance reads, the book-vs-chain
reconciliation contract (live scoring is from chain, so HELM marks from chain),
and route/liquidity pre-validation that keeps the agent out of unroutable names.
"""

from __future__ import annotations

from types import SimpleNamespace

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
