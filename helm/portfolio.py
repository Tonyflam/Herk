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
    qty: float                  # always POSITIVE size; the sign lives in `direction`
    avg_entry: float
    stop_price: float
    take_profit_price: float
    stop_distance: float
    highest_price: float
    entry_ts: str
    # Trailing profit-lock re-arm reference: the peak (highest_price) at which the
    # last trail-lock slice was banked. Lets the guard fire once per FRESH high
    # rather than every cycle while price sits below the give-back band.
    trail_anchor: float = 0.0
    direction: int = 1          # +1 = long, -1 = short (perps). Defaults long for back-compat.
    lowest_price: float = 0.0   # running trough since entry (shorts trail off this); 0 = unset

    def trailing_stop(self) -> float:
        """Direction-aware trailing stop.

        Long: the higher of the fixed stop and (peak − stop_dist) — ratchets up.
        Short: the lower of the fixed stop and (trough + stop_dist) — ratchets down.
        """
        if self.direction >= 0:
            return max(self.stop_price, self.highest_price - self.stop_distance)
        trough = self.lowest_price if self.lowest_price > 0 else self.avg_entry
        return min(self.stop_price, trough + self.stop_distance)

    def mark_value(self, price: float) -> float:
        """USD the position contributes to equity if closed at `price`.

        Long: qty·price. Short: reserved margin + short P&L = qty·(2·entry − price),
        so equity rises as a short moves favorably (price down) and reduces to the
        long formula exactly when direction = +1.
        """
        if self.direction >= 0:
            return self.qty * price
        return self.qty * (2.0 * self.avg_entry - price)

    def unrealized_pnl(self, price: float) -> float:
        return self.direction * (price - self.avg_entry) * self.qty


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
    gas_paid: float = 0.0
    trades_today: int = 0
    total_trades: int = 0
    # Manual swing control (operator-directed); persisted across restarts.
    swing_armed: bool = False        # True after a manual sell, waiting for the dip rebuy
    swing_sell_px: float = 0.0       # realized price of the last manual swing sell
    swing_token: str = ""            # last consumed HELM_SWING_CMD token (one-shot idempotency)
    swing_flat: bool = False         # whole-book 'cash out': hold ALL freed cash for the dip rebuy
    # Volatility harvester (autonomous grid on the swing symbol); persisted.
    harvest_anchor_px: float = 0.0   # moving reference price for the next harvest band cross
    harvest_peak_px: float = 0.0     # running peak since last harvest action (trailing profit-lock)

    @classmethod
    def new(cls, initial_equity: float) -> "Portfolio":
        p = cls(initial_equity=initial_equity, cash=initial_equity)
        p.peak_equity = initial_equity
        p.day_start_equity = initial_equity
        p._day = datetime.now(timezone.utc).date()
        return p

    # ------------------------------------------------------------ marking
    def position_value(self, prices: dict[str, float]) -> float:
        return sum(p.mark_value(prices.get(s, p.avg_entry)) for s, p in self.positions.items())

    def equity(self, prices: dict[str, float]) -> float:
        return self.cash + self.position_value(prices)

    def gross_usd(self, prices: dict[str, float]) -> float:
        # True exposure magnitude (|notional|) regardless of side — qty is positive.
        return sum(p.qty * prices.get(s, p.avg_entry) for s, p in self.positions.items())

    def update_marks(self, prices: dict[str, float]) -> None:
        eq = self.equity(prices)
        self.peak_equity = max(self.peak_equity, eq)
        for s, pos in self.positions.items():
            px = prices.get(s, pos.avg_entry)
            pos.highest_price = max(pos.highest_price, px)
            pos.lowest_price = px if pos.lowest_price <= 0 else min(pos.lowest_price, px)

    def roll_day(self, now: datetime, prices: dict[str, float]) -> None:
        today = now.date()
        if self._day is None or today != self._day:
            self._day = today
            self.day_start_equity = self.equity(prices)
            self.trades_today = 0

    # --------------------------------------------------------------- fills
    def apply_buy(self, fill: Fill, stop_price: float, take_profit_price: float,
                  stop_distance: float) -> None:
        cost = fill.notional_usd + fill.fee_usd + fill.gas_usd
        self.cash -= cost
        self.fees_paid += fill.fee_usd
        self.gas_paid += fill.gas_usd
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

    def apply_open(self, fill: Fill, direction: int, stop_price: float,
                   take_profit_price: float, stop_distance: float) -> None:
        """Open or add to a position in `direction` (+1 long / -1 short).

        Reserves the full notional as margin from cash (1x-equivalent bookkeeping),
        identical to ``apply_buy`` when direction = +1. Perp long AND short entries
        route here so the accounting stays symmetric.
        """
        cost = fill.notional_usd + fill.fee_usd + fill.gas_usd
        self.cash -= cost
        self.fees_paid += fill.fee_usd
        self.gas_paid += fill.gas_usd
        self.trades_today += 1
        self.total_trades += 1
        d = 1 if direction >= 0 else -1
        existing = self.positions.get(fill.symbol)
        if existing and existing.direction == d:
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
            existing.lowest_price = (fill.price if existing.lowest_price <= 0
                                     else min(existing.lowest_price, fill.price))
        else:
            self.positions[fill.symbol] = Position(
                symbol=fill.symbol, qty=fill.qty, avg_entry=fill.price,
                stop_price=stop_price, take_profit_price=take_profit_price,
                stop_distance=stop_distance, highest_price=fill.price, entry_ts=fill.ts,
                direction=d, lowest_price=fill.price,
            )

    def apply_close(self, fill: Fill) -> float:
        """Close / reduce the existing position by ``fill.qty``; returns realized P&L.

        Direction-aware: returns reserved margin + signed P&L to cash. Reduces to
        ``apply_sell`` exactly for a long (direction = +1).
        """
        pos = self.positions.get(fill.symbol)
        if not pos:
            # Stray close with nothing on the book: mirror legacy apply_sell cash-in.
            self.cash += fill.notional_usd - fill.fee_usd - fill.gas_usd
            self.fees_paid += fill.fee_usd
            self.gas_paid += fill.gas_usd
            self.trades_today += 1
            self.total_trades += 1
            return 0.0
        close_qty = min(fill.qty, pos.qty)
        gross = pos.direction * (fill.price - pos.avg_entry) * close_qty
        self.cash += close_qty * pos.avg_entry + gross - fill.fee_usd - fill.gas_usd
        realized = gross - fill.fee_usd - fill.gas_usd
        self.realized_pnl += realized
        self.fees_paid += fill.fee_usd
        self.gas_paid += fill.gas_usd
        self.trades_today += 1
        self.total_trades += 1
        pos.qty -= close_qty
        if pos.qty <= 1e-12:
            del self.positions[fill.symbol]
        return realized

    def apply_sell(self, fill: Fill) -> float:
        """Apply a sell fill; returns realized P&L for the closed quantity."""
        pos = self.positions.get(fill.symbol)
        self.cash += fill.notional_usd - fill.fee_usd - fill.gas_usd
        self.fees_paid += fill.fee_usd
        self.gas_paid += fill.gas_usd
        self.trades_today += 1
        self.total_trades += 1
        realized = 0.0
        if pos:
            realized = (fill.price - pos.avg_entry) * fill.qty - fill.fee_usd - fill.gas_usd
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
            if pos.direction >= 0:
                if px <= pos.stop_price:
                    out.append(ExitSignal(s, pos.qty, "stop", px))
                elif px >= pos.take_profit_price:
                    out.append(ExitSignal(s, pos.qty, "take_profit", px))
                elif trailing and px <= pos.trailing_stop():
                    out.append(ExitSignal(s, pos.qty, "trailing_stop", px))
            else:
                # Short: loss is UP, profit is DOWN — mirror every threshold.
                if px >= pos.stop_price:
                    out.append(ExitSignal(s, pos.qty, "stop", px))
                elif px <= pos.take_profit_price:
                    out.append(ExitSignal(s, pos.qty, "take_profit", px))
                elif trailing and px >= pos.trailing_stop():
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
            "gas_paid": self.gas_paid,
            "open_positions": len(self.positions),
            "trades_today": self.trades_today,
            "total_trades": self.total_trades,
        }
