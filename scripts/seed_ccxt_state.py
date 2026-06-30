#!/usr/bin/env python3
"""Seed a FRESH CEX (ccxt) book for HELM go-live.

The deployed Railway volume still holds the OLD on-chain (twak) book — FET/USDT
positions that don't exist on Bybit. When HELM switches to a CEX it tracks its
own fills from a seeded starting cash (it does NOT auto-read CEX balances), so
the book must start clean and equal to your actual exchange balance.

Usage:
  # write a fresh state file (cash = initial_equity = your Bybit USDT balance):
  python scripts/seed_ccxt_state.py 88.40

  # also print the base64 to paste into Railway as HELM_STATE_B64:
  python scripts/seed_ccxt_state.py 88.40 --b64

Then on Railway (one-time clean reseed of the persistent volume):
  HELM_STATE_B64=<printed value>
  HELM_STATE_FORCE_RESEED=1     # fires once, guarded by a volume sentinel
"""
from __future__ import annotations

import base64
import json
import os
import sys
from dataclasses import asdict

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from helm.portfolio import Portfolio


def build_state(balance: float) -> dict:
    p = Portfolio.new(float(balance))
    return {
        "initial_equity": p.initial_equity,
        "cash": p.cash,
        "peak_equity": p.peak_equity,
        "day_start_equity": p.day_start_equity,
        "day": p._day.isoformat() if p._day else None,
        "realized_pnl": p.realized_pnl,
        "fees_paid": p.fees_paid,
        "trades_today": p.trades_today,
        "total_trades": p.total_trades,
        "positions": {s: asdict(pos) for s, pos in p.positions.items()},
        "swing_armed": p.swing_armed,
        "swing_sell_px": p.swing_sell_px,
        "swing_token": p.swing_token,
        "swing_flat": p.swing_flat,
        "harvest_anchor_px": p.harvest_anchor_px,
        "harvest_peak_px": p.harvest_peak_px,
    }


def main() -> int:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    flags = {a for a in sys.argv[1:] if a.startswith("--")}
    if not args:
        print(__doc__)
        return 2
    try:
        balance = float(args[0])
    except ValueError:
        print(f"error: '{args[0]}' is not a number (USDT balance)")
        return 2
    if balance <= 0:
        print("error: balance must be > 0")
        return 2

    out = "data/runtime/state.json"
    if "--out" in sys.argv:
        out = sys.argv[sys.argv.index("--out") + 1]

    doc = build_state(balance)
    blob = json.dumps(doc, indent=2, default=str)

    os.makedirs(os.path.dirname(out), exist_ok=True)
    with open(out, "w") as f:
        f.write(blob)
    print(f"wrote fresh CEX book -> {out}")
    print(f"  initial_equity = cash = ${balance:.2f}  |  positions: none")

    if "--b64" in flags:
        b64 = base64.b64encode(blob.encode()).decode()
        print("\nHELM_STATE_B64 (paste into Railway, with HELM_STATE_FORCE_RESEED=1):")
        print(b64)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
