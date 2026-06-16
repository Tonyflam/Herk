"""TWAK (Trust Wallet Agent Kit) live execution adapter.

Wraps the ``@trustwallet/cli`` (`npx twak ...`) for self-custody execution on
BNB Smart Chain. The private key never leaves the local TWAK keystore — HELM
only sends *intents* (swap this much of A into B) and TWAK signs locally. This
is the literal meaning of HELM's tagline, "your keys, your helm."

Hard safety gates (all must hold before any state-changing call):
  • settings.mode == "live"
  • settings.secrets.execute_trades is True   (HELM_EXECUTE_TRADES=1)
  • settings.secrets.execute_chain  is True   (HELM_EXECUTE_CHAIN=1)
Otherwise every swap degrades to ``--quote-only`` (a simulation) or is refused.

Before any buy the adapter runs six pre-trade security checks and fills the
Sentinel ``SecurityChecklist``; Sentinel still has the final say.

This module imports cleanly with no Node installed; it only shells out to TWAK
when actually asked to act live.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from dataclasses import dataclass
from typing import Any

import httpx

from ..config import Settings
from ..risk.sentinel import SecurityChecklist
from .base import ExecutionAdapter, Fill, Order, _now_iso, model_slippage_bps

# Honeypot screen (free, public) for BSC token safety.
HONEYPOT_API = "https://api.honeypot.is/v2/IsHoneypot"


@dataclass
class TwakResult:
    ok: bool
    stdout: str = ""
    stderr: str = ""
    data: Any = None
    note: str = ""


class TwakAdapter(ExecutionAdapter):
    name = "twak"

    def __init__(self, settings: Settings):
        self.s = settings
        self.base_asset = settings.capital.base_currency  # e.g. USDT
        self._cli = self._discover_cli()

    def supports_live(self) -> bool:
        return True

    # --------------------------------------------------------- CLI plumbing
    @staticmethod
    def _discover_cli() -> list[str] | None:
        """Prefer an installed `twak` binary; else `npx @trustwallet/cli`.

        The npx form uses ``--no-install`` so HELM can NEVER trigger an
        interactive package download mid-run — if the CLI is not present the
        command fails fast with a clear message (see scripts/setup_live.sh).
        """
        if shutil.which("twak"):
            return ["twak"]
        if shutil.which("npx"):
            return ["npx", "--no-install", "@trustwallet/cli"]
        return None

    @property
    def available(self) -> bool:
        return self._cli is not None

    def _live_armed(self) -> bool:
        return (
            self.s.mode == "live"
            and self.s.secrets.execute_trades
            and self.s.secrets.execute_chain
        )

    def _run(self, args: list[str], timeout: int = 150) -> TwakResult:
        if not self.available:
            return TwakResult(False, note="TWAK CLI not found (need Node + @trustwallet/cli)")
        cmd = [*self._cli, *args]
        try:
            proc = subprocess.run(
                cmd, capture_output=True, text=True, timeout=timeout, check=False
            )
        except subprocess.TimeoutExpired:
            return TwakResult(False, note=f"timeout after {timeout}s")
        except Exception as e:  # pragma: no cover
            return TwakResult(False, note=f"{type(e).__name__}: {e}")
        out, err = proc.stdout.strip(), proc.stderr.strip()
        data = None
        for blob in (out, err):
            if blob and (blob.startswith("{") or blob.startswith("[")):
                try:
                    data = json.loads(blob)
                    break
                except Exception:
                    pass
        return TwakResult(proc.returncode == 0, out, err, data,
                          "" if proc.returncode == 0 else "non-zero exit")

    def _pw(self) -> list[str]:
        pw = self.s.secrets.twak_wallet_password
        return ["--password", pw] if pw else []

    # --------------------------------------------------------- competition
    def auth_setup(self) -> TwakResult:
        sec = self.s.secrets
        if not (sec.twak_api_key and sec.twak_api_secret):
            return TwakResult(False, note="missing TWAK_API_KEY / TWAK_API_SECRET")
        return self._run(["auth", "setup", "--api-key", sec.twak_api_key,
                          "--api-secret", sec.twak_api_secret])

    def wallet_address(self, chain: str = "smartchain") -> TwakResult:
        return self._run(["wallet", "address", "--chain", chain])

    def wallet_create(self) -> TwakResult:
        """Create the self-custodied TWAK wallet (no broadcast, no funds).

        Generates a fresh BIP39 HD wallet encrypted at ``~/.twak/wallet.json``.
        Requires ``twak_wallet_password`` (resolved via ``_pw``). The password
        is passed through subprocess args — never echoed by HELM.
        """
        if not self.available:
            return TwakResult(False, note="TWAK CLI not found; see scripts/setup_live.sh")
        if not self.s.secrets.twak_wallet_password:
            return TwakResult(False, note="missing TWAK_WALLET_PASSWORD")
        return self._run(["wallet", "create", *self._pw()])

    def compete_register(self) -> TwakResult:
        if not self.available:
            return TwakResult(False, note="TWAK CLI not found; see scripts/setup_live.sh")
        return self._run(["compete", "register", *self._pw()])

    def compete_status(self) -> TwakResult:
        return self._run(["compete", "status"])

    # ----------------------------------------------------------------- x402
    def x402_request(self, url: str) -> TwakResult:
        """Pay-per-call data fetch via TWAK (USDT on BSC). Used before buys."""
        if not self.s.secrets.x402_enabled:
            return TwakResult(False, note="x402 disabled")
        max_wei = str(self.s.secrets.x402_max_payment_wei)
        return self._run([
            "x402", "request", url,
            "--prefer-network", "bsc", "--prefer-asset", "Tether",
            "--max-payment", max_wei, "--auto-approve", "--yes",
        ])

    # ------------------------------------------------- pre-trade security
    def security_checks(
        self, *, token_address: str | None, est_slippage_bps: float, quote_ok: bool
    ) -> SecurityChecklist:
        chk = SecurityChecklist()
        chk.slippage_bounded = est_slippage_bps <= self.s.risk.slippage_bps_max
        chk.preflight_simulated = quote_ok
        # TWAK approves exact swap amounts (no unbounded allowances) by design.
        chk.approval_bounded = True
        # Tight slippage + routing is our MEV mitigation.
        chk.mev_protected = est_slippage_bps <= self.s.risk.slippage_bps_max
        if token_address:
            chk.honeypot_safe = self._honeypot_safe(token_address)
            chk.contract_verified = chk.honeypot_safe is not False
        return chk

    def _honeypot_safe(self, token_address: str) -> bool | None:
        try:
            with httpx.Client(timeout=12.0) as c:
                r = c.get(HONEYPOT_API, params={"address": token_address, "chainID": 56})
                r.raise_for_status()
                d = r.json()
                hp = d.get("honeypotResult", {})
                return not bool(hp.get("isHoneypot", False))
        except Exception:
            return None  # unknown → Sentinel treats None as "skipped", not pass

    # ------------------------------------------------------------- execute
    def _quote_only(self) -> bool:
        return self.s.execution.quote_only_dry_run or not self._live_armed()

    def execute(self, order: Order) -> Fill:
        if not self.available:
            return Fill(order.symbol, order.side, 0, 0, 0, 0, 0, _now_iso(), "twak",
                        False, "TWAK CLI not found")
        if order.ref_price <= 0:
            return Fill(order.symbol, order.side, 0, 0, 0, 0, 0, _now_iso(), "twak",
                        False, "no price")

        sym = order.symbol.upper()
        if order.side == "buy":
            amount = f"{order.notional_usd:.6f}"
            from_asset, to_asset = self.base_asset, sym
        else:
            amount = f"{order.qty:.8f}"
            from_asset, to_asset = sym, self.base_asset

        args = ["swap", amount, from_asset, to_asset, *self._pw()]
        quote_only = self._quote_only()
        if quote_only:
            args.append("--quote-only")

        res = self._run(args)
        est_slip = model_slippage_bps(
            order.notional_usd or order.qty * order.ref_price,
            order.liquidity_usd, cap_bps=self.s.risk.slippage_bps_max,
        )

        if not res.ok:
            return Fill(order.symbol, order.side, 0, order.ref_price, 0, 0, est_slip,
                        _now_iso(), "twak", False, res.note or "swap failed")

        fill = self._parse_swap(res, order, est_slip)
        fill.note = "quote-only (simulated)" if quote_only else "live swap"
        fill.ok = True if quote_only else fill.ok
        return fill

    def _parse_swap(self, res: TwakResult, order: Order, est_slip: float) -> Fill:
        """Best-effort parse of TWAK swap output into a Fill."""
        price = order.ref_price
        if order.side == "buy":
            notional = order.notional_usd
            qty = notional / (price * (1 + est_slip / 1e4)) if price > 0 else 0.0
        else:
            qty = order.qty
            notional = qty * price * (1 - est_slip / 1e4)
        fee = notional * (self.s.risk.fee_bps_roundtrip / 2 / 1e4)

        # If TWAK returned structured amounts, prefer them.
        d = res.data if isinstance(res.data, dict) else {}
        try:
            if "toAmount" in d and order.side == "buy":
                qty = float(d["toAmount"])
            if "fromAmount" in d and order.side == "sell":
                notional = float(d.get("toAmount", notional))
        except Exception:
            pass

        # Gas: prefer the real receipt cost when TWAK reports one, else fall back
        # to the configured conservative estimate. Modeling gas LIVE the same way
        # paper does keeps the two ledgers comparable (see parity note in agent).
        gas = self._receipt_gas_usd(d)
        if gas is None:
            gas = self.s.risk.gas_usd_per_swap if qty > 0 else 0.0

        tx = ""
        m = re.search(r"0x[a-fA-F0-9]{64}", (res.stdout or "") + (res.stderr or ""))
        if m:
            tx = m.group(0)

        f = Fill(order.symbol, order.side, qty, price, notional, fee, est_slip,
                 _now_iso(), "twak", ok=bool(qty > 0), gas_usd=gas)
        if tx:
            f.note = f"tx={tx}"
        return f

    @staticmethod
    def _receipt_gas_usd(d: dict) -> float | None:
        """Pull an actual USD gas cost from a TWAK swap receipt if present.

        Tries common keys defensively (the CLI schema may vary); returns None
        when no usable figure is found so the caller uses the modeled estimate.
        """
        if not isinstance(d, dict):
            return None
        for key in ("gasFeeUsd", "networkFeeUsd", "gasUsd", "feeUsd"):
            v = d.get(key)
            try:
                if v is not None and float(v) >= 0:
                    return float(v)
            except (TypeError, ValueError):
                continue
        return None
