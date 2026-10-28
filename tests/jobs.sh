#!/usr/bin/env bash
# M0.3 job checks on annex-selftest: scheduled runs happen, `annex run` works, and output (rows in the app's
# database, files in /data) survives a redeploy.
source "$(dirname "$0")/../scripts/lib.sh"
source "$(dirname "$0")/lib.sh"
APP=$SELFTEST
DB=app_annex_selftest
C=annex-app-$APP
DIR=$ROOT/.build/selftest-jobs
on_exit "selftest_remove $APP"

sql() { vm "docker exec annex-appdata-db-1 psql -U postgres -d $DB -Atc \"$1\""; }

selftest_build "$DIR" --drop webhooks,notify
selftest_deploy "$DIR" >/dev/null || { bad "deploy $APP" "failed"; finish; exit 1; }

echo "Schedule"
jobs=$(vm "docker logs annex-ofelia-1 2>&1 | sed 's/\x1b\[[0-9;]*m//g'")
grep -q "registered \"$APP-tick\"" <<<"$jobs" && ok "tick job registered (every minute)" || bad "tick job registered" "not in Ofelia log"
grep -q "registered \"$APP-work\"" <<<"$jobs" && ok "work job registered (never on its own)" || bad "work job registered" "not in Ofelia log"
for _ in $(seq 40); do
  jobs=$(vm "docker logs annex-ofelia-1 2>&1 | sed 's/\x1b\[[0-9;]*m//g'")
  grep -q "$APP-tick.*Finished in" <<<"$jobs" && break
  sleep 3
done
done_runs=$(grep -c "$APP-tick.*Finished in" <<<"$jobs" || true)
if [ "$done_runs" -gt 0 ]; then
  ok "scheduler ran the tick job $done_runs time(s): $(grep "$APP-tick.*Finished in" <<<"$jobs" | tail -1 | grep -o 'Finished in.*')"
else
  bad "scheduler ran the tick job" "no finished run within two minutes"
fi

echo "Manual run"
out=$("$ROOT/annex" run "$APP" work 2>&1) && ok "annex run: $(head -1 <<<"$out" | cut -c1-90)" || bad "annex run" "$out"
"$ROOT/annex" run "$APP" work >/dev/null 2>&1
bad_job=$("$ROOT/annex" run "$APP" nothing 2>&1 || true)
has "an unknown job names the jobs there are" "tick" "$bad_job"

echo "Output survives a redeploy"
runs=$(sql "SELECT count(*) FROM selftest_runs")
files=$(vm "docker exec $C sh -c 'ls /data/runs | wc -l'")
first=$(vm "docker exec $C sh -c 'cd /data/runs && sha256sum \$(ls | head -1)'" | cut -c1-16)
[ "$runs" -ge 2 ] && ok "before: $runs rows in the run history, $files files" || bad "before the redeploy" "$runs rows"
image=$("$ROOT/annex" status "$APP" | awk '/^image/ {print $2}')
selftest_deploy "$DIR" >/dev/null || bad "redeploy" "failed"
new_image=$("$ROOT/annex" status "$APP" | awk '/^image/ {print $2}')
[ "$image" != "$new_image" ] && ok "redeployed ($new_image)" || bad "redeployed" "same image $image"
[ "$(sql "SELECT count(*) FROM selftest_runs")" -ge "$runs" ] && ok "run history kept ($runs rows)" || bad "run history kept" "fewer rows"
[ "$(vm "docker exec $C sh -c 'cd /data/runs && sha256sum \$(ls | head -1)'" | cut -c1-16)" = "$first" ] \
  && ok "files in /data kept" || bad "files in /data kept" "changed or missing"
finish
