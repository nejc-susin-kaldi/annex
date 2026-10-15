#!/usr/bin/env bash
# M0.1 acceptance checks, run from the Mac through the SSH tunnel:
#   health of every container, login for all demo users, access rules,
#   identity-header spoofing, Metabase, and network isolation of product-db.
# Needs the extras profile running (scripts/extras.sh on) for the Metabase and
# Dozzle checks; they are skipped otherwise.
source "$(dirname "$0")/lib.sh"
render >/dev/null
"$ROOT/scripts/tunnel.sh" up >/dev/null

PORT=${ANNEX_PUBLIC_PORT:-8443}
CA="$(mkcert -CAROOT)/rootCA.pem"
HOME_URL=$(setting ANNEX_URL_HOME)
AUTH_URL=$(setting ANNEX_URL_AUTH)
LOGS_URL=$(setting ANNEX_URL_LOGS)
MB_URL=$(setting ANNEX_URL_METABASE)
GW_URL=$(setting ANNEX_URL_GATEWAY)
MAIL_URL=$(setting ANNEX_URL_MAIL)
STATUS_URL=$(setting ANNEX_URL_STATUS)
PATTERN=$(setting ANNEX_HOST_PATTERN)
TMP=$(mktemp -d)
trap 'rm -rf "$TMP"; vm "docker rm -f annex-test-echo >/dev/null 2>&1" || true' EXIT

pass=0 fail=0
ok()  { pass=$((pass + 1)); printf '  \033[32mok\033[0m   %s\n' "$1"; }
bad() { fail=$((fail + 1)); printf '  \033[31mFAIL\033[0m %s (%s)\n' "$1" "$2"; }
expect() { # expect DESC EXPECTED ACTUAL
  if [ "$2" = "$3" ]; then ok "$1"; else bad "$1" "expected $2, got $3"; fi
}
# curl through the tunnel: every host:port is sent to 127.0.0.1:$PORT
c() { curl -sS --cacert "$CA" --connect-to "::127.0.0.1:$PORT" "$@"; }
code() { c -o /dev/null -w '%{http_code}' "$@"; }
password() { vm "awk -v u='$1' '\$1 == u {print \$2}' $DATA/secrets/users.txt"; }
login() { # login USER -> cookie jar $TMP/USER.jar, prints HTTP status
  local pw; pw=$(password "$1")
  c -c "$TMP/$1.jar" -o "$TMP/$1.login" -w '%{http_code}' -H 'Content-Type: application/json' \
    --data "{\"username\":\"$1\",\"password\":\"$pw\",\"keepMeLoggedIn\":false,\"targetURL\":\"$HOME_URL/\"}" \
    "$AUTH_URL/api/firstfactor"
}
as() { local u=$1; shift; c -b "$TMP/$u.jar" "$@"; }
extras=$(vm "cd $DATA/platform && docker compose ps --status running --services" | grep -cx -e metabase -e dozzle || true)

echo "Health"
unhealthy=$(vm "docker ps --filter label=com.kaldi.annex=true --format '{{.Names}} {{.Status}}'" | grep -v '(healthy)' || true)
if [ -z "$unhealthy" ]; then ok "all Annex containers healthy"; else bad "all Annex containers healthy" "$unhealthy"; fi
ports=$(vm "docker ps --filter publish=1-65535 --format '{{.Names}} {{.Ports}}'" | grep -v '^annex-traefik-1 ' || true)
if [ -z "$ports" ]; then ok "only traefik publishes ports"; else bad "only traefik publishes ports" "$ports"; fi

echo "Login"
loc=$(c -o /dev/null -w '%{redirect_url}' "$HOME_URL/")
case "$loc" in "$AUTH_URL"/*) ok "anonymous request to home redirects to login" ;; *) bad "anonymous redirect" "$loc" ;; esac
expect "wrong password is rejected" 401 "$(c -o /dev/null -w '%{http_code}' -H 'Content-Type: application/json' \
  --data '{"username":"sam","password":"nope","keepMeLoggedIn":false}' "$AUTH_URL/api/firstfactor")"
for u in sam cm kaldi-admin; do
  expect "$u logs in" 200 "$(login "$u")"
  expect "$u reaches the discovery page" 200 "$(code -b "$TMP/$u.jar" "$HOME_URL/")"
done
# Homepage renders its tiles client-side from /api/services
services=$(as cm "$HOME_URL/api/services")
for s in Metabase Logs "Data gateway" Mail Status "Goat Mailer" Bekafon; do
  if grep -q "\"name\":\"$s\"" <<<"$services"; then ok "discovery page lists $s"; else bad "discovery page lists $s" "missing"; fi
done

echo "Access rules"
expect "gateway: sam is forbidden" 403 "$(code -b "$TMP/sam.jar" "$GW_URL/")"
expect "gateway: kaldi-admin passes the front door (PostgREST rejects a call without JWT)" 401 "$(code -b "$TMP/kaldi-admin.jar" "$GW_URL/")"
expect "status: anonymous is sent to login" 302 "$(code "$STATUS_URL/")"
expect "status: cm sees the status page" 200 "$(code -b "$TMP/cm.jar" "$STATUS_URL/")"
expect "mail: sam is forbidden" 403 "$(code -b "$TMP/sam.jar" "$MAIL_URL/")"
expect "mail: cm is forbidden" 403 "$(code -b "$TMP/cm.jar" "$MAIL_URL/")"
expect "mail: kaldi-admin gets Mailpit" 200 "$(code -b "$TMP/kaldi-admin.jar" "$MAIL_URL/")"
if [ "$extras" -ge 2 ]; then
  expect "logs: sam is forbidden" 403 "$(code -b "$TMP/sam.jar" "$LOGS_URL/")"
  expect "logs: cm is forbidden" 403 "$(code -b "$TMP/cm.jar" "$LOGS_URL/")"
  expect "logs: kaldi-admin gets Dozzle" 200 "$(code -b "$TMP/kaldi-admin.jar" "$LOGS_URL/")"
  expect "metabase: anonymous is sent to login" 302 "$(code "$MB_URL/")"
  expect "metabase: cm reaches Metabase" 200 "$(code -b "$TMP/cm.jar" "$MB_URL/api/health")"
  mbpw=$(password kaldi-admin)
  mbs=$(as kaldi-admin -H 'Content-Type: application/json' --data "{\"username\":\"admin@annex.example\",\"password\":\"$mbpw\"}" "$MB_URL/api/session" | sed -n 's/.*"id":"\([^"]*\)".*/\1/p')
  dbs=$(as kaldi-admin -H "X-Metabase-Session: $mbs" "$MB_URL/api/database")
  if grep -q '"name":"Product"' <<<"$dbs" && grep -q '"user":"metabase_ro"' <<<"$dbs"; then
    ok "metabase: Product database connected as metabase_ro"
  else bad "metabase: Product database connected as metabase_ro" "$(head -c 200 <<<"$dbs")"; fi
  dbid=$(python3 -c 'import json,sys; print(next(d["id"] for d in json.load(sys.stdin)["data"] if d["name"] == "Product"))' <<<"$dbs")
  q() { as kaldi-admin -H "X-Metabase-Session: $mbs" -H 'Content-Type: application/json' \
          --data "{\"database\":${dbid:-0},\"type\":\"native\",\"native\":{\"query\":\"$1\"}}" "$MB_URL/api/dataset"; }
  if grep -q '"rows":\[\[' <<<"$(q 'select count(*) from annex_api.client_summary')"; then ok "metabase: can read annex_api views"; else bad "metabase: can read annex_api views" "no rows"; fi
  if grep -q 'permission denied' <<<"$(q 'select count(*) from product.clients')"; then ok "metabase: cannot read base tables"; else bad "metabase: cannot read base tables" "no permission error"; fi
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
for net in annex-edge annex-appdata; do
  expect "app network $net cannot resolve product-db" 2 "$(probe "$net" product-db)"
  expect "app network $net cannot reach product-db by IP" 2 "$(probe "$net" "$pdb_ip")"
done
expect "control: appdata network reaches appdata-db" 0 "$(probe annex-appdata appdata-db)"

echo
echo "passed $pass, failed $fail"
[ "$fail" -eq 0 ]
