"""Signal engine: assembles per-symbol features, ranks them cross-sectionally,
and applies three honest gates before anything is tradeable.

Pipeline per refresh:
  1. Pull regime once  → gross-exposure scalar (de-risk in fear).
  2. Per symbol: candles + quote → lookback returns, vol-adjusted, realized vol,
     ATR (for stops), venue liquidity.
  3. Cross-sectional z-score the vol-adjusted returns per horizon, weight-blend
     into one ``composite`` relative-strength score.
  4. Gates:
       • liquidity  — venue 24h volume ≥ min_liquidity_usd
       • quality    — composite ≥ min_composite_score
       • net-of-cost— recent move (bps) ≥ round-trip friction (fee + slippage)
     Only symbols passing all three are eligible; ranked by composite desc.

The engine never sizes or trades — it only proposes a clean, ranked shortlist.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..config import Settings
from ..data.market import MarketData
from . import momentum as M
from . import volatility as V
from .regime import RegimeAssessment, assess_regime


@dataclass
class SymbolSignal:
    symbol: str
    price: float = 0.0
    composite: float = 0.0
    mom_blended_return: float = 0.0
    fast_return: float = 0.0          # short-horizon (few-hour) return — the autopilot's gauge
    realized_vol_annual: float = 0.0
    atr: float = 0.0
    atr_pct: float = 0.0
    liquidity_usd: float = 0.0
    lookback_returns: dict[int, float] = field(default_factory=dict)
    passes_liquidity: bool = False
    passes_quality: bool = False
    passes_cost: bool = False
    source: str = ""
    note: str = ""

    @property
    def eligible(self) -> bool:
        return self.passes_liquidity and self.passes_quality and self.passes_cost


@dataclass
class SignalSnapshot:
    regime: RegimeAssessment
    signals: list[SymbolSignal]          # everything we scored (for transparency)
    ranked: list[SymbolSignal]           # eligible only, composite desc
    cost_bps: float = 0.0

    def top(self, n: int) -> list[SymbolSignal]:
        return self.ranked[: max(0, n)]


class SignalEngine:
    def __init__(self, settings: Settings, market: MarketData):
        self.settings = settings
        self.market = market

    def compute(self, universe: list[str], candle_limit: int = 200) -> SignalSnapshot:
        s = self.settings
        lookbacks = list(s.signals.lookbacks_hours)
        weights = list(s.signals.momentum_weights)
        if len(weights) != len(lookbacks):  # defensive: equal-weight fallback
            weights = [1.0 / len(lookbacks)] * len(lookbacks)

        regime = assess_regime(self.market.get_regime(), s)

        # ---- Stage 1: per-symbol features -------------------------------
        rows: list[SymbolSignal] = []
        # vol_adj[L] holds aligned vol-adjusted returns for the cross-section.
        vol_adj: dict[int, list[float]] = {L: [] for L in lookbacks}
        idx_with_data: list[int] = []

        for sym in universe:
            candles = self.market.get_candles(sym, "1h", candle_limit)
            quote = self.market.get_quote(sym)
            sig = SymbolSignal(symbol=sym, source=candles.provenance.source)

            if len(candles) < min(lookbacks) + 2:
                sig.note = "insufficient candles"
                rows.append(sig)
                continue

            closes = candles.closes
            sig.price = quote.price or (closes[-1] if closes else 0.0)
            sig.liquidity_usd = quote.volume_24h_usd
            vh = V.realized_vol_hourly(closes)
            sig.realized_vol_annual = V.realized_vol_annual(closes)
            sig.atr = V.atr(candles.highs, candles.lows, closes)
            sig.atr_pct = (sig.atr / sig.price) if sig.price > 0 else 0.0

            blended = 0.0
            for L, w in zip(lookbacks, weights):
                r = M.lookback_return(closes, L)
                va = M.vol_adjusted_return(r, vh, L)
                if r is not None:
                    sig.lookback_returns[L] = r
                    blended += w * max(r, 0.0)  # cost gate uses positive drift only
                vol_adj[L].append(va if va is not None else float("nan"))
            sig.mom_blended_return = blended
            # Fast momentum gauge for the autonomous autopilot (few-hour return on
            # the same 1h closes the engine already holds — no extra data call).
            fh = int(getattr(s.signals, "autopilot_fast_hours", 3) or 3)
            _fr = M.lookback_return(closes, fh)
            sig.fast_return = _fr if _fr is not None else 0.0

            idx_with_data.append(len(rows))
            rows.append(sig)

        # ---- Stage 2: cross-sectional z-score → composite ---------------
        # Build per-horizon z-scores over symbols that had data, then blend.
        if idx_with_data:
            for j, L in enumerate(lookbacks):
                series = [vol_adj[L][pos] for pos in range(len(idx_with_data))]
                zs = M.zscore(series)
                for pos, k in enumerate(idx_with_data):
                    z = float(zs[pos]) if pos < len(zs) else 0.0
                    rows[k].composite += weights[j] * z

            # ---- Volatility (grid-fuel) tilt --------------------------------
            # A harvester/grid earns from price *range*; vol-adjusted momentum
            # above divides return by vol, penalising the very volatility that
            # fuels it. When enabled, add a z-scored ATR% term so high-range
            # liquid names (best grid fuel) rank ahead of calm ones — a
            # volatility-first selection. Weight 0 leaves the composite untouched.
            vt = s.signals.vol_tilt_weight
            if vt:
                atrp = [rows[k].atr_pct for k in idx_with_data]
                zv = M.zscore(atrp)
                for pos, k in enumerate(idx_with_data):
                    z = float(zv[pos]) if pos < len(zv) else 0.0
                    rows[k].composite += vt * z

        # ---- Stage 3: gates ---------------------------------------------
        cost_bps = s.risk.fee_bps_roundtrip + s.risk.slippage_bps_max
        min_liq = s.risk.min_liquidity_usd
        min_q = s.signals.min_composite_score
        gate_cost = s.signals.net_of_cost_gate

        for row in rows:
            if not row.lookback_returns:
                continue
            row.passes_liquidity = row.liquidity_usd >= min_liq
            row.passes_quality = row.composite >= min_q
            move_bps = row.mom_blended_return * 10_000.0
            row.passes_cost = (move_bps >= cost_bps) if gate_cost else True

        ranked = sorted(
            (r for r in rows if r.eligible),
            key=lambda r: r.composite,
            reverse=True,
        )
        return SignalSnapshot(regime=regime, signals=rows, ranked=ranked, cost_bps=cost_bps)
