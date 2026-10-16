#!/usr/bin/env bash
# Smoke test: is everything up and wired together? Run it after every push (about 2 minutes).
# It touches each part once, cheaply, and changes nothing that matters: one redeploy of client-lookup
# (cached build), one run of monthly-report's job (one more history row, one email to cm in Mailpit).
# It doesn't prove timings, retries or "exactly one alert": that's what the full suites are for
# (scripts/test-all.sh, 30+ minutes: run them only when you changed those parts).
source "$(dirname "$0")/lib.sh"
source "$(dirname "$0")/testlib.sh"
use_tunnel
ANNEX="$ROOT/annex"
url() { echo "https://$(setting ANNEX_HOST_PATTERN | sed "s/{name}/$1/")$(setting ANNEX_PORT_SUFFIX)"; }
ctl() { vm "docker exec annex-ops-1 python -m annex_ops.ctl $* --json"; }
py() { python3 -c "import json,sys; d=json.load(sys.stdin); print($1)"; }

echo "Local"
"$ROOT/scripts/sync-runtime.sh" --check >/dev/null && ok "runtime helpers: every copy matches runtime/" \
  || bad "runtime helpers" "a copy differs: scripts/sync-runtime.sh"
out=$(cd "$ROOT" && uv run --quiet --python 3.13 --with-requirements ops/requirements.txt --with pytest pytest ops/tests -q 2>&1 | tail -1)
has "annex-ops unit tests" " passed" "$out"
(cd "$ROOT" && python3 -m unittest discover -s vm/tests -q >/dev/null 2>&1) && ok "annex_vm unit tests" || bad "annex_vm unit tests" "failed"

echo "Platform"
wait_healthy 120 >/dev/null || true
left=$(vm "docker ps --filter label=com.kaldi.annex=true --format '{{.Names}} {{.Status}}'" | grep -v '(healthy)' | grep -v '^annex-app-' || true)
expect "every platform container is healthy" "" "$left"
ports=$(vm "docker ps --filter publish=1-65535 --format '{{.Names}}'" | grep -v '^annex-traefik-1$' || true)
expect "only Traefik publishes ports" "" "$ports"
for u in sam cm kaldi-admin; do expect "$u signs in" 200 "$(login "$u")"; done
expect "the discovery page" 200 "$(code -b "$TMP/cm.jar" "$HOME_URL/")"
expect "anonymous is sent to login" 302 "$(code "$HOME_URL/")"
expect "the status page" 200 "$(code -b "$TMP/cm.jar" "$(setting ANNEX_URL_STATUS)/")"
expect "Mailpit is admins only" "403 200" "$(code -b "$TMP/cm.jar" "$(setting ANNEX_URL_MAIL)/") $(code -b "$TMP/kaldi-admin.jar" "$(setting ANNEX_URL_MAIL)/")"
has "annex users lists the accounts" "kaldi-admin" "$("$ANNEX" users list 2>&1)"

echo "Deploy, identity, gateway"
out=$("$ANNEX" deploy "$ROOT/examples/client-lookup" 2>&1) && ok "annex deploy examples/client-lookup ($(grep -o 'r[0-9]* is live' <<<"$out"))" \
  || bad "annex deploy client-lookup" "$(tail -3 <<<"$out")"
page=$(c -b "$TMP/cm.jar" "$(url client-lookup)/" | sed 's/<[^>]*>/ /g' | tr -s ' ')
has "it greets the signed-in user" "Hello, Maja Horvat" "$page"
has "a granted view is readable" "client_summary : allowed" "$page"
has "an ungranted view is denied" "advisor_book : denied" "$page"
st=$("$ANNEX" --json status client-lookup)
expect "annex status: health check up" up "$(py '(d.get("health_check") or {}).get("state")' <<<"$st")"
expect "annex status: no open incident" None "$(py 'd.get("incident")' <<<"$st")"

echo "Jobs and email"
mail_before=$(mail_count cm@annex.example "Monthly positions")
out=$("$ANNEX" run monthly-report monthly 2>&1) && ok "annex run monthly-report monthly" || bad "annex run" "$(tail -3 <<<"$out")"
mail_wait cm@annex.example "Monthly positions" $((mail_before + 1)) 30 && ok "cm got the report email (Mailpit)" \
  || bad "report email" "none in Mailpit"

echo "annex-ops"
expect "no open platform incident" "" "$(ctl incidents --open | py '" ".join(i["subject"] for i in d if i["subject"].startswith("platform:"))')"
h=$(ctl host)
expect "host numbers are measured (disk, memory, backup)" True "$(py 'all(d[k] is not None for k in ("disk_used_pct", "mem_available_mb", "backup_age_h"))' <<<"$h")"
expect "the newest backup is under 26 h old" True "$(py 'd["backup_age_h"] < 26' <<<"$h")"
tile=$(vm "docker exec annex-ops-1 python -c \"import urllib.request; print(urllib.request.urlopen('http://127.0.0.1:8000/tiles/client-lookup').read().decode())\"")
has "the usage tile answers, without logins" '"users_7d"' "$tile"
hasnt "  ...and without any login in it" '"sam"' "$tile"
gat=$(vm "docker exec annex-ops-1 python -c \"import urllib.request; print(urllib.request.urlopen('http://gatus:8080/api/v1/endpoints/statuses').read().decode())\"")
has "Gatus checks the front door" '"platform_front-door"' "$gat"
has "Gatus checks client-lookup" '"apps_client-lookup"' "$gat"

echo "Webhooks (the receiver)"
HOOKS=$(setting ANNEX_URL_HOOKS)
expect "the hooks host has no login, and an unknown hook is a 404" 404 "$(code -X POST -d '{}' "$HOOKS/h/no-such-app/x")"
expect "nothing outside /h/ is served there" 404 "$(code "$HOOKS/v1/me")"
renv=$(vm "docker exec annex-hooks-1 printenv" | cut -d= -f1)
hasnt "the receiver holds no Slack or internal token" "OPS_INTERNAL_TOKEN" "$renv"
finish
