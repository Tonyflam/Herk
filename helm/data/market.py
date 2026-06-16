"""MarketData facade: caching + provider priority + honest provenance.

Wraps the public fetchers and the CMC Agent Hub client behind one interface the
rest of HELM uses. Resolution order follows ``settings.data.provider_priority``;
whatever path actually served a datum is recorded so the dashboard can show it.

Design rule: this facade NEVER raises into the agent loop. A data miss returns a
clearly-flagged empty/neutral value, and the strategy treats stale/missing data
as a reason to de-risk — not to guess.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

import httpx

from ..config import Settings
from .cmc import CMCClient
from .sources import (
    Candles,
    Quote,
    fetch_btc_dominance,
    fetch_fear_greed,
    fetch_klines,
    fetch_quote,
)


@dataclass
class Regime:
    fear_greed: int = 50
    fg_class: str = "Neutral"
    btc_dominance: float = 50.0
    funding_annual: float | None = None
    sources: dict[str, str] = field(default_factory=dict)


class MarketData:
    """Cached, provenance-tagged market data for the strategy + dashboard."""

    def __init__(self, settings: Settings):
        self.settings = settings
        self.ttl = max(1, int(settings.data.cache_ttl_sec))
        self._client = httpx.Client()
        self._cmc = CMCClient(settings.secrets.cmc_api_key, client=self._client)
        self._cache: dict[str, tuple[float, Any]] = {}

    # ----------------------------------------------------------- cache utils
    def _get_cached(self, key: str) -> Any | None:
        hit = self._cache.get(key)
        if hit is None:
            return None
        ts, val = hit
        if (time.monotonic() - ts) <= self.ttl:
            return val
        return None

    def _put_cached(self, key: str, val: Any) -> None:
        self._cache[key] = (time.monotonic(), val)

    # ---------------------------------------------------------------- prices
    def get_candles(self, symbol: str, interval: str = "1h", limit: int = 200) -> Candles:
        key = f"klines:{symbol}:{interval}:{limit}"
        cached = self._get_cached(key)
        if cached is not None:
            return cached
        candles = fetch_klines(symbol, interval, limit, client=self._client)
        if len(candles):  # only cache good pulls
            self._put_cached(key, candles)
        return candles

    def get_quote(self, symbol: str) -> Quote:
        key = f"quote:{symbol}"
        cached = self._get_cached(key)
        if cached is not None:
            return cached
        quote = fetch_quote(symbol, client=self._client)
        if quote.provenance.ok:
            self._put_cached(key, quote)
        return quote

    def get_quotes(self, symbols: list[str]) -> dict[str, Quote]:
        return {s: self.get_quote(s) for s in symbols}

    # ---------------------------------------------------------------- regime
    def get_regime(self) -> Regime:
        key = "regime"
        cached = self._get_cached(key)
        if cached is not None:
            return cached

        reg = Regime()

        # --- Fear & Greed: prefer CMC (key), else public alternative.me -----
        fg_done = False
        if self._cmc.has_key:
            res = self._cmc.rest_fear_greed()
            if res.ok and isinstance(res.data, dict):
                d = res.data.get("data", res.data)
                try:
                    reg.fear_greed = int(float(d["value"]))
                    reg.fg_class = str(d.get("value_classification", reg.fg_class))
                    reg.sources["fear_greed"] = "cmc_rest"
                    fg_done = True
                except Exception:
                    fg_done = False
        if not fg_done:
            val, cls, prov = fetch_fear_greed(client=self._client)
            reg.fear_greed, reg.fg_class = val, cls
            reg.sources["fear_greed"] = prov.source + ("" if prov.ok else ":stale")

        # --- BTC dominance: prefer CMC global, else public CoinGecko --------
        dom_done = False
        if self._cmc.has_key:
            res = self._cmc.rest_global()
            if res.ok and isinstance(res.data, dict):
                d = res.data.get("data", res.data)
                try:
                    reg.btc_dominance = float(d["btc_dominance"])
                    reg.sources["btc_dominance"] = "cmc_rest"
                    dom_done = True
                except Exception:
                    dom_done = False
        if not dom_done:
            dom, prov = fetch_btc_dominance(client=self._client)
            reg.btc_dominance = dom
            reg.sources["btc_dominance"] = prov.source + ("" if prov.ok else ":stale")

        # --- Derivatives funding: agent-native MCP only (optional) ----------
        if self.settings.regime.use_derivatives_funding and self._cmc.has_key:
            funding = self._funding_via_mcp()
            if funding is not None:
                reg.funding_annual = funding
                reg.sources["funding"] = "cmc_mcp"

        self._put_cached(key, reg)
        return reg

    def _funding_via_mcp(self) -> float | None:
        """Best-effort average funding (annualized) from CMC derivatives MCP."""
        res = self._cmc.mcp_call("get_global_crypto_derivatives_metrics", {})
        if not res.ok:
            return None
        data = res.data
        # Defensive: the payload shape may evolve; pull any funding-rate field.
        try:
            if isinstance(data, dict):
                for k in ("funding_rate", "avg_funding_rate", "funding_rate_avg"):
                    if k in data:
                        return float(data[k])
                inner = data.get("data")
                if isinstance(inner, dict):
                    for k in ("funding_rate", "avg_funding_rate", "funding_rate_avg"):
                        if k in inner:
                            return float(inner[k])
        except Exception:
            return None
        return None

    # ----------------------------------------------------------------- close
    def close(self) -> None:
        self._cmc.close()
        try:
            self._client.close()
        except Exception:
            pass
