#!/usr/bin/env bash
# Bring Annex back to a clean demo state (see docs/demo.md):
#   1. platform up with extras (Metabase, Dozzle), gateway views re-applied
#   2. every app except client-lookup and monthly-report removed (incl. vibe-dashboard,
#      which is built live in the demo), with its data
#   3. client-lookup redeployed; monthly-report removed and redeployed with empty
#      history, then run once so its page shows a report
#   4. SSH tunnel up, checks run (test-platform.sh, test-apps.sh)
# Doesn't touch secrets, demo passwords, backups or anything outside Annex.
# Takes 3-5 minutes.
#
# Usage: scripts/reset-demo.sh [--yes] [--keep <app>]... [--keep-history] [--no-check]
#   --keep <app>      also keep this app (e.g. a pre-built vibe-dashboard as a fallback)
#   --keep-history    keep monthly-report's run history and CSVs (only redeploy it)
#   --no-check        skip the test suites at the end
source "$(dirname "$0")/lib.sh"
ANNEX="$ROOT/annex"

keep=(client-lookup monthly-report)
yes= keep_history= check=1
while [ $# -gt 0 ]; do
  case "$1" in
    --yes) yes=1 ;;
    --keep) keep+=("${2:?--keep needs an app name}"); shift ;;
    --keep-history) keep_history=1 ;;
    --no-check) check= ;;
    *) echo "usage: $0 [--yes] [--keep <app>]... [--keep-history] [--no-check]" >&2; exit 2 ;;
  esac
  shift
done
is_kept() { local a; for a in "${keep[@]}"; do [ "$a" = "$1" ] && return 0; done; return 1; }

render >/dev/null
deployed=$("$ANNEX" list | awk 'NR > 1 {print $1}')
remove=()
for app in $deployed; do is_kept "$app" || remove+=("$app"); done

echo "Demo reset on $WHERE:"
echo "  start platform with extras (Metabase, Dozzle)"
[ ${#remove[@]} -gt 0 ] && echo "  REMOVE with their data: ${remove[*]}"
echo "  redeploy client-lookup"
if [ -n "$keep_history" ]; then echo "  redeploy monthly-report (history kept)"
else echo "  monthly-report: DELETE its history and CSVs, redeploy, run once"; fi
extra_keep=("${keep[@]:2}")
[ ${#extra_keep[@]} -gt 0 ] && echo "  keep as is: ${extra_keep[*]}"
if [ -z "$yes" ]; then
  read -r -p "Continue? [y/N] " answer
  [ "$answer" = y ] || [ "$answer" = Y ] || { echo "Aborted."; exit 1; }
fi

log "Platform"
"$ROOT/scripts/push.sh" --extras

for app in ${remove[@]+"${remove[@]}"}; do
  log "Removing $app"
  "$ANNEX" remove "$app" --yes
done

log "client-lookup"
"$ANNEX" deploy "$ROOT/examples/client-lookup"

log "monthly-report"
if [ -z "$keep_history" ] && grep -qx monthly-report <<<"$deployed"; then
  "$ANNEX" remove monthly-report --yes
fi
"$ANNEX" deploy "$ROOT/examples/monthly-report"
"$ANNEX" run monthly-report monthly

log "Tunnel"
"$ROOT/scripts/tunnel.sh" up || true

if [ -n "$check" ]; then
  log "Checks"
  "$ROOT/scripts/test-platform.sh" | tail -1
  "$ROOT/scripts/test-apps.sh" | tail -1
fi

log "Ready"
"$ANNEX" list
echo
echo "Demo users (user / password):"
"$ROOT/scripts/show-users.sh"
echo
echo "Home: $(setting ANNEX_URL_HOME)"
echo "Next: the 'Before the demo' checklist in docs/demo.md (browser windows, Metabase login, empty folder for Claude Code)."
