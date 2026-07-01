"""OMEGA execution adapter: CEX spot + perps via ccxt.

This is the post-contest execution path that frees HELM from the BSC /
PancakeSwap spot-only constraint. Through ``ccxt`` it can reach thousands of
pairs across major venues (Binance / Bybit / OKX ...) for both spot and
USDT-margined perpetuals, with optional leverage that is ALWAYS clamped to
``ExecutionCfg.max_leverage``.

Safety posture (paper-first):
  * Defaults to the exchange TESTNET / sandbox — no real money moves until
    ``execution.testnet`` is explicitly turned off.
  * API credentials are read from the environment only (``Secrets``) and are
    NEVER logged; any exchange error surfaced into ``Fill.note`` is scrubbed of
    the key / secret first.
  * Use trade-only API keys with NO withdrawal permission, IP-allowlisted.
  * Leverage is OFF by default and hard-capped; spot is the default market type.

The adapter accepts an injected ``client`` so it is fully unit-testable with a
fake exchange (no network, no ccxt install required for the core test suite).
"""

from __future__ import annotations

import time

from ..config import Settings
from .base import ExecutionAdapter, Fill, Order, _now_iso

# Currencies treated as ~1 USD when valuing fees.
_USD_LIKE = {"USDT", "USD", "USDC", "BUSD", "FDUSD", "TUSD", "DAI"}


class CcxtAdapter(ExecutionAdapter):
    name = "ccxt"

    def __init__(self, settings: Settings, client: object | None = None):
        self.s = settings
        ex = settings.execution
        self.exchange_id = (ex.exchange or "binance").strip().lower()
        self.market_type = (ex.market_type or "spot").strip().lower()
        self.quote = (ex.quote_currency or "USDT").strip().upper()
        self.testnet = bool(ex.testnet)
        self.leverage_enabled = bool(ex.leverage_enabled)
        self.max_leverage = max(1.0, float(ex.max_leverage or 1.0))
        self._secrets = settings.secrets
        self._client = client  # injectable for tests
        self._markets_loaded = False
        self._init_error = ""
        if self._client is None:
            self._client = self._build_client()

    # ------------------------------------------------------------ build client
    def _build_client(self):
        try:
            import ccxt  # lazy: keeps the core paper runtime dependency-light
        except Exception as e:  # pragma: no cover - import guard
            self._init_error = f"ccxt not installed: {type(e).__name__}"
            return None
        try:
            klass = getattr(ccxt, self.exchange_id)
        except AttributeError:
            self._init_error = f"unknown exchange '{self.exchange_id}'"
            return None
        opts: dict = {
            "enableRateLimit": True,
            "options": {
                "defaultType": "swap" if self.market_type == "swap" else "spot",
            },
        }
        if self._secrets.ccxt_api_key:
            opts["apiKey"] = self._secrets.ccxt_api_key
        if self._secrets.ccxt_secret:
            opts["secret"] = self._secrets.ccxt_secret
        if self._secrets.ccxt_password:
            opts["password"] = self._secrets.ccxt_password
        try:
            client = klass(opts)
        except Exception as e:  # pragma: no cover - construction guard
            self._init_error = f"ccxt init: {self._sanitize(str(e))}"
            return None
        if self.testnet:
            try:
                client.set_sandbox_mode(True)
            except Exception as e:
                self._init_error = f"sandbox unavailable: {self._sanitize(str(e))}"
        return client

    @property
    def available(self) -> bool:
        """True only when a client exists AND trade credentials are present."""
        if self._client is None:
            return False
        return bool(self._secrets.ccxt_api_key and self._secrets.ccxt_secret)

    def supports_live(self) -> bool:
        return True

    # -------------------------------------------------------------- symbol map
    def market_symbol(self, symbol: str) -> str:
        base = symbol.upper()
        if base.endswith("/" + self.quote) or ":" in base:
            return base
        if self.market_type == "swap":
            return f"{base}/{self.quote}:{self.quote}"
        return f"{base}/{self.quote}"

    # ----------------------------------------------------------------- secrets
    def _sanitize(self, msg: str) -> str:
        """Strip any credential material an exchange error might echo back."""
        out = msg or ""
        for sv in (
            self._secrets.ccxt_api_key,
            self._secrets.ccxt_secret,
            self._secrets.ccxt_password,
        ):
            if sv and sv in out:
                out = out.replace(sv, "***")
        return out[:160]

    # ---------------------------------------------------------------- leverage
    def _apply_leverage(self, market: str, order_leverage: float = 0.0) -> float:
        """Set venue leverage for perps, clamped to the hard ceiling AND to any
        per-trade cap the caller derived from the stop distance (so the stop
        always sits inside the liquidation price). Returns the leverage actually
        requested (1.0 for spot or when disabled)."""
        if self.market_type != "swap" or not self.leverage_enabled:
            return 1.0
        lev = self.max_leverage
        if order_leverage and order_leverage > 0:
            lev = min(lev, float(order_leverage))
        lev = max(1.0, min(self.max_leverage, lev))
        try:
            self._client.set_leverage(lev, market)
        except Exception:
            pass  # venue may reject / no-op; sizing already assumes <= cap
        return lev

    # -------------------------------------------------------- universe discovery
    def perp_ticker_rows(self) -> list[dict]:
        """Synthetic 24h-ticker rows for every active linear ``{quote}`` perp the
        venue lists, shaped as ``{"symbol": base+quote, "quoteVolume": usd}`` so
        the caller can feed them straight into ``rank_full_market``. This binds
        the tradeable universe to the EXACT venue that will route the orders, so
        the engine can never top-rank a name that isn't actually listed as a
        USDT-margined perp here. Returns an empty list on any failure (the caller
        falls back to the curated book — the agent is never left blind). Only
        meaningful for ``market_type == 'swap'``.
        """
        if self._client is None or self.market_type != "swap":
            return []
        try:
            if not self._markets_loaded:
                self._client.load_markets()
                self._markets_loaded = True
            markets = getattr(self._client, "markets", None) or {}
            tickers = self._client.fetch_tickers()
        except Exception:
            return []
        q = self.quote
        rows: list[dict] = []
        for sym, m in markets.items():
            try:
                if not isinstance(m, dict):
                    continue
                if not (m.get("swap") and m.get("linear")):
                    continue
                if m.get("active") is False:
                    continue
                if str(m.get("quote", "")).upper() != q:
                    continue
                settle = str(m.get("settle", q) or q).upper()
                if settle != q:
                    continue
                base = str(m.get("base", "")).upper()
                if not base:
                    continue
                t = tickers.get(sym) or {}
                vol = t.get("quoteVolume")
                if not vol:
                    bv = t.get("baseVolume")
                    last = t.get("last") or t.get("close")
                    try:
                        vol = float(bv) * float(last) if (bv and last) else 0.0
                    except (TypeError, ValueError):
                        vol = 0.0
                rows.append({"symbol": f"{base}{q}", "quoteVolume": float(vol or 0.0)})
            except Exception:
                continue
        return rows

    # ----------------------------------------------------------------- execute
    def execute(self, order: Order) -> Fill:
        def fail(note: str) -> Fill:
            return Fill(
                order.symbol, order.side, 0.0, 0.0, 0.0, 0.0, 0.0,
                _now_iso(), self.name, False, note,
            )

        if self._client is None:
            return fail(self._init_error or "ccxt client unavailable")
        if not self.available:
            return fail("ccxt credentials missing")
        if order.ref_price <= 0:
            return fail("no price")

        market = self.market_symbol(order.symbol)
        try:
            if not self._markets_loaded:
                self._client.load_markets()
                self._markets_loaded = True
        except Exception as e:
            return fail(f"load_markets: {self._sanitize(str(e))}")

        # Leverage (perps only) — always clamped to the hard ceiling.
        self._apply_leverage(market, order.leverage)

        # Size: prefer an explicit base qty when present (closes, shorts, spot
        # sells); otherwise derive it from notional/ref (a long OPEN sized in USD).
        # This single rule is correct for all four open/close legs:
        #   long open  = buy,  qty=0  -> notional/ref
        #   long close = sell, qty>0  -> qty   (reduce_only)
        #   short open = sell, qty>0  -> qty
        #   short close= buy,  qty>0  -> qty   (reduce_only)
        amount = order.qty if order.qty > 0 else (order.notional_usd / order.ref_price)
        if amount <= 0:
            return fail("zero amount")

        # Round to the venue's lot-size precision so a REAL exchange accepts the
        # order (Bybit/Binance reject "too many decimals"). The fake test client
        # has no amount_to_precision -> we keep the raw amount untouched.
        if hasattr(self._client, "amount_to_precision"):
            try:
                amount = float(self._client.amount_to_precision(market, amount))
            except Exception:
                pass
            if amount <= 0:
                return fail("amount below venue lot size")

        # reduceOnly on perps guarantees a close can never accidentally flip into
        # a fresh opposite position if the book and the venue disagree on size.
        params: dict = {}
        if order.reduce_only and self.market_type == "swap":
            params["reduceOnly"] = True

        # Exchange-native SL/TP on OPEN perps (never on closes). Bybit will
        # enforce these even if this agent goes down — a real safety net, and
        # they show up in the Bybit UI (users kept seeing empty "no protection"
        # positions because we only tracked stops in-memory). reduce_only orders
        # skip attachment (they are the close itself).
        if (not order.reduce_only) and self.market_type == "swap":
            if order.stop_price and order.stop_price > 0:
                params["stopLoss"] = self._client.price_to_precision(
                    market, order.stop_price
                ) if hasattr(self._client, "price_to_precision") else order.stop_price
            if order.take_profit_price and order.take_profit_price > 0:
                params["takeProfit"] = self._client.price_to_precision(
                    market, order.take_profit_price
                ) if hasattr(self._client, "price_to_precision") else order.take_profit_price

        try:
            res = self._client.create_order(
                market, "market", order.side, amount, params=params
            )
        except Exception as e:
            return fail(self._sanitize(str(e)))

        # Market orders on some venues (notably Bybit) ACK before the fill is
        # reflected in the create response (filled=0). Re-fetch so the booked
        # Fill carries the REAL settled qty/price — otherwise the agent treats a
        # filled live order as 'unfilled' and its book desyncs from the venue.
        res = self._settle_order(res, market)
        return self._fill_from_order(order, res)

    # ------------------------------------------------------------------ settle
    def _settle_order(self, res: dict, market: str) -> dict:
        """Re-fetch an order until its fill is reported (handles async market
        fills). Returns the freshest order dict; degrades to the original on any
        failure so a successful trade is never lost to a transient read error."""
        res = res or {}
        if float(res.get("filled") or 0.0) > 0 and (res.get("average") or res.get("price")):
            return res
        oid = res.get("id") or (res.get("info") or {}).get("orderId")
        if not oid:
            return res
        last = res
        for _ in range(6):
            time.sleep(0.35)
            o = self._fetch_settled(oid, market)
            if o:
                last = o
                if float(o.get("filled") or 0.0) > 0:
                    return o
        return last

    def _fetch_settled(self, oid: str, market: str) -> dict | None:
        """Read a placed order's settled state. Bybit's ``fetch_order`` requires
        ``params.acknowledged=True`` (else it raises); fall back to scanning the
        recent closed orders by id for venues where ``fetch_order`` is unavailable
        or restricted."""
        if hasattr(self._client, "fetch_order"):
            try:
                return self._client.fetch_order(oid, market, {"acknowledged": True})
            except Exception:
                pass
        if hasattr(self._client, "fetch_closed_orders"):
            try:
                rows = self._client.fetch_closed_orders(market, None, 20) or []
                for o in reversed(rows):
                    if str(o.get("id")) == str(oid):
                        return o
            except Exception:
                pass
        return None

    # ------------------------------------------------------------------- parse
    def _fill_from_order(self, order: Order, res: dict) -> Fill:
        res = res or {}
        price = float(res.get("average") or res.get("price") or order.ref_price or 0.0)
        qty = float(res.get("filled") or res.get("amount") or 0.0)
        cost = res.get("cost")
        notional = float(cost) if cost not in (None, 0, 0.0) else qty * price
        fee_usd = self._fee_usd(res, price)
        slip = self._slippage_bps(order.side, order.ref_price, price)
        ok = qty > 0 and price > 0
        return Fill(
            symbol=order.symbol,
            side=order.side,
            qty=qty,
            price=price,
            notional_usd=notional,
            fee_usd=fee_usd,
            slippage_bps=slip,
            ts=_now_iso(),
            source=self.name,
            ok=ok,
            note="" if ok else "unfilled",
            gas_usd=0.0,
        )

    def _fee_usd(self, res: dict, price: float) -> float:
        fees = res.get("fees")
        if not fees and res.get("fee"):
            fees = [res["fee"]]
        total = 0.0
        for f in fees or []:
            if not f:
                continue
            c = float(f.get("cost") or 0.0)
            cur = (f.get("currency") or "").upper()
            if cur and cur not in _USD_LIKE:
                c *= price  # fee charged in base units -> USD
            total += c
        return total

    @staticmethod
    def _slippage_bps(side: str, ref: float, fill: float) -> float:
        if ref <= 0 or fill <= 0:
            return 0.0
        adverse = (fill - ref) if side == "buy" else (ref - fill)
        return max(0.0, adverse / ref * 10_000.0)
