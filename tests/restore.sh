#!/usr/bin/env bash
# Backup/restore proof on annex-selftest: write marker data to the app's database and /data, back up,
# destroy both, restore, verify. Only touches the self-test app's own data, and removes the app at the end.
source "$(dirname "$0")/../scripts/lib.sh"
source "$(dirname "$0")/lib.sh"
APP=$SELFTEST
DB=app_annex_selftest
DIR=$ROOT/.build/selftest-restore
on_exit "selftest_remove $APP"
C=annex-app-$APP
MARK="restore-test-$(date +%Y%m%d%H%M%S)-$RANDOM"

sql() { vm "docker exec annex-appdata-db-1 psql -q -U postgres -d $DB -Atc \"SET ROLE $DB; $1\""; }

selftest_build "$DIR" --drop webhooks,notify
selftest_deploy "$DIR" >/dev/null
"$ROOT/annex" run "$APP" work >/dev/null

echo "Write markers"
sql "CREATE TABLE IF NOT EXISTS restore_marker (note text); INSERT INTO restore_marker VALUES ('$MARK');" >/dev/null
vm "docker exec $C sh -c 'echo $MARK > /data/restore-marker.txt'"
runs=$(sql "SELECT count(*) FROM selftest_runs")
csvs=$(vm "docker exec $C sh -c 'ls /data/runs | wc -l'")
ok "marker '$MARK' in database and /data ($runs runs, $csvs files)"

echo "Back up"
archive=$("$ROOT/scripts/backup.sh" | cut -d' ' -f1)
[ -n "$archive" ] && ok "backup $archive" || bad "backup" "no archive"

echo "Destroy"
sql "DROP TABLE restore_marker; DELETE FROM selftest_runs;" >/dev/null
vm "docker exec $C sh -c 'rm -rf /data/runs /data/restore-marker.txt'"
expect "run history is gone" 0 "$(sql "SELECT count(*) FROM selftest_runs")"
expect "files are gone" 0 "$(vm "docker exec $C sh -c 'ls -A /data | wc -l'")"

echo "Restore"
if out=$("$ROOT/scripts/restore.sh" "$APP" "$archive" --yes 2>&1); then ok "$out"; else bad "restore" "$out"; fi

echo "Verify"
for _ in $(seq 30); do [ "$(vm "docker inspect -f '{{.State.Health.Status}}' $C")" = healthy ] && break; sleep 2; done
expect "app is healthy again" healthy "$(vm "docker inspect -f '{{.State.Health.Status}}' $C")"
expect "database marker is back" "$MARK" "$(sql "SELECT note FROM restore_marker WHERE note = '$MARK'")"
expect "run history is back" "$runs" "$(sql "SELECT count(*) FROM selftest_runs")"
expect "file marker is back" "$MARK" "$(vm "docker exec $C cat /data/restore-marker.txt")"
expect "run files are back" "$csvs" "$(vm "docker exec $C sh -c 'ls /data/runs | wc -l'")"
expect "restored files belong to the app user" "10001" "$(vm "docker exec $C stat -c %u /data/restore-marker.txt")"
out=$("$ROOT/annex" run "$APP" work 2>&1) && ok "job still runs after restore" || bad "job still runs after restore" "$out"

sql "DROP TABLE restore_marker;" >/dev/null
vm "docker exec $C rm -f /data/restore-marker.txt"
finish
