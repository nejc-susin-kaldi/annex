#!/bin/bash
# Starts Kanboard under the Annex contract: uid 10001, read-only root filesystem, writable /tmp and /data only,
# listening on $PORT. Runs php-fpm and nginx side by side and stops the container when either of them stops, so
# Annex sees a crash instead of a half-working app.
set -euo pipefail

log() { # log LEVEL MSG [extra JSON fields, e.g. '"rows":3']
  printf '{"ts":"%s","level":"%s","msg":"%s"%s}\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$1" "$2" "${3:+,$3}"
}

: "${PORT:?PORT is not set (Annex sets it for web apps)}"
: "${DATABASE_URL:?DATABASE_URL is not set: annex.yaml needs storage.postgres: true}"
if [ ! -w /data ]; then
  log error "/data is not writable: annex.yaml needs storage.files: true"
  exit 1
fi
mkdir -p /data/files /data/cache /data/plugins /tmp/nginx

# Kanboard trusts the Remote-User header only from these networks: the container's own (internal) networks,
# which only Annex's platform containers share. Annex's front door strips Remote-* from clients and sets it from
# the session, so the header can be trusted on any request that reaches this container.
nets=$(ip -o -f inet addr show scope global | awk '{print $4}' | while read -r cidr; do
  eval "$(ipcalc -n -p "$cidr")"; echo "$NETWORK/$PREFIX"; done | paste -sd, -)
if [ -z "$nets" ]; then
  log error "no network address found; Kanboard would refuse every sign-in"
  exit 1
fi
printf '%s\n' "$nets" > /tmp/annex-trusted-networks

case "$(printf %s "${ANNEX_CSRF:-on}" | tr '[:upper:]' '[:lower:]')" in
  off|false|0|no) csrf=0 ;;
  *) csrf=1 ;;
esac
sed -e "s/__PORT__/${PORT}/g" -e "s/__CSRF__/${csrf}/g" /opt/annex/nginx.conf.tmpl > /tmp/nginx.conf

# The schema is migrated once here (not by the first web requests, which could race), then Kanboard's built-in
# admin/admin account is disabled: people sign in through Annex only.
php /var/www/app/cli db:migrate >/tmp/migrate.log 2>&1 || { cat /tmp/migrate.log >&2; log error "database migration failed"; exit 1; }
php /opt/annex/lockdown.php

log info "kanboard starting" "\"port\":${PORT},\"trusted_networks\":\"${nets}\",\"csrf_check\":${csrf}"
php-fpm84 -F -y /opt/annex/php-fpm.conf &
fpm=$!
nginx -e /dev/stderr -c /tmp/nginx.conf -g 'daemon off;' &
web=$!

stop() { kill -TERM "$fpm" "$web" 2>/dev/null || true; wait || true; exit 0; }
trap stop TERM INT
set +e
wait -n "$fpm" "$web"
status=$?
log error "kanboard stopped: php-fpm or nginx exited" "\"exit_code\":${status}"
kill -TERM "$fpm" "$web" 2>/dev/null
wait
exit 1
