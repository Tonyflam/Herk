"""Execution interface shared by paper and live adapters.

An ``Order`` is intent; a ``Fill`` is what actually happened. The same slippage
model feeds both Sentinel's pre-trade estimate and the paper fill, so the gate
and the simulator never disagree.
"""

from __future__ import annotations

import math
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime, timezone


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def model_slippage_bps(
    notional_usd: float,
    liquidity_usd: float,
    base_bps: float = 8.0,
    k: float = 2500.0,
    cap_bps: float = 150.0,
) -> float:
    """Square-root market-impact model: base + k·sqrt(notional/liquidity)."""
    if liquidity_usd <= 0:
        return cap_bps
    impact = k * math.sqrt(max(0.0, notional_usd) / liquidity_usd)
    return float(min(cap_bps, base_bps + impact))


@dataclass
class Order:
    symbol: str
    side: str                 # 'buy' | 'sell'
    ref_price: float
    notional_usd: float = 0.0  # used for buys / opens (notional sizing)
    qty: float = 0.0           # used for sells / closes (exact-unit sizing)
    liquidity_usd: float = 0.0
    reason: str = ""
    reduce_only: bool = False  # perps: True = close/reduce only (never opens a new position)
    leverage: float = 0.0      # perps: per-trade leverage cap (0 = use adapter default/ceiling)


@dataclass
class Fill:
    symbol: str
    side: str
    qty: float
    price: float
    notional_usd: float
    fee_usd: float
    slippage_bps: float
    ts: str
    source: str
    ok: bool = True
    note: str = ""
    gas_usd: float = 0.0       # BSC network gas burned on this swap (0 in pure sim if no fill)


class ExecutionAdapter(ABC):
    name: str = "base"

    @abstractmethod
    def execute(self, order: Order) -> Fill: ...

    def supports_live(self) -> bool:
        return False
