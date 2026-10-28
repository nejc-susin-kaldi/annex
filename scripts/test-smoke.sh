#!/usr/bin/env bash
# Smoke test: is everything up and wired together? Run it after every push (under a minute).
# It touches each part once, cheaply, and changes nothing that matters: one deploy of Annex's own test app
# (annex-selftest, removed again at the end), two runs of its jobs (one email to maja in Mailpit, one Slack post on #annex-selftest).
# It doesn't prove timings, retries or "exactly one alert": that's what the full suites are for
# (scripts/test-all.sh, 30+ minutes: run them only when you changed those parts).
source "$(dirname "$0")/lib.sh"
source "$(dirname "$0")/testlib.sh"
use_tunnel
ANNEX="$ROOT/annex"
APP=$SELFTEST   # Annex's own test app: platform tests never use an example or a client's app
on_exit "selftest_remove $APP"
url() { echo "https://$(setting ANNEX_HOST_PATTERN | sed "s/{name}/$1/")$(setting ANNEX_PORT_SUFFIX)"; }
ctl() { vm "docker exec annex-ops-1 python -m annex_ops.ctl $* --json"; }
py() { python3 -c "import json,sys; d=json.load(sys.stdin); print($1)"; }

echo "Local"
"$ROOT/scripts/sync-runtime.sh" --check >/dev/null && ok "runtime helpers: every copy matches runtime/" \
  || bad "runtime helpers" "a copy differs: scripts/sync-runtime.sh"
"$ROOT/scripts/sync-contract.sh" --check >/dev/null && ok "contract summaries: every copy matches contract.md" \
  || bad "contract summaries" "a copy is stale: scripts/sync-contract.sh"
out=$(cd "$ROOT" && uv run --quiet --python 3.13 --with-requirements platform/ops/requirements.txt --with pytest pytest platform/ops/tests -q 2>&1 | tail -1)
has "annex-ops unit tests" " passed" "$out"
(cd "$ROOT" && python3 -m unittest discover -s platform/vm/tests -q >/dev/null 2>&1) && ok "annex_vm unit tests" || bad "annex_vm unit tests" "failed"
(cd "$ROOT" && PYTHONPATH=cli/src python3 -m unittest discover -s cli/tests -q >/dev/null 2>&1) && ok "CLI unit tests (log check)" || bad "CLI unit tests" "failed"
(cd "$ROOT" && python3 -m unittest discover -s runtime/tests -q >/dev/null 2>&1) && ok "runtime logging tests (python)" || bad "runtime logging tests (python)" "failed"
if command -v node >/dev/null; then
  (cd "$ROOT" && node --test runtime/tests/*.test.mjs >/dev/null 2>&1) && ok "runtime logging tests (node)" || bad "runtime logging tests (node)" "failed"
fi

echo "Platform"
wait_healthy 120 >/dev/null || true
left=$(vm "docker ps --filter label=com.kaldi.annex=true --format '{{.Names}} {{.Status}}'" | grep -v '(healthy)' | grep -v '^annex-app-' || true)
expect "every platform container is healthy" "" "$left"
ports=$(vm "docker ps --filter publish=1-65535 --format '{{.Names}}'" | grep -v '^annex-traefik-1$' || true)
expect "only Traefik publishes ports" "" "$ports"
for u in sam maja kaldi-admin; do expect "$u signs in" 200 "$(login "$u")"; done
expect "the discovery page" 200 "$(code -b "$TMP/maja.jar" "$HOME_URL/")"
expect "anonymous is sent to login" 302 "$(code "$HOME_URL/")"
expect "the status page (the console's /status)" 200 "$(code -b "$TMP/maja.jar" "$(setting ANNEX_URL_CONSOLE)/status")"
expect "Mailpit is admins only" "403 200" "$(code -b "$TMP/maja.jar" "$(setting ANNEX_URL_MAIL)/") $(code -b "$TMP/kaldi-admin.jar" "$(setting ANNEX_URL_MAIL)/")"
has "annex users lists the accounts" "kaldi-admin" "$("$ANNEX" users list 2>&1)"
# the demo client's database (Goat Arcade, D77): scripts/push.sh brings it up and links it (D112), and the fixture's views are its
expect "Goat Arcade's database runs (scripts/push.sh starts it)" healthy \
  "$(vm "docker inspect democlient-arcade-db-1 --format '{{.State.Health.Status}}'" 2>&1)"
expect "Goat Arcade's data answers through the link (machine_floor)" t \
  "$(vm "docker exec annex-product-db-1 psql -XAt -U postgres -d product -c 'SELECT count(*) > 100 FROM annex_api.machine_floor'" 2>&1)"

echo "Deploy, identity, gateway"
selftest_build "$ROOT/.build/selftest-smoke"
out=$(selftest_deploy "$ROOT/.build/selftest-smoke") && ok "annex deploy annex-selftest ($(grep -o 'r[0-9]* is live' <<<"$out" | head -1))" \
  || bad "annex deploy annex-selftest" "$(tail -3 <<<"$out")"
page=$(c -b "$TMP/maja.jar" "$(url $APP)/" | sed 's/<[^>]*>/ /g' | tr -s ' ')
has "it greets the signed-in user" "Hello, Maja Horvat" "$page"
has "a granted view is readable" "venues : allowed" "$page"
has "an ungranted view is denied" "player_profiles : denied" "$page"
has "a deploy of a conforming app has no logging warning" "is live" "$out"
hasnt "  ...(logs are structured)" "logs aren't structured" "$out"
# the logging convention (M2.1): one request carries one id in its response header, its log lines and Traefik's access log
rid_headers=$(c -D - -o /dev/null -b "$TMP/maja.jar" "$(url $APP)/?q=novak")
rid=$(tr -d '\r' <<<"$rid_headers" | sed -n 's/^[Xx]-[Rr]equest-[Ii]d: //p')
expect "the response carries an X-Request-Id" 16 "${#rid}"
sleep 2
lines=$(vm "docker logs --tail 40 annex-app-$APP 2>&1")
has "the app's log lines carry the same request_id" "\"request_id\": \"$rid\"" "$lines"
expect "the app's last log line is structured (ts, level, msg)" True "$(tail -1 <<<"$lines" | py 'all(k in d for k in ("ts", "level", "msg")) and d["level"] in ("debug", "info", "warn", "error")')"
has "Traefik's access log carries it too" "\"downstream_X-Request-Id\":\"$rid\"" "$(vm "docker logs --tail 60 annex-traefik-1 2>&1")"
hasnt "no search text in the app's log" "novak" "$lines"
# stored logs (M2.2): the same request is in annex-ops's table, found by its id, tagged with the release
stored=""
for _ in 1 2 3 4 5 6 7 8; do stored=$("$ANNEX" logs $APP --request "$rid" 2>&1) && grep -q "request" <<<"$stored" && break; sleep 2; done
has "annex logs --request finds the request in the stored logs" " r$(py 'd["release"]' <<<"$("$ANNEX" --json status $APP)" | tr -d r) info  request" "$stored"
# the audit trail (M2.5): the deploy just made is in it, with who did it
expect "annex audit shows this deploy of $APP" True "$("$ANNEX" --json audit --app $APP --action deploy --since 15m | py 'len(d) >= 1 and bool(d[0]["actor"])')"
# error groups (M2.3): the list answers, and the app serves the browser error script
expect "annex errors answers (a list)" list "$("$ANNEX" --json errors $APP | py 'type(d).__name__')"
has "the app serves /annex-errors.js" "unhandledrejection" "$(c -b "$TMP/maja.jar" "$(url $APP)/annex-errors.js")"
st=$("$ANNEX" --json status $APP)
expect "annex status: health check up" up "$(py '(d.get("health_check") or {}).get("state")' <<<"$st")"
expect "annex status: no open incident" None "$(py 'd.get("incident")' <<<"$st")"

echo "Jobs and email"
out=$("$ANNEX" run $APP work 2>&1) && ok "annex run $APP work (the scheduler's exec path: gateway, database, files)" || bad "annex run work" "$(tail -3 <<<"$out")"
has "  ...it read the gateway and wrote its run" '"msg": "job finished"' "$out"
mail_before=$(mail_count maja@goat-arcade.example "annex-selftest: notification check")
out=$("$ANNEX" run $APP notify 2>&1) && ok "annex run $APP notify (email, and Slack on the test channel)" || bad "annex run notify" "$(tail -3 <<<"$out")"
mail_wait maja@goat-arcade.example "annex-selftest: notification check" $((mail_before + 1)) 30 && ok "maja got the email (Mailpit)" \
  || bad "notify email" "none in Mailpit"

echo "annex-ops"
expect "no open platform incident" "" "$(ctl incidents --open | py '" ".join(i["subject"] for i in d if i["subject"].startswith("platform:"))')"
h=$(ctl host)
expect "host numbers are measured (disk, memory, backup)" True "$(py 'all(d[k] is not None for k in ("disk_used_pct", "mem_available_mb", "backup_age_h"))' <<<"$h")"
expect "the newest backup is under 26 h old" True "$(py 'd["backup_age_h"] < 26' <<<"$h")"
tile=$(vm "docker exec annex-ops-1 python -c \"import urllib.request; print(urllib.request.urlopen('http://127.0.0.1:8000/tiles/$APP').read().decode())\"")
has "the usage tile answers, without logins" '"users_7d"' "$tile"
hasnt "  ...and without any login in it" '"sam"' "$tile"
hc=$(ctl health)
expect "annex-ops checks the front door (a fresh result, up)" "up" "$(py 'd["platform:front-door"]["state"]' <<<"$hc")"
expect "annex-ops checks $APP (a fresh result, up)" "up" "$(py "d['app:$APP']['state']" <<<"$hc")"
has "the discovery page's Status tile has counts" '"down"' "$(vm "docker exec annex-ops-1 python -c \"import urllib.request; print(urllib.request.urlopen('http://127.0.0.1:8000/tiles/_status').read().decode())\"")"

echo "Console (M2.4)"
CONSOLE=$(setting ANNEX_URL_CONSOLE)
expect "the console shows sam his app, and gives maja a 403 on it (and maja /status)" "200 403 200" \
  "$(code -b "$TMP/sam.jar" "$CONSOLE/apps/$APP") $(code -b "$TMP/maja.jar" "$CONSOLE/apps/$APP") $(code -b "$TMP/maja.jar" "$CONSOLE/status")"
expect "the console is on no network apps are on" "annex-console annex-platform " "$(vm "docker inspect annex-console-1 -f '{{range \$k, \$v := .NetworkSettings.Networks}}{{\$k}} {{end}}'" | tr ' ' '\n' | grep . | sort | tr '\n' ' ')"
has "Homepage lists the Console and Status tiles (its services.yaml parses)" "$CONSOLE/status" "$(c -b "$TMP/maja.jar" "$HOME_URL/api/services")"

echo "Webhooks (the receiver)"
HOOKS=$(setting ANNEX_URL_HOOKS)
expect "the hooks host has no login, and an unknown hook is a 404" 404 "$(code -X POST -d '{}' "$HOOKS/h/no-such-app/x")"
expect "nothing outside /h/ is served there" 404 "$(code "$HOOKS/v1/me")"
renv=$(vm "docker exec annex-hooks-1 printenv" | cut -d= -f1)
hasnt "the receiver holds no Slack token" "SLACK_BOT_TOKEN" "$renv"
finish
