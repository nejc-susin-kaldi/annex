#!/usr/bin/env bash
# Runs ON THE VM (synced to /srv/annex/democlient by scripts/democlient.sh, which calls it). The client's side of
# the demo (D77): Goat Arcade's database, and Annex's link to it. Prints no secret values.
# Usage: vm.sh up | down | reseed [YYYY-MM-DD] | status | check | link | dictionary | psql
set -euo pipefail
D=${ANNEX_DATA_DIR:-/srv/annex}
HERE=$D/democlient
SECRETS=$D/secrets/democlient.env
STATE=$D/state/democlient-db
cd "$HERE"

dc() { docker compose -p democlient "$@"; }
die() { echo "democlient: $*" >&2; exit 1; }
arcade() { dc exec -T arcade-db psql -q -X -v ON_ERROR_STOP=1 -U postgres -d arcade "$@"; }
product() { (cd "$D/platform" && docker compose exec -T -e PGOPTIONS='-c client_min_messages=warning' product-db \
               psql -q -X -v ON_ERROR_STOP=1 -U postgres -d product "$@"); }
running() { [ "$(dc ps --status running -q arcade-db 2>/dev/null)" != "" ]; }

# the client's passwords: its admin password, and the read-only login it gives Annex
secrets() {
  [ -s "$SECRETS" ] && return
  (umask 077; printf 'POSTGRES_PASSWORD=%s\nANNEX_READER_PASSWORD=%s\n' "$(openssl rand -hex 24)" "$(openssl rand -hex 24)" > "$SECRETS")
  echo "democlient: generated $SECRETS"
}

up() {
  docker network inspect annex-clientlink >/dev/null 2>&1 \
    || die "network annex-clientlink doesn't exist: push the platform first (scripts/push.sh)"
  secrets
  mkdir -p "$STATE"
  [ -n "$(ls -A "$STATE" 2>/dev/null)" ] || echo "democlient: first start, seeding (about a minute)"
  dc up -d --wait --wait-timeout 420 >/dev/null || {
    docker logs democlient-arcade-db-1 2>&1 | grep -E 'ERROR|FATAL|psql:' | tail -5 >&2
    die "arcade-db didn't become healthy (a failed seed isn't retried: fix it, then scripts/democlient.sh reseed)"; }
  echo "democlient: arcade-db is up"
}

case "${1:-}" in
  up) up ;;
  down) dc down; echo "democlient: arcade-db stopped (data kept in $STATE)" ;;
  reseed)
    dc down >/dev/null 2>&1 || true
    # the data dir belongs to the container's postgres user: remove it as root, from the same image
    image=$(sed -n 's/^ *image: //p' compose.yaml | head -1)
    docker run --rm --network none -v "$D/state:/state" --entrypoint rm "$image" -rf /state/democlient-db
    export ARCADE_ANCHOR=${2:-}
    up ;;
  status)
    running || { echo "democlient: arcade-db is not running (scripts/democlient.sh up)"; exit 0; }
    arcade -At -F ' ' <<'SQL'
SELECT 'anchor', (max(started_at AT TIME ZONE 'Europe/Ljubljana'))::date + 1, '(data ends the day before)' FROM arcade.games;
SELECT t, (xpath('/row/n/text()', query_to_xml('SELECT count(*) AS n FROM arcade.' || t, false, true, '')))[1]::text, ''
FROM unnest(ARRAY['venues', 'machine_models', 'machines', 'machine_moves', 'players', 'games', 'machine_events', 'service_tickets',
                  'coin_collections', 'tournaments', 'tournament_entries', 'tournament_matches']) t;
SELECT 'size', pg_size_pretty(pg_database_size('arcade')), '';
SQL
    if product -At -c "SELECT 1 FROM pg_namespace WHERE nspname = 'src_arcade'" | grep -q 1; then
      echo "link: product-db is linked (src_arcade)"
    else
      echo "link: not linked yet (scripts/democlient.sh link)"
    fi ;;
  check)
    running || die "arcade-db is not running (scripts/democlient.sh up)"
    arcade -F ' | ' -A -P footer=off < checks.sql ;;
  link)
    running || die "arcade-db is not running (scripts/democlient.sh up)"
    # shellcheck disable=SC1090
    . "$SECRETS"
    product -v reader_pw="$ANNEX_READER_PASSWORD" -f /docker-entrypoint-initdb.d/clientlink/link.sql
    product -f /docker-entrypoint-initdb.d/clientlink/api_views_arcade.sql
    echo "democlient: product-db linked; the Goat Arcade views are in annex_api" ;;
  dictionary) product -At < dictionary.sql ;;
  psql) dc exec arcade-db psql -U postgres -d arcade ;;
  *) die "usage: vm.sh up | down | reseed [YYYY-MM-DD] | status | check | link | dictionary | psql" ;;
esac
