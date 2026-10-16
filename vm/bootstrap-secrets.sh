#!/usr/bin/env bash
# Runs ON THE VM (called by scripts/bootstrap.sh). Creates the /srv/annex layout,
# generates platform secrets and demo users once, and (re)writes the per-service
# env files from them. Never overwrites an existing secret.
# Usage: bootstrap-secrets.sh <authelia-image>
set -euo pipefail
D=${ANNEX_DATA_DIR:-/srv/annex}
AUTHELIA_IMAGE=${1:?usage: bootstrap-secrets.sh <authelia-image>}
S=$D/secrets
umask 077

mkdir -p "$S/authelia" "$S/apps" "$S/hooks" "$D/state/authelia" "$D/state/product-db" "$D/state/appdata-db" \
         "$D/state/mailpit" "$D/state/directory" "$D/state/gatus/apps" "$D/state/gatus/data" "$D/state/ops" "$D/apps" "$D/deploy" "$D/backups" "$D/dumps" "$D/certs" "$D/platform"

# incoming webhooks (M1.5): one JSON file per app with its signing secrets, written by annex_vm.py provision and
# owned by uid 10001 (the ops container). The directory is the mount: replacing files in it never goes stale (D7).
chmod 755 "$S/hooks"

rand() { openssl rand -hex "${1:-24}"; }
password() { openssl rand -base64 24 | tr -dc 'A-Za-z0-9' | head -c 20; }

# master.env is never rewritten, so a key added in a later milestone is appended only if it's missing
touch "$S/master.env"
ensure_key() { # ensure_key NAME [random bytes]
  grep -q "^$1=" "$S/master.env" || { echo "$1=$(rand "${2:-24}")" >> "$S/master.env"; echo "bootstrap: generated $1"; }
}
ensure_key PRODUCT_DB_POSTGRES_PASSWORD
ensure_key PRODUCT_DB_AUTHENTICATOR_PASSWORD
ensure_key PRODUCT_DB_METABASE_RO_PASSWORD
ensure_key GATEWAY_JWT_SECRET 32
ensure_key APPDATA_DB_POSTGRES_PASSWORD
ensure_key APPDATA_DB_METABASE_PASSWORD
ensure_key APPDATA_DB_OPS_PASSWORD
ensure_key APPDATA_DB_METABASE_USAGE_PASSWORD
ensure_key OPS_INTERNAL_TOKEN 32
ensure_key APPDATA_DB_HOOKS_PASSWORD
for f in session_secret storage_encryption_key reset_password_jwt_secret; do
  [ -s "$S/authelia/$f" ] || { rand 32 > "$S/authelia/$f"; echo "bootstrap: generated authelia/$f"; }
done

# shellcheck disable=SC1091
. "$S/master.env"

cat > "$S/product-db.env" <<VARS
POSTGRES_PASSWORD=$PRODUCT_DB_POSTGRES_PASSWORD
AUTHENTICATOR_PASSWORD=$PRODUCT_DB_AUTHENTICATOR_PASSWORD
METABASE_RO_PASSWORD=$PRODUCT_DB_METABASE_RO_PASSWORD
VARS
cat > "$S/gateway.env" <<VARS
PGRST_DB_URI=postgres://authenticator:$PRODUCT_DB_AUTHENTICATOR_PASSWORD@product-db:5432/product
PGRST_JWT_SECRET=$GATEWAY_JWT_SECRET
VARS
cat > "$S/appdata-db.env" <<VARS
POSTGRES_PASSWORD=$APPDATA_DB_POSTGRES_PASSWORD
METABASE_DB_PASSWORD=$APPDATA_DB_METABASE_PASSWORD
VARS
cat > "$S/metabase.env" <<VARS
MB_DB_PASS=$APPDATA_DB_METABASE_PASSWORD
VARS
cat > "$S/ops.env" <<VARS
OPS_DATABASE_URL=postgres://annex_ops:$APPDATA_DB_OPS_PASSWORD@appdata-db:5432/annex_ops
OPS_INTERNAL_TOKEN=$OPS_INTERNAL_TOKEN
VARS
# the webhook receiver (its own container): a role that may only insert events
cat > "$S/hooks-receiver.env" <<VARS
OPS_DATABASE_URL=postgres://annex_hooks:$APPDATA_DB_HOOKS_PASSWORD@appdata-db:5432/annex_ops
VARS
# Gatus authenticates its alert webhook to ops with this token
cat > "$S/gatus.env" <<VARS
OPS_INTERNAL_TOKEN=$OPS_INTERNAL_TOKEN
VARS
# ops reads runtime tuning (state/ops/tuning.json) as uid 10001; the file only exists during tests
chmod 755 "$D/state/ops"

# Demo users, created on the first run only: login, display name, email, groups. More users: `annex users add`.
USERS=(
  "sam|Sam Novak|sam@annex.example|builders"
  "cm|Maja Horvat|cm@annex.example|users"
  "kaldi-admin|Kaldi Admin|admin@annex.example|admins,builders"
)
wrote_users=
if [ ! -s "$S/users.txt" ]; then
  : > "$S/users.txt"
  for u in "${USERS[@]}"; do echo "${u%%|*} $(password)" >> "$S/users.txt"; done
  rm -f "$S/authelia/users.yml"
  echo "bootstrap: generated demo user passwords"
fi
if [ ! -s "$S/authelia/users.yml" ]; then
  docker pull -q "$AUTHELIA_IMAGE" >/dev/null
  {
    echo "users:"
    for u in "${USERS[@]}"; do
      IFS='|' read -r login name email groups <<<"$u"
      pw=$(awk -v u="$login" '$1 == u {print $2}' "$S/users.txt")
      hash=$(docker run --rm "$AUTHELIA_IMAGE" authelia crypto hash generate argon2 --password "$pw" | sed -n 's/^Digest: //p')
      echo "  $login:"
      echo "    displayname: \"$name\""
      echo "    email: $email"
      echo "    password: \"$hash\""
      echo "    groups: [${groups//,/, }]"
    done
  } > "$S/authelia/users.yml.tmp"
  mv "$S/authelia/users.yml.tmp" "$S/authelia/users.yml"
  wrote_users=1
  echo "bootstrap: wrote authelia/users.yml"
fi
# The user directory for annex-ops (login -> name, email, groups; no password hashes). Written here only on the
# first run: after that, users are managed with `annex users` (annex_vm.py), which rewrites it from users.yml,
# so a re-run of bootstrap must not overwrite added users. Mounted as a directory (a replaced single-file bind
# mount keeps the old inode, D7) and readable by the ops container (uid 10001), unlike everything under secrets/.
chmod 755 "$D/state/directory"
if [ -n "$wrote_users" ] || [ ! -s "$D/state/directory/users.json" ]; then
  users_json=$(for u in "${USERS[@]}"; do
    IFS='|' read -r login name email groups <<<"$u"
    jq -n --arg l "$login" --arg n "$name" --arg e "$email" --arg g "$groups" \
      '{($l): {name: $n, email: $e, groups: ($g | split(","))}}'
  done | jq -s 'add')
  echo "$users_json" > "$D/state/directory/users.json.tmp"
  chmod 644 "$D/state/directory/users.json.tmp"
  mv "$D/state/directory/users.json.tmp" "$D/state/directory/users.json"
fi
echo "bootstrap: secrets ready in $S"
