#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# ensure_live.sh — idempotent guardian for the contest watchdog.
#
# Safe to call as often as you like (shell startup, cron, by hand). It launches
# the live watchdog ONLY when ALL of these hold:
#   * no watchdog is already running   (never double-trades)
#   * .env is armed for live           (HELM_MODE=live — never auto-arms paper)
#   * the kill switch is not set        (/tmp/helm.STOP absent)
#
# Purpose: a GitHub Codespace stops on idle / disconnect. Its *disk* (state.json,
# .env, ledger) and your *on-chain* wallet both survive. This script makes HELM
# resurrect itself from the last checkpoint the moment the Codespace is back, so
# a power/network blip on your side never leaves the bot down for long.
# ---------------------------------------------------------------------------
set -uo pipefail
cd "$(dirname "$0")/.." 2>/dev/null || exit 0

CONTEST_END="${HELM_CONTEST_END:-2026-06-29T00:00:00Z}"
INTERVAL="${HELM_INTERVAL:-3600}"
LOCK="data/runtime/.ensure_live.lock"

# Serialize concurrent invocations (two shells opening at once) so we can never
# spawn two watchdogs. flock auto-releases if this process dies. The launched
# child gets fd 9 closed (9>&-) so it never holds the lock past our exit.
mkdir -p data/runtime 2>/dev/null || true
exec 9>"$LOCK" 2>/dev/null || exit 0
flock -n 9 2>/dev/null || exit 0

# Already supervising? nothing to do.
if pgrep -f 'scripts/run_live.sh' >/dev/null 2>&1; then
  exit 0
fi

# Only resurrect a LIVE-armed bot. Never auto-launch in paper/disarmed.
if ! grep -qE '^HELM_MODE=live' .env 2>/dev/null; then
  exit 0
fi

# Respect the kill switch.
if [ -f /tmp/helm.STOP ]; then
  exit 0
fi

{
  echo ""
  echo "===== AUTO-RESUME $(date -u +%Y-%m-%dT%H:%M:%SZ) (ensure_live) ====="
} >> data/runtime/live_run.log 2>/dev/null || true

setsid bash -c "exec bash scripts/run_live.sh '$CONTEST_END' '$INTERVAL' >> data/runtime/live_run.log 2>&1" \
  < /dev/null 9>&- &
disown 2>/dev/null || true
exit 0
