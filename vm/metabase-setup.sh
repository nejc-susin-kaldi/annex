#!/usr/bin/env bash
# Runs ON THE VM (called by scripts/push.sh when Metabase is running).
# Idempotent: first-run setup with kaldi-admin as Metabase admin, demo users with
# their Annex passwords, and the product database connected read-only as
# metabase_ro, limited to the annex_api schema.
set -euo pipefail
D=${ANNEX_DATA_DIR:-/srv/annex}
cd "$D/platform"
# shellcheck disable=SC1091
. "$D/secrets/master.env"
pw() { awk -v u="$1" '$1 == u {print $2}' "$D/secrets/users.txt"; }

SESSION=
mb() { # mb METHOD PATH [json on stdin]
  local args=(-fsS -X "$1" -H 'Content-Type: application/json')
  [ -n "$SESSION" ] && args+=(-H "X-Metabase-Session: $SESSION")
  [ "$1" != GET ] && args+=(--data-binary @-)
  docker compose exec -T metabase curl "${args[@]}" "http://127.0.0.1:3000$2"
}

props=$(mb GET /api/session/properties)
if [ "$(jq -r '."has-user-setup"' <<<"$props")" != true ]; then
  jq -n --arg token "$(jq -r '."setup-token"' <<<"$props")" --arg pw "$(pw kaldi-admin)" '{
    token: $token,
    user: {first_name: "Kaldi", last_name: "Admin", email: "admin@annex.example", password: $pw, site_name: "Annex"},
    prefs: {site_name: "Annex", site_locale: "en", allow_tracking: false}
  }' | mb POST /api/setup >/dev/null
  echo "metabase: initial setup done"
fi

SESSION=$(jq -n --arg pw "$(pw kaldi-admin)" '{username: "admin@annex.example", password: $pw}' \
  | mb POST /api/session | jq -r .id)

if ! mb GET /api/database | jq -e '.data[] | select(.name == "Product")' >/dev/null; then
  jq -n --arg pw "$PRODUCT_DB_METABASE_RO_PASSWORD" '{
    engine: "postgres", name: "Product", is_full_sync: true,
    details: {host: "product-db", port: 5432, dbname: "product", user: "metabase_ro", password: $pw,
              ssl: false, "schema-filters-type": "inclusion", "schema-filters-patterns": "annex_api"}
  }' | mb POST /api/database >/dev/null
  echo "metabase: connected Product (annex_api, read-only)"
fi

existing=$(mb GET '/api/user?status=all' | jq -r '.data[].email')
while IFS='|' read -r login first last email; do
  grep -qx "$email" <<<"$existing" && continue
  jq -n --arg f "$first" --arg l "$last" --arg e "$email" --arg pw "$(pw "$login")" \
    '{first_name: $f, last_name: $l, email: $e, password: $pw}' | mb POST /api/user >/dev/null
  echo "metabase: created user $email"
done <<'USERS'
sam|Sam|Novak|sam@annex.example
cm|Maja|Horvat|cm@annex.example
USERS
echo "metabase: ready"
