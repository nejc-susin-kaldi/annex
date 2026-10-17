#!/usr/bin/env bash
# M1.1 acceptance checks for rollback, on annex-selftest:
#   1. a version with a broken health check is rolled back automatically, and the deploy error carries its log lines;
#   2. `annex rollback` brings back the previous version;
#   3. `annex rollback --with-data` undoes a destructive migration (the run history in Postgres and the files in /data).
# Variants are builds of the fixture (scripts/selftest.py). Removes the app at the end.
source "$(dirname "$0")/lib.sh"
source "$(dirname "$0")/testlib.sh"
use_tunnel

APP=$SELFTEST
C=annex-app-$APP
URL="https://$(setting ANNEX_HOST_PATTERN | sed "s/{name}/$APP/")$(setting ANNEX_PORT_SUFFIX)"
ANNEX="$ROOT/annex"
WORK=$ROOT/.build/test-rollback
mkdir -p "${WORK:?}" && find "${WORK:?}" -mindepth 1 -delete
on_exit "selftest_remove $APP"

expect "sam signs in" 200 "$(login sam)"
page() { c -b "$TMP/sam.jar" "$URL/" | sed 's/<[^>]*>/ /g' | tr -s ' '; }
release() { "$ANNEX" --json status "$1" | python3 -c 'import json,sys; print(json.load(sys.stdin)["release"])'; }
last_release() { "$ANNEX" --json releases "$1" | python3 -c "import json,sys; r = json.load(sys.stdin)[0]; print($2)"; }
sql() { vm "docker exec annex-appdata-db-1 psql -U postgres -d app_annex_selftest -Atc \"$1\""; }

echo "Baseline"
selftest_build "$WORK/base"
selftest_deploy "$WORK/base" >/dev/null && ok "$APP deployed" || bad "$APP deployed" "deploy failed"
base=$(release $APP)
has "it greets the user" "Hello, Sam Novak" "$(page)"

echo "1. Broken health check rolls back automatically"
selftest_build "$WORK/broken" --broken-health --env "SELFTEST_MARKER=v2 cannot reach its dependency"
if out=$(selftest_deploy "$WORK/broken"); then bad "broken deploy fails" "it succeeded"; else ok "broken deploy fails"; fi
has "error names the health gate" "failed the health gate" "$out"
has "error says it rolled back to $base" "rolled back to $base, which is live again" "$out"
has "error carries the new release's log lines" "v2 cannot reach its dependency" "$out"
expect "$base is live again" "$base" "$(release $APP)"
has "app still answers" "Hello, Sam Novak" "$(page)"
expect "release history records the failure" "failed" "$(last_release $APP 'r["result"]')"
expect "broken image is removed" "" "$(vm "docker image ls -q annex-app/$APP:$(last_release $APP '"r%d" % r["release"]')")"

echo "2. annex rollback restores the previous version"
selftest_build "$WORK/v2" --env "SELFTEST_GREETING=Hi there"
selftest_deploy "$WORK/v2" >/dev/null && ok "v2 deployed" || bad "v2 deployed" "deploy failed"
v2=$(release $APP)
has "v2 is serving" "Hi there, Sam Novak" "$(page)"
if out=$("$ANNEX" rollback $APP 2>&1); then ok "annex rollback: $(tail -1 <<<"$out")"; else bad "annex rollback" "$out"; fi
has "previous version is serving again" "Hello, Sam Novak" "$(page)"
expect "rollback is recorded as a new release" "rollback from $v2 to $base" "$(last_release $APP 'r["note"]')"
has "a bad target is refused with the options" "Possible targets" "$("$ANNEX" rollback $APP --to r999 2>&1)"

echo "3. rollback --with-data undoes a destructive migration"
"$ANNEX" run $APP work >/dev/null 2>&1
"$ANNEX" run $APP work >/dev/null 2>&1
live=$(release $APP)
runs=$(sql "SELECT count(*) FROM selftest_runs")
files=$(vm "docker exec $C sh -c 'ls /data/runs | wc -l'")
ok "before: $runs rows in the run history, $files files ($live live)"
selftest_build "$WORK/bad" --env SELFTEST_BAD_MIGRATION=1
selftest_deploy "$WORK/bad" >/dev/null && ok "v2 with the bad migration deployed" || bad "v2 deployed" "deploy failed"
expect "deploy took a data snapshot while $live was live" True \
  "$("$ANNEX" --json releases $APP | python3 -c 'import json,sys; print(json.load(sys.stdin)[1]["data_snapshot"])')"
expect "the migration dropped the history (a new table, empty)" 0 "$(sql "SELECT count(*) FROM selftest_runs")"
"$ANNEX" run $APP work >/dev/null 2>&1
vm "docker exec $C rm -rf /data/runs"
expect "v2 has one row, written after the migration" 1 "$(sql "SELECT count(*) FROM selftest_runs")"
expect "and no files" 0 "$(vm "docker exec $C sh -c 'ls /data/runs 2>/dev/null | wc -l'")"
if out=$("$ANNEX" rollback $APP --with-data --yes 2>&1); then ok "annex rollback --with-data: $(tail -1 <<<"$out")"; else bad "annex rollback --with-data" "$out"; fi
expect "history is back" "$runs" "$(sql "SELECT count(*) FROM selftest_runs")"
expect "files are back" "$files" "$(vm "docker exec $C sh -c 'ls /data/runs | wc -l'")"
expect "restored files belong to the app user" 10001 "$(vm "docker exec $C stat -c %u /data/runs")"
out=$("$ANNEX" run $APP work 2>&1) && ok "the job runs on the restored data" || bad "job after rollback" "$out"
expect "and adds to the restored history" "$((runs + 1))" "$(sql "SELECT count(*) FROM selftest_runs")"

echo "Retention"
n=$(vm "docker image ls -q annex-app/$APP | sort -u | wc -l")
[ "$n" -le 3 ] && ok "$APP keeps $n image(s) (at most 3)" || bad "$APP image retention" "$n images"
n=$(vm "ls $DATA/snapshots/$APP 2>/dev/null | wc -l")
[ "$n" -le 3 ] && ok "$APP keeps $n data snapshot(s) (at most 3)" || bad "$APP snapshot retention" "$n snapshots"

finish
