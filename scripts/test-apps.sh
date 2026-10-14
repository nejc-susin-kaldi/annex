#!/usr/bin/env bash
# M0.2 acceptance checks: deploys examples/client-lookup from the Mac and
# verifies login + greeting, scoped gateway access, the discovery entry with its
# owner, and list/status/logs. Leaves client-lookup deployed.
source "$(dirname "$0")/lib.sh"
render >/dev/null
"$ROOT/scripts/tunnel.sh" up >/dev/null

PORT=${ANNEX_PUBLIC_PORT:-8443}
CA="$(mkcert -CAROOT)/rootCA.pem"
AUTH_URL=$(setting ANNEX_URL_AUTH)
HOME_URL=$(setting ANNEX_URL_HOME)
APP_URL="https://$(setting ANNEX_HOST_PATTERN | sed 's/{name}/client-lookup/')$(setting ANNEX_PORT_SUFFIX)"
ANNEX="$ROOT/annex"
TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT

pass=0 fail=0
ok()  { pass=$((pass + 1)); printf '  \033[32mok\033[0m   %s\n' "$1"; }
bad() { fail=$((fail + 1)); printf '  \033[31mFAIL\033[0m %s (%s)\n' "$1" "$2"; }
has() { if grep -q -- "$2" <<<"$3"; then ok "$1"; else bad "$1" "missing: $2"; fi; }
c() { curl -sS --cacert "$CA" --connect-to "::127.0.0.1:$PORT" "$@"; }
login() {
  local pw; pw=$(vm "awk -v u='$1' '\$1 == u {print \$2}' $DATA/secrets/users.txt")
  c -c "$TMP/$1.jar" -o /dev/null -H 'Content-Type: application/json' \
    --data "{\"username\":\"$1\",\"password\":\"$pw\",\"keepMeLoggedIn\":false}" "$AUTH_URL/api/firstfactor"
}
text() { sed 's/<[^>]*>/ /g' | tr -s ' '; }

echo "Deploy"
if out=$("$ANNEX" deploy "$ROOT/examples/client-lookup" 2>&1); then ok "annex deploy examples/client-lookup"; else bad "annex deploy" "$out"; fi

echo "Login and identity"
login sam; login cm
[ "$(c -o /dev/null -w '%{http_code}' "$APP_URL/")" = 302 ] && ok "anonymous is sent to login" || bad "anonymous is sent to login" "no redirect"
page=$(c -b "$TMP/sam.jar" "$APP_URL/" | text)
has "greets sam by name" "Hello, Sam Novak" "$page"
has "greets cm by name" "Hello, Maja Horvat" "$(c -b "$TMP/cm.jar" "$APP_URL/" | text)"

echo "Scoped data access"
has "granted view client_summary is readable" "client_summary : allowed" "$page"
has "granted view top_clients_by_aum is readable" "top_clients_by_aum : allowed" "$page"
has "monthly_positions is denied" "monthly_positions : denied" "$page"
has "advisor_book is denied" "advisor_book : denied" "$page"
has "search returns clients" "Novak" "$(c -b "$TMP/cm.jar" "$APP_URL/?q=novak" | text)"
has "gateway logs the app's role" "role=app_client_lookup" "$(vm "docker logs --since 5m annex-product-db-1 2>&1 | grep annex-gateway")"

echo "Discovery page"
services=$(c -b "$TMP/cm.jar" "$HOME_URL/api/services")
has "client-lookup is listed" '"name":"client-lookup"' "$services"
has "with its owner" 'owner: sam' "$services"

echo "CLI"
has "annex list shows it healthy" "client-lookup  sam    web   running  healthy" "$("$ANNEX" list)"
has "annex status shows views" "client_summary" "$("$ANNEX" status client-lookup)"
has "annex logs shows requests" "GET / HTTP" "$("$ANNEX" logs client-lookup -n 20)"

echo
echo "passed $pass, failed $fail"
[ "$fail" -eq 0 ]
