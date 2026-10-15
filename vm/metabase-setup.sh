#!/usr/bin/env bash
# Runs ON THE VM (called by scripts/push.sh when Metabase is running).
# Idempotent: first-run setup with kaldi-admin as Metabase admin, demo users with
# their Annex passwords, the product database connected read-only as metabase_ro
# (limited to the annex_api schema), and the "Annex usage" connection (metabase_usage:
# summaries only) with the "Annex usage" dashboard from config/metabase/usage-dashboard.json.
# Existing cards are left alone; --rebuild-usage deletes the usage cards and dashboard first.
set -euo pipefail
REBUILD=0
[ "${1:-}" = --rebuild-usage ] && REBUILD=1
D=${ANNEX_DATA_DIR:-/srv/annex}
cd "$D/platform"
# shellcheck disable=SC1091
. "$D/secrets/master.env"
pw() { awk -v u="$1" '$1 == u {print $2}' "$D/secrets/users.txt"; }

SESSION=
mb() { # mb METHOD PATH [json on stdin]
  local args=(-fsS -X "$1" -H 'Content-Type: application/json')
  [ -n "$SESSION" ] && args+=(-H "X-Metabase-Session: $SESSION")
  if [ "$1" = GET ] || [ "$1" = DELETE ]; then  # no body: don't let exec swallow our stdin
    docker compose exec -T metabase curl "${args[@]}" "http://127.0.0.1:3000$2" </dev/null
  else
    args+=(--data-binary @-)
    docker compose exec -T metabase curl "${args[@]}" "http://127.0.0.1:3000$2"
  fi
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

# --- Annex usage: a second connection (summaries only) and the dashboard -------------------------
SPEC=$D/platform/config/metabase/usage-dashboard.json
USAGE_DB=$(mb GET /api/database | jq -r '.data[] | select(.name == "Annex usage") | .id')
if [ -z "$USAGE_DB" ]; then
  USAGE_DB=$(jq -n --arg pw "$APPDATA_DB_METABASE_USAGE_PASSWORD" '{
    engine: "postgres", name: "Annex usage", is_full_sync: true,
    details: {host: "appdata-db", port: 5432, dbname: "annex_ops", user: "metabase_usage", password: $pw,
              ssl: false, "schema-filters-type": "inclusion", "schema-filters-patterns": "annex_ops"}
  }' | mb POST /api/database | jq -r .id)
  echo "metabase: connected Annex usage (summaries only)"
fi
COL_NAME=$(jq -r .collection "$SPEC")
COL=$(mb GET /api/collection | jq -r --arg n "$COL_NAME" '[.[] | select(.name == $n and (.archived | not)) | .id][0] // empty')
if [ -z "$COL" ]; then
  COL=$(jq -n --arg n "$COL_NAME" '{name: $n, description: "Annex platform dashboards"}' | mb POST /api/collection | jq -r .id)
  echo "metabase: created collection $COL_NAME"
fi
if [ "$REBUILD" = 1 ]; then
  for id in $(mb GET "/api/collection/$COL/items" | jq -r '.data[] | select(.model == "dashboard") | .id'); do mb DELETE "/api/dashboard/$id" >/dev/null; done
  for id in $(mb GET "/api/collection/$COL/items" | jq -r '.data[] | select(.model == "card") | .id'); do mb DELETE "/api/card/$id" >/dev/null; done
  echo "metabase: removed the usage cards and dashboard (--rebuild-usage)"
fi
items=$(mb GET "/api/collection/$COL/items")
IDS='{}'
for i in $(seq 0 $(($(jq '.cards | length' "$SPEC") - 1))); do
  name=$(jq -r ".cards[$i].name" "$SPEC")
  id=$(jq -r --arg n "$name" '[.data[] | select(.model == "card" and .name == $n) | .id][0] // empty' <<<"$items")
  if [ -z "$id" ]; then
    id=$(jq --argjson i "$i" --argjson db "$USAGE_DB" --argjson col "$COL" '.cards[$i] | {
        name, display, collection_id: $col, visualization_settings: (.visualization_settings // {}),
        dataset_query: {type: "native", database: $db, native: {query: .sql}}}' "$SPEC" | mb POST /api/card | jq -r .id)
    echo "metabase: created card $name"
  fi
  IDS=$(jq --arg n "$name" --argjson id "$id" '. + {($n): $id}' <<<"$IDS")
done
DASH_NAME=$(jq -r .dashboard "$SPEC")
DASH=$(jq -r --arg n "$DASH_NAME" '[.data[] | select(.model == "dashboard" and .name == $n) | .id][0] // empty' <<<"$items")
if [ -z "$DASH" ]; then
  DASH=$(jq -n --arg n "$DASH_NAME" --argjson col "$COL" '{name: $n, collection_id: $col}' | mb POST /api/dashboard | jq -r .id)
  echo "metabase: created dashboard $DASH_NAME"
fi
# add the cards the dashboard doesn't have yet; leave the ones it has (and their layout) alone
cur=$(mb GET "/api/dashboard/$DASH")
payload=$(jq -n --argjson cur "$cur" --slurpfile spec "$SPEC" --argjson ids "$IDS" '
  ($cur.dashcards | map({id, card_id, row, col, size_x, size_y, visualization_settings, parameter_mappings})) as $have
  | ($have | map(.card_id)) as $haveids
  | [$spec[0].cards | to_entries[] | . as $e | select(($haveids | index($ids[$e.value.name])) == null)
     | {id: (-1 - $e.key), card_id: $ids[$e.value.name], row: $e.value.row, col: $e.value.col, size_x: $e.value.size_x,
        size_y: $e.value.size_y, visualization_settings: {}, parameter_mappings: []}] as $new
  | {dashcards: ($have + $new), added: ($new | length)}')
if [ "$(jq .added <<<"$payload")" -gt 0 ]; then
  jq 'del(.added)' <<<"$payload" | mb PUT "/api/dashboard/$DASH" >/dev/null
  echo "metabase: dashboard $DASH_NAME has $(jq '.added' <<<"$payload") new card(s)"
fi
echo "metabase: ready"
