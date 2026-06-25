#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# docker-entrypoint.sh — reconstruct runtime secrets from env, then run the
# live watchdog. Used by the Railway / always-on container.
#
# Secrets are NEVER committed to the image. Railway injects them as env vars:
#   TWAK_CREDENTIALS_B64  base64 of ~/.twak/credentials.json   (API auth)
#   TWAK_WALLET_B64       base64 of ~/.twak/wallet.json        (encrypted wallet)
#   HELM_STATE_B64        base64 of data/runtime/state.json    (book continuity; first boot only)
#   + every HELM_*/TWAK_*/CMC_*/BNB_*/X402_* arming + credential var from .env
#
# These all-caps env vars are already in the process environment on Railway, so
# helm.config (load_dotenv) and run_live.sh pick them up without an .env file.
# ---------------------------------------------------------------------------
set -euo pipefail
cd "$(dirname "$0")"

umask 077
mkdir -p "$HOME/.twak" data/runtime

# --- TWAK auth + the self-custodied trading wallet (same address as local) ---
if [[ -n "${TWAK_CREDENTIALS_B64:-}" ]]; then
  echo "$TWAK_CREDENTIALS_B64" | base64 -d > "$HOME/.twak/credentials.json"
  chmod 600 "$HOME/.twak/credentials.json"
  echo "==> restored ~/.twak/credentials.json"
fi
if [[ -n "${TWAK_WALLET_B64:-}" ]]; then
  echo "$TWAK_WALLET_B64" | base64 -d > "$HOME/.twak/wallet.json"
  chmod 600 "$HOME/.twak/wallet.json"
  echo "==> restored ~/.twak/wallet.json (trading wallet keystore)"
fi

# --- book continuity: seed state.json ONCE (never clobber the live volume) ---
if [[ -n "${HELM_STATE_B64:-}" && ! -f data/runtime/state.json ]]; then
  echo "$HELM_STATE_B64" | base64 -d > data/runtime/state.json
  echo "==> seeded data/runtime/state.json from HELM_STATE_B64 (first boot)"
# --- one-time forced reseed for a local->cloud LIVE handoff. Overwrites a stale
#     (e.g. paper) volume state with the authoritative book exactly once. Guarded
#     by a sentinel ON THE VOLUME so it fires once even if the flag is left set —
#     subsequent restarts resume from the evolving live state, never the seed. ---
elif [[ "${HELM_STATE_FORCE_RESEED:-0}" == "1" && -n "${HELM_STATE_B64:-}" && ! -f data/runtime/.state_reseeded ]]; then
  echo "$HELM_STATE_B64" | base64 -d > data/runtime/state.json
  : > data/runtime/.state_reseeded
  echo "==> FORCE-reseeded data/runtime/state.json from HELM_STATE_B64 (one-time live handoff)"
fi

# --- sanity: do not silently run unarmed (would be a no-broadcast DQ) --------
echo "==> HELM container boot $(date -u +%Y-%m-%dT%H:%M:%SZ)"
echo "    mode=${HELM_MODE:-unset} adapter=${HELM_EXECUTION_ADAPTER:-unset} quote_only=${HELM_QUOTE_ONLY:-unset} profile=${HELM_PROFILE:-unset}"
if ! command -v twak >/dev/null 2>&1; then
  echo "!! twak CLI not found on PATH — live swaps will fail" >&2
fi

exec bash scripts/run_live.sh "${HELM_CONTEST_END:-2026-06-29T00:00:00Z}" "${HELM_INTERVAL:-3600}"
