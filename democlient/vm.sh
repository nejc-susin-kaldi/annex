#!/usr/bin/env bash
# Runs ON THE VM (synced to /srv/annex/democlient by scripts/democlient.sh, which calls it). The client's side of
# the demo (D77): Goat Arcade's database, and Annex's link to it. Prints no secret values.
# Usage: vm.sh up | down | reseed [YYYY-MM-DD] | status | check | link [--refresh t[,t]] | ensure | dictionary | psql
#   (ensure: up if not running, link if not linked, else re-apply the views; scripts/push.sh calls it, D112)
set -euo pipefail
D=${ANNEX_DATA_DIR:-/srv/annex}
HERE=$D/democlient
SECRETS=$D/secrets/democlient.env    # Annex's copy of the reader's password (archived like Annex's other credentials)
CLIENT_ENV=$D/state/democlient.env   # the client's own file: its admin password and the reader login (never archived, D106)
STATE=$D/state/democlient-db
cd "$HERE"

dc() { docker compose -p democlient "$@"; }
die() { echo "democlient: $*" >&2; exit 1; }
arcade() { dc exec -T arcade-db psql -q -X -v ON_ERROR_STOP=1 -U postgres -d arcade "$@"; }
product() { (cd "$D/platform" && docker compose exec -T -e PGOPTIONS='-c client_min_messages=warning' product-db \
               psql -q -X -v ON_ERROR_STOP=1 -U postgres -d product "$@"); }
running() { [ "$(dc ps --status running -q arcade-db 2>/dev/null)" != "" ]; }

# The client's passwords live in the client's own file (state/, not in Annex's backups: D106): its admin password
# and the read-only login it gives Annex. Annex keeps only the reader's password, in secrets/ like its other
# credentials (product-db's user mapping holds it too). Before D106 one file in secrets/ held both: moved once.
secrets() {
  if [ ! -s "$CLIENT_ENV" ]; then
    if [ -s "$SECRETS" ] && grep -q '^POSTGRES_PASSWORD=' "$SECRETS"; then
      (umask 077; cp "$SECRETS" "$CLIENT_ENV")
      echo "democlient: moved the client's passwords to $CLIENT_ENV"
    else
      (umask 077; printf 'POSTGRES_PASSWORD=%s\nANNEX_READER_PASSWORD=%s\n' "$(openssl rand -hex 24)" "$(openssl rand -hex 24)" > "$CLIENT_ENV")
      echo "democlient: generated $CLIENT_ENV"
    fi
  fi
  local reader
  reader=$(sed -n 's/^ANNEX_READER_PASSWORD=//p' "$CLIENT_ENV")
  [ -n "$reader" ] || die "$CLIENT_ENV has no ANNEX_READER_PASSWORD"
  if [ "$(cat "$SECRETS" 2>/dev/null)" != "ANNEX_READER_PASSWORD=$reader" ]; then
    (umask 077; printf 'ANNEX_READER_PASSWORD=%s\n' "$reader" > "$SECRETS.tmp" && mv "$SECRETS.tmp" "$SECRETS")
    echo "democlient: wrote $SECRETS (the reader's password only)"
  fi
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
  arcade < role.sql
  echo "democlient: arcade-db is up (annex_reader's limits applied from role.sql)"
}

# Connect product-db to arcade-db and (re)apply the views. $1 = --refresh t[,t] or nothing.
do_link() {
  running || die "arcade-db is not running (scripts/democlient.sh up)"
  # --refresh t[,t]: a client table changed shape (D106). The foreign table is renamed aside, the current
  # definition imported, the views re-applied (CREATE OR REPLACE keeps every app's grants) and the old one
  # dropped. If a view no longer fits, the old table stays as src_arcade.<t>__old and the message says so.
  refresh=
  if [ "${1:-}" = --refresh ]; then
    refresh=${2:?--refresh needs a table name, or several separated by commas}
    [[ "$refresh" =~ ^[a-z_][a-z0-9_]*(,[a-z_][a-z0-9_]*)*$ ]] || die "--refresh takes table names only, e.g. machines,games"
  elif [ -n "${1:-}" ]; then
    die "usage: vm.sh link [--refresh t[,t]]"
  fi
  # shellcheck disable=SC1090
  . "$SECRETS"
  for t in ${refresh//,/ }; do
    product -c "DROP FOREIGN TABLE IF EXISTS src_arcade.${t}__old" -c "ALTER FOREIGN TABLE IF EXISTS src_arcade.$t RENAME TO ${t}__old"
  done
  product -v reader_pw="$ANNEX_READER_PASSWORD" -f /docker-entrypoint-initdb.d/clientlink/link.sql
  if ! product -f /docker-entrypoint-initdb.d/clientlink/api_views_arcade.sql; then
    [ -n "$refresh" ] && echo "democlient: the views no longer fit the refreshed table(s); the old definition is kept as src_arcade.<t>__old." \
      "Fix db/product/clientlink/api_views_arcade.sql, scripts/push.sh, then scripts/democlient.sh link (without --refresh)" >&2
    exit 1
  fi
  for t in ${refresh//,/ }; do
    product -c "DROP FOREIGN TABLE IF EXISTS src_arcade.${t}__old"
  done
  echo "democlient: product-db linked; the Goat Arcade views are in annex_api${refresh:+ (refreshed: $refresh)}"
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
  link) shift; do_link "$@" ;;
  ensure)
    # scripts/push.sh, after the platform is up (D112): this instance's client database comes up with the platform.
    # Starts arcade-db if it isn't running (seeding it on the first start), links product-db if it isn't linked,
    # otherwise only re-applies the views. Idempotent.
    running || up
    if product -At -c "SELECT 1 FROM pg_namespace WHERE nspname = 'src_arcade'" | grep -qx 1; then
      product -f /docker-entrypoint-initdb.d/clientlink/api_views_arcade.sql
      echo "democlient: arcade-db running, product-db linked; the Goat Arcade views re-applied"
    else
      do_link
    fi ;;
  dictionary) product -At < dictionary.sql ;;
  psql) dc exec arcade-db psql -U postgres -d arcade ;;
  *) die "usage: vm.sh up | down | reseed [YYYY-MM-DD] | status | check | link [--refresh t[,t]] | ensure | dictionary | psql" ;;
esac
