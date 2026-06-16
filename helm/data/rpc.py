"""Redundant BSC RPC endpoints with health-probing failover.

A single RPC provider hiccup must never blind HELM during the live week. This
module keeps an ordered list of public BNB Smart Chain endpoints and picks the
first responsive one, so on-chain reads (balance marking, nonce, gas) always
have a live path. Live execution itself is still signed locally by TWAK — these
endpoints are read-only plumbing and a transparent failover surface.

No API key required. ``probe`` does a tiny ``eth_blockNumber`` JSON-RPC POST and
returns latency so ``helm preflight`` can show which endpoints are healthy.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

import httpx

from ..config import Settings

# Well-known public BSC mainnet RPCs, tried in order. Multiple operators so no
# single provider outage takes the agent offline.
DEFAULT_BSC_RPCS: tuple[str, ...] = (
    "https://bsc-dataseed.bnbchain.org",
    "https://bsc-dataseed1.defibit.io",
    "https://bsc-dataseed1.ninicoin.io",
    "https://bsc.publicnode.com",
    "https://rpc.ankr.com/bsc",
)

_TIMEOUT = httpx.Timeout(6.0, connect=3.0)
_HEADERS = {"Content-Type": "application/json", "User-Agent": "HELM/0.1 (+bnb-hack agent)"}


@dataclass
class RpcHealth:
    url: str
    ok: bool
    block: int = 0
    latency_ms: float = 0.0
    note: str = ""


def endpoints(settings: Settings) -> list[str]:
    """Configured ``execution.bsc_rpc_urls`` if any, else the public defaults."""
    configured = [u.strip() for u in settings.execution.bsc_rpc_urls if u and u.strip()]
    return configured or list(DEFAULT_BSC_RPCS)


def probe(url: str, client: httpx.Client | None = None) -> RpcHealth:
    """Health-check one endpoint via ``eth_blockNumber``; never raises."""
    own = client is None
    client = client or httpx.Client()
    payload = {"jsonrpc": "2.0", "id": 1, "method": "eth_blockNumber", "params": []}
    t0 = time.monotonic()
    try:
        r = client.post(url, json=payload, headers=_HEADERS, timeout=_TIMEOUT)
        r.raise_for_status()
        result = r.json().get("result")
        if not isinstance(result, str) or not result.startswith("0x"):
            return RpcHealth(url, ok=False, note="no block in response")
        block = int(result, 16)
        latency = (time.monotonic() - t0) * 1000.0
        return RpcHealth(url, ok=block > 0, block=block, latency_ms=latency)
    except Exception as e:  # unreachable / rate-limited / malformed
        return RpcHealth(url, ok=False, note=f"{type(e).__name__}")
    finally:
        if own:
            client.close()


def health(settings: Settings) -> list[RpcHealth]:
    """Probe every configured endpoint (for preflight visibility)."""
    with httpx.Client() as client:
        return [probe(u, client) for u in endpoints(settings)]


def first_live(settings: Settings) -> str | None:
    """The first responsive endpoint, or ``None`` if all are down."""
    with httpx.Client() as client:
        for url in endpoints(settings):
            if probe(url, client).ok:
                return url
    return None
