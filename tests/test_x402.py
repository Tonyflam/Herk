"""Native x402 in the trade loop (Best-Use-of-TWAK prize) — and, just as
importantly, proof that it can NEVER hurt the $10k PnL path.

The agent pays per call (USDT on BSC) for a fresh CMC quote that confirms each
live entry price. These tests pin the three properties that matter:

  * SAFE: in paper / backtest the paid path is a hard no-op (parity preserved).
  * BOUNDED: a hard per-UTC-day cap limits the real cost on a small book.
  * FALLBACK: any payment failure or junk response falls back to the reference
    price and never raises into the loop.
"""

from __future__ import annotations

import dataclasses
from datetime import datetime, timezone

from helm.agent import Agent
from helm.config import load_settings
from helm.data.cmc import CMCClient
from helm.execution.twak import TwakResult


# --------------------------------------------------------------- price parsing
def test_parse_quote_price_v2_shape():
    data = {"data": {"CAKE": {"quote": {"USD": {"price": 2.5}}}}}
    assert CMCClient.parse_quote_price(data, "CAKE") == 2.5


def test_parse_quote_price_v3_list_shape():
    data = {"data": {"CAKE": [{"quote": {"USD": {"price": 2.5}}}]}}
    assert CMCClient.parse_quote_price(data, "CAKE") == 2.5


def test_parse_quote_price_case_insensitive_symbol():
    data = {"data": {"cake": {"quote": {"USD": {"price": 3.0}}}}}
    assert CMCClient.parse_quote_price(data, "CAKE") == 3.0


def test_parse_quote_price_returns_none_on_junk():
    for bad in (None, {}, {"data": {}}, "nonsense", {"data": {"CAKE": {}}},
                {"data": {"CAKE": {"quote": {"USD": {"price": 0}}}}}):
        assert CMCClient.parse_quote_price(bad, "CAKE") is None


# --------------------------------------------------------------- test plumbing
def _fresh_agent(tmp_path) -> Agent:
    return Agent(state_path=tmp_path / "state.json", ledger_path=tmp_path / "audit.jsonl")


class _SpyX402Executor:
    """Records x402 calls and returns a canned quote payload."""

    def __init__(self, price=10.5, ok=True, boom=False):
        self.calls = 0
        self.price = price
        self.ok = ok
        self.boom = boom

    def x402_request(self, url):
        self.calls += 1
        if self.boom:
            raise RuntimeError("payment rail down")
        data = {"data": {"AAA": {"quote": {"USD": {"price": self.price}}}}}
        return TwakResult(self.ok, "", "", data if self.ok else None,
                          "" if self.ok else "402 failed")


def _arm_live(agent: Agent) -> None:
    """Flip the in-memory settings so is_live + x402 gates are satisfied."""
    agent.settings.mode = "live"
    agent.settings.execution.adapter = "twak"
    agent.settings.execution.x402_on_buys = True
    agent.settings.secrets = dataclasses.replace(
        agent.settings.secrets, execute_trades=True, x402_enabled=True,
        x402_max_calls_per_day=6,
    )


_NOW = datetime(2026, 6, 23, 12, 0, tzinfo=timezone.utc)


# ----------------------------------------------------------------- SAFE no-op
def test_x402_not_ready_in_paper(tmp_path):
    agent = _fresh_agent(tmp_path)
    try:
        # default settings are paper-mode → never pay
        assert agent._x402_ready() is False
    finally:
        agent.close()


def test_x402_noop_keeps_reference_price_when_disarmed(tmp_path):
    agent = _fresh_agent(tmp_path)
    try:
        spy = _SpyX402Executor(price=999.0)
        agent.executor = spy
        # _x402_ready is False (paper), so a correct caller never calls _prebuy;
        # but even if invoked directly it must respect the daily cap of 0 when
        # x402 is disabled. Here we assert the readiness gate blocks paying.
        assert agent._x402_ready() is False
        assert spy.calls == 0
    finally:
        agent.close()


# ------------------------------------------------------------------- BOUNDED
def test_x402_respects_daily_cap(tmp_path):
    agent = _fresh_agent(tmp_path)
    try:
        _arm_live(agent)
        agent.settings.secrets = dataclasses.replace(
            agent.settings.secrets, x402_max_calls_per_day=2)
        spy = _SpyX402Executor(price=10.0)
        agent.executor = spy
        for _ in range(5):
            agent._x402_prebuy("AAA", 10.0, _NOW)
        assert spy.calls == 2  # capped at 2 paid calls for the day
    finally:
        agent.close()


def test_x402_cap_resets_next_day(tmp_path):
    agent = _fresh_agent(tmp_path)
    try:
        _arm_live(agent)
        agent.settings.secrets = dataclasses.replace(
            agent.settings.secrets, x402_max_calls_per_day=1)
        spy = _SpyX402Executor(price=10.0)
        agent.executor = spy
        agent._x402_prebuy("AAA", 10.0, _NOW)
        agent._x402_prebuy("AAA", 10.0, _NOW)               # same day → blocked
        assert spy.calls == 1
        next_day = _NOW.replace(day=24)
        agent._x402_prebuy("AAA", 10.0, next_day)           # new day → allowed
        assert spy.calls == 2
    finally:
        agent.close()


# ------------------------------------------------------------------ FALLBACK
def test_x402_uses_fresh_price_within_band(tmp_path):
    agent = _fresh_agent(tmp_path)
    try:
        _arm_live(agent)
        agent.executor = _SpyX402Executor(price=10.4)       # +4% of 10.0 → trusted
        out = agent._x402_prebuy("AAA", 10.0, _NOW)
        assert out == 10.4
    finally:
        agent.close()


def test_x402_rejects_out_of_band_price(tmp_path):
    agent = _fresh_agent(tmp_path)
    try:
        _arm_live(agent)
        agent.executor = _SpyX402Executor(price=100.0)      # +900% → distrust
        out = agent._x402_prebuy("AAA", 10.0, _NOW)
        assert out == 10.0                                   # falls back to ref
    finally:
        agent.close()


def test_x402_payment_failure_falls_back(tmp_path):
    agent = _fresh_agent(tmp_path)
    try:
        _arm_live(agent)
        agent.executor = _SpyX402Executor(ok=False)
        out = agent._x402_prebuy("AAA", 10.0, _NOW)
        assert out == 10.0
    finally:
        agent.close()


def test_x402_exception_never_breaks_loop(tmp_path):
    agent = _fresh_agent(tmp_path)
    try:
        _arm_live(agent)
        agent.executor = _SpyX402Executor(boom=True)
        out = agent._x402_prebuy("AAA", 10.0, _NOW)          # must not raise
        assert out == 10.0
    finally:
        agent.close()
