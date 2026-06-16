"""CoinMarketCap Agent Hub client.

Three access paths, in order of "agent-nativeness":

1. ``mcp_call``  — CMC Agent Hub over MCP (https://mcp.coinmarketcap.com/mcp).
   The agent-native surface: pre-computed signals (RSI/MACD/EMA), derivatives,
   technicals, narratives. Header ``X-CMC-MCP-API-KEY``.
2. ``x402_get`` — pay-per-call data via TWAK (USDT on BSC, no API key). Wired in
   the live trade loop right before buys (see execution.twak). Payment is auth.
3. ``rest_*``   — classic Pro REST with ``X-CMC_PRO_API_KEY`` (quotes, F&G, global).

Every method degrades to ``None`` on missing key / error so the MarketData facade
falls back to the public layer. Nothing here ever raises into the agent loop.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

import httpx

MCP_URL = "https://mcp.coinmarketcap.com/mcp"
REST_BASE = "https://pro-api.coinmarketcap.com"
X402_QUOTES = f"{REST_BASE}/x402/v3/cryptocurrency/quotes/latest"
X402_DEX_SEARCH = f"{REST_BASE}/x402/v1/dex/search"

_TIMEOUT = httpx.Timeout(15.0, connect=6.0)


@dataclass
class CMCResult:
    ok: bool
    data: Any = None
    source: str = "cmc"
    note: str = ""


class CMCClient:
    """Thin, fail-soft CoinMarketCap Agent Hub client."""

    def __init__(self, api_key: str = "", client: httpx.Client | None = None):
        self.api_key = api_key or ""
        self._client = client or httpx.Client(timeout=_TIMEOUT)
        self._mcp_id = 0

    @property
    def has_key(self) -> bool:
        return bool(self.api_key)

    def close(self) -> None:
        try:
            self._client.close()
        except Exception:
            pass

    # ----------------------------------------------------------------- REST
    def _rest(self, path: str, params: dict[str, Any]) -> CMCResult:
        if not self.has_key:
            return CMCResult(False, source="cmc_rest", note="no api key")
        try:
            r = self._client.get(
                f"{REST_BASE}{path}",
                params=params,
                headers={"X-CMC_PRO_API_KEY": self.api_key, "Accept": "application/json"},
            )
            r.raise_for_status()
            return CMCResult(True, r.json(), "cmc_rest")
        except Exception as e:
            return CMCResult(False, source="cmc_rest", note=f"{type(e).__name__}: {e}")

    def rest_quotes(self, symbols: list[str]) -> CMCResult:
        return self._rest(
            "/v2/cryptocurrency/quotes/latest",
            {"symbol": ",".join(symbols), "convert": "USD"},
        )

    def rest_fear_greed(self) -> CMCResult:
        return self._rest("/v3/fear-and-greed/latest", {})

    def rest_global(self) -> CMCResult:
        return self._rest("/v1/global-metrics/quotes/latest", {"convert": "USD"})

    # ------------------------------------------------------------------ MCP
    def mcp_call(self, tool: str, arguments: dict[str, Any]) -> CMCResult:
        """Call an Agent Hub tool over MCP (JSON-RPC, streamable HTTP).

        Best-effort: handles both plain-JSON and SSE (``data:`` line) responses.
        """
        if not self.has_key:
            return CMCResult(False, source="cmc_mcp", note="no api key")
        self._mcp_id += 1
        payload = {
            "jsonrpc": "2.0",
            "id": self._mcp_id,
            "method": "tools/call",
            "params": {"name": tool, "arguments": arguments},
        }
        try:
            r = self._client.post(
                MCP_URL,
                json=payload,
                headers={
                    "X-CMC-MCP-API-KEY": self.api_key,
                    "Content-Type": "application/json",
                    "Accept": "application/json, text/event-stream",
                },
            )
            r.raise_for_status()
            parsed = self._parse_mcp(r.text)
            if parsed is None:
                return CMCResult(False, source="cmc_mcp", note="unparseable MCP response")
            return CMCResult(True, parsed, "cmc_mcp")
        except Exception as e:
            return CMCResult(False, source="cmc_mcp", note=f"{type(e).__name__}: {e}")

    @staticmethod
    def _parse_mcp(text: str) -> Any | None:
        """Extract the JSON-RPC result payload from JSON or SSE text."""
        # SSE: lines like "data: {...}". Take the last data line with a result.
        candidate = text.strip()
        if "data:" in candidate and not candidate.startswith("{"):
            blocks = [
                ln[len("data:"):].strip()
                for ln in candidate.splitlines()
                if ln.strip().startswith("data:")
            ]
            for blk in reversed(blocks):
                try:
                    obj = json.loads(blk)
                    return CMCClient._unwrap(obj)
                except Exception:
                    continue
            return None
        try:
            return CMCClient._unwrap(json.loads(candidate))
        except Exception:
            return None

    @staticmethod
    def _unwrap(obj: dict[str, Any]) -> Any:
        """Pull structured content out of an MCP tools/call result envelope."""
        result = obj.get("result", obj)
        content = result.get("content") if isinstance(result, dict) else None
        if isinstance(content, list):
            for part in content:
                if isinstance(part, dict) and part.get("type") == "text":
                    txt = part.get("text", "")
                    try:
                        return json.loads(txt)
                    except Exception:
                        return txt
        return result

    # ----------------------------------------------------------------- x402
    def x402_url_for(self, kind: str, **params: Any) -> str:
        """Build a CMC x402 endpoint URL (payment handled by TWAK adapter)."""
        if kind == "quotes":
            sym = params.get("symbol", "")
            return f"{X402_QUOTES}?symbol={sym}"
        if kind == "dex_search":
            q = params.get("query", "")
            return f"{X402_DEX_SEARCH}?query={q}"
        raise ValueError(f"unknown x402 kind: {kind}")

    @staticmethod
    def parse_quote_price(data: Any, symbol: str) -> float | None:
        """Best-effort USD price for ``symbol`` from a quotes payload.

        Handles the v2 (dict-per-symbol) and v3 (list-per-symbol) shapes and the
        optional top-level ``data`` envelope, with a case-insensitive symbol
        match. Returns ``None`` on anything unexpected so the caller falls back
        to its own reference price — this never raises into the trade loop.
        """
        try:
            root = data.get("data", data) if isinstance(data, dict) else data
            if not isinstance(root, dict):
                return None
            entry = root.get(symbol)
            if entry is None:
                for k, v in root.items():
                    if str(k).upper() == symbol.upper():
                        entry = v
                        break
            if isinstance(entry, list):
                entry = entry[0] if entry else None
            if not isinstance(entry, dict):
                return None
            usd = entry.get("quote", {}).get("USD", {})
            price = usd.get("price") if isinstance(usd, dict) else None
            if price is None:
                return None
            price = float(price)
            return price if price > 0 else None
        except Exception:
            return None
