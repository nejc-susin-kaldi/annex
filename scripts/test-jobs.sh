#!/usr/bin/env bash
# M0.3 job checks on examples/monthly-report: scheduled runs happen, `annex run`
# works, and output (CSV in /data, history in the app DB) survives a redeploy.
source "$(dirname "$0")/lib.sh"
APP=monthly-report
DB=app_monthly_report
C=annex-app-$APP

pass=0 fail=0
ok()  { pass=$((pass + 1)); printf '  \033[32mok\033[0m   %s\n' "$1"; }
bad() { fail=$((fail + 1)); printf '  \033[31mFAIL\033[0m %s (%s)\n' "$1" "$2"; }
sql() { vm "docker exec annex-appdata-db-1 psql -U postgres -d $DB -Atc \"$1\""; }

"$ROOT/annex" status "$APP" >/dev/null 2>&1 || "$ROOT/annex" deploy "$ROOT/examples/$APP" >/dev/null

echo "Schedule"
jobs=$(vm "docker logs annex-ofelia-1 2>&1 | sed 's/\x1b\[[0-9;]*m//g'")
grep -q 'registered "monthly-report-monthly"' <<<"$jobs" && ok "monthly job registered (0 6 1 * *)" || bad "monthly job registered" "not in Ofelia log"
grep -q 'registered "monthly-report-refresh"' <<<"$jobs" && ok "refresh job registered (*/10 * * * *)" || bad "refresh job registered" "not in Ofelia log"
done_runs=$(grep -c 'monthly-report-refresh.*Finished in' <<<"$jobs" || true)
if [ "$done_runs" -gt 0 ]; then
  ok "scheduler ran the refresh job $done_runs time(s): $(grep 'monthly-report-refresh.*Finished in' <<<"$jobs" | tail -1 | grep -o 'Finished in.*')"
else
  bad "scheduler ran the refresh job" "no finished run yet; it fires every 10 minutes, try again after the next :x0"
fi

echo "Manual run"
out=$("$ROOT/annex" run "$APP" monthly 2>&1) && ok "annex run: $(head -1 <<<"$out")" || bad "annex run" "$out"

echo "Output survives a redeploy"
runs=$(sql "SELECT count(*) FROM report_runs")
csv=$(vm "docker exec $C sha256sum /data/reports/monthly-positions-2026-09.csv" | cut -c1-16)
image=$("$ROOT/annex" status "$APP" | awk '/^image/ {print $2}')
"$ROOT/annex" deploy "$ROOT/examples/$APP" >/dev/null 2>&1 || bad "redeploy" "failed"
new_image=$("$ROOT/annex" status "$APP" | awk '/^image/ {print $2}')
[ "$image" != "$new_image" ] && ok "redeployed ($new_image)" || bad "redeployed" "same image $image"
[ "$(sql "SELECT count(*) FROM report_runs")" -ge "$runs" ] && ok "report history kept ($runs runs)" || bad "report history kept" "fewer runs"
[ "$(vm "docker exec $C sha256sum /data/reports/monthly-positions-2026-09.csv" | cut -c1-16)" = "$csv" ] \
  && ok "CSV in /data kept" || bad "CSV in /data kept" "changed or missing"

echo
echo "passed $pass, failed $fail"
[ "$fail" -eq 0 ]
