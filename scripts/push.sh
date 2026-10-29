#!/usr/bin/env bash
# Render, sync the platform bundle to the VM (or the local stack: ANNEX_ENV=local, D99) and (re)start it with its Compose;
# then the demo client's database (demo/client-db/, Goat Arcade) is brought up and linked if it isn't yet (D112),
# unless ANNEX_DEMO=off (D118).
# Usage: scripts/push.sh [--extras] [--force]
#   --extras  also starts Metabase and Dozzle
#   --force   push although the VM's last pushed commit is not an ancestor of this checkout
# By default it refuses when the VM has history this checkout lacks (platform/VERSION, D85).
source "$(dirname "$0")/lib.sh"

profile=()
force=()
for arg in "$@"; do
  case "$arg" in
    --extras) profile=(--profile extras) ;;
    --force) force=(--force) ;;
    *) echo "usage: scripts/push.sh [--extras] [--force]" >&2; exit 2 ;;
  esac
done

# Per-app networks (D88): the platform containers an app needs join its network with `docker network connect`, at
# runtime. That survives a restart but not a recreate, and Compose recreates a container whenever its config changes,
# so every app's network is synced again after each step below that may recreate one. (Compose compares only the
# networks a service declares, so the extra connections never make it recreate anything by themselves.) A failed
# sync doesn't stop the push: it says which app, and `annex_vm.py networks-sync` can be run again by hand.
networks_sync() {
  vm "set -o pipefail; python3 $DATA/platform/vm/annex_vm.py networks-sync | jq -c '{synced: [.apps[] | .app + (if (.connected | length) > 0 then \" +\" + (.connected | join(\",\")) else \"\" end)], errors, still_shared, orphan_networks}'" \
    || echo "push: networks-sync reported a problem (above): those apps may be unreachable. Fix it, then run on the VM: python3 $DATA/platform/vm/annex_vm.py networks-sync" >&2
}

render
# the mail relay's settings are a human's (they hold the relay's password): never written by a script (D118)
if [ "$(setting_local ANNEX_MAIL_RELAY)" = on ] && ! vm "test -s $DATA/secrets/mailpit/relay.yaml"; then
  echo "push: ANNEX_MAIL_RELAY=on, but $DATA/secrets/mailpit/relay.yaml is missing on $WHERE: write it first (docs/new-instance.md, \"Mail\")" >&2
  exit 1
fi
# so is the tailnet's OIDC client secret (D120)
if [ -n "$(setting_local ANNEX_ACCESS_OIDC_ISSUER)" ] && ! vm "test -s $DATA/secrets/headscale/oidc_client_secret"; then
  echo "push: ANNEX_ACCESS_OIDC_ISSUER is set, but $DATA/secrets/headscale/oidc_client_secret is missing on $WHERE: write it first (docs/new-instance.md, \"Employee access\")" >&2
  exit 1
fi
# Headscale's state directory, for an instance bootstrapped before D120 (bootstrap-secrets.sh makes it on new ones)
if [ "$(setting_local ANNEX_ACCESS)" = headscale ]; then
  vm "mkdir -p $DATA/state/headscale $DATA/secrets/headscale && chmod 700 $DATA/state/headscale $DATA/secrets/headscale"
fi
# the VM's stamp first: a checkout that doesn't contain what is deployed must not overwrite it
vm_version=$(mktemp "$ROOT/.build/vm-version.XXXXXX")
trap 'rm -f "$vm_version"' EXIT
vm "if [ -f $DATA/platform/VERSION ]; then cat $DATA/platform/VERSION; fi" > "$vm_version" \
  || { echo "push: could not read $DATA/platform/VERSION on $WHERE" >&2; exit 1; }
vm_arg=$vm_version; [ -s "$vm_version" ] || vm_arg=-
python3 "$ROOT/scripts/pushcheck.py" check "$BUILD/platform/VERSION" "$vm_arg" ${force[@]+"${force[@]}"}
log "Syncing platform to $WHERE: $DATA/platform"
rsync -rlpt --delete "$BUILD/platform/" "$(dest "$DATA/platform/")"

# annex-ops's database goes in before the full start: its healthcheck needs the database, so
# `up --wait` would time out on the first push. The password stays on the VM.
log "Applying annex-ops database: roles (platform/db/appdata/bootstrap.sql), then pending migrations (platform/db/appdata/migrations, D93)"
vm "cd $DATA/platform && docker compose up -d --wait appdata-db >/dev/null"
networks_sync  # a recreated appdata-db is back on the networks of the apps with Postgres before the long start below
vm "cd $DATA/platform && . $DATA/secrets/master.env 2>/dev/null && [ -n \"\${APPDATA_DB_OPS_PASSWORD:-}\" ] && [ -n \"\${APPDATA_DB_HOOKS_PASSWORD:-}\" ] && [ -n \"\${APPDATA_DB_CONSOLE_PASSWORD:-}\" ]" \
  || { echo "push: APPDATA_DB_OPS_PASSWORD, APPDATA_DB_HOOKS_PASSWORD or APPDATA_DB_CONSOLE_PASSWORD is missing in $DATA/secrets/master.env: run scripts/bootstrap.sh first" >&2; exit 1; }
# the passwords reach psql through its environment (`-e NAME` copies it from this shell), never through an argument list (D80)
vm "cd $DATA/platform && set -a && . $DATA/secrets/master.env && set +a && docker compose exec -T -e PGOPTIONS='-c client_min_messages=warning' \
      -e APPDATA_DB_OPS_PASSWORD -e APPDATA_DB_HOOKS_PASSWORD -e APPDATA_DB_CONSOLE_PASSWORD appdata-db \
      psql -q -v ON_ERROR_STOP=1 -U postgres -d postgres -f - < db/appdata/bootstrap.sql"
# numbered schema files, each in one transaction, recorded in annex_ops.schema_version; stops at the first failure and names it (D93)
vm "cd $DATA/platform && python3 vm/migrate.py"

log "Starting platform ${profile[*]:-(core)}"
vm "cd $DATA/platform && docker compose ${profile[*]:-} up -d --build --wait --wait-timeout 420 --remove-orphans"
log "Connecting the platform to every app's network (D88)"
networks_sync

# every app's releases.jsonl into annex_ops.releases (idempotent; the CLI records new ones as they happen)
vm "python3 $DATA/platform/vm/annex_vm.py release-backfill >/dev/null"

# the user directory for annex-ops follows Authelia's users.yml (users change it by resetting passwords; D57)
vm "python3 $DATA/platform/vm/annex_vm.py users-sync >/dev/null"

log "Applying gateway schema and log hook (platform/db/product/annex_api.sql, gateway.sql)"
vm "cd $DATA/platform && for f in annex_api.sql gateway.sql; do
      docker compose exec -T -e PGOPTIONS='-c client_min_messages=warning' product-db psql -q -v ON_ERROR_STOP=1 -U postgres -d product -f /docker-entrypoint-initdb.d/\$f
    done"
if [ "$(setting_local ANNEX_DEMO)" = off ]; then
  :  # an instance for real use (D118): no demo client, so the gateway has no views
elif [ -d "$ROOT/demo/client-db" ]; then
  # This instance's client is Goat Arcade (D77): its database comes up with the platform, so the suites can rely on
  # it (D112). Only now: its network, annex-clientlink, is the platform's and exists once the platform has started.
  # vm.sh ensure starts arcade-db if it isn't running (the first start seeds it: about a minute), links product-db
  # if it isn't linked, and otherwise only re-applies the views, as a push did before.
  log "Goat Arcade, the demo client's database (demo/client-db/, D77): up, linked, views applied"
  rsync -rlpt --delete "$ROOT/demo/client-db/" "$(dest "$DATA/democlient/")"
  vm "ANNEX_DATA_DIR=$DATA bash $DATA/democlient/vm.sh ensure"
# a checkout without demo/client-db/: the views only, if product-db is linked already
elif vm "cd $DATA/platform && docker compose exec -T product-db psql -XAt -U postgres -d product -c \"SELECT 1 FROM pg_namespace WHERE nspname = 'src_arcade'\"" | grep -qx 1; then
  log "Applying the Goat Arcade views (platform/db/product/clientlink/api_views_arcade.sql)"
  vm "cd $DATA/platform && docker compose exec -T -e PGOPTIONS='-c client_min_messages=warning' product-db psql -q -X -v ON_ERROR_STOP=1 -U postgres -d product -f /docker-entrypoint-initdb.d/clientlink/api_views_arcade.sql"
fi

if vm "cd $DATA/platform && docker compose ps --status running --services | grep -qx metabase"; then
  log "Configuring Metabase"
  vm "ANNEX_DATA_DIR=$DATA bash $DATA/platform/vm/metabase-setup.sh"
fi

if [ "$TARGET" = local ]; then
  # this machine's Docker holds the user's own images and build cache too: only Annex's dangling images go (D99)
  vm "docker image prune -f --filter label=com.kaldi.annex=true >/dev/null"
else
  vm "docker image prune -f >/dev/null; docker builder prune -f --max-used-space 1500mb >/dev/null"
fi
vm "cd $DATA/platform && docker compose ps --format 'table {{.Service}}\t{{.Status}}'"
log "Home: $(setting_local ANNEX_URL_HOME)"
