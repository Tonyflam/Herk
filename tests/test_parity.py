"""Paper/live parity: the simulator must never flatter itself versus live.

The failure mode that quietly kills a trading agent is a paper book that looks
better than the live one. HELM closes the structural gaps so the two ledgers are
comparable:

  • BSC network gas is charged on EVERY swap in BOTH paper and live (a naive
    backtest charges none, so live silently underperforms paper).
  • A failed/empty live swap must not book a phantom position or trade — paper
    never fails there, so live must skip it too (guarded in ``Agent``).
  • The SAME ``Fill`` moves the book identically regardless of ``source`` — the
    accounting is execution-venue-agnostic.

These tests pin those invariants so a refactor can't silently reopen the gap.
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from helm.config import load_settings
from helm.execution.base import Fill, Order
from helm.execution.paper import PaperExecutor
from helm.execution.twak import TwakAdapter
from helm.portfolio import Portfolio


def _settings(gas: float = 0.50):
    """A private settings copy with a known gas cost (never the shared fixture)."""
    s = load_settings()
    s.risk = replace(s.risk, gas_usd_per_swap=gas)
    return s


# --------------------------------------------------------------- gas modelled
def test_paper_buy_charges_gas():
    ex = PaperExecutor(_settings(gas=0.50))
    fill = ex.execute(Order("ETH", "buy", ref_price=2000.0,
                            notional_usd=500.0, liquidity_usd=50_000_000.0))
    assert fill.ok and fill.qty > 0
    assert fill.gas_usd == pytest.approx(0.50)


def test_paper_sell_charges_gas():
    ex = PaperExecutor(_settings(gas=0.50))
    fill = ex.execute(Order("ETH", "sell", ref_price=2000.0,
                            qty=0.25, liquidity_usd=50_000_000.0))
    assert fill.ok and fill.qty > 0
    assert fill.gas_usd == pytest.approx(0.50)


def test_no_fill_charges_no_gas():
    # A zero-price order never executes — and must not burn modelled gas.
    ex = PaperExecutor(_settings(gas=0.50))
    fill = ex.execute(Order("ETH", "buy", ref_price=0.0,
                            notional_usd=500.0, liquidity_usd=50_000_000.0))
    assert not fill.ok
    assert fill.gas_usd == 0.0


# --------------------------------------------- accounting is venue-agnostic
def test_same_fill_moves_book_identically_paper_vs_live():
    """A paper Fill and a twak Fill with identical economics must produce the
    identical book — proving the accounting is blind to execution venue."""
    econ = dict(symbol="ETH", side="buy", qty=0.25, price=2000.0,
                notional_usd=500.0, fee_usd=1.5, slippage_bps=8.0,
                ts="t", ok=True, gas_usd=0.50)
    paper_fill = Fill(source="paper", **econ)
    live_fill = Fill(source="twak", **econ)

    pa = Portfolio.new(10_000.0)
    pl = Portfolio.new(10_000.0)
    pa.apply_buy(paper_fill, stop_price=1800.0, take_profit_price=2400.0, stop_distance=200.0)
    pl.apply_buy(live_fill, stop_price=1800.0, take_profit_price=2400.0, stop_distance=200.0)

    assert pa.cash == pytest.approx(pl.cash)
    assert pa.gas_paid == pytest.approx(pl.gas_paid) == pytest.approx(0.50)
    assert pa.fees_paid == pytest.approx(pl.fees_paid)
    assert pa.equity({"ETH": 2000.0}) == pytest.approx(pl.equity({"ETH": 2000.0}))


def test_buy_debits_cash_by_notional_plus_fee_plus_gas():
    pf = Portfolio.new(10_000.0)
    fill = Fill("ETH", "buy", qty=0.25, price=2000.0, notional_usd=500.0,
                fee_usd=1.5, slippage_bps=8.0, ts="t", source="paper", gas_usd=0.50)
    pf.apply_buy(fill, stop_price=1800.0, take_profit_price=2400.0, stop_distance=200.0)
    # cash out = notional + fee + gas
    assert pf.cash == pytest.approx(10_000.0 - 500.0 - 1.5 - 0.50)
    assert pf.gas_paid == pytest.approx(0.50)


# --------------------------------------- the anti-"paper looks rosier" guard
def test_flat_roundtrip_loses_exactly_fees_and_gas():
    """Buy then sell the SAME quantity at the SAME price: the book must LOSE
    exactly (2 fees + 2 gas), never break even. This is the core invariant that
    stops paper from out-printing live on churn."""
    gas, fee = 0.50, 1.5
    pf = Portfolio.new(10_000.0)
    buy = Fill("ETH", "buy", qty=0.25, price=2000.0, notional_usd=500.0,
               fee_usd=fee, slippage_bps=0.0, ts="t", source="paper", gas_usd=gas)
    pf.apply_buy(buy, stop_price=1800.0, take_profit_price=2400.0, stop_distance=200.0)

    sell = Fill("ETH", "sell", qty=0.25, price=2000.0, notional_usd=500.0,
                fee_usd=fee, slippage_bps=0.0, ts="t", source="paper", gas_usd=gas)
    realized = pf.apply_sell(sell)

    expected_drag = 2 * fee + 2 * gas
    assert pf.cash == pytest.approx(10_000.0 - expected_drag)
    assert pf.gas_paid == pytest.approx(2 * gas)
    # realized P&L on the closed leg nets the price move (0) minus this leg's costs.
    assert realized == pytest.approx(-(fee + gas))
    assert "ETH" not in pf.positions  # fully closed


# ------------------------------------------- live receipt gas extraction
def test_receipt_gas_prefers_real_cost():
    assert TwakAdapter._receipt_gas_usd({"gasFeeUsd": "0.21"}) == pytest.approx(0.21)
    assert TwakAdapter._receipt_gas_usd({"networkFeeUsd": 0.18}) == pytest.approx(0.18)


def test_receipt_gas_falls_back_to_none_when_absent():
    assert TwakAdapter._receipt_gas_usd({"toAmount": "1.0"}) is None
    assert TwakAdapter._receipt_gas_usd({}) is None
    assert TwakAdapter._receipt_gas_usd({"gasFeeUsd": "not-a-number"}) is None
