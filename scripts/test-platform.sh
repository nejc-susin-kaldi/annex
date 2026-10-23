#!/usr/bin/env bash
# M0.1 acceptance checks, run from the Mac through the SSH tunnel:
#   health of every container, login for all demo users, access rules,
#   identity-header spoofing, Metabase, network isolation of product-db, app isolation (D88: own networks, egress,
#   networks-sync after a recreate), and the demo client's
#   database behind the gateway (D77; skipped when it isn't running).
# Needs the extras profile running (scripts/extras.sh on) for the Metabase and
# Dozzle checks; they are skipped otherwise.
source "$(dirname "$0")/lib.sh"
source "$(dirname "$0")/testlib.sh"
use_tunnel

LOGS_URL=$(setting ANNEX_URL_LOGS)
MB_URL=$(setting ANNEX_URL_METABASE)
GW_URL=$(setting ANNEX_URL_GATEWAY)
MAIL_URL=$(setting ANNEX_URL_MAIL)
STATUS_URL=$(setting ANNEX_URL_CONSOLE)/status
PATTERN=$(setting ANNEX_HOST_PATTERN)
on_exit 'vm "docker rm -f annex-test-echo >/dev/null 2>&1"' 

extras=$(vm "cd $DATA/platform && docker compose ps --status running --services" | grep -cx -e metabase -e dozzle || true)

echo "Health"
# a deploy just before this suite may have restarted Ofelia (D18): let healthchecks settle first
wait_healthy 90 >/dev/null || true
unhealthy=$(vm "docker ps --filter label=com.kaldi.annex=true --format '{{.Names}} {{.Status}}'" | grep -v '(healthy)' || true)
if [ -z "$unhealthy" ]; then ok "all Annex containers healthy"; else bad "all Annex containers healthy" "$unhealthy"; fi
ports=$(vm "docker ps --filter publish=1-65535 --format '{{.Names}} {{.Ports}}'" | grep -v '^annex-traefik-1 ' || true)
if [ -z "$ports" ]; then ok "only traefik publishes ports"; else bad "only traefik publishes ports" "$ports"; fi

echo "Login"
loc=$(c -o /dev/null -w '%{redirect_url}' "$HOME_URL/")
case "$loc" in "$AUTH_URL"/*) ok "anonymous request to home redirects to login" ;; *) bad "anonymous redirect" "$loc" ;; esac
expect "wrong password is rejected" 401 "$(c -o /dev/null -w '%{http_code}' -H 'Content-Type: application/json' \
  --data '{"username":"sam","password":"nope","keepMeLoggedIn":false}' "$AUTH_URL/api/firstfactor")"
for u in sam maja kaldi-admin; do
  expect "$u logs in" 200 "$(login "$u")"
  expect "$u reaches the discovery page" 200 "$(code -b "$TMP/$u.jar" "$HOME_URL/")"
done
# Homepage renders its tiles client-side from /api/services
services=$(as maja "$HOME_URL/api/services")
for s in Metabase Logs "Data gateway" Mail Status "Goat Mailer" Bekafon; do
  if grep -q "\"name\":\"$s\"" <<<"$services"; then ok "discovery page lists $s"; else bad "discovery page lists $s" "missing"; fi
done

echo "Access rules"
expect "gateway: sam is forbidden" 403 "$(code -b "$TMP/sam.jar" "$GW_URL/")"
expect "gateway: kaldi-admin passes the front door (PostgREST rejects a call without JWT)" 401 "$(code -b "$TMP/kaldi-admin.jar" "$GW_URL/")"
expect "status: anonymous is sent to login" 302 "$(code "$STATUS_URL")"
expect "status: maja sees the status page" 200 "$(code -b "$TMP/maja.jar" "$STATUS_URL")"
expect "mail: sam is forbidden" 403 "$(code -b "$TMP/sam.jar" "$MAIL_URL/")"
expect "mail: maja is forbidden" 403 "$(code -b "$TMP/maja.jar" "$MAIL_URL/")"
expect "mail: kaldi-admin gets Mailpit" 200 "$(code -b "$TMP/kaldi-admin.jar" "$MAIL_URL/")"
if [ "$extras" -ge 2 ]; then
  expect "logs: sam is forbidden" 403 "$(code -b "$TMP/sam.jar" "$LOGS_URL/")"
  expect "logs: maja is forbidden" 403 "$(code -b "$TMP/maja.jar" "$LOGS_URL/")"
  expect "logs: kaldi-admin gets Dozzle" 200 "$(code -b "$TMP/kaldi-admin.jar" "$LOGS_URL/")"
  expect "metabase: anonymous is sent to login" 302 "$(code "$MB_URL/")"
  expect "metabase: maja reaches Metabase" 200 "$(code -b "$TMP/maja.jar" "$MB_URL/api/health")"
  mbpw=$(password kaldi-admin)
  mbs=$(as kaldi-admin -H 'Content-Type: application/json' --data "{\"username\":\"admin@annex.example\",\"password\":\"$mbpw\"}" "$MB_URL/api/session" | sed -n 's/.*"id":"\([^"]*\)".*/\1/p')
  dbs=$(as kaldi-admin -H "X-Metabase-Session: $mbs" "$MB_URL/api/database")
  if grep -q '"name":"Goat Arcade"' <<<"$dbs" && grep -q '"user":"metabase_ro"' <<<"$dbs"; then
    ok "metabase: Goat Arcade database connected as metabase_ro"
  else bad "metabase: Goat Arcade database connected as metabase_ro" "$(head -c 200 <<<"$dbs")"; fi
  dbid=$(python3 -c 'import json,sys; print(next(d["id"] for d in json.load(sys.stdin)["data"] if d["name"] == "Goat Arcade"))' <<<"$dbs")
  q() { as kaldi-admin -H "X-Metabase-Session: $mbs" -H 'Content-Type: application/json' \
          --data "{\"database\":${dbid:-0},\"type\":\"native\",\"native\":{\"query\":\"$1\"}}" "$MB_URL/api/dataset"; }
  if grep -q '"rows":\[\[' <<<"$(q 'select count(*) from annex_api.venues')"; then ok "metabase: can read annex_api views (venues)"; else bad "metabase: can read annex_api views (venues)" "no rows"; fi
  if grep -q 'permission denied' <<<"$(q 'select count(*) from src_arcade.games')"; then ok "metabase: cannot read the client's raw tables (src_arcade)"; else bad "metabase: cannot read the client's raw tables (src_arcade)" "no permission error"; fi
else
  echo "  (skipped Metabase and Dozzle checks: extras profile is off)"
fi

echo "Identity header spoofing"
ECHO_HOST=${PATTERN//\{name\}/echo-test}
ECHO_URL="https://$ECHO_HOST${ANNEX_PUBLIC_PORT:+:$ANNEX_PUBLIC_PORT}"
vm "docker run -d --rm --name annex-test-echo --network annex-edge --user 65534 \
  --label com.kaldi.annex=true --label traefik.enable=true \
  --label 'traefik.http.routers.echo-test.rule=Host(\`$ECHO_HOST\`)' \
  --label traefik.http.services.echo-test.loadbalancer.server.port=8000 \
  python:3.13.16-slim-trixie python -c '
import http.server, json
class H(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        body = json.dumps({k: v for k, v in self.headers.items() if k.lower().startswith(\"remote-\")}).encode()
        self.send_response(200); self.send_header(\"Content-Type\", \"application/json\"); self.end_headers(); self.wfile.write(body)
http.server.HTTPServer((\"0.0.0.0\", 8000), H).serve_forever()'" >/dev/null
for _ in $(seq 20); do [ "$(code -b "$TMP/sam.jar" "$ECHO_URL/")" = 200 ] && break; sleep 1; done
spoof=(-H 'Remote-User: kaldi-admin' -H 'Remote-Groups: admins' -H 'Remote-Email: admin@annex.example' -H 'Remote-Name: Mallory'
       -H 'Remote_User: kaldi-admin' -H 'Remote.Groups: admins')
expect "spoofed headers without a session still redirect to login" 302 "$(code "${spoof[@]}" "$ECHO_URL/")"
echoed=$(as sam "${spoof[@]}" "$ECHO_URL/")
if grep -q '"Remote-User": "sam"' <<<"$echoed" && grep -q '"Remote-Groups": "builders"' <<<"$echoed" \
   && ! grep -q -i -e kaldi-admin -e Mallory -e admins -e remote_ -e 'remote\.' <<<"$echoed"; then
  ok "spoofed Remote-* headers are replaced by the real identity"
else bad "spoofed Remote-* headers are replaced by the real identity" "$echoed"; fi
if [ "$extras" -ge 2 ]; then
  expect "spoofed admin group does not open logs for sam" 403 "$(code -b "$TMP/sam.jar" "${spoof[@]}" "$LOGS_URL/")"
fi

echo "Network isolation"
PG=$(compose_image postgres)
probe() { # probe NETWORK HOST -> exit status of pg_isready
  vm "docker run --rm --network $1 --label com.kaldi.annex=true $PG pg_isready -h $2 -t 3 >/dev/null 2>&1; echo \$?"
}
pdb_ip=$(vm "docker inspect annex-product-db-1 --format '{{(index .NetworkSettings.Networks \"annex-product\").IPAddress}}'")
expect "control: product network reaches product-db" 0 "$(probe annex-product product-db)"
for net in annex-edge annex-appdata; do  # platform networks (no app is on them since D88)
  expect "network $net cannot resolve product-db" 2 "$(probe "$net" product-db)"
  expect "network $net cannot reach product-db by IP" 2 "$(probe "$net" "$pdb_ip")"
done
expect "control: appdata network reaches appdata-db" 0 "$(probe annex-appdata appdata-db)"

echo "App isolation: every app on its own network (D88)"
A=annex-selftest; B=annex-selftest-other; E=annex-selftest-egress
for x in $A $B $E; do on_exit "selftest_remove $x"; done
selftest_build "$ROOT/.build/iso-a" --drop notify,webhooks                                  # Postgres and views
selftest_build "$ROOT/.build/iso-b" --name $B --drop notify,webhooks --no-postgres          # views only
selftest_build "$ROOT/.build/iso-e" --name $E --drop notify,webhooks --no-postgres --no-views --egress one.one.one.one
for d in iso-a iso-b iso-e; do
  out=$(selftest_deploy "$ROOT/.build/$d") && ok "deploy $(sed -n 's/^name: //p' "$ROOT/.build/$d/annex.yaml")" \
    || bad "deploy $d" "$(tail -3 <<<"$out")"
done
reach() { # reach CONTAINER HOST PORT -> ok, or the error's type (gaierror: the name doesn't resolve)
  vm "docker exec $1 python -c \"
import socket, sys
try:
    socket.create_connection((sys.argv[1], int(sys.argv[2])), timeout=3).close(); print('ok')
except OSError as e:
    print(type(e).__name__)
\" $2 $3"
}
members() { vm "docker network inspect $1 -f '{{range .Containers}}{{.Name}} {{end}}'" | tr ' ' '\n' | grep . | sort | xargs; }
nets_of() { vm "docker inspect $1 -f '{{range \$k, \$v := .NetworkSettings.Networks}}{{\$k}} {{end}}'" | tr ' ' '\n' | grep . | sort | xargs; }
B_IP=$(vm "docker inspect annex-app-$B --format '{{(index .NetworkSettings.Networks \"annex-app-$B\").IPAddress}}'")
expect "$A can't resolve the other app's container name" gaierror "$(reach annex-app-$A annex-app-$B 8080)"
expect "  ...or its service name" gaierror "$(reach annex-app-$A $B 8080)"
has_re "  ...or connect to its address ($B_IP)" "^(OSError|TimeoutError|ConnectionRefusedError|timeout)" "$(reach annex-app-$A "${B_IP:-10.213.255.254}" 8080)"
expect "  ...and the other way round" gaierror "$(reach annex-app-$B annex-app-$A 8080)"
expect "control: $A reaches the gateway" ok "$(reach annex-app-$A gateway 3000)"
expect "control: $A reaches appdata-db" ok "$(reach annex-app-$A appdata-db 5432)"
expect "control: $A reaches ops /healthz" 200 "$(vm "docker exec annex-app-$A python -c 'import urllib.request; print(urllib.request.urlopen(\"http://ops:8000/healthz\", timeout=5).status)'" 2>&1)"
expect "control: $B (no Postgres) can't resolve appdata-db" gaierror "$(reach annex-app-$B appdata-db 5432)"
expect "$A's container is on its own network only" "annex-app-$A" "$(nets_of annex-app-$A)"
expect "$E's container is on its own network and its egress network" "annex-app-$E annex-egress-$E" "$(nets_of annex-app-$E)"
for x in $A $B; do
  expect "annex-app-$x is internal" true "$(vm "docker network inspect annex-app-$x -f '{{.Internal}}'")"
  has_re "  ...with an explicit subnet from ANNEX_APP_SUBNETS" '^10\.213\.[0-9]+\.0/24$' "$(vm "docker network inspect annex-app-$x -f '{{range .IPAM.Config}}{{.Subnet}}{{end}}'")"
done
expect "annex-app-$A holds the app, Traefik, ops, the gateway and appdata-db" \
  "annex-app-$A annex-appdata-db-1 annex-gateway-1 annex-ops-1 annex-traefik-1" "$(members annex-app-$A)"
expect "annex-app-$B holds the app, Traefik, ops and the gateway" "annex-app-$B annex-gateway-1 annex-ops-1 annex-traefik-1" "$(members annex-app-$B)"
expect "annex-app-$E holds the app, Traefik and ops" "annex-app-$E annex-ops-1 annex-traefik-1" "$(members annex-app-$E)"
expect "annex-egress-$E is not internal" false "$(vm "docker network inspect annex-egress-$E -f '{{.Internal}}'")"
expect "  ...and holds only $E (no platform container)" "annex-app-$E" "$(members annex-egress-$E)"
# vibe-leads predates D88 and has no source in the repo: Nejc left it on the shared networks until the Goat Arcade
# demo removes it (docs/plans/goat-arcade-demo.md). Any other app there is a failure.
LEGACY_SHARED='^annex-app-vibe-leads$'
expect "no app is on annex-edge (vibe-leads excepted)" "" "$(members annex-edge | tr ' ' '\n' | grep '^annex-app-' | grep -Ev "$LEGACY_SHARED" | xargs)"
expect "no app is on annex-appdata (vibe-leads excepted)" "" "$(members annex-appdata | tr ' ' '\n' | grep '^annex-app-' | grep -Ev "$LEGACY_SHARED" | xargs)"
hasnt "egress: [] can't reach the internet (1.1.1.1:443)" ok "$(reach annex-app-$A 1.1.1.1 443)"
expect "an app that declares egress can (1.1.1.1:443)" ok "$(reach annex-app-$E 1.1.1.1 443)"
hasnt "  ...but still not another app" ok "$(reach annex-app-$E "${B_IP:-10.213.255.254}" 8080)"
expect "the routed page still answers through the front door" 200 \
  "$(code -b "$TMP/sam.jar" "https://${PATTERN//\{name\}/$A}$(setting ANNEX_PORT_SUFFIX)/healthz")"
vm "cd $DATA/platform && docker compose up -d --force-recreate --wait gateway >/dev/null 2>&1"
expect "after a recreate the gateway has left the app networks" "" "$(members annex-app-$A | tr ' ' '\n' | grep gateway || true)"
sync=$(vm "python3 $DATA/platform/vm/annex_vm.py networks-sync")
has "networks-sync reconnects it" '"connected": ["gateway"]' "$sync"
expect "  ...and $A reaches the gateway again" ok "$(reach annex-app-$A gateway 3000)"
has "  ...and reports no test app on a shared network" '"still_shared": {' "$sync"
hasnt "  ...($A)" "\"$A\": [" "$(python3 -c 'import json,sys; print(json.dumps(json.load(sys.stdin)["still_shared"]))' <<<"$sync")"
expect "networks-sync is idempotent (nothing to connect the second time)" "[]" \
  "$(vm "python3 $DATA/platform/vm/annex_vm.py networks-sync $A --web --postgres --views" | python3 -c 'import json,sys; print(json.load(sys.stdin)["connected"])')"
selftest_remove $E
expect "removing an app removes its networks" "" "$(vm "docker network ls -q --filter name=^annex-app-$E\$ --filter name=^annex-egress-$E\$")"

echo "Demo client database (Goat Arcade, D77)"
pdb() { vm "docker exec -i annex-product-db-1 psql -XAt -U postgres -d product" 2>&1; }
# scripts/push.sh brings it up and links it (D112), so a missing database or link is a failure, not a skip
if [ -z "$(vm "docker ps -q --filter name=^democlient-arcade-db-1\$ --filter health=healthy")" ]; then
  bad "the demo client's database runs" "democlient-arcade-db-1 isn't healthy (scripts/push.sh starts it; scripts/democlient.sh status)"
elif [ "$(pdb <<<"SELECT 1 FROM pg_namespace WHERE nspname = 'src_arcade'")" != 1 ]; then
  bad "product-db is linked to the demo client's database" "no src_arcade (scripts/push.sh links it; scripts/democlient.sh link)"
else
  ARC=annex-selftest-arcade
  on_exit "selftest_remove $ARC"
  selftest_build "$ROOT/.build/selftest-arcade" --name $ARC --drop notify,webhooks --views leaderboard,machine_floor
  out=$(selftest_deploy "$ROOT/.build/selftest-arcade") && ok "deploy $ARC with views leaderboard, machine_floor" \
    || bad "deploy $ARC" "$(tail -3 <<<"$out")"
  ARC_URL="https://${PATTERN//\{name\}/$ARC}$(setting ANNEX_PORT_SUFFIX)"
  gw() { c -b "$TMP/maja.jar" "$ARC_URL/gateway/$1"; }
  has "a granted view reads the client's data: leaderboard" '"rows": 5' "$(gw leaderboard)"
  has "a granted view reads the client's data: machine_floor" '"rows": 5' "$(gw machine_floor)"
  has_re "a view the app didn't request is denied: player_profiles" '"status": 40[13]' "$(gw player_profiles)"
  has "the app's role can't read the client's raw tables (src_arcade)" "permission denied for schema src_arcade" \
    "$(pdb <<<"SET ROLE app_${ARC//-/_}; SELECT count(*) FROM src_arcade.games;")"
  expect "only postgres (the views' owner) has a user mapping to the client's database" postgres \
    "$(pdb <<<"SELECT string_agg(usename, ',') FROM pg_user_mappings WHERE srvname = 'goat_arcade';")"
  expect "the views run as their owner (no security_invoker)" 0 \
    "$(pdb <<<"SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace WHERE n.nspname = 'annex_api' AND 'security_invoker=true' = ANY (c.reloptions);")"
  expect "no personal data in the views (names, emails, phones, birth dates, pay, rent)" "" \
    "$(pdb <<<"SELECT string_agg(table_name || '.' || column_name, ', ') FROM information_schema.columns WHERE table_schema = 'annex_api' AND column_name ~ '(full_name|email|phone|birth|hourly_rate|monthly_rent)';")"
  spec=$(vm "docker exec annex-app-$ARC python -c 'import os, urllib.request; print(urllib.request.urlopen(urllib.request.Request(os.environ[\"ANNEX_GATEWAY_URL\"] + \"/\", headers={\"Authorization\": \"Bearer \" + os.environ[\"ANNEX_GATEWAY_TOKEN\"]})).read().decode())'" 2>&1)
  has "the gateway's OpenAPI carries the data dictionary (column comments)" "Three-letter initials" "$spec"
  hasnt "  ...and shows the app only its own views" '"/player_profiles"' "$spec"
  has_re "the client's reader login can't write" "read-only transaction|permission denied" \
    "$(vm "docker exec democlient-arcade-db-1 psql -XAt -U annex_reader -d arcade -c 'CREATE TABLE arcade.annex_was_here (i int)'" 2>&1)"
  arc_ip=$(vm "docker inspect democlient-arcade-db-1 --format '{{(index .NetworkSettings.Networks \"annex-clientlink\").IPAddress}}'")
  expect "control: the link network reaches arcade-db" 0 "$(probe annex-clientlink arcade-db)"
  for net in annex-edge annex-appdata annex-product annex-app-$ARC; do  # the app's own network is where the gateway is (D88)
    expect "network $net cannot resolve arcade-db" 2 "$(probe "$net" arcade-db)"
    expect "network $net cannot reach arcade-db by IP" 2 "$(probe "$net" "$arc_ip")"
  done
  expect "annex-clientlink has exactly product-db and arcade-db" "annex-product-db-1 democlient-arcade-db-1" \
    "$(vm "docker network inspect annex-clientlink --format '{{range .Containers}}{{.Name}} {{end}}'" | tr ' ' '\n' | grep . | sort | xargs)"
fi

echo "Images with platform labels are refused (D83)"
EVIL=annex-selftest-evil
on_exit "selftest_remove $EVIL"
selftest_build "$ROOT/.build/selftest-evil" --name $EVIL --drop notify,webhooks --evil-label
if out=$(selftest_deploy "$ROOT/.build/selftest-evil"); then bad "an image with a traefik.* label is refused" "deploy succeeded: $(tail -3 <<<"$out")"
else has "an image with a traefik.* label is refused, naming the label" "labels that are Annex's job: traefik.http.routers.evil.rule" "$out"; fi
expect "the refused image is removed" "" "$(vm "docker image ls -q annex-app/$EVIL")"
selftest_build "$ROOT/.build/selftest-evil" --name $EVIL --drop notify,webhooks
out=$(selftest_deploy "$ROOT/.build/selftest-evil") && ok "the clean fixture still deploys ($EVIL)" \
  || bad "the clean fixture still deploys ($EVIL)" "$(tail -3 <<<"$out")"
finish
