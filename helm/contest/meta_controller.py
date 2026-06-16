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
    def _posture(
        self, elapsed: float, ret_pct: float, external_rank: int | None
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
            reasons.append(
                f"CATCH_UP: {ret_pct:+.1f}% behind late → bounded variance (risk ×1.25)"
            )
            return "catch_up", 1.0, 1.25, reasons

        reasons.append(f"BUILD: {ret_pct:+.1f}% → grow the stack (×1.0)")
        return "build", 1.0, 1.0, reasons

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
        posture, gross_mult, risk_mult, reasons = self._posture(elapsed, ret_pct, external_rank)

        if halt:
            posture = "halt"
            reasons = [f"HALT: drawdown {dd_pct:.1f}% ≥ halt line "
                       f"{self.settings.contest.halt_drawdown_pct:.0f}% → no new risk"]

        # Compose. Survival (dd_factor) gates everything. Regime folds into gross.
        exposure_scale = dd_factor * time_factor * gross_mult * regime_gross_scale
        aggression_scale = dd_factor * risk_mult

        exposure_scale = max(0.0, min(1.0, exposure_scale))
        aggression_scale = max(0.0, min(1.5, aggression_scale))

        reasons.insert(0, f"phase={phase} elapsed={elapsed*100:.0f}% "
                          f"dd={dd_pct:.1f}% budget_left={budget_left*100:.0f}%")
        if regime_gross_scale < 1.0:
            reasons.append(f"regime gross ×{regime_gross_scale:.2f} folded in")

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
            max_gross_pct=self.settings.risk.max_gross_exposure * exposure_scale,
            per_trade_risk_pct=self.settings.risk.per_trade_risk_pct * aggression_scale,
            reasons=reasons,
        )
