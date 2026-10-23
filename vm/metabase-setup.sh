#!/usr/bin/env bash
# Runs ON THE VM (called by scripts/push.sh when Metabase is running).
# Idempotent: first-run setup with kaldi-admin as Metabase admin, the site name, an account for every Annex user with
# a line in secrets/users.txt (their Annex password) in the groups of people.json (D107), accounts of people no longer
# in Annex deactivated, and the "Goat Arcade" database (product-db's annex_api, read-only as metabase_ro, limited to
# that schema). Then vm/metabase_content.py applies goat-arcade/: column types, collections, permissions and the five
# dashboards (D109). The "Annex usage" dashboard is retired (M2.9): usage lives in the console;
# vm/metabase-retire-usage.sh removes what an earlier setup created.
# METABASE_SPEC_DIR: where people.json and goat-arcade/ are (default: the pushed config/metabase); METABASE_VERBOSE=1
# prints every card's row count.
set -euo pipefail
D=${ANNEX_DATA_DIR:-/srv/annex}
SPEC=${METABASE_SPEC_DIR:-$D/platform/config/metabase}
HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
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

# The connection: "Goat Arcade" (D109; before GA.3 it was called "Product", and an existing one is renamed in place,
# so its questions, field settings and permissions stay).
dbs=$(mb GET /api/database)
if ! jq -e '.data[] | select(.name == "Goat Arcade")' <<<"$dbs" >/dev/null; then
  old=$(jq -r '.data[] | select(.name == "Product" and .details.host == "product-db") | .id' <<<"$dbs" | head -1)
  if [ -n "$old" ]; then
    printf '%s' '{"name": "Goat Arcade"}' | mb PUT "/api/database/$old" >/dev/null
    echo "metabase: renamed the database Product to Goat Arcade"
  else
    jq -n --arg pw "$PRODUCT_DB_METABASE_RO_PASSWORD" '{
      engine: "postgres", name: "Goat Arcade", is_full_sync: true,
      details: {host: "product-db", port: 5432, dbname: "product", user: "metabase_ro", password: $pw,
                ssl: false, "schema-filters-type": "inclusion", "schema-filters-patterns": "annex_api"}
    }' | mb POST /api/database >/dev/null
    echo "metabase: connected Goat Arcade (product-db annex_api, read-only)"
  fi
fi

# The site name is a setting (not part of the first-run setup above), so an existing instance gets it too
printf '%s' '{"value": "Goat Arcade analytics"}' | mb PUT /api/setting/site-name >/dev/null

# People (D107): an account for every active Annex account that has a line in secrets/users.txt, with that password and
# the name and email from the user directory. Groups and who is in them: config/metabase/people.json. Logins without
# a users.txt line (invited by email, so the password is their own) or missing from the directory are skipped quietly.
# Memberships are only ever added. kaldi-admin is the Metabase admin made above. (Every mb call reads its body from a
# pipe or /dev/null, so the loops' own input can't be swallowed by `docker compose exec`.)
DIRECTORY=$D/state/directory/users.json
PEOPLE=$SPEC/people.json
group_id() { mb GET /api/permissions/group | jq -r --arg n "$1" '.[] | select(.name == $n) | .id' | head -1; }
if [ -s "$DIRECTORY" ] && [ -s "$PEOPLE" ]; then
  while IFS= read -r g; do
    [ -n "$(group_id "$g")" ] && continue
    jq -n --arg n "$g" '{name: $n}' | mb POST /api/permissions/group >/dev/null
    echo "metabase: created group $g"
  done < <(jq -r '.groups[]' "$PEOPLE")
  while IFS=$'\t' read -r login name email <&3; do
    [ "$login" = kaldi-admin ] && continue
    password=$(pw "$login")
    [ -n "$password" ] || continue
    found=$(mb GET '/api/user?status=all' | jq -r --arg e "$email" '.data[] | select(.email == $e) | "\(.id) \(.is_active)"' | head -1)
    uid=${found% *}
    if [ -n "$uid" ] && [ "${found#* }" = false ]; then  # back in Annex: reactivate (hygiene below deactivated it)
      printf "{}" | mb PUT "/api/user/$uid/reactivate" >/dev/null
      echo "metabase: reactivated $email"
    fi
    if [ -z "$uid" ]; then
      first=${name%% *}
      last=${name#"$first"}; last=${last# }; [ -n "$last" ] || last=$first
      uid=$(jq -n --arg f "$first" --arg l "$last" --arg e "$email" --arg pw "$password" \
              '{first_name: $f, last_name: $l, email: $e, password: $pw}' | mb POST /api/user | jq -r .id)
      echo "metabase: created user $email"
    fi
    have=$(mb GET /api/permissions/membership | jq -r --arg u "$uid" '(.[$u] // [])[] | .group_id')
    while IFS= read -r g; do
      gid=$(group_id "$g")
      [ -n "$gid" ] || continue
      grep -qx "$gid" <<<"$have" && continue
      jq -n --argjson g "$gid" --argjson u "$uid" '{group_id: $g, user_id: $u}' | mb POST /api/permissions/membership >/dev/null
      echo "metabase: $login joined $g"
    done < <(jq -r --arg l "$login" '(.members[$l] // [])[]' "$PEOPLE")
  done 3< <(jq -r 'to_entries[] | [.key, .value.name, .value.email] | @tsv' "$DIRECTORY")

  # Hygiene (D109): an active Metabase account whose email is no active Annex account's (removed, or disabled, which
  # takes it out of the directory) is deactivated, never deleted; its questions stay. Never the admin or a superuser.
  # Only with a directory that lists someone, so a broken file can't lock everyone out.
  if jq -e 'length > 0' "$DIRECTORY" >/dev/null; then
    while IFS=$'\t' read -r uid email <&3; do
      mb DELETE "/api/user/$uid" >/dev/null
      echo "metabase: deactivated $email (no active Annex account)"
    done 3< <(mb GET /api/user | jq -r --slurpfile dir "$DIRECTORY" '
      ($dir[0] | [.[].email | ascii_downcase]) as $keep
      | .data[] | select(.is_active and (.is_superuser | not) and .email != "admin@annex.example"
                         and ((.email | ascii_downcase) as $e | $keep | any(. == $e) | not))
      | [.id, .email] | @tsv')
  fi
fi

# Goat Arcade's column types, collections, permissions and dashboards (D109)
MB_CONTAINER=$(docker compose ps -q metabase) MB_SESSION=$SESSION METABASE_SPEC_DIR=$SPEC \
  python3 "$HERE/metabase_content.py"

echo "metabase: ready"
