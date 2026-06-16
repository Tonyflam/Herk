"""On-chain balance reads for score-truthful marking.

The competition scores the live week from the wallet's *actual on-chain balances*.
HELM's internal book can drift from chain (gas spent in BNB, slippage vs. the
modeled fill, dust, partial fills), so in live mode the agent reconciles — and
optionally marks — against what the chain actually holds. That is what the judges
see, so that is what HELM scores itself on.

Pure standard JSON-RPC (``eth_call`` / ``eth_getBalance``) over the redundant BSC
endpoints in :mod:`helm.data.rpc` — no dependency on any vendor SDK or guessed
CLI surface. Read-only; never signs. Best-effort: every helper degrades to a
clearly-flagged miss rather than raising into the trading loop.

Contract addresses: the built-in registry below covers only the *high-confidence*
cash leg + a few blue chips (canonical BSC/Binance-Peg tokens). Authoritative
addresses for anything else are supplied via ``execution.token_addresses`` in
config (e.g. mirrored from TWAK). An unknown symbol is skipped, not guessed.
"""

from __future__ import annotations

from dataclasses import dataclass

import httpx

from ..config import Settings
from . import rpc

_TIMEOUT = httpx.Timeout(8.0, connect=4.0)
_HEADERS = {"Content-Type": "application/json", "User-Agent": "HELM/0.1 (+bnb-hack agent)"}

# ERC-20 function selectors (first 4 bytes of keccak256 signature).
_SEL_BALANCE_OF = "70a08231"   # balanceOf(address)
_SEL_DECIMALS = "313ce567"     # decimals()
_SEL_SYMBOL = "95d89b41"       # symbol()

# High-confidence canonical BSC (chain 56) token registry: address + decimals.
# Deliberately small — only tokens whose mainnet address is unambiguous. The
# cash leg (USDT) dominates a survival-first book's equity, so verifying it on
# chain with certainty captures most of the marking value at near-zero risk.
_REGISTRY: dict[str, tuple[str, int]] = {
    "USDT": ("0x55d398326f99059fF775485246999027B3197955", 18),  # BSC-USD
    "USDC": ("0x8AC76a51cc950d9822D68b83fE1Ad97B32Cd580d", 18),
    "WBNB": ("0xbb4CdB9CBd36B01bD1cBaEBF2De08d9173bc095c", 18),
    "BNB":  ("0xbb4CdB9CBd36B01bD1cBaEBF2De08d9173bc095c", 18),  # wrapped, for ref
    "ETH":  ("0x2170Ed0880ac9A755fd29B2688956BD959F933F8", 18),
    "BTCB": ("0x7130d2A12B9BCBFAe4f2634d864A1Ee1Ce3Ead9c", 18),
    "CAKE": ("0x0E09FaBB73Bd3Ade0a17ECC321fD13a19e81cE82", 18),
}


@dataclass
class Holding:
    symbol: str
    units: float = 0.0       # human units (raw / 10**decimals)
    raw: int = 0
    decimals: int = 18
    address: str = ""
    ok: bool = False
    note: str = ""


def _pad_addr(addr: str) -> str:
    """Left-pad a 20-byte hex address to a 32-byte ABI word."""
    return addr.lower().replace("0x", "").rjust(64, "0")


def _rpc_call(url: str, method: str, params: list, client: httpx.Client) -> object:
    payload = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
    r = client.post(url, json=payload, headers=_HEADERS, timeout=_TIMEOUT)
    r.raise_for_status()
    body = r.json()
    if "error" in body and body["error"]:
        raise RuntimeError(str(body["error"]))
    return body.get("result")


def _eth_call(url: str, to: str, data_hex: str, client: httpx.Client) -> str:
    res = _rpc_call(url, "eth_call", [{"to": to, "data": "0x" + data_hex}, "latest"], client)
    return res if isinstance(res, str) else ""


def erc20_decimals(url: str, token: str, client: httpx.Client) -> int | None:
    try:
        res = _eth_call(url, token, _SEL_DECIMALS, client)
        return int(res, 16) if res and res != "0x" else None
    except Exception:
        return None


def erc20_symbol(url: str, token: str, client: httpx.Client) -> str:
    """Best-effort decode of ERC-20 ``symbol()`` (dynamic string). '' on miss."""
    try:
        res = _eth_call(url, token, _SEL_SYMBOL, client)
        h = (res or "").replace("0x", "")
        if len(h) >= 128:  # offset + length + data
            length = int(h[64:128], 16)
            raw = bytes.fromhex(h[128:128 + length * 2])
            return raw.decode("utf-8", "ignore").strip("\x00").strip()
        # Some tokens return a fixed bytes32 symbol.
        if h:
            return bytes.fromhex(h).decode("utf-8", "ignore").strip("\x00").strip()
    except Exception:
        pass
    return ""


def erc20_balance_of(url: str, token: str, wallet: str, client: httpx.Client) -> int | None:
    try:
        data = _SEL_BALANCE_OF + _pad_addr(wallet)
        res = _eth_call(url, token, data, client)
        return int(res, 16) if res and res != "0x" else 0
    except Exception:
        return None


def native_balance(url: str, wallet: str, client: httpx.Client) -> int | None:
    """Native BNB balance in wei (the gas leg — needs no token address)."""
    try:
        res = _rpc_call(url, "eth_getBalance", [wallet, "latest"], client)
        return int(res, 16) if isinstance(res, str) else None
    except Exception:
        return None


def token_meta(settings: Settings, symbol: str) -> tuple[str, int | None] | None:
    """Resolve (address, decimals_hint) for a symbol.

    Config override (``execution.token_addresses``) wins over the built-in
    registry. Returns ``None`` for an unknown symbol (caller skips it). Decimals
    from the registry are a hint; live reads confirm them on chain.
    """
    sym = symbol.upper()
    override = {k.upper(): v for k, v in (settings.execution.token_addresses or {}).items()}
    if sym in override and override[sym]:
        return str(override[sym]), None
    if sym in _REGISTRY:
        addr, dec = _REGISTRY[sym]
        return addr, dec
    return None


def resolve_wallet(settings: Settings) -> str:
    """Best-effort wallet address: explicit config > saved ERC-8004 identity."""
    if settings.execution.wallet_address:
        return settings.execution.wallet_address.strip()
    try:
        from ..identity.erc8004 import Erc8004Identity
        ident = Erc8004Identity.load()
        if ident and ident.get("address"):
            return str(ident["address"]).strip()
    except Exception:
        pass
    return ""


def wallet_holdings(
    settings: Settings,
    wallet: str,
    symbols: list[str],
    client: httpx.Client | None = None,
) -> dict[str, Holding]:
    """Read on-chain holdings for ``symbols`` (+ native BNB) for one wallet.

    Uses the first responsive BSC RPC. Unknown-address symbols are returned with
    ``ok=False`` so the caller can fall back to the booked mark for them.
    """
    out: dict[str, Holding] = {}
    if not wallet:
        return out
    url = rpc.first_live(settings)
    own = client is None
    client = client or httpx.Client()
    try:
        if url is None:
            return {s: Holding(s, note="no live RPC") for s in symbols}
        # Native BNB (gas leg).
        wei = native_balance(url, wallet, client)
        if wei is not None:
            out["BNB"] = Holding("BNB", units=wei / 1e18, raw=wei, decimals=18,
                                 address="native", ok=True)
        for sym in symbols:
            meta = token_meta(settings, sym)
            if meta is None:
                out[sym] = Holding(sym, note="no known address (configure execution.token_addresses)")
                continue
            addr, dec_hint = meta
            dec = erc20_decimals(url, addr, client)
            if dec is None:
                dec = dec_hint if dec_hint is not None else 18
            raw = erc20_balance_of(url, addr, wallet, client)
            if raw is None:
                out[sym] = Holding(sym, address=addr, decimals=dec, note="balance read failed")
                continue
            out[sym] = Holding(sym, units=raw / (10 ** dec), raw=raw, decimals=dec,
                               address=addr, ok=True)
        return out
    finally:
        if own:
            client.close()
