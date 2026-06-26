"""Configuration loader: merges ``config/settings.yaml`` with ``.env`` overrides.

Typed, attribute-access config with safe defaults so HELM always boots — even
with an empty workspace and no credentials (paper mode).
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field, fields, is_dataclass
from pathlib import Path
from typing import Any

import yaml

try:  # optional; .env is convenience only
    from dotenv import load_dotenv

    load_dotenv()
except Exception:  # pragma: no cover
    pass


REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_SETTINGS_PATH = REPO_ROOT / "config" / "settings.yaml"


# --------------------------------------------------------------------------- #
# Typed sections                                                              #
# --------------------------------------------------------------------------- #
@dataclass
class CapitalCfg:
    base_currency: str = "USDT"
    initial_paper_equity_usd: float = 100.0


@dataclass
class ContestCfg:
    enabled: bool = True
    start_utc: str = "2026-06-22T12:00:00Z"
    end_utc: str = "2026-06-28T12:00:00Z"
    max_drawdown_pct: float = 30.0
    halt_drawdown_pct: float = 22.0
    min_trades_per_day: int = 1
    dust_floor_usd: float = 1.0
    # Daily-floor guarantee: from this UTC hour onward HELM will force the
    # >=1-trade/day compliance ping if nothing else has traded that day. Set
    # early (default 18:00, not 23:59) so a transient failure leaves several
    # hourly cycles of retry buffer before the midnight DQ deadline.
    min_trade_deadline_hour: int = 18
    min_trade_retry_attempts: int = 3
    build_phase_frac: float = 0.35
    endgame_phase_frac: float = 0.80
    protect_lead_return_pct: float = 20.0
    catchup_behind_return_pct: float = 2.0
    # Codified endgame escalation (pre-committed; the engine decides, not a human).
    # When behind late with survival budget intact, per-trade risk is escalated
    # along a graduated curve between these multipliers — never beyond the
    # drawdown taper that protects the gate.
    catchup_risk_mult: float = 1.25
    catchup_max_risk_mult: float = 1.5
    endgame_escalate_dd_budget_min: float = 0.5


@dataclass
class RiskCfg:
    target_portfolio_vol_annual: float = 0.45
    max_gross_exposure: float = 0.90
    max_position_pct: float = 0.20
    max_open_positions: int = 5
    daily_loss_limit_pct: float = 8.0
    per_trade_risk_pct: float = 1.5
    stop_loss_atr_mult: float = 2.5
    take_profit_atr_mult: float = 4.0
    trailing_stop: bool = True
    slippage_bps_max: float = 150.0
    min_liquidity_usd: float = 250_000.0
    fee_bps_roundtrip: float = 60.0
    # Flat BSC network gas charged per swap (USD), modeled in BOTH paper and live
    # so the simulator is never blind to the one cost that always hits live and
    # never hits a naive backtest. ~150k gas x ~1-3 gwei x BNB price ~= $0.10-0.30;
    # the default is set at the conservative end so paper, if anything, runs
    # PESSIMISTIC vs live (live should beat the sim, never disappoint). The live
    # adapter overrides this with the actual receipt gas when it can read one.
    gas_usd_per_swap: float = 0.30
    # A single swap's gas must stay below this fraction of its notional, else the
    # trade is rejected as uneconomic. Makes the dust floor gas-aware: it auto-
    # scales up if gas rises, and is inert on a properly-funded book. Protects a
    # small book from death-by-gas (a $1 trade paying $0.30 gas loses 30%).
    gas_max_pct_of_notional: float = 0.015
    # --- Leader rotation (capital recycling) ------------------------------
    # Momentum decays: a name bought days ago can fall out of the ranked
    # shortlist while still holding most of the book, starving the current
    # leader of fresh capital (we are nearly fully invested, so entries have no
    # cash). Rotation sells such dead weight into the leader, with strict
    # hysteresis so it never thrashes. Stops, DQ taper, Sentinel and the
    # min-hold guard all still apply.
    rotation_enabled: bool = True
    rotation_min_edge: float = 0.40          # leader composite must beat the stale name by >= this
    rotation_min_hold_hours: float = 4.0     # never rotate a position younger than this
    rotation_cash_floor_usd: float = 15.0    # "capital-constrained" trigger: free cash below this
    rotation_big_holding_frac: float = 0.45  # OR the stale name is >= this fraction of equity
    rotation_min_stale_usd: float = 10.0     # never rotate a holding smaller than this (gas-inefficient)
    rotation_topup_frac: float = 0.85        # leader is "underfunded" if held < this x its max target
    # --- Manual swing control (operator-directed take-profit + dip rebuy) ---
    # OFF by default. When enabled, the operator can fire a one-shot SELL of
    # ``swing_symbol`` to cash via the HELM_SWING_CMD env var (verb#token, e.g.
    # ``sell#1``); the agent then "arms" and auto-rebuys the same name once it
    # dips ``swing_rebuy_drop`` below the realized sell price. While armed the
    # normal entry/rotation logic is blocked from re-buying that name (so the
    # manual exit is not instantly undone). Every guardrail (stops, DQ floor,
    # drawdown taper, Sentinel) still applies to the rebuy.
    swing_enabled: bool = False
    swing_symbol: str = ""                    # name under manual swing control (e.g. "AAVE")
    swing_rebuy_drop: float = 0.02           # rebuy once price <= sell_px * (1 - this)


@dataclass
class SignalsCfg:
    lookbacks_hours: list[int] = field(default_factory=lambda: [6, 24, 72, 168])
    momentum_weights: list[float] = field(default_factory=lambda: [0.15, 0.35, 0.30, 0.20])
    min_composite_score: float = 0.15
    net_of_cost_gate: bool = True
    rebalance_interval_min: int = 60
    top_n: int = 3


@dataclass
class RegimeCfg:
    fear_greed_risk_off: int = 25
    fear_greed_risk_on: int = 78
    btc_dominance_trend_lookback_h: int = 72
    use_derivatives_funding: bool = True
    risk_off_gross_scale: float = 0.35
    # Survival-gated regime overlay: with full drawdown budget only this fraction
    # of the regime de-risking cut is applied (stay deployed through fear spikes
    # rather than de-risk into a V-recovery); as budget thins the cut ramps to
    # full strength, reinforcing survival. 1.0 disables the gate (always full cut).
    overlay_dd_gate_floor: float = 0.35


@dataclass
class DataCfg:
    provider_priority: list[str] = field(
        default_factory=lambda: ["twak_x402", "cmc_mcp", "cmc_rest", "public"]
    )
    cache_ttl_sec: int = 45
    candles_source: str = "public"


@dataclass
class UniverseCfg:
    use_curated_tradeable: bool = True
    extra_tradeable: list[str] = field(default_factory=list)
    exclude: list[str] = field(default_factory=list)


@dataclass
class ExecutionCfg:
    adapter: str = "paper"
    dex: str = "pancakeswap"
    chain_id: int = 56
    x402_on_buys: bool = True
    quote_only_dry_run: bool = True
    # Redundant BSC RPC endpoints (failover order). Empty = use the built-in
    # public defaults in helm/data/rpc.py. A single RPC going down must never
    # blind the agent during the live week.
    bsc_rpc_urls: list[str] = field(default_factory=list)
    # Optional wallet address override for on-chain balance reads. Empty = resolve
    # from the saved ERC-8004 identity (data/runtime/identity.json) or TWAK.
    wallet_address: str = ""
    # Symbol -> BEP-20 contract address overrides for on-chain marking. The
    # built-in registry (helm/data/onchain.py) covers the high-confidence cash
    # leg + blue chips; supply the rest here (authoritative, e.g. from TWAK).
    token_addresses: dict = field(default_factory=dict)


@dataclass
class IdentityCfg:
    register_erc8004: bool = False
    network: str = "bsc-testnet"


@dataclass
class ScoringCfg:
    """Competition scoring assumptions HELM encodes. CONFIRM these against the
    official BNB Hack rules and set ``confirmed: true`` once verified — preflight
    warns until then. The live week is scored from on-chain balances, so HELM
    marks from chain (see ScoringCfg.mark_from_onchain) to match the judges' view.
    """

    # An hour is scored 0% if the wallet holds less than this in eligible value.
    min_hold_usd_per_hour: float = 1.0
    # Only holdings in the eligible set count; never park value out of scope.
    must_hold_in_scope: bool = True
    # In live mode, mark equity from actual on-chain balances (score-truthful)
    # rather than HELM's booked quantity.
    mark_from_onchain: bool = True
    # Log a ledger alert when booked vs on-chain holdings drift beyond this.
    onchain_drift_alert_pct: float = 2.0
    # Optional link to the official rules; user confirms then flips ``confirmed``.
    rules_url: str = ""
    confirmed: bool = False


@dataclass
class DashboardCfg:
    host: str = "127.0.0.1"
    port: int = 8600


@dataclass
class Secrets:
    """Loaded from environment only (never from yaml, never logged)."""

    cmc_api_key: str = ""
    twak_api_key: str = ""
    twak_api_secret: str = ""
    twak_wallet_password: str = ""
    bnb_agent_private_key: str = ""
    bnb_agent_wallet_password: str = ""
    bnb_agent_network: str = "bsc-testnet"
    x402_max_payment_wei: int = 20_000_000_000_000_000
    x402_enabled: bool = True
    # Hard cap on PAID x402 data calls per UTC day. On a small book each call
    # (~$0.01 USDT on BSC) is a real cost, so this bounds the drag while still
    # exercising native x402 in the live trade loop. 0 disables paid x402.
    x402_max_calls_per_day: int = 6
    execute_trades: bool = False
    execute_chain: bool = False

    def redacted(self) -> dict[str, str]:
        """Presence map for safe logging (never reveals values)."""
        def mark(v: Any) -> str:
            return "set" if v else "—"

        return {
            "cmc_api_key": mark(self.cmc_api_key),
            "twak_api_key": mark(self.twak_api_key),
            "twak_api_secret": mark(self.twak_api_secret),
            "twak_wallet_password": mark(self.twak_wallet_password),
            "bnb_agent_private_key": mark(self.bnb_agent_private_key),
            "execute_trades": str(self.execute_trades),
            "execute_chain": str(self.execute_chain),
        }


@dataclass
class Settings:
    mode: str = "paper"
    profile: str = "balanced"
    capital: CapitalCfg = field(default_factory=CapitalCfg)
    contest: ContestCfg = field(default_factory=ContestCfg)
    risk: RiskCfg = field(default_factory=RiskCfg)
    signals: SignalsCfg = field(default_factory=SignalsCfg)
    regime: RegimeCfg = field(default_factory=RegimeCfg)
    data: DataCfg = field(default_factory=DataCfg)
    universe: UniverseCfg = field(default_factory=UniverseCfg)
    execution: ExecutionCfg = field(default_factory=ExecutionCfg)
    identity: IdentityCfg = field(default_factory=IdentityCfg)
    dashboard: DashboardCfg = field(default_factory=DashboardCfg)
    scoring: ScoringCfg = field(default_factory=ScoringCfg)
    secrets: Secrets = field(default_factory=Secrets)

    @property
    def is_live(self) -> bool:
        """True only when explicitly in live mode AND both safety flags set."""
        return (
            self.mode == "live"
            and self.secrets.execute_trades
            and self.execution.adapter == "twak"
        )


# --------------------------------------------------------------------------- #
# Loading helpers                                                             #
# --------------------------------------------------------------------------- #
def _coerce(type_str: Any, val: Any) -> Any:
    """Coerce a YAML scalar to the dataclass field's declared primitive type.

    Guards against formatting slips (e.g. a missing space before a ``#`` comment
    turning ``2.0`` into the string ``"2.0# ..."``) ever reaching the trade loop.
    """
    t = type_str if isinstance(type_str, str) else getattr(type_str, "__name__", "")
    try:
        if t == "int" and not isinstance(val, bool):
            return int(float(val))
        if t == "float":
            return float(val)
        if t == "bool":
            if isinstance(val, str):
                return val.strip().lower() in {"1", "true", "yes", "on"}
            return bool(val)
    except (ValueError, TypeError):
        return val
    return val


def _build(dc_type: type, data: dict[str, Any] | None) -> Any:
    """Instantiate a dataclass from a dict, ignoring unknown keys."""
    data = data or {}
    kwargs: dict[str, Any] = {}
    for f in fields(dc_type):
        if f.name not in data:
            continue
        val = data[f.name]
        if is_dataclass(f.type) and isinstance(val, dict):
            kwargs[f.name] = _build(f.type, val)  # type: ignore[arg-type]
        else:
            kwargs[f.name] = _coerce(f.type, val)
    return dc_type(**kwargs)


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _deep_merge(base: dict[str, Any], over: dict[str, Any]) -> dict[str, Any]:
    """Recursively overlay ``over`` onto ``base`` (returns a new dict)."""
    out = dict(base)
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def _apply_profile(raw: dict[str, Any]) -> tuple[dict[str, Any], str]:
    """Overlay the selected risk profile's overrides onto the base config.

    The base ``settings.yaml`` holds survival-first defaults (profile=balanced).
    A ``profiles:`` block can define named overrides (e.g. ``aggressive`` tuned
    for the contest's pure-total-return objective). The active profile is chosen
    by ``HELM_PROFILE`` env, else the top-level ``profile:`` key, else balanced.
    Only the risk/regime/signals/contest knobs differ between profiles — the
    guardrails (kill-switch, Sentinel, ledger, DQ gate) are always enforced.
    """
    profiles = raw.get("profiles") or {}
    active = os.getenv("HELM_PROFILE") or raw.get("profile") or "balanced"
    active = str(active).strip().lower()
    overrides = profiles.get(active)
    merged = dict(raw)
    merged.pop("profiles", None)
    if overrides:
        merged = _deep_merge(merged, overrides)
    merged["profile"] = active
    return merged, active


def _load_secrets() -> Secrets:
    return Secrets(
        cmc_api_key=os.getenv("CMC_API_KEY", ""),
        twak_api_key=os.getenv("TWAK_API_KEY", ""),
        twak_api_secret=os.getenv("TWAK_API_SECRET", ""),
        twak_wallet_password=os.getenv("TWAK_WALLET_PASSWORD", ""),
        bnb_agent_private_key=os.getenv("BNB_AGENT_PRIVATE_KEY", ""),
        bnb_agent_wallet_password=os.getenv("BNB_AGENT_WALLET_PASSWORD", ""),
        bnb_agent_network=os.getenv("BNB_AGENT_NETWORK", "bsc-testnet"),
        x402_max_payment_wei=int(os.getenv("X402_MAX_PAYMENT_WEI", "20000000000000000")),
        x402_enabled=_env_bool("X402_ENABLED", True),
        x402_max_calls_per_day=int(os.getenv("X402_MAX_CALLS_PER_DAY", "6")),
        execute_trades=_env_bool("HELM_EXECUTE_TRADES", False),
        execute_chain=_env_bool("HELM_EXECUTE_CHAIN", False),
    )


def load_settings(path: str | Path | None = None) -> Settings:
    """Load settings from yaml, layer env overrides, attach secrets."""
    p = Path(path) if path else DEFAULT_SETTINGS_PATH
    raw: dict[str, Any] = {}
    if p.exists():
        raw = yaml.safe_load(p.read_text()) or {}

    raw, active_profile = _apply_profile(raw)

    s = Settings(
        mode=raw.get("mode", "paper"),
        profile=active_profile,
        capital=_build(CapitalCfg, raw.get("capital")),
        contest=_build(ContestCfg, raw.get("contest")),
        risk=_build(RiskCfg, raw.get("risk")),
        signals=_build(SignalsCfg, raw.get("signals")),
        regime=_build(RegimeCfg, raw.get("regime")),
        data=_build(DataCfg, raw.get("data")),
        universe=_build(UniverseCfg, raw.get("universe")),
        execution=_build(ExecutionCfg, raw.get("execution")),
        identity=_build(IdentityCfg, raw.get("identity")),
        dashboard=_build(DashboardCfg, raw.get("dashboard")),
        secrets=_load_secrets(),
    )

    # Env overrides for the few knobs that flip behavior.
    s.mode = os.getenv("HELM_MODE", s.mode)
    if os.getenv("HELM_EXECUTION_ADAPTER"):
        s.execution.adapter = os.environ["HELM_EXECUTION_ADAPTER"]
    if os.getenv("HELM_WALLET_ADDRESS"):
        s.execution.wallet_address = os.environ["HELM_WALLET_ADDRESS"].strip()
    if os.getenv("HELM_QUOTE_ONLY") is not None:
        # Final broadcast gate. Default (unset) keeps the settings.yaml value.
        # Set HELM_QUOTE_ONLY=0 at arming so live swaps actually broadcast.
        s.execution.quote_only_dry_run = _env_bool(
            "HELM_QUOTE_ONLY", s.execution.quote_only_dry_run)
    return s
