#!/usr/bin/env bash
# M0.2 acceptance checks: deploys annex-selftest from the Mac and verifies login + greeting, scoped
# gateway access, the discovery entry with its owner, and list/status/logs. Removes it at the end.
source "$(dirname "$0")/lib.sh"
source "$(dirname "$0")/testlib.sh"
use_tunnel

APP_URL="https://$(setting ANNEX_HOST_PATTERN | sed 's/{name}/annex-selftest/')$(setting ANNEX_PORT_SUFFIX)"
ANNEX="$ROOT/annex"
DIR=$ROOT/.build/selftest-apps
on_exit "selftest_remove annex-selftest; selftest_remove annex-selftest-plain"

text() { sed 's/<[^>]*>/ /g' | tr -s ' '; }

echo "Deploy"
selftest_build "$DIR" --drop webhooks,notify
if out=$(selftest_deploy "$DIR"); then ok "annex deploy annex-selftest"; else bad "annex deploy" "$out"; fi

echo "Login and identity"
login sam >/dev/null; login cm >/dev/null
[ "$(c -o /dev/null -w '%{http_code}' "$APP_URL/")" = 302 ] && ok "anonymous is sent to login" || bad "anonymous is sent to login" "no redirect"
page=$(c -b "$TMP/sam.jar" "$APP_URL/" | text)
has "greets sam by name" "Hello, Sam Novak" "$page"
has "greets cm by name" "Hello, Maja Horvat" "$(c -b "$TMP/cm.jar" "$APP_URL/" | text)"

echo "Scoped data access"
has "granted view client_summary is readable" "client_summary : allowed" "$page"
has "granted view top_clients_by_aum is readable" "top_clients_by_aum : allowed" "$page"
has "monthly_positions is denied (403)" '"status": 403' "$(c -b "$TMP/sam.jar" "$APP_URL/gateway/monthly_positions")"
has "advisor_book is denied" "advisor_book : denied" "$page"
has "a read through the gateway returns rows" '"rows": 5' "$(c -b "$TMP/cm.jar" "$APP_URL/gateway/client_summary")"
has "gateway logs the app's role" "role=app_annex_selftest" "$(vm "docker logs --since 5m annex-product-db-1 2>&1 | grep annex-gateway")"

echo "Discovery page"
services=$(c -b "$TMP/cm.jar" "$HOME_URL/api/services")
has "annex-selftest is listed" '"name":"annex-selftest"' "$services"
has "with its owner" 'owner: sam' "$services"

echo "CLI"
has "annex list shows it healthy" "sam web+job running healthy" "$("$ANNEX" list | awk '$1 == "annex-selftest" {print $2, $3, $4, $5}')"
has "annex status shows views" "client_summary" "$("$ANNEX" status annex-selftest)"
has "annex logs shows requests (stored, structured lines)" ' info  request  ' "$("$ANNEX" logs annex-selftest --grep request -n 20)"

echo "Logging convention (M2.1)"
has "a conforming app deploys without the logging warning" "is live" "$out"
hasnt "  ...no 'logs aren't structured'" "logs aren't structured" "$out"
selftest_build "$ROOT/.build/selftest-plain" --name annex-selftest-plain --drop webhooks,notify --env SELFTEST_LOG=plain
pout=$(selftest_deploy "$ROOT/.build/selftest-plain") && ok "an app that logs with print() still deploys" || bad "plain-logging deploy" "$(tail -3 <<<"$pout")"
has "  ...with the warning" "logs aren't structured" "$pout"
has "  ...that says how to fix it" "use \`log\` from annex_runtime.py" "$pout"
"$ANNEX" remove annex-selftest-plain --yes >/dev/null 2>&1 && ok "annex-selftest-plain removed" || bad "annex-selftest-plain removed" "failed"

echo "Build limits (D95)"
BIG=annex-selftest-big
on_exit "selftest_remove $BIG"
selftest_build "$ROOT/.build/selftest-big" --name $BIG --drop notify,webhooks --big-file 120
if out=$(ANNEX_IMAGE_MAX_MB=100 selftest_deploy "$ROOT/.build/selftest-big"); then bad "an image above the cap is refused" "deploy succeeded: $(tail -3 <<<"$out")"
else has "an image above the cap is refused, naming the size and the limit" "limit of 100 MB" "$out"; has "  ...and what to do" "slim base image" "$out"; fi
expect "the refused image is removed" "" "$(vm "docker image ls -q annex-app/$BIG")"
expect "the refused release is recorded as failed" "failed" "$("$ANNEX" --json releases $BIG | python3 -c 'import json,sys; print(json.load(sys.stdin)[0]["result"])')"
out=$(ANNEX_BUILD_MIN_FREE_GB=100000 selftest_deploy "$ROOT/.build/selftest-big") && bad "a build with too little free disk is refused" "deploy succeeded" \
  || has "a build with too little free disk is refused, naming docker system df" "docker system df" "$out"
selftest_build "$ROOT/.build/selftest-big" --name $BIG --drop notify,webhooks
out=$(selftest_deploy "$ROOT/.build/selftest-big") && ok "the normal fixture deploys under the default limits ($BIG)" \
  || bad "the normal fixture deploys under the default limits ($BIG)" "$(tail -3 <<<"$out")"
expect "the app container has a process limit of 256" "256" "$(vm "docker inspect -f '{{.HostConfig.PidsLimit}}' annex-app-$BIG")"
finish
