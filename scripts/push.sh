#!/usr/bin/env bash
# Render, sync the platform bundle to the VM and (re)start it with the VM's Compose.
# Usage: scripts/push.sh [--extras]   (--extras also starts Metabase and Dozzle)
source "$(dirname "$0")/lib.sh"

profile=()
[ "${1:-}" = --extras ] && profile=(--profile extras)

render
log "Syncing platform to $SSH_HOST:$DATA/platform"
rsync -rlpt --delete "$ROOT/.build/platform/" "$SSH_HOST:$DATA/platform/"

# annex-ops's database goes in before the full start: its healthcheck needs the database, so
# `up --wait` would time out on the first push. The password stays on the VM.
log "Applying annex-ops database (db/appdata/annex_ops.sql)"
vm "cd $DATA/platform && docker compose up -d --wait appdata-db >/dev/null"
vm "cd $DATA/platform && . $DATA/secrets/master.env 2>/dev/null && [ -n \"\${APPDATA_DB_OPS_PASSWORD:-}\" ] && [ -n \"\${APPDATA_DB_METABASE_USAGE_PASSWORD:-}\" ] && [ -n \"\${APPDATA_DB_HOOKS_PASSWORD:-}\" ] && [ -n \"\${APPDATA_DB_CONSOLE_PASSWORD:-}\" ]" \
  || { echo "push: APPDATA_DB_OPS_PASSWORD, APPDATA_DB_METABASE_USAGE_PASSWORD, APPDATA_DB_HOOKS_PASSWORD or APPDATA_DB_CONSOLE_PASSWORD is missing in $DATA/secrets/master.env: run scripts/bootstrap.sh first" >&2; exit 1; }
# the passwords reach psql through its environment (`-e NAME` copies it from this shell), never through an argument list (D80)
vm "cd $DATA/platform && set -a && . $DATA/secrets/master.env && set +a && docker compose exec -T -e PGOPTIONS='-c client_min_messages=warning' \
      -e APPDATA_DB_OPS_PASSWORD -e APPDATA_DB_METABASE_USAGE_PASSWORD -e APPDATA_DB_HOOKS_PASSWORD -e APPDATA_DB_CONSOLE_PASSWORD appdata-db \
      psql -q -v ON_ERROR_STOP=1 -U postgres -d postgres -f - < db/appdata/annex_ops.sql"

log "Starting platform ${profile[*]:-(core)}"
vm "cd $DATA/platform && docker compose ${profile[*]:-} up -d --build --wait --wait-timeout 420 --remove-orphans"

# every app's releases.jsonl into annex_ops.releases (idempotent; the CLI records new ones as they happen)
vm "python3 $DATA/platform/vm/annex_vm.py release-backfill >/dev/null"

# the user directory for annex-ops follows Authelia's users.yml (users change it by resetting passwords; D57)
vm "python3 $DATA/platform/vm/annex_vm.py users-sync >/dev/null"

log "Applying gateway allowlist (db/product/api_views.sql)"
vm "cd $DATA/platform && for f in api_views.sql gateway.sql; do
      docker compose exec -T -e PGOPTIONS='-c client_min_messages=warning' product-db psql -q -v ON_ERROR_STOP=1 -U postgres -d product -f /docker-entrypoint-initdb.d/\$f
    done"
# the demo client's views (D77), once scripts/democlient.sh link has connected product-db to its database
if vm "cd $DATA/platform && docker compose exec -T product-db psql -XAt -U postgres -d product -c \"SELECT 1 FROM pg_namespace WHERE nspname = 'src_arcade'\"" | grep -qx 1; then
  log "Applying the Goat Arcade views (db/product/clientlink/api_views_arcade.sql)"
  vm "cd $DATA/platform && docker compose exec -T -e PGOPTIONS='-c client_min_messages=warning' product-db psql -q -X -v ON_ERROR_STOP=1 -U postgres -d product -f /docker-entrypoint-initdb.d/clientlink/api_views_arcade.sql"
fi

if vm "cd $DATA/platform && docker compose ps --status running --services | grep -qx metabase"; then
  log "Configuring Metabase"
  vm "ANNEX_DATA_DIR=$DATA bash $DATA/platform/vm/metabase-setup.sh"
fi

vm "docker image prune -f >/dev/null; docker builder prune -f --max-used-space 1500mb >/dev/null"
vm "cd $DATA/platform && docker compose ps --format 'table {{.Service}}\t{{.Status}}'"
log "Home: $(setting ANNEX_URL_HOME)"
