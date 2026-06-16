"""Regime assessment: turn raw market context (Fear & Greed, BTC dominance,
derivatives funding) into a single gross-exposure scalar + human-readable
reasons. The meta-controller multiplies this with the contest posture.

Philosophy: regime never *picks* trades — it only decides how much risk the
book is allowed to carry right now. Fear shrinks the book; froth trims it.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..config import Settings
from ..data.market import Regime


@dataclass
class RegimeAssessment:
    label: str
    gross_scale: float
    fear_greed: int
    btc_dominance: float
    funding_annual: float | None
    reasons: list[str] = field(default_factory=list)
    sources: dict[str, str] = field(default_factory=dict)


def assess_regime(reg: Regime, settings: Settings) -> RegimeAssessment:
    rc = settings.regime
    reasons: list[str] = []
    fg = int(reg.fear_greed)

    if fg <= rc.fear_greed_risk_off:
        scale = rc.risk_off_gross_scale
        label = "risk_off"
        reasons.append(
            f"F&G {fg} ≤ {rc.fear_greed_risk_off} (extreme fear) → gross {scale:.2f}"
        )
    elif fg >= rc.fear_greed_risk_on:
        scale = 0.85
        label = "euphoria"
        reasons.append(f"F&G {fg} ≥ {rc.fear_greed_risk_on} (greed) → trim {scale:.2f}")
    else:
        lo, hi = rc.fear_greed_risk_off, rc.fear_greed_risk_on
        frac = (fg - lo) / max(1, (hi - lo))
        scale = rc.risk_off_gross_scale + frac * (1.0 - rc.risk_off_gross_scale)
        label = "neutral"
        reasons.append(f"F&G {fg} → graded gross {scale:.2f}")

    # High BTC dominance bleeds alts: soft trim on our alt-heavy book.
    if reg.btc_dominance >= 55.0:
        scale *= 0.92
        reasons.append(f"BTC dominance {reg.btc_dominance:.1f}% high → alt trim ×0.92")

    # Crowded perp longs (very positive funding) → trim into the crowd.
    if reg.funding_annual is not None and reg.funding_annual > 0.25:
        scale *= 0.90
        reasons.append(f"funding {reg.funding_annual:.2f}/yr hot → trim ×0.90")

    floor = rc.risk_off_gross_scale * 0.5
    scale = max(floor, min(1.0, scale))
    return RegimeAssessment(
        label=label,
        gross_scale=scale,
        fear_greed=fg,
        btc_dominance=reg.btc_dominance,
        funding_annual=reg.funding_annual,
        reasons=reasons,
        sources=dict(reg.sources),
    )
