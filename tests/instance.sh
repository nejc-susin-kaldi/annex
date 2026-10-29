#!/usr/bin/env bash
# Read-only check of an instance for real use (ANNEX_DEMO=off, D118), e.g. ANNEX_ENV=kaldi tests/instance.sh.
# It changes nothing: no deploy, no sign-in, no test mode (Slack alerts keep going where they go), no tunnel. HTTP goes
# to the instance's public names with the system's trust store, as a colleague's browser does, so this machine must be
# allowed in (the security group). The suites (tests/smoke.sh and the rest) are for the VM and the local lane.
export ANNEX_READ_ONLY_CHECK=1 ANNEX_TEST_MODE=0
source "$(dirname "$0")/../scripts/lib.sh"
source "$(dirname "$0")/lib.sh"
ctl() { vm "docker exec annex-ops-1 python -m annex_ops.ctl $* --json"; }
py() { python3 -c "import json,sys; d=json.load(sys.stdin); print($1)"; }
pub() { curl -sS --max-time 15 "$@"; }
HOME_URL=$(setting ANNEX_URL_HOME)
MODE=$(setting ANNEX_TLS_MODE)

echo "Settings"
expect "the instance runs without the demo (ANNEX_DEMO)" off "$(setting ANNEX_DEMO)"
has "the platform on $WHERE was pushed from this checkout's settings (VERSION)" "env_sha256=" "$(vm "cat $DATA/platform/VERSION")"

echo "Platform"
left=$(vm "docker ps --filter label=com.kaldi.annex=true --format '{{.Names}} {{.Status}}'" | grep -v '(healthy)' | grep -v '^annex-app-' || true)
expect "every platform container is healthy" "" "$left"
ports=$(vm "docker ps --filter publish=1-65535 --format '{{.Names}}'" | grep -v '^annex-traefik-1$' || true)
expect "only Traefik publishes ports" "" "$ports"
expect "no demo client database (Goat Arcade)" "" "$(vm "docker ps -a --format '{{.Names}}'" | grep '^democlient-' || true)"
expect "the gateway has no client link (no src_arcade schema)" "" \
  "$(vm "docker exec annex-product-db-1 psql -XAt -U postgres -d product -c \"SELECT 1 FROM pg_namespace WHERE nspname = 'src_arcade'\"" 2>&1)"
expect "Metabase and Dozzle are off" "" "$(vm "docker ps --format '{{.Names}}'" | grep -E '^annex-(metabase|dozzle)-1$' || true)"
has "annex users answers" "admins" "$("$ROOT/annex" users list 2>&1)"

echo "Front door ($HOME_URL)"
out=$(pub -o /dev/null -w '%{http_code} %{redirect_url}' "$HOME_URL/" 2>&1)
has_re "anonymous is sent to the sign-in page, over a certificate this machine trusts" "^302 $(setting ANNEX_URL_AUTH)" "$out"
expect "plain HTTP is redirected to HTTPS" 302 "$(pub -o /dev/null -w '%{http_code}' "http://$(setting ANNEX_HOST_HOME)/" 2>&1)"
if [ "$MODE" = acme ]; then
  cert=$(echo | openssl s_client -connect "$(setting ANNEX_HOST_HOME):443" -servername "$(setting ANNEX_HOST_HOME)" 2>/dev/null \
           | openssl x509 -noout -issuer -ext subjectAltName -checkend $((14 * 86400)) 2>&1)
  has "the certificate is the wildcard *.$(setting ANNEX_CERT_DOMAIN)" "DNS:*.$(setting ANNEX_CERT_DOMAIN)" "$cert"
  has "  ...and is valid for at least 14 more days (Traefik renews it at 30)" "will not expire" "$cert"
  if [ "$(setting ANNEX_ACME_CA)" = https://acme-v02.api.letsencrypt.org/directory ]; then
    has "  ...from Let's Encrypt's production CA" "Let's Encrypt" "$cert"
  fi
fi
expect "the hooks host has no login, and an unknown hook is a 404" 404 \
  "$(pub -o /dev/null -w '%{http_code}' -X POST -d '{}' "$(setting ANNEX_URL_HOOKS)/h/no-such-app/x" 2>&1)"

echo "Sign-in and mail"
policy=$(setting ANNEX_AUTH_POLICY)
expect "Authelia asks for $policy on every app (ANNEX_AUTH_FACTORS)" 2 \
  "$(vm "grep -c 'policy: $policy' $DATA/platform/config/authelia/configuration.yml")"
if [ "$(setting ANNEX_MAIL_RELAY)" = on ]; then
  has "Mailpit relays every message (ANNEX_MAIL_RELAY)" "auto-relaying all new messages" "$(vm "docker logs annex-mailpit-1 2>&1" | grep relay)"
fi

echo "annex-ops"
expect "no open platform incident" "" "$(ctl incidents --open | py '" ".join(i["subject"] for i in d if i["subject"].startswith("platform:"))')"
expect "annex-ops sees the front door up" up "$(ctl health | py 'd["platform:front-door"]["state"]')"
h=$(ctl host)
expect "the newest backup is under 26 h old (scripts/backup.sh after the first push)" True "$(py 'd["backup_age_h"] is not None and d["backup_age_h"] < 26' <<<"$h")"
expect "disk under 80% full" True "$(py 'd["disk_used_pct"] < 80' <<<"$h")"
finish
