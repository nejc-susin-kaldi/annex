#!/usr/bin/env bash
# M1.4 acceptance checks for usage stats, run from the Mac through the SSH tunnel:
# a scripted session with `sam` and `cm` across three apps (two static ones and annex-selftest) must show up with the same numbers in `annex usage`,
# the discovery tiles and the console's Usage tab, and match the script; requests that must not count don't;
# nothing personal leaks; raw rows are deleted after 30 days; a restart neither loses nor repeats rows; the
# weekly digest flags a dormant app. Deploys two throwaway static apps (usage-probe, usage-idle) and annex-selftest, and removes them.
source "$(dirname "$0")/lib.sh"
source "$(dirname "$0")/testlib.sh"
use_tunnel

MAIL_URL=$(setting ANNEX_URL_MAIL)
PATTERN=$(setting ANNEX_HOST_PATTERN)
SUFFIX=${ANNEX_PUBLIC_PORT:+:$ANNEX_PUBLIC_PORT}
PROBE=usage-probe IDLE=usage-idle LOOKUP=$SELFTEST
url() { echo "https://${PATTERN//\{name\}/$1}$SUFFIX"; }
WORK=$ROOT/.build/test-usage

ctl() { vm "docker exec annex-ops-1 python -m annex_ops.ctl $* --json"; }
sql() { vm "docker exec annex-appdata-db-1 psql -U postgres -d annex_ops -Atc \"$1\" 2>&1"; }
hits() { # hits USER APP PATH COUNT: signed-in requests
  local i; for i in $(seq "$4"); do code -b "$TMP/$1.jar" "$(url "$2")$3" >/dev/null; done
}
by_user() { # by_user APP -> "login=count ..." sorted by count
  ctl usage --app "$1" --by-user | python3 -c "
import json, sys
print(' '.join(u['login'] + '=' + str(u['requests']) for u in json.load(sys.stdin)['by_user']))"
}
count_of() { # count_of APP LOGIN (from the by-user list)
  by_user "$1" | tr ' ' '\n' | sed -n "s/^$2=//p" | head -1 | grep . || echo 0
}
field() { # field APP FIELD -> a value of that app's `annex usage` row
  ctl usage | python3 -c 'import json,sys; print(next(r for r in json.load(sys.stdin) if r["app"] == sys.argv[1])[sys.argv[2]])' "$1" "$2"
}

echo "Setup"
rm -rf "$WORK"; mkdir -p "$WORK"
for app in $PROBE $IDLE; do
  "$ROOT/annex" init static "$app" --dir "$WORK/$app" --owner sam >/dev/null
  "$ROOT/annex" deploy "$WORK/$app" >/dev/null 2>&1 && ok "$app deployed" || { bad "$app deployed" "see: annex deploy $WORK/$app"; echo "passed $pass, failed $fail"; exit 1; }
done
selftest_build "$WORK/$LOOKUP" --drop webhooks,notify
selftest_deploy "$WORK/$LOOKUP" >/dev/null && ok "$LOOKUP deployed (the self-test app: the one with real pages and a gateway view)" || bad "$LOOKUP deployed" "failed"
on_exit "selftest_remove $LOOKUP"
expect "sam logs in" 200 "$(login sam)"
expect "cm logs in" 200 "$(login cm)"
expect "kaldi-admin logs in" 200 "$(login kaldi-admin)"
ctl rollup >/dev/null
# the apps must carry the health-path label, or their health checks would count
expect "$PROBE carries the health-path label" "/healthz" "$(vm "docker inspect -f '{{index .Config.Labels \"com.kaldi.annex.healthcheck\"}}' annex-app-$PROBE")"
BEFORE_L_REQ=$(field $LOOKUP requests_7d); BEFORE_P_REQ=$(field $PROBE requests_7d); BEFORE_I_REQ=$(field $IDLE requests_7d)
B_L_ROOT=$(sql "SELECT count(*) FROM annex_ops.requests WHERE app = '$LOOKUP' AND login = 'cm' AND path = '/'")
B_L_SAM=$(count_of $LOOKUP sam); B_L_CM=$(count_of $LOOKUP cm); B_P_SAM=$(count_of $PROBE sam); B_P_CM=$(count_of $PROBE cm)
sleep 3

echo "Scripted session"
hits sam $LOOKUP "/" 5
hits cm $LOOKUP "/?q=novak" 2
hits cm $PROBE "/" 3
hits sam $PROBE "/" 4
# must NOT count: health paths, static assets, anonymous requests (they get a login redirect)
hits sam $LOOKUP "/healthz" 2
hits sam $PROBE "/healthz" 3
hits sam $PROBE "/favicon.ico" 2
hits sam $PROBE "/style.css" 1
for _ in 1 2; do code "$(url $LOOKUP)/" >/dev/null; done
expect "an anonymous request gets the login redirect" 302 "$(code "$(url $LOOKUP)/")"
ok "session sent: sam 5+4, cm 2+3 counted; health paths, assets and anonymous requests not"
end=$((SECONDS + 60))
until [ "$(count_of $LOOKUP sam)" -ge $((B_L_SAM + 5)) ] && [ "$(count_of $PROBE sam)" -ge $((B_P_SAM + 4)) ] && [ "$(count_of $PROBE cm)" -ge $((B_P_CM + 3)) ]; do
  [ $SECONDS -ge $end ] && break; sleep 3
done
sleep 7  # let anything counted by mistake arrive too
ctl rollup >/dev/null

echo "Numbers"
expect "$LOOKUP: sam's requests increased by 5" 5 $(( $(count_of $LOOKUP sam) - B_L_SAM ))
expect "$LOOKUP: cm's requests increased by 2" 2 $(( $(count_of $LOOKUP cm) - B_L_CM ))
expect "$PROBE: sam's requests increased by 4" 4 $(( $(count_of $PROBE sam) - B_P_SAM ))
expect "$PROBE: cm's requests increased by 3" 3 $(( $(count_of $PROBE cm) - B_P_CM ))
expect "$LOOKUP: requests_7d increased by 7" 7 $(( $(field $LOOKUP requests_7d) - BEFORE_L_REQ ))
expect "$PROBE: requests_7d increased by 7" 7 $(( $(field $PROBE requests_7d) - BEFORE_P_REQ ))
expect "$IDLE: requests_7d did not change" "$BEFORE_I_REQ" "$(field $IDLE requests_7d)"
expect "$LOOKUP is used by sam and cm (--by-user)" "cm sam" "$(by_user $LOOKUP | tr ' ' '\n' | cut -d= -f1 | sort | tr '\n' ' ' | sed 's/ $//')"
expect "$PROBE is used by sam and cm (--by-user)" "cm sam" "$(by_user $PROBE | tr ' ' '\n' | cut -d= -f1 | sort | tr '\n' ' ' | sed 's/ $//')"
expect "$IDLE has no users" 0 "$(field $IDLE users_7d)"
usage_json=$(ctl usage)
tile_json() { vm "docker exec annex-ops-1 python -c \"import urllib.request; print(urllib.request.urlopen('http://127.0.0.1:8000/tiles/$1').read().decode())\""; }
for app in $LOOKUP $PROBE $IDLE; do
  t=$(tile_json $app)
  want=$(python3 -c '
import json, sys
r = next(r for r in json.loads(sys.argv[1]) if r["app"] == sys.argv[2])
print(r["users_7d"], r["trend"], r["requests_7d"])' "$usage_json" "$app")
  got=$(python3 -c 'import json,sys; t=json.loads(sys.argv[1]); print(t["users_7d"], t["trend"], t["requests_7d"])' "$t")
  expect "tile of $app equals annex usage (users, trend, requests)" "$want" "$got"
done
services=$(as cm "$HOME_URL/api/services")
has "Homepage lists the customapi widget for $LOOKUP" "\"service_name\":\"$LOOKUP\"" "$services"
has "  ...with the 'Users 7d' mapping" '"label":"Users 7d"' "$services"
proxied=$(as cm "$HOME_URL/api/services/proxy?type=customapi&group=Tools&service=$LOOKUP&index=0")
expect "Homepage serves the tile data (users_7d)" "$(field $LOOKUP users_7d)" "$(python3 -c 'import json,sys; print(json.loads(sys.argv[1])["users_7d"])' "$proxied")"
hasnt "the retired Usage tile (M2.9) is not on the discovery page" '"name":"Usage"' "$services"

CON=$(setting ANNEX_URL_CONSOLE)
page=$(as kaldi-admin "$CON/apps/$LOOKUP/usage" | sed 's/<style>.*<\/style>//; s/<[^>]*>/ /g' | tr -s ' \n')
for app in $LOOKUP; do
  want=$(python3 -c 'import json,sys; r=next(r for r in json.loads(sys.argv[1]) if r["app"]==sys.argv[2]); print(f'"'"'{r["users_7d"]} user(s) in the last 7 days ({r["trend"]}), {r["users_30d"]} in 30 days, {r["requests_7d"]} requests'"'"')' "$usage_json" "$app")
  has "the console's Usage tab for $app says what annex usage says (users, trend, requests)" "$want" "$page"
done
has "  ...and draws its charts (inline SVG, no script)" '<svg class="chart"' "$(as kaldi-admin "$CON/apps/$LOOKUP/usage")"

echo "Privacy"
expect "no stored path has a query string" 0 "$(sql "SELECT count(*) FROM annex_ops.requests WHERE path LIKE '%?%'")"
expect "cm's 2 requests for /?q=novak are stored as plain /" "$((B_L_ROOT + 2))" "$(sql "SELECT count(*) FROM annex_ops.requests WHERE app = '$LOOKUP' AND login = 'cm' AND path = '/'")"
if grep -qE '"(login|logins)"' <<<"$usage_json" || grep -qE '"(sam|cm|kaldi-admin)"' <<<"$usage_json"; then bad "annex usage (no --by-user) shows no logins" "found one"; else ok "annex usage (no --by-user) shows no logins"; fi
all_tiles="$(tile_json $LOOKUP)$(tile_json $PROBE)$(tile_json $IDLE)"
if grep -qE '(sam|cm|kaldi-admin)' <<<"$all_tiles"; then bad "tile JSON shows no logins" "$all_tiles"; else ok "tile JSON shows no logins"; fi
has "--by-user needs an app" "needs an app" "$("$ROOT/annex" usage --by-user 2>&1 || true)"
has "annex usage <app> --by-user prints the personal-data reminder" "personal data" "$("$ROOT/annex" usage $LOOKUP --by-user 2>&1)"

echo "Retention"
sql "INSERT INTO annex_ops.requests VALUES (now() - interval '31 days', '$PROBE', 'cm', 'GET', '/old', 200, 5)" >/dev/null
expect "a 31-day-old row exists before the rollup" 1 "$(sql "SELECT count(*) FROM annex_ops.requests WHERE path = '/old'")"
ctl rollup >/dev/null
expect "the rollup deleted it" 0 "$(sql "SELECT count(*) FROM annex_ops.requests WHERE path = '/old'")"
expect "rows from the session are still there" "$((B_P_SAM + 4))" "$(sql "SELECT count(*) FROM annex_ops.requests WHERE app = '$PROBE' AND login = 'sam'")"

echo "Restart"
before=$(sql "SELECT count(*) FROM annex_ops.requests")
vm "docker restart annex-ops-1 >/dev/null"
end=$((SECONDS + 90)); until [ "$(vm "docker inspect -f '{{.State.Health.Status}}' annex-ops-1")" = healthy ] || [ $SECONDS -ge $end ]; do sleep 2; done
hits sam $PROBE "/" 2
end=$((SECONDS + 60)); until [ "$(sql "SELECT count(*) FROM annex_ops.requests")" -ge $((before + 2)) ] || [ $SECONDS -ge $end ]; do sleep 3; done
sleep 14  # replayed log lines would have arrived by now
expect "after a restart, exactly the 2 new requests were added (no duplicates, no gap)" $((before + 2)) "$(sql "SELECT count(*) FROM annex_ops.requests")"

echo "Digest"
c -o /dev/null -X DELETE -b "$TMP/kaldi-admin.jar" "$MAIL_URL/api/v1/messages"
sent=$(ctl digest --send --dormant-days 1 --min-age-days 0)
has "the digest was sent to the admin" "admin@annex.example" "$sent"
has "the digest was sent to sam, who owns the test apps" "sam@annex.example" "$sent"
sleep 3
msgs=$(as kaldi-admin "$MAIL_URL/api/v1/messages?limit=50")
digest_to() { python3 -c '
import json, sys
print(sum(1 for m in json.loads(sys.argv[1])["messages"] if m["Subject"].startswith("[Annex] Weekly usage") and any(t["Address"] == sys.argv[2] for t in m["To"])))' "$msgs" "$1"; }
expect "admin gets exactly one digest" 1 "$(digest_to admin@annex.example)"
expect "sam gets exactly one digest" 1 "$(digest_to sam@annex.example)"
mid=$(python3 -c '
import json, sys
print(next(m["ID"] for m in json.loads(sys.argv[1])["messages"] if any(t["Address"] == "admin@annex.example" for t in m["To"]) and m["Subject"].startswith("[Annex] Weekly usage")))' "$msgs")
body=$(as kaldi-admin "$MAIL_URL/api/v1/message/$mid" | python3 -c 'import json,sys; print(json.load(sys.stdin)["Text"])')
dormant_line=$(grep -E '^Dormant' <<<"$body")
has "the digest lists $IDLE under Dormant" "$IDLE" "$dormant_line"
if grep -qF "$LOOKUP" <<<"$dormant_line"; then bad "$LOOKUP is not listed as dormant" "$dormant_line"; else ok "$LOOKUP is not listed as dormant"; fi
has "the digest has the app's line with its trend" "$LOOKUP" "$body"
if grep -qiE '\b(sam|cm|kaldi-admin)\b' <<<"$body"; then bad "the digest contains no login" "$(grep -iE '\b(sam|cm|kaldi-admin)\b' <<<"$body" | head -2)"; else ok "the digest contains no login"; fi

echo "Teardown"
for app in $PROBE $IDLE; do "$ROOT/annex" remove "$app" --yes >/dev/null 2>&1 && ok "removed $app" || bad "removed $app" "failed"; done
ctl rollup >/dev/null
names=$(ctl usage | python3 -c 'import json,sys; print(" ".join(r["app"] for r in json.load(sys.stdin)))')
if grep -qE "$PROBE|$IDLE" <<<"$names"; then bad "removed apps are hidden from annex usage" "$names"; else ok "removed apps are hidden from annex usage"; fi
has "their history stays (usage-probe by day)" "removed" "$("$ROOT/annex" usage $PROBE 2>&1)"
rm -rf "$WORK"
finish
