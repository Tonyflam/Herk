"""Regime-overlay A/B harness — validates HELM's live de-risking lever.

The walk-forward backtest historically ran with the regime overlay *neutral*
because there is no historical Fear & Greed feed through our fetchers. That left
the live de-risking overlay unvalidated: the failure mode is an overlay that
de-risks into a V-shaped recovery — you "survive" but lose the return race.

This harness closes that gap. For each regime window it loads the candles ONCE,
then replays the identical data three ways:
  * OFF      — overlay neutral (the published behavior)
  * ON       — proxy Fear & Greed overlay, ungated (raw de-risking)
  * ON+gate  — proxy overlay, survival-gated (de-risk scaled by drawdown budget)

It tabulates the difference so the overlay can be validated and tuned. The gate
exists because the raw overlay bleeds return in clean uptrends (de-risks into a
V-recovery) while helping in chop; gating by drawdown budget keeps the book
deployed through fear when survival margin is ample.

Run:  python -m backtest.regime_ab
      python -m backtest.regime_ab --days 30 --stride 6
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone

from helm.config import REPO_ROOT, load_settings
from helm.universe import tradeable_universe

from .walk_forward import BacktestResult, load_history, run

# (label, end-date) — None end means the latest available window.
WINDOWS: list[tuple[str, str | None]] = [
    ("bull  (Q4-2024)", "2024-12-15"),
    ("chop  (Aug-Sep)", "2024-09-10"),
    ("bear  (latest) ", None),
]


def _end_ms(end: str | None) -> int | None:
    if not end:
        return None
    return int(datetime.fromisoformat(end).replace(tzinfo=timezone.utc).timestamp() * 1000)


def _row(label: str, off: BacktestResult, on: BacktestResult, gated: BacktestResult) -> dict:
    return {
        "window": label.strip(),
        "off_return_pct": round(off.total_return_pct, 2),
        "on_return_pct": round(on.total_return_pct, 2),
        "gated_return_pct": round(gated.total_return_pct, 2),
        "gated_vs_off_pct": round(gated.total_return_pct - off.total_return_pct, 2),
        "off_max_dd_pct": round(off.max_drawdown_pct, 2),
        "on_max_dd_pct": round(on.max_drawdown_pct, 2),
        "gated_max_dd_pct": round(gated.max_drawdown_pct, 2),
        "btc_pct": round(off.btc_buyhold_pct, 2),
        "off_breached": off.breached_halt,
        "on_breached": on.breached_halt,
        "gated_breached": gated.breached_halt,
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="HELM regime-overlay A/B validation")
    ap.add_argument("--days", type=int, default=33)
    ap.add_argument("--top", type=int, default=3)
    ap.add_argument("--warmup", type=int, default=200)
    ap.add_argument("--stride", type=int, default=6)
    args = ap.parse_args(argv)

    settings = load_settings()
    bars = min(1000, args.warmup + args.days * 24)
    symbols = list(tradeable_universe(
        use_curated=settings.universe.use_curated_tradeable,
        extra=tuple(settings.universe.extra_tradeable),
        exclude=tuple(settings.universe.exclude),
    ))

    rows: list[dict] = []
    for label, end in WINDOWS:
        end_ms = _end_ms(end)
        print(f"\n=== {label.strip()} — loading {len(symbols)} symbols × {bars} bars ===")
        # Load ONCE so all arms see byte-identical data (valid A/B).
        history, btc = load_history(symbols, bars, end_ms=end_ms)
        print(f"    loaded {len(history)}/{len(symbols)} symbols + BTC={'ok' if btc else 'MISSING'}")
        common = dict(days=args.days, top_n=args.top, warmup=args.warmup,
                      stride=args.stride, end_ms=end_ms, history=history, btc=btc,
                      verbose=False)
        # OFF: overlay neutral.
        off = run(settings, regime_overlay=False, **common)
        # ON ungated: full regime cut (gate floor 1.0 disables the gate).
        settings.regime.overlay_dd_gate_floor = 1.0
        on = run(settings, regime_overlay=True, **common)
        # ON + survival gate: de-risk scaled by drawdown budget.
        settings.regime.overlay_dd_gate_floor = 0.35
        gated = run(settings, regime_overlay=True, **common)
        r = _row(label, off, on, gated)
        rows.append(r)
        print(f"  OFF        : {r['off_return_pct']:+6.2f}%  (maxDD {r['off_max_dd_pct']:.1f}%)")
        print(f"  ON ungated : {r['on_return_pct']:+6.2f}%  (maxDD {r['on_max_dd_pct']:.1f}%)")
        print(f"  ON +gate   : {r['gated_return_pct']:+6.2f}%  (maxDD {r['gated_max_dd_pct']:.1f}%)")
        print(f"  gate vs OFF: {r['gated_vs_off_pct']:+6.2f}%")

    _print_table(rows)
    out = REPO_ROOT / "backtest" / "regime_ab.json"
    out.write_text(json.dumps({"params": vars(args), "windows": rows}, indent=2))
    print(f"\nwrote {out.relative_to(REPO_ROOT)}")
    return 0


def _print_table(rows: list[dict]) -> None:
    try:
        from rich.console import Console
        from rich.table import Table

        c = Console()
        t = Table(title="Regime overlay A/B — OFF vs ON(ungated) vs ON(dd-gated)")
        for col in ("window", "OFF", "ON", "ON+gate", "gate−OFF", "OFF DD", "ON DD", "gate DD", "BTC"):
            t.add_column(col, justify="right" if col != "window" else "left")
        for r in rows:
            dg = r["gated_vs_off_pct"]
            style = "green" if dg >= -0.25 else "red"
            t.add_row(
                r["window"],
                f"{r['off_return_pct']:+.2f}%", f"{r['on_return_pct']:+.2f}%",
                f"{r['gated_return_pct']:+.2f}%",
                f"[{style}]{dg:+.2f}%[/{style}]",
                f"{r['off_max_dd_pct']:.1f}%", f"{r['on_max_dd_pct']:.1f}%",
                f"{r['gated_max_dd_pct']:.1f}%", f"{r['btc_pct']:+.2f}%",
            )
        c.print(t)
    except Exception:
        print(json.dumps(rows, indent=2))


if __name__ == "__main__":
    raise SystemExit(main())
