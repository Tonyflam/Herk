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

        posture = self.meta.assess(
            now=now,
            equity=self.portfolio.equity(prices),
            peak_equity=self.portfolio.peak_equity,
            initial_equity=self.portfolio.initial_equity,
            regime_gross_scale=snap.regime.gross_scale,
        )

        actions: list[Action] = []
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
        if not posture.halt_new_risk and not self.sentinel.kill_switch_engaged():
            self._run_entries(snap, prices, posture, actions, dry_run)
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
            realized = self.portfolio.apply_sell(fill)
            self.ledger.append("trade", {**asdict(fill), "reason": ex.reason,
                                          "realized_pnl": round(realized, 4)})
            actions.append(Action("exit", ex.symbol,
                                  f"{ex.reason} @ {fill.price:.4f} pnl {realized:+.2f}"))

    # -------------------------------------------------------------- entries
    def _run_entries(self, snap: SignalSnapshot, prices, posture, actions, dry_run) -> None:
        s = self.settings
        equity = self.portfolio.equity(prices)
        gross_cap_usd = posture.max_gross_pct * equity
        start_gross = self.portfolio.gross_usd(prices)
        consumed = 0.0  # tracks gross taken this cycle (honest even in dry-run)
        # Small reserve so sizing never targets the exact cap (equity drifts as
        # we fill above mid); avoids off-by-epsilon rejections at the boundary.
        reserve = 0.01 * gross_cap_usd
        for sig in snap.top(s.signals.top_n):
            if sig.symbol in self.portfolio.positions:
                continue
            headroom = gross_cap_usd - start_gross - consumed - reserve
            if headroom < s.contest.dust_floor_usd:
                break
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
            order = Order(sig.symbol, "buy", ref_price=sig.price,
                          notional_usd=plan.notional_usd, liquidity_usd=sig.liquidity_usd,
                          reason="entry")
            fill = self.executor.execute(order)
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
        if not s.contest.enabled or posture.halt_new_risk:
            return
        if self.portfolio.trades_today >= s.contest.min_trades_per_day:
            return
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
        notional = max(s.contest.dust_floor_usd * 2, 0.0)
        if dry_run:
            actions.append(Action("compliance", cand.symbol, f"would ping ${notional:.2f}"))
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
        self.ledger.append("trade", {**asdict(fill), "reason": "min_daily_trade"})
        actions.append(Action("compliance", cand.symbol, f"daily-floor ping ${fill.notional_usd:.2f}"))

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

    # ----------------------------------------------------------------- loops
    def close(self) -> None:
        self.market.close()
