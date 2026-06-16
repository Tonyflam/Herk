"""Sentinel: the deterministic pre-trade gate. Nothing trades unless Sentinel
approves — the LLM never reaches this code path.

Two responsibilities:
  1. Portfolio-risk checks (paper + live): kill-switch, halt, daily-loss limit,
     position slots, gross cap, per-position cap, liquidity, slippage, dust.
  2. On-chain security checklist (live only): honeypot, contract verification,
     slippage bound, approval bound, preflight simulation, MEV protection. The
     TWAK/DEX adapter fills these in; in paper mode they are reported as skipped.

Every verdict is returned as a list of explicit, human-readable checks so it can
be written to the audit ledger and shown on the dashboard.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

from ..config import Settings
from ..contest.meta_controller import ContestPosture
from .sizing import SizePlan

_EPS = 1e-6


@dataclass
class BookState:
    equity: float
    peak_equity: float
    day_start_equity: float
    gross_usd: float
    open_positions: int
    holds_symbol: bool = False


@dataclass
class Check:
    name: str
    passed: bool
    detail: str = ""


@dataclass
class SecurityChecklist:
    """On-chain pre-trade checks (populated by the live adapter)."""

    honeypot_safe: bool | None = None
    contract_verified: bool | None = None
    slippage_bounded: bool | None = None
    approval_bounded: bool | None = None
    preflight_simulated: bool | None = None
    mev_protected: bool | None = None

    def to_checks(self) -> list[Check]:
        labels = {
            "honeypot_safe": "sec:honeypot",
            "contract_verified": "sec:contract_verified",
            "slippage_bounded": "sec:slippage_bound",
            "approval_bounded": "sec:approval_bound",
            "preflight_simulated": "sec:preflight_sim",
            "mev_protected": "sec:mev_protection",
        }
        out: list[Check] = []
        for attr, label in labels.items():
            val = getattr(self, attr)
            if val is None:
                out.append(Check(label, True, "skipped (paper)"))
            else:
                out.append(Check(label, bool(val), "ok" if val else "FAILED"))
        return out


@dataclass
class RiskDecision:
    approved: bool
    checks: list[Check] = field(default_factory=list)
    plan: SizePlan | None = None
    reason: str = ""

    @property
    def failed_checks(self) -> list[str]:
        return [c.name for c in self.checks if not c.passed]


class Sentinel:
    def __init__(self, settings: Settings, kill_switch_path: str = "/tmp/helm.STOP"):
        self.settings = settings
        self.kill_switch_path = kill_switch_path

    def kill_switch_engaged(self) -> bool:
        return os.path.exists(self.kill_switch_path)

    # --------------------------------------------------------------- buys
    def pre_trade(
        self,
        *,
        symbol: str,
        plan: SizePlan,
        book: BookState,
        posture: ContestPosture,
        liquidity_usd: float,
        est_slippage_bps: float,
        security: SecurityChecklist | None = None,
    ) -> RiskDecision:
        r = self.settings.risk
        c = self.settings.contest
        checks: list[Check] = []

        ks = self.kill_switch_engaged()
        checks.append(Check("kill_switch", not ks,
                            f"touch {self.kill_switch_path} to halt" if not ks else "ENGAGED"))

        checks.append(Check("contest_halt", not posture.halt_new_risk,
                            posture.posture if not posture.halt_new_risk else "drawdown halt"))

        day_loss_pct = 0.0
        if book.day_start_equity > 0:
            day_loss_pct = (book.day_start_equity - book.equity) / book.day_start_equity * 100.0
        checks.append(Check("daily_loss_limit", day_loss_pct < r.daily_loss_limit_pct + _EPS,
                            f"day P&L {-day_loss_pct:+.2f}% vs limit -{r.daily_loss_limit_pct:.0f}%"))

        slot_ok = book.holds_symbol or book.open_positions < r.max_open_positions
        checks.append(Check("position_slots", slot_ok,
                            f"{book.open_positions}/{r.max_open_positions} open"))

        gross_cap_usd = posture.max_gross_pct * book.equity
        gross_after = book.gross_usd + (plan.notional_usd if plan else 0.0)
        checks.append(Check("gross_cap", gross_after <= gross_cap_usd + _EPS,
                            f"gross ${gross_after:,.2f} vs cap ${gross_cap_usd:,.2f}"))

        pos_ok = bool(plan) and plan.pct_of_equity <= r.max_position_pct + _EPS
        checks.append(Check("position_cap", pos_ok,
                            f"{(plan.pct_of_equity*100 if plan else 0):.1f}% vs {r.max_position_pct*100:.0f}%"))

        checks.append(Check("liquidity", liquidity_usd >= r.min_liquidity_usd,
                            f"${liquidity_usd:,.0f} vs min ${r.min_liquidity_usd:,.0f}"))

        checks.append(Check("slippage", est_slippage_bps <= r.slippage_bps_max + _EPS,
                            f"{est_slippage_bps:.0f}bps vs max {r.slippage_bps_max:.0f}bps"))

        notional = plan.notional_usd if plan else 0.0
        checks.append(Check("dust_floor", notional >= c.dust_floor_usd and bool(plan) and plan.ok,
                            f"notional ${notional:,.2f} vs dust ${c.dust_floor_usd:.2f}"))

        if security is not None:
            checks.extend(security.to_checks())

        approved = all(ch.passed for ch in checks)
        reason = "" if approved else "; ".join(
            f"{ch.name}: {ch.detail}" for ch in checks if not ch.passed
        )
        return RiskDecision(approved=approved, checks=checks, plan=plan, reason=reason)

    # -------------------------------------------------------------- exits
    def pre_exit(self, *, symbol: str, qty: float, held_qty: float) -> RiskDecision:
        """Exits reduce risk: always allowed (even under kill-switch/halt)."""
        checks = [
            Check("has_position", held_qty > 0, f"held {held_qty:g}"),
            Check("valid_qty", 0 < qty <= held_qty + _EPS, f"sell {qty:g} of {held_qty:g}"),
        ]
        approved = all(c.passed for c in checks)
        return RiskDecision(approved=approved, checks=checks,
                            reason="" if approved else "nothing to exit")
