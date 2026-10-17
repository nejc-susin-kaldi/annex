#!/usr/bin/env bash
# Do the demo apps in examples/ still build, deploy and work? Run it before a demo, not as a platform check: the platform
# suites use annex-selftest and never touch these (they are stand-ins for client apps and change with the client).
# It never runs monthly-report's `monthly` job (that one emails and posts to Slack: it is the demo's to run, on purpose).
# Leaves both apps deployed, as the demo wants them.
source "$(dirname "$0")/lib.sh"
source "$(dirname "$0")/testlib.sh"
use_tunnel
ANNEX="$ROOT/annex"
url() { echo "https://$(setting ANNEX_HOST_PATTERN | sed "s/{name}/$1/")$(setting ANNEX_PORT_SUFFIX)"; }
text() { sed 's/<[^>]*>/ /g' | tr -s ' '; }
expect "sam signs in" 200 "$(login sam)"
expect "cm signs in" 200 "$(login cm)"

echo "client-lookup"
out=$("$ANNEX" deploy "$ROOT/examples/client-lookup" 2>&1) && ok "deploys ($(grep -o 'r[0-9]* is live' <<<"$out" | head -1))" || bad "deploy client-lookup" "$(tail -3 <<<"$out")"
hasnt "no warnings at deploy" "warning:" "$out"
page=$(as sam "$(url client-lookup)/" | text)
has "greets the signed-in user" "Hello, Sam Novak" "$page"
has "reads its granted views" "client_summary : allowed" "$page"
has "is refused the ungranted one" "advisor_book : denied" "$page"
has "a search returns clients" "Novak" "$(as cm "$(url client-lookup)/?q=novak" | text)"
expect "annex status: health check up" up "$("$ANNEX" --json status client-lookup | python3 -c 'import json,sys; print(json.load(sys.stdin)["health_check"]["state"])')"

echo "monthly-report"
out=$("$ANNEX" deploy "$ROOT/examples/monthly-report" 2>&1) && ok "deploys ($(grep -o 'r[0-9]* is live' <<<"$out" | head -1))" || bad "deploy monthly-report" "$(tail -3 <<<"$out")"
has "its page answers" "Monthly positions" "$(as cm "$(url monthly-report)/" | text)"
jobs=$(vm "docker logs annex-ofelia-1 2>&1 | sed 's/\x1b\[[0-9;]*m//g'")
has "its monthly job is scheduled (and is not run here)" 'registered "monthly-report-monthly"' "$jobs"
has "so is the demo's refresh job" 'registered "monthly-report-refresh"' "$jobs"
out=$("$ANNEX" run monthly-report refresh 2>&1) && ok "annex run monthly-report refresh (writes the CSV, notifies nobody)" || bad "run refresh" "$(tail -3 <<<"$out")"
has "  ...it logged in the convention" '"msg": "job finished"' "$out"
expect "annex status: health check up" up "$("$ANNEX" --json status monthly-report | python3 -c 'import json,sys; print(json.load(sys.stdin)["health_check"]["state"])')"
finish
