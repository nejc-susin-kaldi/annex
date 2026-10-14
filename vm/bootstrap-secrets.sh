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

mkdir -p "$S/authelia" "$S/apps" "$D/state/authelia" "$D/state/product-db" "$D/state/appdata-db" \
         "$D/apps" "$D/deploy" "$D/backups" "$D/dumps" "$D/certs" "$D/platform"

rand() { openssl rand -hex "${1:-24}"; }
password() { openssl rand -base64 24 | tr -dc 'A-Za-z0-9' | head -c 20; }

if [ ! -s "$S/master.env" ]; then
  cat > "$S/master.env" <<VARS
PRODUCT_DB_POSTGRES_PASSWORD=$(rand)
PRODUCT_DB_AUTHENTICATOR_PASSWORD=$(rand)
PRODUCT_DB_METABASE_RO_PASSWORD=$(rand)
GATEWAY_JWT_SECRET=$(rand 32)
APPDATA_DB_POSTGRES_PASSWORD=$(rand)
APPDATA_DB_METABASE_PASSWORD=$(rand)
VARS
  echo "bootstrap: generated master.env"
fi
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

# Demo users: login, display name, email, groups
USERS=(
  "sam|Sam Novak|sam@annex.example|builders"
  "cm|Maja Horvat|cm@annex.example|users"
  "kaldi-admin|Kaldi Admin|admin@annex.example|admins,builders"
)
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
  echo "bootstrap: wrote authelia/users.yml"
fi
echo "bootstrap: secrets ready in $S"
