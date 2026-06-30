#!/usr/bin/env python3
"""Bybit (or any ccxt venue) TESTNET smoke test for HELM's long/short perps path.

Proves the execution engine end-to-end against the venue's SANDBOX before any
real money moves. It drives the SAME ``CcxtAdapter`` the live agent uses, so a
pass here means the agent will route identically when it goes live.

Everything is read from the environment (NEVER hard-code keys):

  HELM_EXCHANGE      ccxt venue id              (default: bybit)
  HELM_MARKET_TYPE   spot | swap                (default: swap)
  HELM_QUOTE_CCY     quote currency             (default: USDT)
  HELM_TESTNET       1 = sandbox (keep this on) (default: 1)
  HELM_CCXT_API_KEY  TESTNET trade-only key (NO withdrawal, IP-allowlisted)
  HELM_CCXT_SECRET   TESTNET secret
  HELM_SMOKE_SYMBOL  base asset to test         (default: BTC)
  HELM_SMOKE_USD     notional per test leg, USD (default: 12)

Usage:
  # read-only checks (creds + market discovery + price) — never trades:
  python scripts/testnet_smoke.py

  # ALSO place + close a tiny TESTNET long and short (proves all four legs):
  python scripts/testnet_smoke.py --trade

SAFETY: refuses to place any order unless HELM_TESTNET is truthy. This is a
validation harness — safe to delete; it is NOT part of the agent runtime.
"""
from __future__ import annotations

import os
import sys

# Allow running as `python scripts/testnet_smoke.py` from the repo root.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from helm.config import load_settings
from helm.execution.base import Order
from helm.execution.ccxt_adapter import CcxtAdapter


def _truthy(v: str | None) -> bool:
    return str(v or "").strip().lower() in {"1", "true", "yes", "on"}


def _line(label: str, ok: bool, detail: str = "") -> bool:
    mark = "PASS" if ok else "FAIL"
    print(f"  [{mark}] {label}{(' — ' + detail) if detail else ''}")
    return ok


def _show_fill(tag: str, fill) -> bool:
    ok = bool(fill.ok and fill.qty > 0)
    detail = (
        f"qty={fill.qty:.8f} px={fill.price:.4f} "
        f"notional=${fill.notional_usd:.2f} fee=${fill.fee_usd:.4f} "
        f"slip={fill.slippage_bps:.1f}bps"
    )
    if not ok and fill.note:
        detail += f" note={fill.note}"
    return _line(tag, ok, detail)


def main() -> int:
    do_trade = "--trade" in sys.argv[1:]

    # Env overrides (HELM_EXCHANGE / HELM_MARKET_TYPE / HELM_TESTNET / keys ...)
    # are applied inside load_settings(); force the ccxt adapter for this harness.
    settings = load_settings()
    settings.execution.adapter = "ccxt"
    ex = settings.execution

    base = (os.getenv("HELM_SMOKE_SYMBOL") or "BTC").strip().upper()
    try:
        usd = float(os.getenv("HELM_SMOKE_USD") or "12")
    except ValueError:
        usd = 12.0

    print("HELM testnet smoke test")
    print(
        f"  venue={ex.exchange} market={ex.market_type} quote={ex.quote_currency} "
        f"testnet={ex.testnet} long_short={ex.long_short_enabled} "
        f"leverage={'on' if ex.leverage_enabled else 'off'}(<= {ex.max_leverage:g}x)"
    )
    print(f"  symbol={base} notional_per_leg=${usd:.2f} trade={do_trade}")
    print("-" * 64)

    adapter = CcxtAdapter(settings)
    all_ok = True

    # 1) Client constructed (ccxt installed + known exchange).
    if not _line(
        "ccxt client built",
        adapter._client is not None,
        adapter._init_error or "ok",
    ):
        print("\n  -> install ccxt:  pip install 'ccxt>=4.3'")
        return 1

    # 2) Trade credentials present (key + secret).
    if not _line("trade credentials present", adapter.available,
                 "set HELM_CCXT_API_KEY + HELM_CCXT_SECRET"):
        all_ok = False

    # 3) Market discovery from the live venue (binds the universe to execution).
    market = adapter.market_symbol(base)
    rows = adapter.perp_ticker_rows() if ex.market_type == "swap" else []
    if ex.market_type == "swap":
        all_ok &= _line("perp universe discovered",
                        len(rows) > 0, f"{len(rows)} active linear {ex.quote_currency} perps")

    # 4) Live price for the test symbol.
    px = 0.0
    try:
        if not adapter._markets_loaded:
            adapter._client.load_markets()
            adapter._markets_loaded = True
        t = adapter._client.fetch_ticker(market)
        px = float(t.get("last") or t.get("close") or 0.0)
    except Exception as e:  # pragma: no cover - network/venue dependent
        all_ok &= _line("price fetch", False, adapter._sanitize(str(e)))
    if px > 0:
        _line("price fetch", True, f"{market} = {px:.4f}")
    else:
        all_ok &= _line("price fetch", False, f"no price for {market}")

    if not do_trade:
        print("-" * 64)
        print("read-only checks done. re-run with --trade to place TESTNET orders.")
        return 0 if all_ok else 1

    # --- live TESTNET order legs (sandbox only) -------------------------------
    if not ex.testnet:
        print("-" * 64)
        print("REFUSING to trade: HELM_TESTNET is not set. Keep the harness on the "
              "sandbox (export HELM_TESTNET=1).")
        return 1
    if not adapter.available or px <= 0:
        print("-" * 64)
        print("cannot trade: missing credentials or price. fix the FAILs above.")
        return 1

    qty = usd / px
    print("-" * 64)
    print("placing tiny TESTNET orders (open then immediately close each side)...")

    # Long: buy to open (notional-sized), then sell reduce-only to close.
    f_lo = adapter.execute(Order(base, "buy", ref_price=px, notional_usd=usd))
    all_ok &= _show_fill("long  open  (buy)", f_lo)
    if f_lo.ok and f_lo.qty > 0:
        f_lc = adapter.execute(
            Order(base, "sell", ref_price=px, qty=f_lo.qty, reduce_only=True)
        )
        all_ok &= _show_fill("long  close (sell, reduceOnly)", f_lc)

    # Short: sell to open (qty-sized), then buy reduce-only to close.
    if ex.market_type == "swap" and ex.long_short_enabled:
        f_so = adapter.execute(Order(base, "sell", ref_price=px, qty=qty))
        all_ok &= _show_fill("short open  (sell)", f_so)
        if f_so.ok and f_so.qty > 0:
            f_sc = adapter.execute(
                Order(base, "buy", ref_price=px, qty=f_so.qty, reduce_only=True)
            )
            all_ok &= _show_fill("short close (buy, reduceOnly)", f_sc)
    else:
        _line("short legs", True, "skipped (enable HELM_LONG_SHORT=1 + swap)")

    print("-" * 64)
    print("SMOKE TEST PASSED" if all_ok else "SMOKE TEST HAD FAILURES (see above)")
    return 0 if all_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
