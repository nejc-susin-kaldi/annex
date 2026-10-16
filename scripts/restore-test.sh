#!/usr/bin/env bash
# Backup/restore proof on examples/monthly-report: write marker data to the
# app's database and /data, back up, destroy both, restore, verify.
# Only touches monthly-report's own data.
source "$(dirname "$0")/lib.sh"
source "$(dirname "$0")/testlib.sh"
APP=monthly-report
DB=app_monthly_report
C=annex-app-$APP
MARK="restore-test-$(date +%Y%m%d%H%M%S)-$RANDOM"

sql() { vm "docker exec annex-appdata-db-1 psql -q -U postgres -d $DB -Atc \"SET ROLE $DB; $1\""; }

"$ROOT/annex" status "$APP" >/dev/null 2>&1 || "$ROOT/annex" deploy "$ROOT/examples/$APP" >/dev/null
"$ROOT/annex" run "$APP" monthly >/dev/null

echo "Write markers"
sql "CREATE TABLE IF NOT EXISTS restore_marker (note text); INSERT INTO restore_marker VALUES ('$MARK');" >/dev/null
vm "docker exec $C sh -c 'echo $MARK > /data/restore-marker.txt'"
runs=$(sql "SELECT count(*) FROM report_runs")
csvs=$(vm "docker exec $C sh -c 'ls /data/reports | wc -l'")
ok "marker '$MARK' in database and /data ($runs report runs, $csvs CSV files)"

echo "Back up"
archive=$("$ROOT/scripts/backup.sh" | cut -d' ' -f1)
[ -n "$archive" ] && ok "backup $archive" || bad "backup" "no archive"

echo "Destroy"
sql "DROP TABLE restore_marker; DELETE FROM report_runs;" >/dev/null
vm "docker exec $C sh -c 'rm -rf /data/reports /data/restore-marker.txt'"
expect "report history is gone" 0 "$(sql "SELECT count(*) FROM report_runs")"
expect "files are gone" 0 "$(vm "docker exec $C sh -c 'ls -A /data | wc -l'")"

echo "Restore"
if out=$("$ROOT/scripts/restore.sh" "$APP" "$archive" --yes 2>&1); then ok "$out"; else bad "restore" "$out"; fi

echo "Verify"
for _ in $(seq 30); do [ "$(vm "docker inspect -f '{{.State.Health.Status}}' $C")" = healthy ] && break; sleep 2; done
expect "app is healthy again" healthy "$(vm "docker inspect -f '{{.State.Health.Status}}' $C")"
expect "database marker is back" "$MARK" "$(sql "SELECT note FROM restore_marker WHERE note = '$MARK'")"
expect "report history is back" "$runs" "$(sql "SELECT count(*) FROM report_runs")"
expect "file marker is back" "$MARK" "$(vm "docker exec $C cat /data/restore-marker.txt")"
expect "CSV reports are back" "$csvs" "$(vm "docker exec $C sh -c 'ls /data/reports | wc -l'")"
expect "restored files belong to the app user" "10001" "$(vm "docker exec $C stat -c %u /data/restore-marker.txt")"
out=$("$ROOT/annex" run "$APP" monthly 2>&1) && ok "job still runs after restore" || bad "job still runs after restore" "$out"

sql "DROP TABLE restore_marker;" >/dev/null
vm "docker exec $C rm -f /data/restore-marker.txt"
finish
