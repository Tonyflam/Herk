"""Public dashboard: a read-only window into HELM's live state.

The agent process owns the trading loop and writes artifacts (state.json,
audit.jsonl, identity.json). The dashboard only *reads* them and marks open
positions with live quotes, so it can run alongside the agent without ever
interfering with trading.

Shows: equity curve stats, contest posture + regime, open positions with live
P&L, recent ledger activity, the audit-chain integrity badge, on-chain identity,
and data provenance — everything a judge needs to trust the agent at a glance.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates

from ..config import REPO_ROOT, Settings, load_settings

RUNTIME_DIR = REPO_ROOT / "data" / "runtime"
TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"


def _latest_of(records: list[dict[str, Any]], rtype: str) -> dict[str, Any] | None:
    for rec in reversed(records):
        if rec.get("type") == rtype:
            return rec
    return None


def build_snapshot(settings: Settings) -> dict[str, Any]:
    from ..data.market import MarketData
    from ..ledger import Ledger
    from ..portfolio import Portfolio, Position

    out: dict[str, Any] = {"mode": settings.mode, "profile": settings.profile, "ok": True}

    # --- ledger ------------------------------------------------------------
    led = Ledger(RUNTIME_DIR / "audit.jsonl")
    chain_ok, n, msg = led.verify()
    tail = led.tail(60)
    out["ledger"] = {"intact": chain_ok, "records": n, "message": msg}
    out["recent"] = [
        {"seq": r.get("seq"), "ts": str(r.get("ts", ""))[11:19],
         "type": r.get("type"), "data": r.get("data", {})}
        for r in tail[-14:]
    ]

    # --- rich signal + posture snapshot (written by the agent each step) ----
    snap_path = RUNTIME_DIR / "snapshot.json"
    if snap_path.exists():
        try:
            out["signal"] = json.loads(snap_path.read_text())
        except Exception:
            out["signal"] = None
    else:
        sig_rec = _latest_of(tail, "signal")
        out["signal"] = sig_rec.get("data") if sig_rec else None

    # --- portfolio + live marks -------------------------------------------
    state_path = RUNTIME_DIR / "state.json"
    positions: list[dict[str, Any]] = []
    summary: dict[str, Any] = {}
    if state_path.exists():
        d = json.loads(state_path.read_text())
        pf = Portfolio(
            initial_equity=d.get("initial_equity", 100.0),
            cash=d.get("cash", 0.0),
            peak_equity=d.get("peak_equity", 0.0),
            day_start_equity=d.get("day_start_equity", 0.0),
            realized_pnl=d.get("realized_pnl", 0.0),
            fees_paid=d.get("fees_paid", 0.0),
            trades_today=d.get("trades_today", 0),
            total_trades=d.get("total_trades", 0),
        )
        pf.positions = {s: Position(**p) for s, p in d.get("positions", {}).items()}

        md = MarketData(settings)
        try:
            prices = {}
            for sym, pos in pf.positions.items():
                q = md.get_quote(sym)
                px = q.price if q.price > 0 else pos.avg_entry
                prices[sym] = px
                upnl = (px - pos.avg_entry) * pos.qty
                positions.append({
                    "symbol": sym, "qty": round(pos.qty, 6),
                    "entry": round(pos.avg_entry, 6), "price": round(px, 6),
                    "value": round(pos.qty * px, 2),
                    "upnl": round(upnl, 2),
                    "upnl_pct": round((px / pos.avg_entry - 1) * 100, 2) if pos.avg_entry else 0,
                    "stop": round(pos.stop_price, 6), "tp": round(pos.take_profit_price, 6),
                })
            summary = pf.summary(prices)
        finally:
            md.close()
    out["positions"] = positions
    out["summary"] = summary

    # --- identity ----------------------------------------------------------
    try:
        from ..identity.erc8004 import Erc8004Identity
        out["identity"] = Erc8004Identity.load()
    except Exception:
        out["identity"] = None

    out["data_sources"] = settings.data.provider_priority
    return out


def create_app(settings: Settings):
    app = FastAPI(title="HELM", docs_url=None, redoc_url=None)
    templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

    @app.get("/", response_class=HTMLResponse)
    def index(request: Request):
        return templates.TemplateResponse(request, "index.html")

    @app.get("/api/snapshot", response_class=JSONResponse)
    def snapshot():
        try:
            return build_snapshot(settings)
        except Exception as e:
            return JSONResponse({"ok": False, "error": f"{type(e).__name__}: {e}"}, status_code=200)

    return app


def serve(settings: Settings | None = None) -> None:
    import uvicorn

    settings = settings or load_settings()
    app = create_app(settings)
    uvicorn.run(app, host=settings.dashboard.host, port=settings.dashboard.port, log_level="warning")
