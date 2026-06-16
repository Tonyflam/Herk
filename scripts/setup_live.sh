#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# setup_live.sh — prepare HELM for LIVE on-chain operation.
# Installs the Trust Wallet Agent Kit CLI + BNB AI Agent SDK, creates the
# self-custodied wallet, and prints the address to fund.
#
# NOTHING here broadcasts a trade. Live execution still requires all arming
# flags (HELM_MODE=live, HELM_EXECUTE_TRADES=1, HELM_EXECUTE_CHAIN=1).
# Secrets are read from .env — never passed on the command line by this script.
# ---------------------------------------------------------------------------
set -euo pipefail
cd "$(dirname "$0")/.."

echo "==> HELM live setup"

# 0) .env
if [[ ! -f .env ]]; then
  echo "==> creating .env from template (edit it, then re-run)"
  cp .env.example .env
  echo "    Fill in TWAK_API_KEY / TWAK_API_SECRET / TWAK_WALLET_PASSWORD"
  echo "    and (optional) CMC_API_KEY, BNB_AGENT_* — then re-run this script."
  exit 0
fi
set -a; # shellcheck disable=SC1091
source .env; set +a

# 1) python venv + live deps
if [[ ! -d .venv ]]; then python3 -m venv .venv; fi
# shellcheck disable=SC1091
source .venv/bin/activate
echo "==> installing core + live Python deps"
pip install -q -r requirements.txt
pip install -q -r requirements-live.txt   # bnbagent, web3, eth-account

# 2) Trust Wallet Agent Kit CLI (Node)
if ! command -v node >/dev/null 2>&1; then
  echo "!! Node.js is required for the TWAK CLI. Install Node 18+ and re-run."
  exit 1
fi
echo "==> installing @trustwallet/cli globally"
npm install -g @trustwallet/cli >/dev/null 2>&1 || \
  echo "   (global install skipped — will fall back to: npx --no-install @trustwallet/cli)"

twak() { command twak "$@" 2>/dev/null || npx --no-install @trustwallet/cli "$@"; }

# 3) TWAK auth + wallet (only if creds present)
if [[ -n "${TWAK_API_KEY:-}" && -n "${TWAK_API_SECRET:-}" ]]; then
  echo "==> configuring TWAK auth"
  twak auth setup --api-key "$TWAK_API_KEY" --api-secret "$TWAK_API_SECRET" || true
  if [[ -n "${TWAK_WALLET_PASSWORD:-}" ]]; then
    echo "==> creating/loading the self-custodied wallet"
    twak wallet create --password "$TWAK_WALLET_PASSWORD" || true
    echo "==> wallet address (fund this on BNB Smart Chain):"
    twak wallet address --chain smartchain || true
  else
    echo "   Set TWAK_WALLET_PASSWORD in .env to create the wallet."
  fi
else
  echo "   TWAK_API_KEY/SECRET not set — skipping wallet creation."
fi

# 4) BNB AI Agent SDK identity wallet (local keystore; gas-free on testnet)
if [[ -n "${BNB_AGENT_WALLET_PASSWORD:-}" ]]; then
  echo "==> preparing ERC-8004 identity wallet (no broadcast yet)"
  python -m helm.cli identity || true
fi

echo ""
echo "==> Setup complete. To ARM live trading, set in .env:"
echo "      HELM_MODE=live"
echo "      HELM_EXECUTE_TRADES=1"
echo "      HELM_EXECUTE_CHAIN=1"
echo "      HELM_PROFILE=aggressive   # contest posture (tuned for total return)"
echo "    Then:  python -m helm.cli register   # join the competition"
echo "           python -m helm.cli run --cycles 1000 --interval 900"
