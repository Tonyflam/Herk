#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# verify_offline.sh — prove HELM works end-to-end with ZERO credentials.
# Runs the test suite, a paper decision cycle, and a ledger integrity check.
# Safe to run anytime; touches nothing on-chain.
# ---------------------------------------------------------------------------
set -euo pipefail
cd "$(dirname "$0")/.."

echo "==> HELM offline verification (paper mode, no credentials)"

# 1) virtualenv
if [[ ! -d .venv ]]; then
  echo "==> creating .venv"
  python3 -m venv .venv
fi
# shellcheck disable=SC1091
source .venv/bin/activate

echo "==> installing core deps"
pip install -q -r requirements.txt
pip install -q -r requirements-dev.txt

echo "==> running unit tests (ledger, meta-controller, sizing, sentinel)"
pytest -q

echo "==> one-shot signal snapshot"
python -m helm.cli signal || true

echo "==> running 2 paper decision cycles"
python -m helm.cli run --cycles 2 --interval 0

echo "==> verifying the hash-chained audit ledger"
python -m helm.cli verify

echo ""
echo "==> OK — HELM is fully functional in paper mode with no keys."
echo "    Launch the dashboard with:  python -m helm.cli dashboard"
