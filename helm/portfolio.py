"""Portfolio: cash + positions accounting, mark-to-market, and exit detection.

Tracks everything the meta-controller and Sentinel need: equity, peak (for
drawdown), day-start equity (for the daily-loss limit and min-trade floor), and
per-position stop / take-profit / trailing levels.

All P&L is realized on USDT terms. Pure bookkeeping — no I/O, no network.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timezone

from .execution.base import Fill
from .risk.sentinel import BookState


@dataclass
class Position:
    symbol: str
    qty: float
    avg_entry: float
    stop_price: float
    take_profit_price: float
    stop_distance: float
    highest_price: float
    entry_ts: str

    def trailing_stop(self) -> float:
        """Trailing stop = the higher of the fixed stop and (peak − stop_dist)."""
        return max(self.stop_price, self.highest_price - self.stop_distance)


@dataclass
class ExitSignal:
    symbol: str
    qty: float
    reason: str          # stop | take_profit | trailing_stop
    ref_price: float


@dataclass
class Portfolio:
    initial_equity: float
    cash: float = 0.0
    positions: dict[str, Position] = field(default_factory=dict)
    peak_equity: float = 0.0
    day_start_equity: float = 0.0
    _day: date | None = None
    realized_pnl: float = 0.0
    fees_paid: float = 0.0
    trades_today: int = 0
    total_trades: int = 0

    @classmethod
    def new(cls, initial_equity: float) -> "Portfolio":
        p = cls(initial_equity=initial_equity, cash=initial_equity)
        p.peak_equity = initial_equity
        p.day_start_equity = initial_equity
        p._day = datetime.now(timezone.utc).date()
        return p

    # ------------------------------------------------------------ marking
    def position_value(self, prices: dict[str, float]) -> float:
        return sum(p.qty * prices.get(s, p.avg_entry) for s, p in self.positions.items())

    def equity(self, prices: dict[str, float]) -> float:
        return self.cash + self.position_value(prices)

    def gross_usd(self, prices: dict[str, float]) -> float:
        return self.position_value(prices)

    def update_marks(self, prices: dict[str, float]) -> None:
        eq = self.equity(prices)
        self.peak_equity = max(self.peak_equity, eq)
        for s, pos in self.positions.items():
            px = prices.get(s, pos.avg_entry)
            pos.highest_price = max(pos.highest_price, px)

    def roll_day(self, now: datetime, prices: dict[str, float]) -> None:
        today = now.date()
        if self._day is None or today != self._day:
            self._day = today
            self.day_start_equity = self.equity(prices)
            self.trades_today = 0

    # --------------------------------------------------------------- fills
    def apply_buy(self, fill: Fill, stop_price: float, take_profit_price: float,
                  stop_distance: float) -> None:
        cost = fill.notional_usd + fill.fee_usd
        self.cash -= cost
        self.fees_paid += fill.fee_usd
        self.trades_today += 1
        self.total_trades += 1
        existing = self.positions.get(fill.symbol)
        if existing:
            new_qty = existing.qty + fill.qty
            existing.avg_entry = (
                (existing.avg_entry * existing.qty + fill.price * fill.qty) / new_qty
                if new_qty > 0 else fill.price
            )
            existing.qty = new_qty
            existing.stop_price = stop_price
            existing.take_profit_price = take_profit_price
            existing.stop_distance = stop_distance
            existing.highest_price = max(existing.highest_price, fill.price)
        else:
            self.positions[fill.symbol] = Position(
                symbol=fill.symbol, qty=fill.qty, avg_entry=fill.price,
                stop_price=stop_price, take_profit_price=take_profit_price,
                stop_distance=stop_distance, highest_price=fill.price, entry_ts=fill.ts,
            )

    def apply_sell(self, fill: Fill) -> float:
        """Apply a sell fill; returns realized P&L for the closed quantity."""
        pos = self.positions.get(fill.symbol)
        self.cash += fill.notional_usd - fill.fee_usd
        self.fees_paid += fill.fee_usd
        self.trades_today += 1
        self.total_trades += 1
        realized = 0.0
        if pos:
            realized = (fill.price - pos.avg_entry) * fill.qty - fill.fee_usd
            self.realized_pnl += realized
            pos.qty -= fill.qty
            if pos.qty <= 1e-12:
                del self.positions[fill.symbol]
        return realized

    # --------------------------------------------------------------- exits
    def exits_to_run(self, prices: dict[str, float], trailing: bool) -> list[ExitSignal]:
        out: list[ExitSignal] = []
        for s, pos in self.positions.items():
            px = prices.get(s)
            if px is None or px <= 0:
                continue
            if px <= pos.stop_price:
                out.append(ExitSignal(s, pos.qty, "stop", px))
            elif px >= pos.take_profit_price:
                out.append(ExitSignal(s, pos.qty, "take_profit", px))
            elif trailing and px <= pos.trailing_stop():
                out.append(ExitSignal(s, pos.qty, "trailing_stop", px))
        return out

    # --------------------------------------------------------------- views
    def book_state(self, prices: dict[str, float], symbol: str | None = None) -> BookState:
        return BookState(
            equity=self.equity(prices),
            peak_equity=self.peak_equity,
            day_start_equity=self.day_start_equity,
            gross_usd=self.gross_usd(prices),
            open_positions=len(self.positions),
            holds_symbol=bool(symbol and symbol in self.positions),
        )

    def summary(self, prices: dict[str, float]) -> dict:
        eq = self.equity(prices)
        return {
            "equity": eq,
            "cash": self.cash,
            "gross": self.gross_usd(prices),
            "peak": self.peak_equity,
            "return_pct": (eq - self.initial_equity) / self.initial_equity * 100.0
            if self.initial_equity > 0 else 0.0,
            "drawdown_pct": (self.peak_equity - eq) / self.peak_equity * 100.0
            if self.peak_equity > 0 else 0.0,
            "realized_pnl": self.realized_pnl,
            "fees_paid": self.fees_paid,
            "open_positions": len(self.positions),
            "trades_today": self.trades_today,
            "total_trades": self.total_trades,
        }
