"""HELM agent: the orchestration loop.

One ``step`` is the whole decision cycle, in strict order:

  1. Refresh market data → cross-sectional signals + regime.
  2. Mark the book; roll the UTC day (resets daily-loss + trade counters).
  3. Ask the meta-controller for today's risk budget (folds in regime).
  4. Manage open risk first: run stops / take-profits / trailing exits.
  5. If new risk is allowed, size the top-ranked names and pass each through
     Sentinel; execute only what Sentinel approves.
  6. Guarantee the ≥1-trade/day contest floor (avoid disqualification).
  7. Append every signal, verdict, and fill to the tamper-evident ledger.

State is persisted after each step so a week-long run survives restarts.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path

from .config import REPO_ROOT, Settings, load_settings
from .contest.meta_controller import ContestPosture, MetaController
from .data.market import MarketData
from .execution.base import ExecutionAdapter, Order, model_slippage_bps
from .execution.paper import PaperExecutor
from .ledger import Ledger
from .portfolio import Portfolio, Position
from .risk.sentinel import Sentinel
from .risk.sizing import plan_position
from .signals.engine import SignalEngine, SignalSnapshot
from .signals.regime import RegimeAssessment
from .universe import tradeable_universe

RUNTIME_DIR = REPO_ROOT / "data" / "runtime"


@dataclass
class Action:
    kind: str        # entry | exit | dry_run | blocked | compliance
    symbol: str
    detail: str


@dataclass
class StepReport:
    ts: str
    posture: ContestPosture
    regime: RegimeAssessment
    top: list[tuple[str, float]]
    actions: list[Action] = field(default_factory=list)
    summary: dict = field(default_factory=dict)


class Agent:
    def __init__(
        self,
        settings: Settings | None = None,
        executor: ExecutionAdapter | None = None,
        ledger_path: str | Path | None = None,
        state_path: str | Path | None = None,
    ):
        self.settings = settings or load_settings()
        self.market = MarketData(self.settings)
        self.engine = SignalEngine(self.settings, self.market)
        self.meta = MetaController(self.settings)
        self.sentinel = Sentinel(self.settings)
        self.executor = executor or self._make_executor()
        RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
        self.ledger = Ledger(ledger_path or RUNTIME_DIR / "audit.jsonl")
        self.state_path = Path(state_path or RUNTIME_DIR / "state.json")
        self.snapshot_path = RUNTIME_DIR / "snapshot.json"
        self.portfolio = self._load_state() or Portfolio.new(
            self.settings.capital.initial_paper_equity_usd
        )
        # Native x402 budget tracking (paid CMC data calls per UTC day).
        self._x402_day: date | None = None
        self._x402_calls = 0

    # --------------------------------------------------------- adapter pick
    def _make_executor(self) -> ExecutionAdapter:
        if self.settings.execution.adapter == "twak":
            from .execution.twak import TwakAdapter
            return TwakAdapter(self.settings)
        return PaperExecutor(self.settings)

    # ----------------------------------------------------------- persistence
    def _load_state(self) -> Portfolio | None:
        if not self.state_path.exists():
            return None
        try:
            d = json.loads(self.state_path.read_text())
            p = Portfolio(
                initial_equity=d["initial_equity"],
                cash=d["cash"],
                peak_equity=d["peak_equity"],
                day_start_equity=d["day_start_equity"],
                realized_pnl=d.get("realized_pnl", 0.0),
                fees_paid=d.get("fees_paid", 0.0),
                trades_today=d.get("trades_today", 0),
                total_trades=d.get("total_trades", 0),
            )
            p._day = date.fromisoformat(d["day"]) if d.get("day") else None
            p.positions = {s: Position(**pos) for s, pos in d.get("positions", {}).items()}
            p.swing_armed = bool(d.get("swing_armed", False))
            p.swing_sell_px = float(d.get("swing_sell_px", 0.0))
            p.swing_token = str(d.get("swing_token", ""))
            return p
        except Exception:
            return None

    def _save_state(self) -> None:
        p = self.portfolio
        d = {
            "initial_equity": p.initial_equity,
            "cash": p.cash,
            "peak_equity": p.peak_equity,
            "day_start_equity": p.day_start_equity,
            "day": p._day.isoformat() if p._day else None,
            "realized_pnl": p.realized_pnl,
            "fees_paid": p.fees_paid,
            "trades_today": p.trades_today,
            "total_trades": p.total_trades,
            "positions": {s: asdict(pos) for s, pos in p.positions.items()},
            "swing_armed": p.swing_armed,
            "swing_sell_px": p.swing_sell_px,
            "swing_token": p.swing_token,
        }
        tmp = self.state_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(d, indent=2, default=str))
        tmp.replace(self.state_path)

    def reload_state(self) -> bool:
        """Re-read the last committed state from disk, discarding any partial
        in-memory mutation from a step that raised mid-cycle. Used by the
        supervised run loop to recover cleanly after a transient failure. State
        is only persisted at the *end* of a successful step, so the on-disk copy
        is always the last good checkpoint. Returns True if a state was loaded.
        """
        restored = self._load_state()
        if restored is not None:
            self.portfolio = restored
            return True
        return False

    def _save_snapshot(self, snap: SignalSnapshot, posture: ContestPosture) -> None:
        """Write a rich, human-readable snapshot for the public dashboard.

        Kept separate from the audit ledger: the ledger stays a lean, tamper-
        evident log, while this file carries the full ranked shortlist + posture
        the dashboard renders. Best-effort — never blocks a trading step.
        """
        try:
            top_syms = {x.symbol for x in snap.top(self.settings.signals.top_n)}
            ranked = [
                {
                    "symbol": x.symbol,
                    "composite": round(x.composite, 4),
                    "mom": round(x.mom_blended_return, 5),
                    "vol": round(x.realized_vol_annual, 4),
                    "atr_pct": round(x.atr_pct, 4),
                    "liquidity": round(x.liquidity_usd, 0),
                    "eligible": x.eligible,
                    "selected": x.symbol in top_syms,
                }
                for x in sorted(snap.signals, key=lambda r: r.composite, reverse=True)
                if x.lookback_returns
            ][:12]
            doc = {
                "ts": datetime.now(timezone.utc).isoformat(),
                "profile": self.settings.profile,
                "regime": snap.regime.label,
                "gross_scale": round(snap.regime.gross_scale, 3),
                "cost_bps": round(snap.cost_bps, 1),
                "fear_greed": snap.regime.fear_greed,
                "btc_dominance": snap.regime.btc_dominance,
                "posture": {
                    "phase": posture.phase,
                    "posture": posture.posture,
                    "elapsed_frac": round(posture.elapsed_frac, 4),
                    "time_left_frac": round(posture.time_left_frac, 4),
                    "drawdown_pct": round(posture.drawdown_pct, 3),
                    "drawdown_budget_left": round(posture.drawdown_budget_left, 4),
                    "our_return_pct": round(posture.our_return_pct, 3),
                    "halt_new_risk": posture.halt_new_risk,
                    "exposure_scale": round(posture.exposure_scale, 4),
                    "aggression_scale": round(posture.aggression_scale, 4),
                    "max_gross_pct": round(posture.max_gross_pct, 4),
                    "per_trade_risk_pct": round(posture.per_trade_risk_pct, 4),
                },
                "ranked": ranked,
            }
            tmp = self.snapshot_path.with_suffix(".tmp")
            tmp.write_text(json.dumps(doc, indent=2, default=str))
            tmp.replace(self.snapshot_path)
        except Exception:
            pass

    # ------------------------------------------------------------- one step
    def step(self, now: datetime | None = None, dry_run: bool = False) -> StepReport:
        now = now or datetime.now(timezone.utc)
        s = self.settings

        universe = list(tradeable_universe(
            use_curated=s.universe.use_curated_tradeable,
            extra=tuple(s.universe.extra_tradeable),
            exclude=tuple(s.universe.exclude),
        ))
        snap = self.engine.compute(universe)

        prices = {sig.symbol: sig.price for sig in snap.signals if sig.price > 0}
        for sym in list(self.portfolio.positions):       # ensure held marks exist
            if sym not in prices:
                q = self.market.get_quote(sym)
                if q.price > 0:
                    prices[sym] = q.price

        self.portfolio.roll_day(now, prices)
        self.portfolio.update_marks(prices)

        actions: list[Action] = []
        # Live scoring is from on-chain balances — reconcile (and optionally mark)
        # the book against what the wallet actually holds before sizing posture.
        if not dry_run:
            self._reconcile_onchain(prices, actions)

        posture = self.meta.assess(
            now=now,
            equity=self.portfolio.equity(prices),
            peak_equity=self.portfolio.peak_equity,
            initial_equity=self.portfolio.initial_equity,
            regime_gross_scale=snap.regime.gross_scale,
        )

        self.ledger.append("signal", {
            "profile": s.profile,
            "top": [(x.symbol, round(x.composite, 3)) for x in snap.top(s.signals.top_n)],
            "regime": snap.regime.label,
            "gross_scale": round(snap.regime.gross_scale, 3),
            "posture": posture.posture,
            "max_gross_pct": round(posture.max_gross_pct, 4),
        })
        self._save_snapshot(snap, posture)

        self._run_exits(prices, actions, dry_run)
        self._run_swing(snap, prices, actions, dry_run, now)
        if not posture.halt_new_risk and not self.sentinel.kill_switch_engaged():
            self._run_rotation(snap, prices, posture, actions, dry_run, now)
            self._run_entries(snap, prices, posture, actions, dry_run, now)
        self._ensure_min_trade(now, snap, prices, posture, actions, dry_run)

        summary = self.portfolio.summary(prices)
        self.ledger.append("mark", {
            "equity": round(summary["equity"], 4),
            "return_pct": round(summary["return_pct"], 3),
            "drawdown_pct": round(summary["drawdown_pct"], 3),
            "open_positions": summary["open_positions"],
            "trades_today": summary["trades_today"],
        })
        if not dry_run:
            self._save_state()

        return StepReport(
            ts=now.isoformat(),
            posture=posture,
            regime=snap.regime,
            top=[(x.symbol, round(x.composite, 3)) for x in snap.top(s.signals.top_n)],
            actions=actions,
            summary=summary,
        )

    # --------------------------------------------------------------- exits
    def _run_exits(self, prices, actions, dry_run) -> None:
        for ex in self.portfolio.exits_to_run(prices, self.settings.risk.trailing_stop):
            pos = self.portfolio.positions.get(ex.symbol)
            if not pos:
                continue
            dec = self.sentinel.pre_exit(symbol=ex.symbol, qty=ex.qty, held_qty=pos.qty)
            if not dec.approved:
                continue
            if dry_run:
                actions.append(Action("dry_run", ex.symbol, f"would exit ({ex.reason})"))
                continue
            order = Order(ex.symbol, "sell", ref_price=ex.ref_price, qty=ex.qty,
                          liquidity_usd=1e9, reason=ex.reason)
            fill = self.executor.execute(order)
            if not fill.ok or fill.qty <= 0:
                # Failed live exit: leave the position booked (it is still held
                # on-chain) and retry next cycle rather than log a phantom close.
                self.ledger.append("alert", {"reason": "exit_unfilled",
                                              "symbol": ex.symbol, "note": fill.note[:80]})
                actions.append(Action("blocked", ex.symbol, f"exit unfilled: {fill.note[:60]}"))
                continue
            realized = self.portfolio.apply_sell(fill)
            self.ledger.append("trade", {**asdict(fill), "reason": ex.reason,
                                          "realized_pnl": round(realized, 4)})
            actions.append(Action("exit", ex.symbol,
                                  f"{ex.reason} @ {fill.price:.4f} pnl {realized:+.2f}"))

    # ----------------------------------------------------------------- x402
    def _x402_ready(self) -> bool:
        """True only when a real, paid x402 data call should be attempted.

        Gated on live mode + the x402-on-buys policy + an executor that can pay
        (TWAK). In paper / backtest / dry-run this is always False, so the
        simulated book is never charged and parity with live is preserved.
        """
        s = self.settings
        return (
            s.is_live
            and s.execution.x402_on_buys
            and s.secrets.x402_enabled
            and hasattr(self.executor, "x402_request")
        )

    def _x402_prebuy(self, symbol: str, ref_price: float, now: datetime) -> float:
        """Pay-per-call (x402) for a fresh CMC quote right before a live buy.

        This is native x402 in the trade loop: the agent pays USDT on BSC for the
        market data that confirms its entry price. The call is hard-capped per UTC
        day (``x402_max_calls_per_day``) and per payment (``x402_max_payment_wei``)
        so the cost stays negligible on a small book, and any failure or budget
        exhaustion falls straight back to ``ref_price``. Never raises into the loop.

        Returns the execution price: the fresher x402 price when it parses within
        a sane band of the reference, else ``ref_price`` unchanged.
        """
        d = now.date()
        if self._x402_day != d:
            self._x402_day, self._x402_calls = d, 0
        if self._x402_calls >= max(0, self.settings.secrets.x402_max_calls_per_day):
            return ref_price
        try:
            url = self.market.cmc_x402_url("quotes", symbol=symbol)
            res = self.executor.x402_request(url)
        except Exception as e:  # payment plumbing must never break the trade loop
            self.ledger.append("x402", {"symbol": symbol, "ok": False,
                                        "note": f"{type(e).__name__}: {str(e)[:60]}"})
            return ref_price
        self._x402_calls += 1
        ok = bool(getattr(res, "ok", False))
        note = (getattr(res, "note", "") or "")[:80]
        fresh = self.market.parse_cmc_price(getattr(res, "data", None), symbol) if ok else None
        # Trust a fresh price only within +/-20% of our reference (guards against a
        # misparsed field ever moving the order to a nonsense level).
        use_price, used_fresh = ref_price, False
        if fresh is not None and ref_price > 0 and abs(fresh - ref_price) / ref_price <= 0.20:
            use_price, used_fresh = fresh, True
        self.ledger.append("x402", {
            "symbol": symbol, "ok": ok, "paid_call": self._x402_calls,
            "fresh_price": round(fresh, 6) if fresh else None,
            "used_fresh": used_fresh, "note": note,
        })
        return use_price
    # --------------------------------------------------------- manual swing
    def _run_swing(self, snap: SignalSnapshot, prices, actions, dry_run, now) -> None:
        """Operator-directed take-profit + dip rebuy (OFF by default).

        Lets a human fire a one-shot SELL of ``swing_symbol`` to cash via the
        ``HELM_SWING_CMD`` env var (``verb#token`` — ``sell`` / ``buy`` / ``off``),
        then auto-rebuys that name once it dips ``swing_rebuy_drop`` below the
        realized sell price — or, when the operator sets an explicit absolute
        target via ``HELM_SWING_REBUY_PX``, once price falls to that level. The
        manual ``buy`` command always wins if it fires first; otherwise the
        passive dip-rebuy is the automatic safety net. Idempotent on the token,
        so a routine restart never re-fires a stale command. While armed,
        ``_run_entries`` / ``_run_rotation`` are blocked from re-buying the name
        (the manual exit is not undone underneath the operator). Every guardrail
        — Sentinel, stops, the DQ floor, the drawdown taper, the kill-switch —
        still applies.
        """
        rc = self.settings.risk
        sym = (getattr(rc, "swing_symbol", "") or "").upper()
        if not getattr(rc, "swing_enabled", False) or not sym:
            return
        p = self.portfolio

        # One-shot command: act only on a token we have not consumed yet.
        cmd = (os.environ.get("HELM_SWING_CMD", "") or "").strip().lower()
        if cmd:
            verb, _, token = cmd.partition("#")
            verb, token = verb.strip(), token.strip()
            if token and token != p.swing_token:
                p.swing_token = token  # consume up-front: never retry-storm a bad command
                if verb == "sell":
                    self._swing_sell(sym, snap, prices, actions, dry_run, now)
                elif verb == "buy":
                    self._swing_buy(sym, snap, prices, actions, dry_run, now, "swing_manual_buy")
                elif verb == "off":
                    p.swing_armed = False
                    actions.append(Action("compliance", sym, "manual swing disarmed"))

        # Passive: while armed, rebuy once price has dipped to the rebuy trigger.
        # The trigger is the operator's absolute target (HELM_SWING_REBUY_PX) when
        # set to a positive number, else ``swing_rebuy_drop`` below the realized
        # sell price. This is the automatic safety net behind the manual buy.
        if p.swing_armed and p.swing_sell_px > 0:
            px = prices.get(sym)
            trigger = p.swing_sell_px * (1.0 - rc.swing_rebuy_drop)
            override = (os.environ.get("HELM_SWING_REBUY_PX", "") or "").strip()
            if override:
                try:
                    ov = float(override)
                    if ov > 0:
                        trigger = ov
                except ValueError:
                    pass  # garbage target falls back to the percentage default
            if px is not None and px > 0 and px <= trigger:
                self._swing_buy(sym, snap, prices, actions, dry_run, now, "swing_dip_rebuy")

    def _swing_sell(self, sym, snap, prices, actions, dry_run, now) -> None:
        """Liquidate the entire swing-symbol position to cash and arm the rebuy."""
        p = self.portfolio
        pos = p.positions.get(sym)
        if pos is None or pos.qty <= 0:
            actions.append(Action("blocked", sym, "manual swing sell: nothing held"))
            return
        ref = prices.get(sym, pos.avg_entry)
        dec = self.sentinel.pre_exit(symbol=sym, qty=pos.qty, held_qty=pos.qty)
        if not dec.approved:
            actions.append(Action("blocked", sym, "manual swing sell vetoed by sentinel"))
            return
        if dry_run:
            actions.append(Action("dry_run", sym, f"would manual-sell all @ {ref:.4f} + arm rebuy"))
            return
        order = Order(sym, "sell", ref_price=ref, qty=pos.qty,
                      liquidity_usd=1e9, reason="swing_sell")
        fill = self.executor.execute(order)
        if not fill.ok or fill.qty <= 0:
            self.ledger.append("alert", {"reason": "swing_sell_unfilled",
                                          "symbol": sym, "note": fill.note[:80]})
            actions.append(Action("blocked", sym, f"manual swing sell unfilled: {fill.note[:60]}"))
            return
        realized = p.apply_sell(fill)
        p.swing_armed = True
        p.swing_sell_px = fill.price
        drop = self.settings.risk.swing_rebuy_drop
        trigger = fill.price * (1.0 - drop)
        self.ledger.append("trade", {**asdict(fill), "reason": "swing_sell",
                                      "realized_pnl": round(realized, 4),
                                      "armed_rebuy_below": round(trigger, 6)})
        actions.append(Action("exit", sym,
            f"manual-sell all @ {fill.price:.4f} pnl {realized:+.2f}; armed rebuy < {trigger:.4f}"))

    def _swing_buy(self, sym, snap, prices, actions, dry_run, now, reason) -> None:
        """Deploy available cash back into the swing symbol and disarm."""
        p = self.portfolio
        s = self.settings
        if self.sentinel.kill_switch_engaged():
            actions.append(Action("blocked", sym, "swing rebuy held: kill-switch engaged"))
            return  # stay armed; retry once the circuit breaker clears
        reserve = max(s.risk.gas_usd_per_swap, 0.0)
        notional = p.cash - reserve
        if notional < s.contest.dust_floor_usd:
            actions.append(Action("blocked", sym, f"swing rebuy skipped: cash ${p.cash:.2f} too low"))
            return
        sig = next((x for x in snap.signals if x.symbol.upper() == sym), None)
        ref = (sig.price if sig else None) or prices.get(sym, 0.0)
        if not ref or ref <= 0:
            actions.append(Action("blocked", sym, "swing rebuy skipped: no price"))
            return
        atr = sig.atr if (sig and sig.atr and sig.atr > 0) else 0.0
        if dry_run:
            p.swing_armed = False
            actions.append(Action("dry_run", sym, f"would swing-rebuy ${notional:.2f} @ {ref:.4f}"))
            return
        exec_price = ref
        if self._x402_ready():
            exec_price = self._x402_prebuy(sym, ref, now)
        order = Order(sym, "buy", ref_price=exec_price, notional_usd=notional,
                      liquidity_usd=1e9, reason=reason)
        fill = self.executor.execute(order)
        if not fill.ok or fill.notional_usd <= 0 or fill.qty <= 0:
            # Stay armed and retry next cycle rather than book a phantom buy.
            self.ledger.append("alert", {"reason": reason + "_unfilled",
                                          "symbol": sym, "note": fill.note[:80]})
            actions.append(Action("blocked", sym, f"swing rebuy unfilled: {fill.note[:60]}"))
            return
        stop_dist = atr * s.risk.stop_loss_atr_mult if atr > 0 else fill.price * 0.06
        stop = max(0.0, fill.price - stop_dist)
        tp = fill.price + (atr * s.risk.take_profit_atr_mult if atr > 0 else fill.price * 0.10)
        p.apply_buy(fill, stop, tp, stop_dist)
        p.swing_armed = False
        self.ledger.append("trade", {**asdict(fill), "reason": reason,
                                      "stop": round(stop, 6), "tp": round(tp, 6)})
        actions.append(Action("entry", sym,
            f"swing-rebuy ${fill.notional_usd:.2f} @ {fill.price:.4f} ({reason})"))

    # ------------------------------------------------------------- rotation
    def _position_age_hours(self, pos: Position, now: datetime) -> float:
        """Hours since a position was opened; unparseable timestamps read as old."""
        try:
            ts = datetime.fromisoformat(pos.entry_ts)
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            return max(0.0, (now - ts).total_seconds() / 3600.0)
        except Exception:
            return 1e9

    def _run_rotation(self, snap: SignalSnapshot, prices, posture, actions, dry_run, now) -> None:
        """Recycle dead-weight capital into the engine's current leader.

        Momentum decays: a name bought days ago can drop out of the ranked
        shortlist while still holding most of the book, starving the live leader
        of capital (we are nearly fully invested, so ``_run_entries`` has no cash
        to deploy). This sells the worst such holding -- one the engine no longer
        ranks and that the leader dominates by a clear margin -- so the same
        cycle's entry step funds the leader. It fires only when we are either
        capital-constrained or sitting on a large stale chunk. Strict hysteresis
        (composite edge + min-hold + min size + one rotation per cycle) prevents
        thrashing or chasing noise. Stops/TP run first in ``_run_exits``; the DQ
        taper still governs how much of the freed cash gets redeployed.
        """
        s = self.settings
        rc = s.risk
        if not getattr(rc, "rotation_enabled", True) or not self.portfolio.positions:
            return
        ranked = snap.top(s.signals.top_n)
        if not ranked:
            return
        swing_block = (rc.swing_symbol or "").upper() if self.portfolio.swing_armed else ""
        if swing_block:
            # Don't let rotation steer freed capital into a name the operator is
            # deliberately holding in cash for a manual dip rebuy.
            ranked = [x for x in ranked if x.symbol.upper() != swing_block]
            if not ranked:
                return
        equity = self.portfolio.equity(prices)
        if equity <= 0:
            return
        top_syms = {x.symbol for x in ranked}

        # Leader = best ranked name we don't yet hold at/near its target weight.
        target_usd = rc.max_position_pct * equity
        leader = None
        for sig in ranked:
            held = self.portfolio.positions.get(sig.symbol)
            if held is None:
                leader = sig
                break
            held_val = held.qty * prices.get(sig.symbol, held.avg_entry)
            if held_val < target_usd * rc.rotation_topup_frac:
                leader = sig
                break
        if leader is None:
            return

        constrained = self.portfolio.cash < rc.rotation_cash_floor_usd
        comp = {x.symbol: x.composite for x in snap.signals}

        # Stale candidates: held, NOT ranked, beaten by the leader by >= the
        # hysteresis edge, old enough, and worth the gas. A candidate qualifies
        # when we're cash-constrained OR it is itself a large stale chunk.
        cands: list[tuple[float, float, str, Position]] = []
        for sym, pos in self.portfolio.positions.items():
            if sym in top_syms or sym == leader.symbol:
                continue
            held_val = pos.qty * prices.get(sym, pos.avg_entry)
            if held_val < rc.rotation_min_stale_usd:
                continue
            big = held_val >= rc.rotation_big_holding_frac * equity
            if not (constrained or big):
                continue
            if self._position_age_hours(pos, now) < rc.rotation_min_hold_hours:
                continue
            edge = leader.composite - comp.get(sym, -999.0)
            if edge < rc.rotation_min_edge:
                continue
            cands.append((edge, held_val, sym, pos))
        if not cands:
            return

        # Rotate the single strongest disagreement (largest edge) this cycle.
        cands.sort(key=lambda t: (t[0], t[1]), reverse=True)
        edge, held_val, sym, pos = cands[0]
        ref = prices.get(sym, pos.avg_entry)
        dec = self.sentinel.pre_exit(symbol=sym, qty=pos.qty, held_qty=pos.qty)
        if not dec.approved:
            return
        if dry_run:
            actions.append(Action("dry_run", sym,
                f"would rotate -> {leader.symbol} (edge {edge:.2f}, ${held_val:.2f})"))
            return
        order = Order(sym, "sell", ref_price=ref, qty=pos.qty,
                      liquidity_usd=1e9, reason="rotation")
        fill = self.executor.execute(order)
        if not fill.ok or fill.qty <= 0:
            self.ledger.append("alert", {"reason": "rotation_unfilled",
                                          "symbol": sym, "note": fill.note[:80]})
            actions.append(Action("blocked", sym, f"rotation unfilled: {fill.note[:60]}"))
            return
        realized = self.portfolio.apply_sell(fill)
        self.ledger.append("trade", {**asdict(fill), "reason": "rotation",
                                      "realized_pnl": round(realized, 4),
                                      "into": leader.symbol})
        actions.append(Action("exit", sym,
            f"rotate->{leader.symbol} @ {fill.price:.4f} pnl {realized:+.2f}"))

    # -------------------------------------------------------------- entries
    def _run_entries(self, snap: SignalSnapshot, prices, posture, actions, dry_run, now) -> None:
        s = self.settings
        equity = self.portfolio.equity(prices)
        gross_cap_usd = posture.max_gross_pct * equity
        start_gross = self.portfolio.gross_usd(prices)
        consumed = 0.0  # tracks gross taken this cycle (honest even in dry-run)
        # Small reserve so sizing never targets the exact cap (equity drifts as
        # we fill above mid); avoids off-by-epsilon rejections at the boundary.
        reserve = 0.01 * gross_cap_usd
        swing_block = (s.risk.swing_symbol or "").upper() if self.portfolio.swing_armed else ""
        for sig in snap.top(s.signals.top_n):
            if swing_block and sig.symbol.upper() == swing_block:
                # Manual swing has this name parked in cash awaiting its dip
                # rebuy; never let the normal entry loop re-buy it underneath us.
                continue
            held = self.portfolio.positions.get(sig.symbol)
            headroom = gross_cap_usd - start_gross - consumed - reserve
            # Never order past the USDT we actually hold. The endgame profile's
            # gross cap can exceed 1.0 to force full deployment of idle cash into
            # the leader; without this a single entry could size beyond our cash
            # and revert on-chain (burning gas). In live, cash is already debited
            # by prior fills this cycle; in dry-run we net the hypothetical spend.
            cash_on_hand = self.portfolio.cash - (consumed if dry_run else 0.0)
            headroom = min(headroom, cash_on_hand - max(reserve, s.risk.gas_usd_per_swap))
            if headroom < s.contest.dust_floor_usd:
                break
            if held is not None:
                # Already hold this ranked name: top it up toward its target
                # weight, never beyond max_position_pct. This is how the cash
                # freed by rotation pyramids into the leader (the leader is
                # usually already a position, and entries would otherwise only
                # ever OPEN new names). Skip when the remaining room is below the
                # gas-aware economic floor (Sentinel would reject it anyway) and
                # try the next ranked name rather than stopping the whole loop.
                held_val = held.qty * prices.get(sig.symbol, held.avg_entry)
                topup_room = s.risk.max_position_pct * equity - held_val
                gas_floor = (s.risk.gas_usd_per_swap / s.risk.gas_max_pct_of_notional
                             if s.risk.gas_max_pct_of_notional > 0 else 0.0)
                if topup_room < max(s.contest.dust_floor_usd, gas_floor):
                    continue
                headroom = min(headroom, topup_room)
            plan = plan_position(
                symbol=sig.symbol, price=sig.price, atr=sig.atr, equity=equity,
                per_trade_risk_pct=posture.per_trade_risk_pct,
                stop_atr_mult=s.risk.stop_loss_atr_mult,
                take_profit_atr_mult=s.risk.take_profit_atr_mult,
                max_position_pct=s.risk.max_position_pct,
                gross_headroom_usd=headroom,
                realized_vol_annual=sig.realized_vol_annual,
                target_vol_annual=s.risk.target_portfolio_vol_annual,
            )
            est_slip = model_slippage_bps(plan.notional_usd, sig.liquidity_usd,
                                          cap_bps=s.risk.slippage_bps_max)
            book = self.portfolio.book_state(prices, sig.symbol)
            security = None
            if hasattr(self.executor, "security_checks") and self.settings.is_live:
                security = self.executor.security_checks(
                    token_address=None, est_slippage_bps=est_slip, quote_ok=True,
                )
            dec = self.sentinel.pre_trade(
                symbol=sig.symbol, plan=plan, book=book, posture=posture,
                liquidity_usd=sig.liquidity_usd, est_slippage_bps=est_slip,
                security=security,
            )
            self.ledger.append("risk", {
                "symbol": sig.symbol, "approved": dec.approved,
                "notional": round(plan.notional_usd, 2),
                "binding": plan.binding_constraint,
                "failed": dec.failed_checks,
            })
            if not dec.approved:
                actions.append(Action("blocked", sig.symbol, dec.reason[:80]))
                continue
            if dry_run:
                actions.append(Action("dry_run", sig.symbol,
                                      f"would buy ${plan.notional_usd:.2f} ({plan.binding_constraint})"))
                consumed += plan.notional_usd
                continue
            # Native x402: pay-per-call for a fresh CMC quote that confirms the
            # entry price (live only, hard-capped; falls back to sig.price).
            exec_price = sig.price
            if self._x402_ready():
                exec_price = self._x402_prebuy(sig.symbol, sig.price, now)
            order = Order(sig.symbol, "buy", ref_price=exec_price,
                          notional_usd=plan.notional_usd, liquidity_usd=sig.liquidity_usd,
                          reason="entry")
            fill = self.executor.execute(order)
            if not fill.ok or fill.notional_usd <= 0 or fill.qty <= 0:
                # A failed/empty live swap must NOT book a phantom position or a
                # trade record — paper never fails here, so skipping keeps the
                # live ledger faithful to the simulated one.
                self.ledger.append("alert", {"reason": "entry_unfilled",
                                              "symbol": sig.symbol, "note": fill.note[:80]})
                actions.append(Action("blocked", sig.symbol, f"entry unfilled: {fill.note[:60]}"))
                continue
            self.portfolio.apply_buy(fill, plan.stop_price, plan.take_profit_price,
                                     plan.stop_distance)
            consumed += fill.notional_usd
            self.ledger.append("trade", {**asdict(fill), "reason": "entry",
                                          "stop": round(plan.stop_price, 6),
                                          "tp": round(plan.take_profit_price, 6)})
            actions.append(Action("entry", sig.symbol,
                                  f"buy ${fill.notional_usd:.2f} @ {fill.price:.4f}"))

    # ------------------------------------------------- min daily trade floor
    def _ensure_min_trade(self, now, snap, prices, posture, actions, dry_run) -> None:
        s = self.settings
        if not s.contest.enabled:
            return
        if self.portfolio.trades_today >= s.contest.min_trades_per_day:
            return
        # The manual kill-switch is an explicit human STOP — honor it even at the
        # cost of the floor. But the *automatic* drawdown halt (posture.halt_new_risk)
        # must NOT suppress this ping: a dust-sized compliance trade adds negligible
        # risk, whereas missing the >=1-trade/day floor is an instant, irreversible
        # disqualification. So at the halt line we still satisfy the trade-count rule
        # without adding real exposure — exactly what the dust ping does.
        if self.sentinel.kill_switch_engaged():
            return
        # Guarantee the floor early in the UTC day (default 18:00, not 23:59) so a
        # transient failure still leaves several hourly cycles of retry buffer
        # before the midnight disqualification deadline.
        if now.hour < s.contest.min_trade_deadline_hour:
            return
        # Smallest compliant maintenance trade in the most liquid eligible name.
        pool = snap.ranked or sorted(snap.signals, key=lambda x: x.liquidity_usd, reverse=True)
        cand = next((x for x in pool if x.price > 0 and x.liquidity_usd >= s.risk.min_liquidity_usd), None)
        if not cand:
            self.ledger.append("alert", {"reason": "min_daily_trade",
                                         "detail": "no eligible candidate (will retry next cycle)"})
            actions.append(Action("blocked", "—", "daily-floor: no eligible candidate"))
            return
        halted = bool(getattr(posture, "halt_new_risk", False))
        notional = max(s.contest.dust_floor_usd * 2, 0.0)
        if dry_run:
            tag = " [under halt]" if halted else ""
            actions.append(Action("compliance", cand.symbol, f"would ping ${notional:.2f}{tag}"))
            return
        order = Order(cand.symbol, "buy", ref_price=cand.price, notional_usd=notional,
                      liquidity_usd=cand.liquidity_usd, reason="min_daily_trade")
        # Retry the compliance ping — missing the >=1-trade/day floor is an instant
        # DQ, so a transient executor/RPC error must never let it slip silently.
        fill, err = self._execute_with_retry(order, s.contest.min_trade_retry_attempts)
        if fill is None:
            self.ledger.append("alert", {"reason": "min_daily_trade", "symbol": cand.symbol,
                                         "detail": f"all attempts failed: {err}"})
            actions.append(Action("blocked", cand.symbol,
                                  f"daily-floor ping FAILED ({err}) — retry next cycle"))
            return
        # Tight protective stop so the compliance ping carries minimal risk.
        stop = cand.price - (cand.atr * s.risk.stop_loss_atr_mult if cand.atr > 0 else cand.price * 0.06)
        tp = cand.price + (cand.atr * s.risk.take_profit_atr_mult if cand.atr > 0 else cand.price * 0.1)
        self.portfolio.apply_buy(fill, max(0.0, stop), tp,
                                 cand.atr * s.risk.stop_loss_atr_mult if cand.atr > 0 else cand.price * 0.06)
        self.ledger.append("trade", {**asdict(fill), "reason": "min_daily_trade",
                                     "under_halt": halted})
        tag = " [under halt]" if halted else ""
        actions.append(Action("compliance", cand.symbol, f"daily-floor ping ${fill.notional_usd:.2f}{tag}"))

    def _execute_with_retry(self, order, attempts: int):
        """Execute an order, retrying on exception or a non-filled result.

        Returns ``(fill, None)`` on success, or ``(None, last_error)`` if every
        attempt fails. Used for the contest-critical daily-floor ping so a
        transient live error cannot cost the >=1-trade/day floor.
        """
        last_err = "unknown"
        for _ in range(max(1, int(attempts))):
            try:
                fill = self.executor.execute(order)
                if fill is not None and fill.notional_usd > 0 and fill.price > 0:
                    return fill, None
                last_err = "no fill"
            except Exception as e:
                last_err = f"{type(e).__name__}: {e}"
        return None, last_err

    # ------------------------------------------------- on-chain reconciliation
    def _reconcile_onchain(self, prices, actions) -> None:
        """Reconcile the booked book against actual on-chain balances.

        The competition scores from on-chain holdings, so in live mode HELM marks
        itself the same way: drift between booked quantity and the wallet's real
        balance (gas spent, slippage vs. model, dust, partial fills) is logged,
        and — when ``scoring.mark_from_onchain`` — the book is corrected to chain.
        Best-effort: any failure degrades to the booked mark and never raises.
        """
        s = self.settings
        if not s.is_live or not s.scoring.mark_from_onchain:
            return
        try:
            from .data import onchain
        except Exception:
            return
        wallet = onchain.resolve_wallet(s)
        if not wallet:
            self.ledger.append("alert", {"reason": "onchain_reconcile",
                                         "detail": "no wallet address resolved"})
            return

        held = list(self.portfolio.positions)
        base = s.capital.base_currency
        try:
            holdings = onchain.wallet_holdings(s, wallet, [*held, base])
        except Exception as e:
            self.ledger.append("alert", {"reason": "onchain_reconcile",
                                         "detail": f"read failed: {type(e).__name__}"})
            return

        tol = max(0.0, s.scoring.onchain_drift_alert_pct) / 100.0
        drifts: list[dict] = []

        # Cash leg (base currency) → portfolio.cash.
        base_h = holdings.get(base)
        if base_h and base_h.ok:
            booked = self.portfolio.cash
            chain = base_h.units
            if booked <= 0 or abs(chain - booked) / max(abs(booked), 1e-9) > tol:
                drifts.append({"sym": base, "booked": round(booked, 4),
                               "chain": round(chain, 4)})
            if s.scoring.mark_from_onchain:
                self.portfolio.cash = chain

        # Position legs → position.qty.
        for sym in held:
            h = holdings.get(sym)
            pos = self.portfolio.positions.get(sym)
            if not pos or not h or not h.ok:
                continue
            booked = pos.qty
            chain = h.units
            if booked <= 0 or abs(chain - booked) / max(abs(booked), 1e-9) > tol:
                drifts.append({"sym": sym, "booked": round(booked, 8),
                               "chain": round(chain, 8)})
            if s.scoring.mark_from_onchain:
                if chain <= 1e-12:
                    del self.portfolio.positions[sym]
                else:
                    pos.qty = chain

        if drifts:
            self.ledger.append("reconcile", {"wallet": wallet[:10] + "…",
                                             "drifts": drifts[:8],
                                             "marked": s.scoring.mark_from_onchain})
            if actions is not None:
                actions.append(Action("reconcile", "on-chain",
                                      f"{len(drifts)} drift(s) vs chain"
                                      + (" — marked to chain" if s.scoring.mark_from_onchain else "")))

    # ----------------------------------------------------------------- loops
    def close(self) -> None:
        self.market.close()
