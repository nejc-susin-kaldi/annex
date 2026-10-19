#!/usr/bin/env bash
# Runs ON THE VM, by a human (docs/setup.md, step 8): removes the "Annex usage" dashboard, its cards, its collection (if empty)
# and the "Annex usage" database connection from Metabase, which an earlier vm/metabase-setup.sh created. M2.9 moved usage to the
# console. It deletes things in Metabase's own database and can't be undone except by an earlier version of the repo (git) and
# `scripts/push.sh --extras`. Idempotent: with nothing left it says so.
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
  docker compose exec -T metabase curl "${args[@]}" "http://127.0.0.1:3000$2" </dev/null
}
SESSION=$(jq -n --arg pw "$(pw kaldi-admin)" '{username: "admin@annex.example", password: $pw}' \
  | docker compose exec -T metabase curl -fsS -X POST -H 'Content-Type: application/json' --data-binary @- http://127.0.0.1:3000/api/session | jq -r .id)
n=0
for id in $(mb GET /api/dashboard | jq -r '.[] | select(.name == "Annex usage") | .id'); do mb DELETE "/api/dashboard/$id" >/dev/null; echo "metabase: removed dashboard $id"; n=$((n + 1)); done
for id in $(mb GET /api/card | jq -r '.[] | select(.name | startswith("Annex usage:")) | .id'); do mb DELETE "/api/card/$id" >/dev/null; echo "metabase: removed card $id"; n=$((n + 1)); done
COL=$(mb GET /api/collection | jq -r '[.[] | select(.name == "Annex" and (.archived | not)) | .id][0] // empty')
if [ -n "$COL" ] && [ "$(mb GET "/api/collection/$COL/items" | jq '.data | length')" = 0 ]; then
  echo '{"archived": true}' | docker compose exec -T metabase curl -fsS -X PUT -H "X-Metabase-Session: $SESSION" -H 'Content-Type: application/json' --data-binary @- "http://127.0.0.1:3000/api/collection/$COL" >/dev/null
  echo "metabase: archived the empty collection Annex"; n=$((n + 1))
fi
for id in $(mb GET /api/database | jq -r '.data[] | select(.name == "Annex usage") | .id'); do mb DELETE "/api/database/$id" >/dev/null; echo "metabase: removed the Annex usage connection"; n=$((n + 1)); done
[ "$n" = 0 ] && echo "metabase: nothing to remove: the usage dashboard is already gone"
echo "metabase: done"
