#!/usr/bin/env bash
# Run every check, in an order where each suite leaves what the next one needs, and
# print a summary. Fast set by default (about 30 minutes); --slow adds test-alerts.sh
# (another 20 minutes: Gatus's 60 s interval and the "exactly one alert" waits).
# This is long: after a push, scripts/test-smoke.sh (about 2 minutes) is the check to run.
# Usage: scripts/test-all.sh [--slow] [--only name[,name]]
#   e.g. scripts/test-all.sh --only unit,platform,apps
source "$(dirname "$0")/lib.sh"
source "$(dirname "$0")/testlib.sh"

slow= only=
while [ $# -gt 0 ]; do
  case "$1" in
    --slow) slow=1 ;;
    --only) only=",${2:?--only needs names},"; shift ;;
    *) echo "usage: $0 [--slow] [--only name[,name]]" >&2; exit 2 ;;
  esac
  shift
done

# name and command; the order matters (apps before jobs and restore, rollback before notify)
suites=(
  "smoke|scripts/test-smoke.sh"
  "runtime|scripts/sync-runtime.sh --check"
  "unit|uv run --quiet --python 3.13 --with-requirements ops/requirements.txt --with pytest pytest ops/tests -q"
  "vm-unit|python3 -m unittest discover -s vm/tests -q"
  "platform|scripts/test-platform.sh"
  "apps|scripts/test-apps.sh"
  "jobs|scripts/test-jobs.sh"
  "restore|scripts/restore-test.sh"
  "rollback|scripts/test-rollback.sh"
  "notify|scripts/test-notify.sh"
  "users|scripts/test-users.sh"
  "usage|scripts/test-usage.sh"
  "hooks|scripts/test-hooks.sh"
  "host|scripts/test-host.sh"
)
[ -n "$slow" ] && suites+=("alerts|scripts/test-alerts.sh")

results=()
failed=0
for entry in "${suites[@]}"; do
  name=${entry%%|*} cmd=${entry#*|}
  [ -n "$only" ] && [[ "$only" != *",$name,"* ]] && continue
  log "$name: $cmd"
  # a suite that deploys may have restarted Ofelia; let healthchecks settle before the next one
  case "$name" in runtime|unit|vm-unit) ;; *) wait_healthy 120 >/dev/null || true ;; esac
  t0=$SECONDS
  if (cd "$ROOT" && eval "$cmd") > "$TMP/$name.log" 2>&1; then status=ok; else status=FAIL; failed=1; fi
  summary=$(grep -E '^passed [0-9]+, failed [0-9]+' "$TMP/$name.log" | tail -1 || true)
  [ -z "$summary" ] && summary=$(tail -1 "$TMP/$name.log")
  results+=("$(printf '%-9s %-4s %4ss  %s' "$name" "$status" "$((SECONDS - t0))" "$summary")")
  if [ "$status" = FAIL ]; then
    grep -E 'FAIL|Error|error' "$TMP/$name.log" | head -15 | sed 's/^/    /' || true
    cp "$TMP/$name.log" "$ROOT/.build/test-$name.log" 2>/dev/null && echo "    full log: .build/test-$name.log"
  fi
done

echo
printf '%s\n' "${results[@]}"
[ -z "$slow" ] && echo "(test-alerts.sh skipped; run with --slow)"
exit "$failed"
