"""HELM command-line interface.

Commands (all run in paper mode with zero credentials):

  helm signal              one-shot: regime + posture + ranked signal table
  helm run [--dry-run]     run the agent (``--cycles N --interval S``)
  helm preflight           contest-readiness checklist (paper- or live-aware)
  helm verify              re-validate the tamper-evident audit ledger
  helm status              portfolio summary + recent ledger activity
  helm register            (live) register the agent for the competition via TWAK
  helm dashboard           launch the public dashboard

Designed so a judge can clone, ``pip install -r requirements.txt``, and run
``helm signal`` to see real, live signals immediately — no keys, no wallet.
"""

from __future__ import annotations

import argparse
import sys
import time

from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from .config import load_settings

console = Console()


def _fmt_pct(x: float) -> str:
    return f"{x:+.2f}%"


def cmd_signal(args) -> int:
    from .agent import Agent

    agent = Agent()
    try:
        report = agent.step(dry_run=True)
    finally:
        agent.close()
    _print_report(report, title=f"HELM — signal · [yellow]{agent.settings.profile}[/yellow] profile (dry run)")
    return 0


def cmd_run(args) -> int:
    from .agent import Agent

    agent = Agent()
    dry = bool(args.dry_run)
    cycles = max(1, int(args.cycles))
    interval = max(0, int(args.interval))
    mode = "DRY-RUN" if dry else ("LIVE" if agent.settings.is_live else "PAPER")
    console.print(f"[bold]HELM[/bold] starting · mode=[cyan]{mode}[/cyan] · "
                  f"profile=[yellow]{agent.settings.profile}[/yellow] · "
                  f"cycles={cycles} · interval={interval}s")
    try:
        for i in range(cycles):
            report = agent.step(dry_run=dry)
            _print_report(report, title=f"HELM — cycle {i + 1}/{cycles} ({mode})")
            if i < cycles - 1 and interval > 0:
                time.sleep(interval)
    finally:
        agent.close()
    return 0


def cmd_verify(args) -> int:
    from .agent import RUNTIME_DIR
    from .ledger import Ledger

    led = Ledger(RUNTIME_DIR / "audit.jsonl")
    ok, n, msg = led.verify()
    color = "green" if ok else "red"
    console.print(Panel(f"[{color}]{'INTACT' if ok else 'TAMPERED'}[/{color}] · "
                        f"{n} records · {msg}", title="Audit ledger verification"))
    return 0 if ok else 1


def cmd_status(args) -> int:
    from .agent import RUNTIME_DIR
    from .ledger import Ledger

    led = Ledger(RUNTIME_DIR / "audit.jsonl")
    tail = led.tail(12)
    table = Table(title="Recent ledger activity", show_lines=False)
    table.add_column("seq", justify="right")
    table.add_column("ts")
    table.add_column("type")
    table.add_column("summary")
    for rec in tail:
        data = rec.get("data", {})
        summary = ", ".join(f"{k}={v}" for k, v in list(data.items())[:3])
        table.add_row(str(rec.get("seq")), str(rec.get("ts", ""))[11:19],
                      str(rec.get("type")), summary[:70])
    console.print(table)
    ok, n, msg = led.verify()
    console.print(f"ledger: [{'green' if ok else 'red'}]{msg}[/] ({n} records)")
    return 0


def cmd_register(args) -> int:
    settings = load_settings()
    try:
        from .execution.twak import TwakAdapter
    except Exception as e:  # adapter not present yet
        console.print(f"[red]live adapter unavailable:[/red] {e}")
        return 1
    adapter = TwakAdapter(settings)
    res = adapter.compete_register()
    console.print(Panel(str(res), title="TWAK compete register"))
    return 0 if getattr(res, "ok", False) else 1


def cmd_identity(args) -> int:
    settings = load_settings()
    from .identity.erc8004 import Erc8004Identity

    ident = Erc8004Identity(settings)
    existing = Erc8004Identity.load()
    if existing and existing.get("agent_id") and not args.force:
        console.print(Panel(
            f"agentId={existing['agent_id']}  addr={existing.get('address','')}\n"
            f"network={existing.get('network','')}  uri={existing.get('agent_uri','')}",
            title="ERC-8004 identity (cached)"))
        return 0
    res = ident.register()
    color = "green" if res.ok else "red"
    console.print(Panel(f"[{color}]{res}[/{color}]", title="ERC-8004 register"))
    return 0 if res.ok else 1


def cmd_dashboard(args) -> int:
    settings = load_settings()
    try:
        from .dashboard.server import serve
    except Exception as e:
        console.print(f"[red]dashboard unavailable:[/red] {e}")
        return 1
    serve(settings)
    return 0


def cmd_preflight(args) -> int:
    """Contest-readiness checklist. Context-aware: in paper mode the live-only
    items are informational; in live mode a missing item is a hard FAIL.

    Exit code 0 only when there are no FAILs (warnings are allowed).
    """
    from datetime import datetime, timezone

    from .agent import RUNTIME_DIR
    from .ledger import Ledger
    from .risk.sentinel import Sentinel

    s = load_settings()
    live = s.mode == "live"
    rows: list[tuple[str, str, str]] = []  # (status, check, detail)

    def add(status: str, check: str, detail: str = "") -> None:
        rows.append((status, check, detail))

    # --- mode / profile -----------------------------------------------------
    add("ok", "Run mode", f"{s.mode}" + ("  (LIVE — real funds)" if live else "  (paper — simulated)"))
    if s.profile == "aggressive":
        add("ok", "Risk profile", "aggressive (contest posture)")
    elif live:
        add("warn", "Risk profile", f"{s.profile} — set HELM_PROFILE=aggressive for the contest")
    else:
        add("info", "Risk profile", f"{s.profile}")

    # --- two-flag live arming ----------------------------------------------
    armed = s.mode == "live" and s.secrets.execute_trades and s.secrets.execute_chain
    if live:
        add("ok" if armed else "fail", "Live arming (2-flag)",
            "EXECUTE_TRADES & EXECUTE_CHAIN set" if armed
            else "need HELM_EXECUTE_TRADES=1 AND HELM_EXECUTE_CHAIN=1")
        add("ok" if s.execution.adapter == "twak" else "fail", "Execution adapter",
            f"{s.execution.adapter}" + ("" if s.execution.adapter == "twak" else " — must be 'twak' to trade on-chain"))
    else:
        add("info", "Live arming (2-flag)", "not required in paper")
        add("info", "Execution adapter", f"{s.execution.adapter}")

    # --- credentials (presence only; never printed) ------------------------
    sec = s.secrets
    def cred(name: str, present: bool, required_live: bool) -> None:
        if present:
            add("ok", name, "set")
        elif required_live and live:
            add("fail", name, "missing (required for live)")
        elif required_live:
            add("warn", name, "missing (needed before going live)")
        else:
            add("info", name, "not set (optional)")

    cred("TWAK API key", bool(sec.twak_api_key), True)
    cred("TWAK API secret", bool(sec.twak_api_secret), True)
    cred("TWAK wallet password", bool(sec.twak_wallet_password), True)
    cred("BNB agent wallet password", bool(sec.bnb_agent_wallet_password), False)
    cred("CMC API key", bool(sec.cmc_api_key), False)

    # --- TWAK CLI (Node) availability --------------------------------------
    try:
        from .execution.twak import TwakAdapter
        cli_ok = TwakAdapter(s).available
    except Exception:
        cli_ok = False
    if live:
        add("ok" if cli_ok else "fail", "TWAK CLI (Node)",
            "found" if cli_ok else "not found — run scripts/setup_live.sh")
    else:
        add("ok" if cli_ok else "info", "TWAK CLI (Node)",
            "found" if cli_ok else "not installed (only needed for live)")

    # --- on-chain identity --------------------------------------------------
    try:
        from .identity.erc8004 import Erc8004Identity
        ident = Erc8004Identity.load()
    except Exception:
        ident = None
    if ident and ident.get("agent_id"):
        add("ok", "ERC-8004 identity", f"agentId={ident.get('agent_id')} ({ident.get('network','')})")
    elif live:
        add("warn", "ERC-8004 identity", "not registered — run `helm identity` (special-prize credit)")
    else:
        add("info", "ERC-8004 identity", "not registered (paper)")

    # --- kill switch --------------------------------------------------------
    sentinel = Sentinel(s)
    if sentinel.kill_switch_engaged():
        add("fail", "Kill switch", f"ENGAGED — remove {sentinel.kill_switch_path} to allow trading")
    else:
        add("ok", "Kill switch", "clear")

    # --- contest window timing ---------------------------------------------
    try:
        start = datetime.fromisoformat(s.contest.start_utc.replace("Z", "+00:00"))
        end = datetime.fromisoformat(s.contest.end_utc.replace("Z", "+00:00"))
        now = datetime.now(timezone.utc)
        if now < start:
            days = (start - now).total_seconds() / 86400
            add("info", "Contest window", f"starts in {days:.1f} d ({s.contest.start_utc})")
        elif now > end:
            add("warn", "Contest window", f"window CLOSED ({s.contest.end_utc})")
        else:
            left = (end - now).total_seconds() / 86400
            add("ok", "Contest window", f"LIVE — {left:.1f} d remaining")
    except Exception as e:
        add("warn", "Contest window", f"unparseable dates: {e}")

    # --- ledger integrity ---------------------------------------------------
    ok_chain, n, msg = Ledger(RUNTIME_DIR / "audit.jsonl").verify()
    add("ok" if ok_chain else "fail", "Audit ledger", f"{msg} ({n} records)")

    # --- data connectivity --------------------------------------------------
    try:
        from .data.market import MarketData
        md = MarketData(s)
        reg = md.get_regime()
        md.close()
        srcs = ",".join(k for k, v in (reg.sources or {}).items() if v) or "fallback"
        add("ok", "Market data", f"regime F&G={reg.fear_greed} dom={reg.btc_dominance:.1f}% via {srcs}")
    except Exception as e:
        add("warn", "Market data", f"degraded: {type(e).__name__}")

    # --- render -------------------------------------------------------------
    glyph = {"ok": "[green]✓[/green]", "warn": "[yellow]●[/yellow]",
             "fail": "[red]✗[/red]", "info": "[dim]·[/dim]"}
    table = Table(title=f"HELM preflight — {s.mode} / {s.profile}", show_header=True, header_style="bold")
    table.add_column("", justify="center", width=3)
    table.add_column("check")
    table.add_column("detail")
    for status, check, detail in rows:
        table.add_row(glyph.get(status, "?"), check, detail)
    console.print(table)

    fails = sum(1 for st, _, _ in rows if st == "fail")
    warns = sum(1 for st, _, _ in rows if st == "warn")
    if fails:
        console.print(Panel(f"[red]NOT READY[/red] · {fails} blocking issue(s), {warns} warning(s)",
                            title="verdict"))
        return 1
    if warns:
        console.print(Panel(f"[yellow]READY with caveats[/yellow] · {warns} warning(s) to review",
                            title="verdict"))
        return 0
    console.print(Panel("[green]READY[/green] · all checks pass", title="verdict"))
    return 0


def cmd_backtest(args) -> int:
    settings = load_settings()
    try:
        from backtest.walk_forward import run
    except Exception as e:
        console.print(f"[red]backtest unavailable:[/red] {e}")
        return 1
    end_ms = None
    if getattr(args, "end", None):
        from datetime import datetime, timezone
        end_ms = int(datetime.fromisoformat(args.end).replace(tzinfo=timezone.utc).timestamp() * 1000)
    try:
        run(settings, days=args.days, top_n=args.top, warmup=args.warmup,
            stride=args.stride, end_ms=end_ms,
            regime_overlay=getattr(args, "regime_overlay", False), verbose=True)
    except Exception as e:
        console.print(f"[red]backtest failed:[/red] {type(e).__name__}: {e}")
        return 1
    return 0


def _print_report(report, title: str) -> None:
    p, r, sm = report.posture, report.regime, report.summary
    head = (
        f"[bold]equity[/bold] ${sm['equity']:.2f} ({_fmt_pct(sm['return_pct'])})  "
        f"dd {sm['drawdown_pct']:.1f}%  |  "
        f"regime [cyan]{r.label}[/cyan] gross×{r.gross_scale:.2f}  F&G {r.fear_greed}  |  "
        f"posture [magenta]{p.posture}[/magenta] gross≤{p.max_gross_pct * 100:.0f}% "
        f"risk/trade {p.per_trade_risk_pct:.2f}%"
    )
    console.print(Panel(head, title=title))

    if report.top:
        t = Table(show_header=True, header_style="bold")
        t.add_column("rank")
        t.add_column("symbol")
        t.add_column("composite", justify="right")
        for i, (sym, comp) in enumerate(report.top, 1):
            t.add_row(str(i), sym, f"{comp:.2f}")
        console.print(t)

    if report.actions:
        a = Table(title="actions", show_header=True, header_style="bold")
        a.add_column("kind")
        a.add_column("symbol")
        a.add_column("detail")
        for act in report.actions:
            a.add_row(act.kind, act.symbol, act.detail)
        console.print(a)
    else:
        console.print("[dim]no actions this cycle[/dim]")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="helm", description="HELM contest trading agent")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("signal", help="one-shot signal + regime + posture").set_defaults(func=cmd_signal)

    pr = sub.add_parser("run", help="run the agent loop")
    pr.add_argument("--dry-run", action="store_true", help="decide but do not trade")
    pr.add_argument("--cycles", default=1, help="number of decision cycles")
    pr.add_argument("--interval", default=0, help="seconds between cycles")
    pr.set_defaults(func=cmd_run)

    sub.add_parser("verify", help="verify the audit ledger").set_defaults(func=cmd_verify)
    sub.add_parser("status", help="portfolio + ledger status").set_defaults(func=cmd_status)
    sub.add_parser("preflight", help="contest-readiness checklist").set_defaults(func=cmd_preflight)
    sub.add_parser("register", help="(live) register for the competition").set_defaults(func=cmd_register)

    pi = sub.add_parser("identity", help="(live) register ERC-8004 on-chain identity")
    pi.add_argument("--force", action="store_true", help="re-register even if cached")
    pi.set_defaults(func=cmd_identity)

    pb = sub.add_parser("backtest", help="walk-forward backtest over historical data")
    pb.add_argument("--days", type=int, default=40, help="window length (capped by 1000 1h bars)")
    pb.add_argument("--top", type=int, default=3, help="max concurrent names")
    pb.add_argument("--warmup", type=int, default=200, help="warmup bars before trading")
    pb.add_argument("--stride", type=int, default=6, help="rebalance cadence in hours")
    pb.add_argument("--end", type=str, default=None, help="window end date YYYY-MM-DD (default: latest)")
    pb.add_argument("--regime-overlay", action="store_true",
                    help="replay the F&G de-risking overlay via a no-lookahead proxy")
    pb.set_defaults(func=cmd_backtest)

    sub.add_parser("dashboard", help="launch the public dashboard").set_defaults(func=cmd_dashboard)
    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv if argv is not None else sys.argv[1:])
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
