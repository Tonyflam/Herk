"""Contest-Optimal Meta-Controller — HELM's edge.

Most agents optimize the *chart*: maximize expected return each bar. A trading
*tournament* is a different game. Borrowing from poker ICM (Independent Chip
Model): chips (equity) are not linear in prize equity. The optimal amount of
risk depends on **where you are in the tournament**, not just the signal.

This controller modulates HELM's risk by three contest variables:

  1. TIME LEFT      — build early (variance is cheap), lock in late.
  2. DRAWDOWN LEFT  — survival is paramount; a DQ (breaching the gate) is a zero.
                      As we approach the internal halt line, risk is cut hard and
                      convexly. This constraint dominates everything else.
  3. RANK POSTURE   — proxied by our own return vs. configured thresholds (and,
                      in live mode, by `twak compete status`):
                        • PROTECT_LEAD : big lead late  → shed variance.
                        • CATCH_UP     : behind late    → bounded extra variance
                                         to reach the money (never enough to risk
                                         the gate — survival still caps it).
                        • BUILD        : default        → grow the stack.

Output is a single, auditable risk budget: resolved gross cap + per-trade risk,
plus human-readable reasons the dashboard and ledger can display.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import datetime, timezone

from ..config import Settings


def _parse_utc(ts: str) -> datetime:
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except Exception:
        return datetime.now(timezone.utc)


@dataclass
class ContestPosture:
    phase: str                       # build | mid | endgame
    posture: str                     # build | protect_lead | catch_up | halt
    elapsed_frac: float
    time_left_frac: float
    drawdown_pct: float              # current drawdown from peak (%)
    drawdown_budget_left: float      # 1 - dd/halt, clamped [0,1]
    our_return_pct: float
    halt_new_risk: bool
    exposure_scale: float            # multiplier on gross exposure (incl. regime)
    aggression_scale: float          # multiplier on per-trade risk
    max_gross_pct: float             # resolved gross cap (fraction of equity)
    per_trade_risk_pct: float        # resolved per-trade risk (% of equity)
    reasons: list[str] = field(default_factory=list)


class MetaController:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.start = _parse_utc(settings.contest.start_utc)
        self.end = _parse_utc(settings.contest.end_utc)

    # ------------------------------------------------------------- phases
    def _elapsed_frac(self, now: datetime) -> float:
        span = (self.end - self.start).total_seconds()
        if span <= 0:
            return 1.0
        f = (now - self.start).total_seconds() / span
        return max(0.0, min(1.0, f))

    def _phase(self, elapsed: float) -> str:
        c = self.settings.contest
        if elapsed < c.build_phase_frac:
            return "build"
        if elapsed < c.endgame_phase_frac:
            return "mid"
        return "endgame"

    def _time_factor(self, elapsed: float) -> float:
        """Gentle base taper of aggression as the week elapses (1.0 → 0.6)."""
        c = self.settings.contest
        bf, ef = c.build_phase_frac, c.endgame_phase_frac
        if elapsed < bf:
            return 1.0
        if elapsed < ef:
            f = (elapsed - bf) / max(1e-9, (ef - bf))
            return 1.0 - 0.2 * f          # 1.0 → 0.8
        f = (elapsed - ef) / max(1e-9, (1.0 - ef))
        return 0.8 - 0.2 * min(1.0, f)    # 0.8 → 0.6

    # ----------------------------------------------------------- drawdown
    def _drawdown_factor(self, dd_pct: float) -> tuple[float, float, bool]:
        """Convex survival taper. Returns (factor, budget_left, halt)."""
        halt = max(1e-6, self.settings.contest.halt_drawdown_pct)
        used = dd_pct / halt
        budget_left = max(0.0, 1.0 - used)
        if dd_pct >= halt:
            return 0.0, 0.0, True
        # Convex: de-risk faster as we near the halt line.
        return budget_left ** 1.3, budget_left, False

    # ------------------------------------------------------------ posture
    def _catchup_risk_mult(
        self, elapsed: float, budget_left: float, external_rank: int | None
    ) -> float:
        """Codified endgame escalation — a pre-committed function of how late it
        is, how much survival budget remains, and rank urgency. No discretion.

        Returns the baseline catch-up multiplier when survival budget is below
        the escalation floor; otherwise ramps linearly toward the ceiling. The
        result is still multiplied by the convex drawdown taper in ``assess``, so
        escalation can never push risk through the halt line.
        """
        c = self.settings.contest
        base, ceil = c.catchup_risk_mult, c.catchup_max_risk_mult
        if budget_left < c.endgame_escalate_dd_budget_min:
            return base  # too little survival margin to escalate beyond baseline
        ef = c.endgame_phase_frac
        lateness = max(0.0, min(1.0, (elapsed - ef) / max(1e-9, 1.0 - ef)))
        floor = c.endgame_escalate_dd_budget_min
        budget_head = max(0.0, min(1.0, (budget_left - floor) / max(1e-9, 1.0 - floor)))
        urgency = 1.0
        if external_rank is not None and external_rank > 3:
            urgency = min(1.0, external_rank / 5.0)
        return base + (ceil - base) * lateness * budget_head * urgency

    def _posture(
        self, elapsed: float, ret_pct: float, external_rank: int | None,
        budget_left: float,
    ) -> tuple[str, float, float, list[str]]:
        """Returns (posture, gross_mult, risk_mult, reasons)."""
        c = self.settings.contest
        reasons: list[str] = []
        endgame = elapsed >= c.endgame_phase_frac

        leading = ret_pct >= c.protect_lead_return_pct
        if external_rank is not None:
            leading = leading or external_rank == 1

        behind = ret_pct <= c.catchup_behind_return_pct
        if external_rank is not None and external_rank > 3:
            behind = True

        if leading:
            if endgame:
                reasons.append(
                    f"PROTECT_LEAD: +{ret_pct:.1f}% in endgame → shed variance (×0.50)"
                )
                return "protect_lead", 0.50, 0.50, reasons
            reasons.append(f"PROTECT_LEAD: +{ret_pct:.1f}% lead → guard (×0.80/0.75)")
            return "protect_lead", 0.80, 0.75, reasons

        if behind and endgame:
            mult = self._catchup_risk_mult(elapsed, budget_left, external_rank)
            reasons.append(
                f"CATCH_UP: {ret_pct:+.1f}% behind late → codified escalation "
                f"risk ×{mult:.2f} (budget_left {budget_left*100:.0f}%)"
            )
            return "catch_up", 1.0, mult, reasons

        reasons.append(f"BUILD: {ret_pct:+.1f}% → grow the stack (×1.0)")
        return "build", 1.0, 1.0, reasons

    # ------------------------------------------------------- gross budget
    def _max_gross_base(self) -> float:
        """Resolved base gross-exposure cap (fraction of equity), env-overridable.

        ``HELM_MAX_GROSS`` lets the operator open or tighten the gross budget live
        — e.g. lean in to deploy idle cash while the field's leaders sit frozen in
        cash late, or pull back if the leader rolls over — without a code redeploy.
        It scales the SAME exposure pipeline: the convex survival taper, the
        endgame time-taper and the regime overlay still multiply it in ``assess``,
        and the per-name position cap + the trend-deploy ceiling still bound the
        actual fill, so it can NEVER push risk through the drawdown halt or the DQ
        gate. A missing or unparseable value falls back to the profile default.
        """
        base = float(self.settings.risk.max_gross_exposure)
        raw = (os.environ.get("HELM_MAX_GROSS", "") or "").strip()
        if raw:
            try:
                base = max(0.1, min(2.5, float(raw)))
            except ValueError:
                pass
        return base

    # ------------------------------------------------------------- assess
    def assess(
        self,
        *,
        now: datetime | None = None,
        equity: float,
        peak_equity: float,
        initial_equity: float,
        regime_gross_scale: float = 1.0,
        external_rank: int | None = None,
    ) -> ContestPosture:
        now = now or datetime.now(timezone.utc)
        elapsed = self._elapsed_frac(now)
        phase = self._phase(elapsed)

        peak = max(peak_equity, equity, 1e-9)
        dd_pct = max(0.0, (peak - equity) / peak * 100.0)
        ret_pct = ((equity - initial_equity) / initial_equity * 100.0) if initial_equity > 0 else 0.0

        dd_factor, budget_left, halt = self._drawdown_factor(dd_pct)
        time_factor = self._time_factor(elapsed)
        posture, gross_mult, risk_mult, reasons = self._posture(
            elapsed, ret_pct, external_rank, budget_left
        )

        if halt:
            posture = "halt"
            reasons = [f"HALT: drawdown {dd_pct:.1f}% ≥ halt line "
                       f"{self.settings.contest.halt_drawdown_pct:.0f}% → no new risk"]

        # Survival-gate the regime overlay: with full drawdown budget, only a
        # fraction of the regime de-risking cut applies (stay deployed through
        # fear spikes rather than de-risk into a V-recovery); as budget thins the
        # cut ramps to full strength. Validated by backtest/regime_ab.py: the
        # ungated overlay bled ~1.9% in a clean uptrend while helping in chop.
        gate_floor = self.settings.regime.overlay_dd_gate_floor
        regime_gate = gate_floor + (1.0 - gate_floor) * (1.0 - budget_left)
        gated_regime_scale = 1.0 - (1.0 - regime_gross_scale) * regime_gate

        # Compose. Survival (dd_factor) gates everything. Regime folds into gross.
        exposure_scale = dd_factor * time_factor * gross_mult * gated_regime_scale
        aggression_scale = dd_factor * risk_mult

        exposure_scale = max(0.0, min(1.0, exposure_scale))
        aggression_scale = max(0.0, min(1.5, aggression_scale))

        reasons.insert(0, f"phase={phase} elapsed={elapsed*100:.0f}% "
                          f"dd={dd_pct:.1f}% budget_left={budget_left*100:.0f}%")
        if gated_regime_scale < 1.0:
            reasons.append(
                f"regime gross ×{gated_regime_scale:.2f} folded in "
                f"(raw ×{regime_gross_scale:.2f}, dd-gate ×{regime_gate:.2f})"
            )

        return ContestPosture(
            phase=phase,
            posture=posture,
            elapsed_frac=elapsed,
            time_left_frac=1.0 - elapsed,
            drawdown_pct=dd_pct,
            drawdown_budget_left=budget_left,
            our_return_pct=ret_pct,
            halt_new_risk=halt,
            exposure_scale=exposure_scale,
            aggression_scale=aggression_scale,
            max_gross_pct=self._max_gross_base() * exposure_scale,
            per_trade_risk_pct=self.settings.risk.per_trade_risk_pct * aggression_scale,
            reasons=reasons,
        )
