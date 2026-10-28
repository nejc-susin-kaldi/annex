#!/usr/bin/env bash
# Bring Goat Arcade's Annex back to a clean demo state (docs/demo.md, D113). DESTRUCTIVE: it removes apps with their
# data, so it lists what it will do and asks first. Works on both lanes (ANNEX_ENV=local in front, never exported).
#   1. the platform up with extras (push.sh --extras: Metabase, Dozzle; it also brings Goat Arcade's database up and
#      links it, D112, and rebuilds the Metabase dashboards)
#   2. every app that isn't a Goat Arcade example removed with its data: tilt-watch (the live build) too, unless --keep
#   3. Goat Arcade's database reseeded (scripts/democlient.sh reseed --yes): its data then ends yesterday
#   4. the examples in demo/apps/ redeployed (a new container drops every in-memory cache of the old data);
#      tilt-watch deployed from there too if --keep tilt-watch and it isn't deployed
#   5. the checks, unless --no-check: test-smoke.sh, test-examples.sh (output in .build/reset-demo/)
#   6. the demo's own state, after the checks so it is the newest: tech-board's claims released (as boris) and its
#      watch job run once on the new data; kanboard's seed (after boris, maja and sam opened Kanboard once, so they're
#      on the boards); one birthday-party request through the webhook (scripts/demo-booking.sh)
#   7. with --usage: scripts/seed-usage.sh (synthetic usage history for the console)
#   8. the SSH tunnel up, the apps, the demo passwords
# Test mode (D74b) is on while it runs: Slack posts from these runs go to #annex-selftest, marked [TEST].
# Kept: secrets, accounts and passwords, backups, the kept apps' own databases (party requests, desk sign-ups, Kanboard
# cards), Mailpit. Takes about 10-15 minutes on the VM with the checks.
#
# Usage: scripts/reset-demo.sh [--yes] [--keep <app>]... [--usage] [--no-check]
#   --keep <app>   also keep this app as it is (--keep tilt-watch keeps the live build's fallback, or deploys it)
#   --usage        also write synthetic usage history (scripts/seed-usage.sh; undo with --clear)
#   --no-check     skip the test suites
source "$(dirname "$0")/lib.sh"
ANNEX="$ROOT/annex"
GA=$ROOT/demo/apps
LIVE=tilt-watch   # the live build: an example folder (the fallback), but removed unless --keep

examples=()
for d in "$GA"/*/; do
  n=$(basename "$d")
  [ -f "$d/annex.yaml" ] && [ "$n" != "$LIVE" ] && examples+=("$n")
done
extra=()
yes= check=1 usage=
while [ $# -gt 0 ]; do
  case "$1" in
    --yes) yes=1 ;;
    --keep) extra+=("${2:?--keep needs an app name}"); shift ;;
    --usage) usage=1 ;;
    --no-check) check= ;;
    *) echo "usage: $0 [--yes] [--keep <app>]... [--usage] [--no-check]" >&2; exit 2 ;;
  esac
  shift
done
in_list() { local x=$1; shift; local a; for a in "$@"; do [ "$a" = "$x" ] && return 0; done; return 1; }
is_kept() { in_list "$1" "${examples[@]}" ${extra[@]+"${extra[@]}"}; }

render >/dev/null
deployed=$("$ANNEX" list | awk 'NR > 1 {print $1}')
remove=()
for app in $deployed; do is_kept "$app" || remove+=("$app"); done
deploy_live=
if in_list "$LIVE" ${extra[@]+"${extra[@]}"} && ! grep -qx "$LIVE" <<<"$deployed"; then deploy_live=1; fi
as_is=()
for app in ${extra[@]+"${extra[@]}"}; do
  in_list "$app" "${examples[@]}" && continue
  [ "$app" = "$LIVE" ] && [ -n "$deploy_live" ] && continue
  as_is+=("$app")
done

echo "Demo reset on $WHERE:"
echo "  start the platform with extras (Metabase, Dozzle); Goat Arcade's database comes up and is linked with it"
[ ${#remove[@]} -gt 0 ] && echo "  REMOVE with their data: ${remove[*]}"
echo "  RESEED Goat Arcade's database (arcade-db): its data then ends yesterday"
echo "  redeploy: ${examples[*]}"
[ -n "$deploy_live" ] && echo "  deploy the live build's fallback: $LIVE (from demo/apps/$LIVE)"
[ ${#as_is[@]} -gt 0 ] && echo "  keep as is: ${as_is[*]}"
[ -n "$check" ] && echo "  run the checks: test-smoke.sh, test-examples.sh"
echo "  then: release tech-board's claims, run tech-board watch, kanboard seed; send one party booking"
[ -n "$usage" ] && echo "  write synthetic usage history (seed-usage.sh)"
echo "  start the tunnel"
if [ -z "$yes" ]; then
  read -r -p "Continue? [y/N] " answer
  [ "$answer" = y ] || [ "$answer" = Y ] || { echo "Aborted."; exit 1; }
fi

problems=()
problem() { problems+=("$1"); printf '\033[31m  problem:\033[0m %s\n' "$1"; }

log "Platform"
"$ROOT/scripts/push.sh" --extras

# testlib after the push: logins, the tunnel and test mode (Slack to #annex-selftest while this runs)
source "$ROOT/scripts/testlib.sh"
url() { echo "https://$(setting ANNEX_HOST_PATTERN | sed "s/{name}/$1/")$(setting ANNEX_PORT_SUFFIX)"; }

for app in ${remove[@]+"${remove[@]}"}; do
  log "Removing $app"
  "$ANNEX" remove "$app" --yes
done

log "Goat Arcade's database"
"$ROOT/scripts/democlient.sh" reseed --yes

for app in "${examples[@]}"; do
  log "Deploying $app"
  if ! out=$("$ANNEX" deploy "$GA/$app" 2>&1); then
    if grep -q BOOKING_HOOK_SECRET <<<"$out"; then
      problem "$app needs its secret first. A human runs: annex secret set $app BOOKING_HOOK_SECRET"
    else
      tail -5 <<<"$out"
      problem "deploying $app failed (above; annex status $app)"
    fi
    continue
  fi
  grep -E 'is live at|warning:' <<<"$out" || true
done
if [ -n "$deploy_live" ]; then
  log "Deploying $LIVE (the live build's fallback)"
  "$ANNEX" deploy "$GA/$LIVE" || problem "deploying $LIVE failed"
fi

LOGS=$ROOT/.build/reset-demo
if [ -n "$check" ]; then
  log "Checks (output in ${LOGS#"$ROOT"/}/)"
  mkdir -p "$LOGS"
  if pgrep -f 'scripts/test-[a-z-]*\.sh' >/dev/null; then
    problem "a test suite is already running (pgrep -fl scripts/test-): checks skipped, run them when it's done"
  else
    for suite in test-smoke test-examples; do
      # into a file, not a pipe: the suite's ssh tunnel would hold a pipe open
      if "$ROOT/scripts/$suite.sh" > "$LOGS/$suite.log" 2>&1; then echo "  $suite: $(tail -1 "$LOGS/$suite.log")"
      else problem "$suite: $(tail -1 "$LOGS/$suite.log") (see ${LOGS#"$ROOT"/}/$suite.log)"; fi
    done
  fi
  test_mode on   # the suites switch it off when they exit
fi

log "The demo's state"
use_tunnel
TB=$(url tech-board)
if [ "$(login boris)" = 200 ]; then
  # every claim the board shows (a held claim is a release form), released as boris; anyone may release one
  held=$(as boris "$TB/" | python3 -c '
import re, sys
for m in re.finditer(r"<form method=\"post\" action=\"release\"[^>]*>(.*?)</form>", sys.stdin.read(), re.S):
    k = re.search(r"name=\"kind\" value=\"(\w+)\"", m.group(1)); r = re.search(r"name=\"ref_id\" value=\"(\d+)\"", m.group(1))
    if k and r: print(k.group(1), r.group(1))' | sort -u) || held=""   # sort -u: a machine can be in two lists
  n=0
  while read -r kind ref; do
    [ -n "$kind" ] || continue
    [ "$(code -b "$TMP/boris.jar" -X POST -d kind="$kind" -d ref_id="$ref" "$TB/release")" = 303 ] && n=$((n + 1)) \
      || problem "could not release tech-board's claim on $kind $ref (release it on the board)"
  done <<<"$held"
  echo "  tech-board: $n claim(s) released"
else
  problem "boris couldn't sign in: release tech-board's claims by hand (each held one has a Release button)"
fi
run_job() { # run_job APP JOB: annex run, its last log line
  local out
  if out=$("$ANNEX" run "$1" "$2" 2>&1); then echo "  $1 $2: $(tail -1 <<<"$out" | cut -c1-160)"
  else tail -3 <<<"$out"; problem "annex run $1 $2 failed"; fi
}
run_job tech-board watch       # records the new data's out-of-order machines, so the next scheduled run posts nothing stale
# Kanboard makes a person's account on their first visit, and the seed adds everyone it knows to the boards (D111):
# a visit first, so the people the demo shows on Kanboard are members even when the checks were skipped
KB=$(url kanboard)
for u in boris maja sam; do
  [ "$u" = boris ] || login "$u" >/dev/null
  as "$u" -o /dev/null "$KB/" || problem "$u couldn't open Kanboard"
done
run_job kanboard seed
if out=$("$ROOT/scripts/demo-booking.sh" --venue LJ1 2>&1); then
  echo "  party-bookings: $(grep -m1 'name:' <<<"$out" | sed 's/^ *//'), stored"
else
  tail -2 <<<"$out"
  problem "the party booking wasn't accepted (scripts/demo-booking.sh; it needs BOOKING_HOOK_SECRET)"
fi

if [ -n "$usage" ]; then
  log "Usage history (synthetic)"
  "$ROOT/scripts/seed-usage.sh"
fi

log "Tunnel"
"$ROOT/scripts/tunnel.sh" up || problem "the tunnel didn't come up (scripts/tunnel.sh up)"

log "Ready"
"$ANNEX" list
echo
echo "Demo users (user / password):"
"$ROOT/scripts/show-users.sh"
echo
echo "Home: $(setting ANNEX_URL_HOME)"
echo "Next: the 'Before the demo' checklist in docs/demo.md (browser windows, Metabase, the empty folder for Claude Code)."
if [ ${#problems[@]} -gt 0 ]; then
  echo
  echo "Problems (${#problems[@]}):"
  printf '  - %s\n' "${problems[@]}"
  exit 1
fi
