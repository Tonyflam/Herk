"""Paper executor: deterministic simulated fills at live mid ± modeled slippage,
minus venue fees. No randomness — same inputs always produce the same fill, so
backtests and live-paper runs are reproducible and auditable.
"""

from __future__ import annotations

from ..config import Settings
from .base import ExecutionAdapter, Fill, Order, _now_iso, model_slippage_bps


class PaperExecutor(ExecutionAdapter):
    name = "paper"

    def __init__(self, settings: Settings):
        self.s = settings

    def execute(self, order: Order) -> Fill:
        cfg = self.s.risk
        if order.ref_price <= 0:
            return Fill(order.symbol, order.side, 0, 0, 0, 0, 0, _now_iso(), "paper", False, "no price")

        est_notional = order.notional_usd if order.side == "buy" else order.qty * order.ref_price
        slip = model_slippage_bps(est_notional, order.liquidity_usd, cap_bps=cfg.slippage_bps_max)
        fee_bps_side = cfg.fee_bps_roundtrip / 2.0

        if order.side == "buy":
            price = order.ref_price * (1 + slip / 10_000.0)
            notional = order.notional_usd
            qty = notional / price if price > 0 else 0.0
        else:  # sell
            price = order.ref_price * (1 - slip / 10_000.0)
            qty = order.qty
            notional = qty * price

        fee = notional * (fee_bps_side / 10_000.0)
        return Fill(
            symbol=order.symbol,
            side=order.side,
            qty=qty,
            price=price,
            notional_usd=notional,
            fee_usd=fee,
            slippage_bps=slip,
            ts=_now_iso(),
            source="paper",
            ok=qty > 0,
        )
