#!/usr/bin/env bash
# Render, sync the platform bundle to the VM and (re)start it with the VM's Compose.
# Usage: scripts/push.sh [--extras]   (--extras also starts Metabase and Dozzle)
source "$(dirname "$0")/lib.sh"

profile=()
[ "${1:-}" = --extras ] && profile=(--profile extras)

render
log "Syncing platform to $SSH_HOST:$DATA/platform"
rsync -rlpt --delete "$ROOT/.build/platform/" "$SSH_HOST:$DATA/platform/"

log "Starting platform ${profile[*]:-(core)}"
vm "cd $DATA/platform && docker compose ${profile[*]:-} up -d --wait --wait-timeout 420 --remove-orphans"

log "Applying gateway allowlist (db/product/api_views.sql)"
vm "cd $DATA/platform && for f in api_views.sql gateway.sql; do
      docker compose exec -T -e PGOPTIONS='-c client_min_messages=warning' product-db psql -q -v ON_ERROR_STOP=1 -U postgres -d product -f /docker-entrypoint-initdb.d/\$f
    done"

if vm "cd $DATA/platform && docker compose ps --status running --services | grep -qx metabase"; then
  log "Configuring Metabase"
  vm "ANNEX_DATA_DIR=$DATA bash $DATA/platform/vm/metabase-setup.sh"
fi

vm "docker image prune -f >/dev/null; docker builder prune -f >/dev/null"
vm "cd $DATA/platform && docker compose ps --format 'table {{.Service}}\t{{.Status}}'"
log "Home: $(setting ANNEX_URL_HOME)"
