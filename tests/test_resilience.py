"""Resilience hardening (#3): redundant RPC failover, the supervised-run state
recovery contract, and the contest-critical daily-floor guarantee — fired early
(default 18:00 UTC, not 23:59) and retried, so a transient error can never cost
the >=1-trade/day disqualification floor.
"""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

from helm.agent import Agent
from helm.data import rpc
from helm.execution.base import Fill, Order, _now_iso


# --------------------------------------------------------------- RPC failover
def test_endpoints_default_when_unconfigured(settings):
    settings.execution.bsc_rpc_urls = []
    eps = rpc.endpoints(settings)
    assert eps == list(rpc.DEFAULT_BSC_RPCS)
    assert len(eps) >= 3  # genuinely redundant, not a single point of failure


def test_endpoints_use_configured_list(settings):
    settings.execution.bsc_rpc_urls = ["https://my-node.example/rpc", " https://b.example/rpc "]
    try:
        assert rpc.endpoints(settings) == ["https://my-node.example/rpc", "https://b.example/rpc"]
    finally:
        settings.execution.bsc_rpc_urls = []


class _FakeResp:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


class _FakeClient:
    def __init__(self, payloads):
        self._payloads = payloads  # url -> payload | Exception

    def post(self, url, **kw):
        p = self._payloads.get(url)
        if isinstance(p, Exception):
            raise p
        return _FakeResp(p)

    def close(self):
        pass


def test_probe_parses_block_number():
    url = "https://node/rpc"
    h = rpc.probe(url, _FakeClient({url: {"result": "0x100"}}))
    assert h.ok and h.block == 256


def test_probe_handles_unreachable():
    url = "https://down/rpc"
    h = rpc.probe(url, _FakeClient({url: RuntimeError("conn refused")}))
    assert not h.ok and h.block == 0


def test_first_live_skips_dead_endpoints(settings, monkeypatch):
    good = "https://good/rpc"
    settings.execution.bsc_rpc_urls = ["https://dead/rpc", good]
    monkeypatch.setattr(rpc, "probe",
                        lambda url, client=None: rpc.RpcHealth(url, ok=(url == good)))
    try:
        assert rpc.first_live(settings) == good
    finally:
        settings.execution.bsc_rpc_urls = []


# ------------------------------------------------------- agent test helpers
def _fresh_agent(tmp_path) -> Agent:
    """Fully offline paper agent with an isolated, empty state + ledger."""
    return Agent(state_path=tmp_path / "state.json", ledger_path=tmp_path / "audit.jsonl")


def _fake_snap():
    sig = SimpleNamespace(symbol="AAA", price=10.0, liquidity_usd=5_000_000.0, atr=0.5)
    return SimpleNamespace(ranked=[sig], signals=[sig])


_POSTURE = SimpleNamespace(halt_new_risk=False)
_ORDER = Order("AAA", "buy", ref_price=10.0, notional_usd=2.0, liquidity_usd=5e6, reason="min_daily_trade")


def _good_fill() -> Fill:
    return Fill("AAA", "buy", qty=0.2, price=10.0, notional_usd=2.0, fee_usd=0.0,
                slippage_bps=0.0, ts=_now_iso(), source="test", ok=True)


class _BoomExecutor:
    def __init__(self):
        self.calls = 0

    def execute(self, order):
        self.calls += 1
        raise RuntimeError("rpc timeout")


class _FlakyExecutor:
    """Fails the first ``fail_n`` calls, then fills."""

    def __init__(self, fail_n):
        self.calls = 0
        self.fail_n = fail_n

    def execute(self, order):
        self.calls += 1
        if self.calls <= self.fail_n:
            raise RuntimeError("transient")
        return _good_fill()


# ----------------------------------------------------- daily-floor deadline
def test_daily_floor_waits_until_deadline(tmp_path):
    agent = _fresh_agent(tmp_path)
    try:
        agent.settings.contest.min_trade_deadline_hour = 18
        before = datetime(2026, 6, 23, 12, 0, tzinfo=timezone.utc)  # 12:00 < 18:00
        actions: list = []
        agent._ensure_min_trade(before, _fake_snap(), {}, _POSTURE, actions, dry_run=False)
        assert agent.portfolio.trades_today == 0
        assert not any(a.kind == "compliance" for a in actions)
    finally:
        agent.close()


def test_daily_floor_fires_after_deadline(tmp_path):
    agent = _fresh_agent(tmp_path)
    try:
        agent.settings.contest.min_trade_deadline_hour = 18
        after = datetime(2026, 6, 23, 19, 0, tzinfo=timezone.utc)  # 19:00 >= 18:00
        actions: list = []
        agent._ensure_min_trade(after, _fake_snap(), {}, _POSTURE, actions, dry_run=False)
        assert agent.portfolio.trades_today == 1
        assert any(a.kind == "compliance" for a in actions)
    finally:
        agent.close()


def test_daily_floor_fires_even_when_halted(tmp_path):
    """The automatic drawdown halt must NOT suppress the >=1-trade/day floor.

    If the book draws down to the internal halt line and parks in cash, the halt
    persists (drawdown is measured from peak). Gating the floor on it would skip
    the compliance ping every subsequent day and walk us into a trade-count DQ —
    while still below the 30% drawdown gate and potentially recoverable. The dust
    ping adds negligible risk, so it must still fire. Only the manual kill-switch
    may suppress it."""
    agent = _fresh_agent(tmp_path)
    try:
        agent.settings.contest.min_trade_deadline_hour = 18
        after = datetime(2026, 6, 23, 19, 0, tzinfo=timezone.utc)
        halted = SimpleNamespace(halt_new_risk=True)
        actions: list = []
        agent._ensure_min_trade(after, _fake_snap(), {}, halted, actions, dry_run=False)
        assert agent.portfolio.trades_today == 1
        ping = next(a for a in actions if a.kind == "compliance")
        assert "under halt" in ping.detail
    finally:
        agent.close()


def test_daily_floor_suppressed_by_kill_switch(tmp_path):
    """The manual kill-switch is an explicit human STOP and DOES suppress the
    floor — even past the deadline — so an operator can halt the agent."""
    agent = _fresh_agent(tmp_path)
    try:
        agent.settings.contest.min_trade_deadline_hour = 18
        after = datetime(2026, 6, 23, 19, 0, tzinfo=timezone.utc)
        agent.sentinel.kill_switch_engaged = lambda: True  # type: ignore[assignment]
        actions: list = []
        agent._ensure_min_trade(after, _fake_snap(), {}, _POSTURE, actions, dry_run=False)
        assert agent.portfolio.trades_today == 0
        assert not any(a.kind == "compliance" for a in actions)
    finally:
        agent.close()


# --------------------------------------------------------- executor retry
def test_execute_with_retry_gives_up_after_attempts(tmp_path):
    agent = _fresh_agent(tmp_path)
    try:
        agent.executor = _BoomExecutor()
        fill, err = agent._execute_with_retry(_ORDER, attempts=3)
        assert fill is None
        assert "RuntimeError" in err
        assert agent.executor.calls == 3  # exhausted all attempts
    finally:
        agent.close()


def test_execute_with_retry_recovers_after_transient(tmp_path):
    agent = _fresh_agent(tmp_path)
    try:
        agent.executor = _FlakyExecutor(fail_n=2)
        fill, err = agent._execute_with_retry(_ORDER, attempts=3)
        assert err is None and fill is not None
        assert agent.executor.calls == 3  # failed twice, third succeeded
    finally:
        agent.close()


def test_daily_floor_failure_is_logged_not_counted(tmp_path):
    """A failed ping must surface for retry and NOT be miscounted as the day's
    trade — otherwise a silent failure would walk us into a DQ."""
    agent = _fresh_agent(tmp_path)
    try:
        agent.executor = _BoomExecutor()
        agent.settings.contest.min_trade_deadline_hour = 18
        after = datetime(2026, 6, 23, 19, 0, tzinfo=timezone.utc)
        actions: list = []
        agent._ensure_min_trade(after, _fake_snap(), {}, _POSTURE, actions, dry_run=False)
        assert agent.portfolio.trades_today == 0
        assert any(a.kind == "blocked" for a in actions)
        assert any(r.get("type") == "alert" for r in agent.ledger.tail(5))
    finally:
        agent.close()


# ------------------------------------------------ supervised-loop recovery
def test_reload_state_recovers_last_checkpoint(tmp_path):
    """The supervisor calls reload_state() after a failed cycle to discard a
    partial in-memory mutation; it must restore the last persisted checkpoint."""
    agent = _fresh_agent(tmp_path)
    try:
        agent.portfolio.trades_today = 0
        agent._save_state()                 # checkpoint
        agent.portfolio.trades_today = 99    # simulate a partial mid-step mutation
        agent.portfolio.cash = -123.0
        assert agent.reload_state() is True
        assert agent.portfolio.trades_today == 0
        assert agent.portfolio.cash != -123.0
    finally:
        agent.close()


# ------------------------------------------------ supervised run loop (CLI)
def _supervise_args(**over):
    base = dict(dry_run=True, cycles=3, interval=0, until=None, supervise=True)
    base.update(over)
    return SimpleNamespace(**base)


def test_supervised_loop_survives_transient_failures(monkeypatch):
    """A step that throws must be caught, logged, recovered, and the loop must
    keep going — a single transient error can't kill the week-long run."""
    from helm import cli

    calls = {"step": 0, "reload": 0}

    class _FakeAgent:
        def __init__(self, *a, **k):
            self.settings = SimpleNamespace(is_live=False, profile="balanced")
            self.ledger = SimpleNamespace(append=lambda *a, **k: None)

        def step(self, dry_run=False):
            calls["step"] += 1
            if calls["step"] <= 2:
                raise RuntimeError("transient boom")
            return "ok"

        def reload_state(self):
            calls["reload"] += 1
            return True

        def close(self):
            pass

    monkeypatch.setattr("helm.agent.Agent", _FakeAgent)
    monkeypatch.setattr(cli, "_print_report", lambda *a, **k: None)

    rc = cli.cmd_run(_supervise_args(cycles=3))
    assert rc == 0
    assert calls["step"] == 3      # 2 failed + 1 succeeded; loop never crashed
    assert calls["reload"] == 2    # recovered from each failure


def test_supervised_loop_aborts_after_max_consecutive(monkeypatch):
    """A systemic failure must not spin forever — abort after the consecutive
    failure ceiling with a non-zero exit so the process watchdog backs off."""
    from helm import cli

    class _FakeAgent:
        def __init__(self, *a, **k):
            self.settings = SimpleNamespace(is_live=False, profile="balanced")
            self.ledger = SimpleNamespace(append=lambda *a, **k: None)

        def step(self, dry_run=False):
            raise RuntimeError("always boom")

        def reload_state(self):
            return True

        def close(self):
            pass

    monkeypatch.setattr("helm.agent.Agent", _FakeAgent)
    monkeypatch.setattr(cli, "_print_report", lambda *a, **k: None)

    rc = cli.cmd_run(_supervise_args(dry_run=False, cycles=100))
    assert rc == 1  # gave up after _MAX_CONSEC_FAILURES, did not loop endlessly
