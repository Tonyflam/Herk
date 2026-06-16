"""Deterministic position sizing: ATR-stop risk budgeting + vol-targeting + caps.

The size of a position is the *minimum* of several independent budgets, so no
single input can blow up the book:

  • risk budget   — lose at most ``per_trade_risk_pct`` of equity if the ATR stop
                    is hit (size = risk$ / stop_distance).
  • vol budget    — scale notional so a single name's annualized vol contributes
                    ≈ the portfolio vol target (calmer names get more capital).
  • position cap  — never exceed ``max_position_pct`` of equity in one name.
  • gross headroom— never exceed remaining gross-exposure allowance.

All numbers come from config + live ATR/vol. No LLM, no discretion.
"""

from __future__ import annotations

from dataclasses import dataclass

_FALLBACK_STOP_PCT = 0.06  # if ATR is unavailable, assume a 6% stop distance


@dataclass
class SizePlan:
    symbol: str
    price: float
    atr: float
    stop_price: float
    take_profit_price: float
    stop_distance: float
    risk_budget_usd: float
    notional_usd: float
    qty: float
    pct_of_equity: float
    binding_constraint: str
    note: str = ""

    @property
    def ok(self) -> bool:
        return self.qty > 0 and self.notional_usd > 0


def plan_position(
    *,
    symbol: str,
    price: float,
    atr: float,
    equity: float,
    per_trade_risk_pct: float,
    stop_atr_mult: float,
    take_profit_atr_mult: float,
    max_position_pct: float,
    gross_headroom_usd: float,
    realized_vol_annual: float | None = None,
    target_vol_annual: float | None = None,
) -> SizePlan:
    if price <= 0 or equity <= 0:
        return SizePlan(symbol, price, atr, 0, 0, 0, 0, 0, 0, 0, "invalid", "price/equity<=0")

    stop_distance = atr * stop_atr_mult if atr > 0 else price * _FALLBACK_STOP_PCT
    stop_distance = max(stop_distance, 1e-12)

    risk_budget_usd = equity * (per_trade_risk_pct / 100.0)

    # 1) Risk-budget notional (how much we can hold so a stop-out costs risk$).
    qty_risk = risk_budget_usd / stop_distance
    notional_risk = qty_risk * price

    # 2) Vol-budget notional (single-name vol-targeting).
    if realized_vol_annual and realized_vol_annual > 0 and target_vol_annual:
        notional_vol = equity * (target_vol_annual / realized_vol_annual)
    else:
        notional_vol = float("inf")

    # 3) Per-position cap.
    notional_cap = equity * max_position_pct

    # 4) Remaining gross allowance.
    notional_gross = max(0.0, gross_headroom_usd)

    budgets = {
        "risk": notional_risk,
        "vol_target": notional_vol,
        "position_cap": notional_cap,
        "gross_headroom": notional_gross,
    }
    binding = min(budgets, key=lambda k: budgets[k])
    notional = max(0.0, min(budgets.values()))

    qty = notional / price
    atr_for_levels = atr if atr > 0 else price * _FALLBACK_STOP_PCT / max(stop_atr_mult, 1e-9)
    stop_price = price - stop_distance
    take_profit_price = price + atr_for_levels * take_profit_atr_mult

    return SizePlan(
        symbol=symbol,
        price=price,
        atr=atr,
        stop_price=max(0.0, stop_price),
        take_profit_price=take_profit_price,
        stop_distance=stop_distance,
        risk_budget_usd=risk_budget_usd,
        notional_usd=notional,
        qty=qty,
        pct_of_equity=(notional / equity) if equity > 0 else 0.0,
        binding_constraint=binding,
    )
