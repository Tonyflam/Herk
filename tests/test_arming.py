"""Go-live arming gates — the difference between trading and a silent DQ.

Three latent live-path bugs were found and fixed before the contest. These
tests pin the fixes so a future edit can never silently re-introduce them:

  * WALLET: live reconcile + official scoring must read the TWAK trading
    wallet that actually holds the funds. ``resolve_wallet`` must return it
    (from config, overridable by ``HELM_WALLET_ADDRESS``) — never "".
  * BROADCAST GATE: a swap only broadcasts when the full live trio is armed
    AND ``quote_only_dry_run`` is false. Every weaker combination stays
    quote-only (simulated) so paper/dry-run can never spend real funds.
  * ENV ARMING: ``HELM_QUOTE_ONLY`` is part of the env-driven arming flow, so
    flipping the broadcast gate doesn't require hand-editing settings.yaml.
"""

from __future__ import annotations

import pytest

from helm.config import load_settings
from helm.data import onchain
from helm.execution.twak import TwakAdapter

# The funded TWAK trading wallet HELM swaps from / is scored on.
TWAK_WALLET = "0x2d8d17a72c8462AdbF1538Bfe03F5f2AaACb471A"

_ARM_KEYS = (
    "HELM_MODE", "HELM_EXECUTE_TRADES", "HELM_EXECUTE_CHAIN",
    "HELM_QUOTE_ONLY", "HELM_EXECUTION_ADAPTER", "HELM_WALLET_ADDRESS",
)


def _settings(monkeypatch, **env):
    """Load settings with a clean, explicit arming environment."""
    monkeypatch.setenv("HELM_PROFILE", "balanced")
    for k in _ARM_KEYS:
        monkeypatch.delenv(k, raising=False)
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    return load_settings()


# ----------------------------------------------------------------- wallet
def test_wallet_resolves_to_funded_twak_wallet(monkeypatch):
    """Empty wallet_address was the bug: reconcile could never read the book."""
    s = _settings(monkeypatch)
    assert s.execution.wallet_address == TWAK_WALLET
    assert onchain.resolve_wallet(s) == TWAK_WALLET


def test_wallet_address_env_override(monkeypatch):
    override = "0xABcdEF0000000000000000000000000000000001"
    s = _settings(monkeypatch, HELM_WALLET_ADDRESS=override)
    assert onchain.resolve_wallet(s) == override


# --------------------------------------------------------- broadcast gate
# (mode, execute_trades, execute_chain, quote_only_env) -> broadcasts?
_MATRIX = [
    ("paper",                {},                                                              False),
    ("live trio, qo unset",  {"HELM_MODE": "live", "HELM_EXECUTE_TRADES": "1", "HELM_EXECUTE_CHAIN": "1"}, False),
    ("live trio + qo=0",     {"HELM_MODE": "live", "HELM_EXECUTE_TRADES": "1", "HELM_EXECUTE_CHAIN": "1", "HELM_QUOTE_ONLY": "0"}, True),
    ("qo=0 but not live",    {"HELM_QUOTE_ONLY": "0"},                                        False),
    ("missing execute_chain",{"HELM_MODE": "live", "HELM_EXECUTE_TRADES": "1", "HELM_QUOTE_ONLY": "0"},   False),
    ("missing execute_trades",{"HELM_MODE": "live", "HELM_EXECUTE_CHAIN": "1", "HELM_QUOTE_ONLY": "0"},   False),
]


@pytest.mark.parametrize("label,env,broadcasts", _MATRIX,
                         ids=[m[0] for m in _MATRIX])
def test_broadcast_gate_matrix(monkeypatch, label, env, broadcasts):
    s = _settings(monkeypatch, **env)
    a = TwakAdapter(s)
    armed = a._live_armed()
    # A swap broadcasts iff armed AND not quote-only.
    assert (armed and not a._quote_only()) is broadcasts


def test_quote_only_env_flips_broadcast(monkeypatch):
    """HELM_QUOTE_ONLY=0 is the single env flag that arms broadcasting."""
    live = {"HELM_MODE": "live", "HELM_EXECUTE_TRADES": "1", "HELM_EXECUTE_CHAIN": "1"}
    blocked = TwakAdapter(_settings(monkeypatch, **live))
    assert blocked._quote_only() is True  # footgun: armed but still simulated

    armed = TwakAdapter(_settings(monkeypatch, **live, HELM_QUOTE_ONLY="0"))
    assert armed._quote_only() is False
    assert armed._live_armed() is True
