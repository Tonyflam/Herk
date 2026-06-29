"""Unit tests for the OMEGA ccxt execution adapter.

A fake exchange is injected so these run with no network and without ccxt
installed. The security test (secret never leaks into Fill.note) is the one that
must never regress.
"""

from __future__ import annotations

import pytest

from helm.config import load_settings
from helm.execution.base import Order
from helm.execution.ccxt_adapter import CcxtAdapter


# --------------------------------------------------------------------------- #
# Fakes                                                                       #
# --------------------------------------------------------------------------- #
class FakeExchange:
    """Minimal stand-in for a ccxt exchange object."""

    def __init__(self, fill_price: float = 100.0, raise_on_order: Exception | None = None):
        self.fill_price = fill_price
        self.raise_on_order = raise_on_order
        self.orders: list[tuple] = []
        self.leverage: tuple | None = None
        self.loaded = False

    def load_markets(self):
        self.loaded = True
        return {}

    def set_leverage(self, lev, market):
        self.leverage = (lev, market)

    def create_order(self, symbol, type_, side, amount, params=None):
        if self.raise_on_order is not None:
            raise self.raise_on_order
        self.orders.append((symbol, type_, side, amount))
        notional = amount * self.fill_price
        return {
            "average": self.fill_price,
            "filled": amount,
            "amount": amount,
            "cost": notional,
            "fee": {"cost": notional * 0.001, "currency": "USDT"},
        }


def _settings(market_type: str = "spot", leverage: bool = False, max_lev: float = 1.0):
    s = load_settings()
    s.execution.adapter = "ccxt"
    s.execution.exchange = "binance"
    s.execution.market_type = market_type
    s.execution.quote_currency = "USDT"
    s.execution.testnet = True
    s.execution.leverage_enabled = leverage
    s.execution.max_leverage = max_lev
    s.secrets.ccxt_api_key = "PUBLICKEY123"
    s.secrets.ccxt_secret = "SUPERSECRET456"
    return s


# --------------------------------------------------------------------------- #
# Spot buy / sell                                                             #
# --------------------------------------------------------------------------- #
def test_spot_buy_maps_notional_to_base_amount():
    fake = FakeExchange(fill_price=100.0)
    a = CcxtAdapter(_settings(), client=fake)
    fill = a.execute(Order("AAVE", "buy", ref_price=100.0, notional_usd=50.0))

    assert fill.ok
    assert fill.source == "ccxt"
    assert fake.orders[0][0] == "AAVE/USDT"      # spot market symbol
    assert fake.orders[0][2] == "buy"
    assert fill.qty == pytest.approx(0.5)         # 50 / 100
    assert fill.price == pytest.approx(100.0)
    assert fill.notional_usd == pytest.approx(50.0)
    assert fill.fee_usd == pytest.approx(0.05)    # 50 * 0.001
    assert fill.gas_usd == 0.0


def test_spot_sell_uses_qty():
    fake = FakeExchange(fill_price=200.0)
    a = CcxtAdapter(_settings(), client=fake)
    fill = a.execute(Order("AAVE", "sell", ref_price=200.0, qty=0.25))

    assert fill.ok
    assert fake.orders[0][2] == "sell"
    assert fill.qty == pytest.approx(0.25)
    assert fill.notional_usd == pytest.approx(50.0)


def test_no_price_fails_cleanly():
    a = CcxtAdapter(_settings(), client=FakeExchange())
    fill = a.execute(Order("AAVE", "buy", ref_price=0.0, notional_usd=50.0))
    assert not fill.ok
    assert fill.note == "no price"


def test_missing_credentials_blocks_trade():
    s = _settings()
    s.secrets.ccxt_api_key = ""
    s.secrets.ccxt_secret = ""
    a = CcxtAdapter(s, client=FakeExchange())
    fill = a.execute(Order("AAVE", "buy", ref_price=100.0, notional_usd=50.0))
    assert not fill.ok
    assert "credentials" in fill.note


# --------------------------------------------------------------------------- #
# Perps + leverage                                                            #
# --------------------------------------------------------------------------- #
def test_swap_symbol_and_leverage_clamped():
    fake = FakeExchange(fill_price=100.0)
    a = CcxtAdapter(_settings(market_type="swap", leverage=True, max_lev=3.0), client=fake)
    fill = a.execute(Order("INJ", "buy", ref_price=100.0, notional_usd=300.0))

    assert fill.ok
    assert fake.orders[0][0] == "INJ/USDT:USDT"   # USDT-margined perp symbol
    assert fake.leverage == (3.0, "INJ/USDT:USDT")  # clamped to the hard cap


def test_leverage_never_exceeds_cap():
    fake = FakeExchange()
    a = CcxtAdapter(_settings(market_type="swap", leverage=True, max_lev=2.0), client=fake)
    a.execute(Order("INJ", "buy", ref_price=100.0, notional_usd=100.0))
    assert fake.leverage is not None
    assert fake.leverage[0] <= 2.0


def test_spot_never_sets_leverage():
    fake = FakeExchange()
    a = CcxtAdapter(_settings(market_type="spot", leverage=True, max_lev=5.0), client=fake)
    a.execute(Order("AAVE", "buy", ref_price=100.0, notional_usd=50.0))
    assert fake.leverage is None  # leverage is a perps-only concept


# --------------------------------------------------------------------------- #
# Security: a credential must NEVER leak into a surfaced error                 #
# --------------------------------------------------------------------------- #
def test_secret_is_scrubbed_from_error_note():
    secret = "SUPERSECRET456"
    boom = Exception(f"auth failed for key=PUBLICKEY123 secret={secret}")
    fake = FakeExchange(raise_on_order=boom)
    a = CcxtAdapter(_settings(), client=fake)
    fill = a.execute(Order("AAVE", "buy", ref_price=100.0, notional_usd=50.0))

    assert not fill.ok
    assert secret not in fill.note
    assert "PUBLICKEY123" not in fill.note
    assert "***" in fill.note


def test_supports_live_true():
    a = CcxtAdapter(_settings(), client=FakeExchange())
    assert a.supports_live() is True
