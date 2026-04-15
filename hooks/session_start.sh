#!/usr/bin/env bash
# Cyborg Score SessionStart hook.
#
# Reminds the user to re-run `/cyborgscore` when their score is stale (> 30
# days). Exits silently if:
#   - the user hasn't paired yet (no config)
#   - they've scanned recently
#   - they've explicitly disabled the hook
#
# Privacy-first: this hook does NOT run any scan and does NOT submit
# anything. Users can opt into auto-scan-local via AUTO_SCAN=true.

set -u

CONFIG_PATH="${HOME}/.config/cyborgscore/config.json"
STALE_AFTER_DAYS=30

# No config → user hasn't set up yet, stay silent
[ -f "$CONFIG_PATH" ] || exit 0

# Extract values without depending on jq
get_json() {
  python3 -c "
import json, sys
try:
    with open('${CONFIG_PATH}') as fh:
        d = json.load(fh)
    v = d.get('$1', '')
    print(v if v is not None else '', end='')
except Exception:
    pass
"
}

HOOK_DISABLED="$(get_json HOOK_DISABLED)"
if [ "$HOOK_DISABLED" = "true" ]; then
  exit 0
fi

LAST_SCAN_AT="$(get_json last_scan_at)"

# No last_scan_at yet (paired but never scanned)? Stay silent — don't
# pester people who just installed.
[ -z "$LAST_SCAN_AT" ] && exit 0

# Parse ISO8601 timestamp to epoch and compare. Pass value via stdin to
# avoid shell-escaping concerns with unusual config content.
NOW_TS="$(date +%s)"
LAST_TS="$(printf '%s' "$LAST_SCAN_AT" | python3 -c "
import sys
from datetime import datetime
try:
    raw = sys.stdin.read()
    ts = raw.rstrip('Z') + '+00:00'
    print(int(datetime.fromisoformat(ts).timestamp()))
except Exception:
    print(0)
")"

if [ "$LAST_TS" -eq 0 ]; then
  exit 0
fi

AGE_DAYS=$(( (NOW_TS - LAST_TS) / 86400 ))

if [ "$AGE_DAYS" -lt "$STALE_AFTER_DAYS" ]; then
  exit 0
fi

# Stale — print single-line reminder to stderr (visible in Claude Code status)
echo "💡 Your Cyborg Score is ${AGE_DAYS} days old. Run /cyborgscore to update your tier." >&2
exit 0
