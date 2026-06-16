"""Pre-trade route & liquidity validation.

Before the live week, confirm every symbol HELM might touch actually has a
tradeable market with acceptable cost — a name that looks eligible on paper but
has no real PancakeSwap route (or only thin liquidity) is a slippage trap and a
stuck-position risk. This screens the universe on public market data (price +
24h volume as a liquidity proxy) and models worst-case slippage at our largest
single-position size, classifying each symbol ok / thin / dead.

Offline-safe (no keys). For live route truth, the CLI can additionally run TWAK
quote-only swaps on top of this screen.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..config import Settings
from ..execution.base import model_slippage_bps
from ..universe import eligible_set, is_stable
from .market import MarketData


@dataclass
class RouteCheck:
    symbol: str
    in_scope: bool        # in the eligible competition set
    tradeable: bool       # in HELM's active trading book
    price: float = 0.0
    vol_24h_usd: float = 0.0
    est_slippage_bps: float = 0.0
    status: str = "dead"  # ok | thin | dead
    note: str = ""


def _ref_notional(settings: Settings) -> float:
    """Worst-case single-position size — the largest clip we'd ever route."""
    eq = settings.capital.initial_paper_equity_usd
    return max(1.0, settings.risk.max_position_pct * eq)


def validate_routes(
    settings: Settings,
    symbols: list[str],
    market: MarketData | None = None,
    tradeable: set[str] | None = None,
) -> list[RouteCheck]:
    own = market is None
    market = market or MarketData(settings)
    elig = eligible_set()
    book = tradeable if tradeable is not None else set()
    ref = _ref_notional(settings)
    min_liq = settings.risk.min_liquidity_usd
    max_slip = settings.risk.slippage_bps_max
    checks: list[RouteCheck] = []
    try:
        for sym in symbols:
            rc = RouteCheck(symbol=sym, in_scope=sym in elig, tradeable=sym in book)
            if is_stable(sym):
                rc.status, rc.note = "ok", "stable (cash leg)"
                checks.append(rc)
                continue
            q = market.get_quote(sym)
            rc.price = q.price
            rc.vol_24h_usd = q.volume_24h_usd
            if not q.provenance.ok or q.price <= 0:
                rc.status = "dead"
                rc.note = "no live quote / no route"
                checks.append(rc)
                continue
            rc.est_slippage_bps = model_slippage_bps(ref, q.volume_24h_usd, cap_bps=max_slip)
            if q.volume_24h_usd < min_liq:
                rc.status = "thin"
                rc.note = f"24h vol ${q.volume_24h_usd:,.0f} < min ${min_liq:,.0f}"
            elif rc.est_slippage_bps >= max_slip:
                rc.status = "thin"
                rc.note = f"slip {rc.est_slippage_bps:.0f}bps >= cap {max_slip:.0f}bps"
            else:
                rc.status = "ok"
            checks.append(rc)
        return checks
    finally:
        if own:
            market.close()


def summarize(checks: list[RouteCheck]) -> dict:
    """Counts for quick gating: how many tradeable names are unroutable."""
    tradeable = [c for c in checks if c.tradeable]
    return {
        "total": len(checks),
        "ok": sum(1 for c in checks if c.status == "ok"),
        "thin": sum(1 for c in checks if c.status == "thin"),
        "dead": sum(1 for c in checks if c.status == "dead"),
        "tradeable_total": len(tradeable),
        "tradeable_ok": sum(1 for c in tradeable if c.status == "ok"),
        "tradeable_bad": sum(1 for c in tradeable if c.status != "ok"),
    }
