#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# run_live.sh — process-level watchdog for the contest week.
#
# HELM's `run --until ... --supervise` loop already recovers from per-cycle
# data/RPC/exec errors in-process. This outer wrapper is belt-and-suspenders:
# if the Python process dies hard (OOM, kill, segfault, host blip) it is
# restarted automatically. State persists to data/runtime/state.json after every
# successful step, so a restart resumes from the last checkpoint with no loss and
# no double-trading.
#
# Usage:
#   scripts/run_live.sh [END_UTC] [INTERVAL_SECONDS]
#   scripts/run_live.sh 2026-06-29T00:00:00Z 3600
#   scripts/run_live.sh none 3600        # personal / always-on (no deadline)
#
# Live trading still requires the two arming flags (HELM_EXECUTE_TRADES=1 and
# HELM_EXECUTE_CHAIN=1) and HELM_MODE=live in .env — this script broadcasts
# nothing on its own. Run it inside tmux/screen or under systemd for the week.
# ---------------------------------------------------------------------------
set -uo pipefail
cd "$(dirname "$0")/.."

END_UTC="${1:-2026-06-29T00:00:00Z}"
# Personal / always-on mode: "none" / "never" / "forever" (case-insensitive)
# means "no deadline" — run indefinitely. We map it to a far-future timestamp so
# the existing deadline math below keeps working unchanged.
case "${END_UTC,,}" in
  none|never|forever) END_UTC="2099-01-01T00:00:00Z" ;;
esac
INTERVAL="${2:-3600}"
MAX_RESTARTS="${HELM_MAX_RESTARTS:-50}"
BACKOFF=10          # seconds, grows on repeated fast failures
MAX_BACKOFF=300

# venv
if [[ -d .venv ]]; then
  # shellcheck disable=SC1091
  source .venv/bin/activate
fi
# secrets (never echoed)
if [[ -f .env ]]; then
  set -a; # shellcheck disable=SC1091
  source .env; set +a
fi

now_epoch() { date -u +%s; }
end_epoch() { date -u -d "${END_UTC/Z/+00:00}" +%s 2>/dev/null || date -u -jf "%Y-%m-%dT%H:%M:%S%z" "${END_UTC/Z/+0000}" +%s; }

END_TS="$(end_epoch)"
echo "==> HELM live watchdog · until=${END_UTC} · interval=${INTERVAL}s · max-restarts=${MAX_RESTARTS}"

restarts=0
while :; do
  if [[ "$(now_epoch)" -ge "$END_TS" ]]; then
    echo "==> contest window closed (${END_UTC}); watchdog exiting."
    break
  fi
  if [[ "$restarts" -ge "$MAX_RESTARTS" ]]; then
    echo "!! reached MAX_RESTARTS=${MAX_RESTARTS}; giving up to avoid a crash loop." >&2
    exit 1
  fi

  started="$(now_epoch)"
  echo "==> [$(date -u +%H:%M:%SZ)] launching HELM (restart #${restarts})"
  python -m helm.cli run --until "$END_UTC" --supervise --interval "$INTERVAL"
  rc=$?

  if [[ "$rc" -eq 0 ]]; then
    echo "==> HELM exited cleanly (rc=0)."
    # Clean exit before the deadline means the loop reached --until; we're done.
    if [[ "$(now_epoch)" -ge "$END_TS" ]]; then break; fi
  fi

  ran=$(( $(now_epoch) - started ))
  restarts=$(( restarts + 1 ))
  # If it died quickly, back off (grows); if it ran a while, reset backoff.
  if [[ "$ran" -lt 60 ]]; then
    BACKOFF=$(( BACKOFF * 2 )); [[ "$BACKOFF" -gt "$MAX_BACKOFF" ]] && BACKOFF="$MAX_BACKOFF"
  else
    BACKOFF=10
  fi
  echo "!! HELM exited rc=${rc} after ${ran}s — restarting in ${BACKOFF}s" >&2
  sleep "$BACKOFF"
done
