#!/usr/bin/env bash
# M1.1 acceptance checks for rollback, on the two example apps:
#   1. a client-lookup version with a broken health check is rolled back
#      automatically, and the deploy error carries its log lines;
#   2. `annex rollback` brings back the previous version;
#   3. `annex rollback --with-data` undoes a destructive migration in monthly-report.
# Variants are built from copies of the examples in .build/test-rollback.
# Leaves both apps running their example versions.
source "$(dirname "$0")/lib.sh"
render >/dev/null
"$ROOT/scripts/tunnel.sh" up >/dev/null

PORT=${ANNEX_PUBLIC_PORT:-8443}
CA="$(mkcert -CAROOT)/rootCA.pem"
AUTH_URL=$(setting ANNEX_URL_AUTH)
CL_URL="https://$(setting ANNEX_HOST_PATTERN | sed 's/{name}/client-lookup/')$(setting ANNEX_PORT_SUFFIX)"
ANNEX="$ROOT/annex"
WORK=$ROOT/.build/test-rollback
rm -rf "$WORK" && mkdir -p "$WORK"

pass=0 fail=0
ok()  { pass=$((pass + 1)); printf '  \033[32mok\033[0m   %s\n' "$1"; }
bad() { fail=$((fail + 1)); printf '  \033[31mFAIL\033[0m %s (%s)\n' "$1" "$2"; }
expect() { if [ "$2" = "$3" ]; then ok "$1"; else bad "$1" "expected '$2', got '$3'"; fi; }
has() { if grep -q -- "$2" <<<"$3"; then ok "$1"; else bad "$1" "missing: $2"; fi; }
c() { curl -sS --cacert "$CA" --connect-to "::127.0.0.1:$PORT" "$@"; }
pw=$(vm "awk '\$1 == \"sam\" {print \$2}' $DATA/secrets/users.txt")
c -c "$WORK/sam.jar" -o /dev/null -H 'Content-Type: application/json' \
  --data "{\"username\":\"sam\",\"password\":\"$pw\",\"keepMeLoggedIn\":false}" "$AUTH_URL/api/firstfactor"
page() { c -b "$WORK/sam.jar" "$CL_URL/" | sed 's/<[^>]*>/ /g' | tr -s ' '; }
release() { "$ANNEX" --json status "$1" | python3 -c 'import json,sys; print(json.load(sys.stdin)["release"])'; }
last_release() { "$ANNEX" --json releases "$1" | python3 -c "import json,sys; r = json.load(sys.stdin)[0]; print($2)"; }
sql() { vm "docker exec annex-appdata-db-1 psql -U postgres -d app_monthly_report -Atc \"$1\""; }

echo "Baseline"
"$ANNEX" deploy "$ROOT/examples/client-lookup" >/dev/null 2>&1 && ok "client-lookup deployed" || bad "client-lookup deployed" "deploy failed"
base=$(release client-lookup)
has "it greets the user" "Hello, Sam Novak" "$(page)"

echo "1. Broken health check rolls back automatically"
cp -R "$ROOT/examples/client-lookup" "$WORK/broken"
sed -i '' 's/    return {"ok": True}/    raise RuntimeError("v2 cannot reach its dependency")/' "$WORK/broken/main.py"
if out=$("$ANNEX" deploy "$WORK/broken" 2>&1); then bad "broken deploy fails" "it succeeded"; else ok "broken deploy fails"; fi
has "error names the health gate" "failed the health gate" "$out"
has "error says it rolled back to $base" "rolled back to $base, which is live again" "$out"
has "error carries the new release's log lines" "v2 cannot reach its dependency" "$out"
expect "$base is live again" "$base" "$(release client-lookup)"
has "app still answers" "Hello, Sam Novak" "$(page)"
expect "release history records the failure" "failed" "$(last_release client-lookup 'r["result"]')"
expect "broken image is removed" "" "$(vm "docker image ls -q annex-app/client-lookup:$(last_release client-lookup '"r%d" % r["release"]')")"

echo "2. annex rollback restores the previous version"
cp -R "$ROOT/examples/client-lookup" "$WORK/v2"
sed -i '' 's/<h1>Hello, /<h1>Hi there, /' "$WORK/v2/main.py"
"$ANNEX" deploy "$WORK/v2" >/dev/null 2>&1 && ok "v2 deployed" || bad "v2 deployed" "deploy failed"
v2=$(release client-lookup)
has "v2 is serving" "Hi there, Sam Novak" "$(page)"
if out=$("$ANNEX" rollback client-lookup 2>&1); then ok "annex rollback: $(tail -1 <<<"$out")"; else bad "annex rollback" "$out"; fi
has "previous version is serving again" "Hello, Sam Novak" "$(page)"
expect "rollback is recorded as a new release" "rollback from $v2 to $base" "$(last_release client-lookup 'r["note"]')"
has "a bad target is refused with the options" "Possible targets" "$("$ANNEX" rollback client-lookup --to r999 2>&1)"

echo "3. rollback --with-data undoes a destructive migration"
"$ANNEX" deploy "$ROOT/examples/monthly-report" >/dev/null 2>&1 && ok "monthly-report deployed" || bad "monthly-report deployed" "deploy failed"
"$ANNEX" run monthly-report monthly >/dev/null
mr_base=$(release monthly-report)
runs=$(sql "SELECT count(*) FROM report_runs")
csvs=$(vm "docker exec annex-app-monthly-report sh -c 'ls /data/reports | wc -l'")
ok "before: $runs report runs, $csvs CSV files ($mr_base live)"
cp -R "$ROOT/examples/monthly-report" "$WORK/mr-v2"
printf '\n# v2: a bad migration that drops the history table\nSCHEMA = "DROP TABLE IF EXISTS report_runs; " + SCHEMA\n' >> "$WORK/mr-v2/store.py"
"$ANNEX" deploy "$WORK/mr-v2" >/dev/null 2>&1 && ok "v2 with the bad migration deployed" || bad "v2 deployed" "deploy failed"
expect "deploy took a data snapshot while $mr_base was live" True \
  "$("$ANNEX" --json releases monthly-report | python3 -c 'import json,sys; print(json.load(sys.stdin)[1]["data_snapshot"])')"
"$ANNEX" run monthly-report monthly >/dev/null
vm "docker exec annex-app-monthly-report rm -rf /data/reports"
expect "v2 lost the history" 1 "$(sql "SELECT count(*) FROM report_runs")"
expect "and the CSVs" 0 "$(vm "docker exec annex-app-monthly-report sh -c 'ls /data/reports 2>/dev/null | wc -l'")"
if out=$("$ANNEX" rollback monthly-report --with-data --yes 2>&1); then ok "annex rollback --with-data: $(tail -1 <<<"$out")"; else bad "annex rollback --with-data" "$out"; fi
expect "history is back" "$runs" "$(sql "SELECT count(*) FROM report_runs")"
expect "CSVs are back" "$csvs" "$(vm "docker exec annex-app-monthly-report sh -c 'ls /data/reports | wc -l'")"
expect "restored files belong to the app user" 10001 "$(vm "docker exec annex-app-monthly-report stat -c %u /data/reports")"
out=$("$ANNEX" run monthly-report monthly 2>&1) && ok "the job runs on the restored data" || bad "job after rollback" "$out"
expect "and adds to the restored history" "$((runs + 1))" "$(sql "SELECT count(*) FROM report_runs")"

echo "Retention"
for app in client-lookup monthly-report; do
  n=$(vm "docker image ls -q annex-app/$app | sort -u | wc -l")
  [ "$n" -le 3 ] && ok "$app keeps $n image(s) (at most 3)" || bad "$app image retention" "$n images"
  n=$(vm "ls $DATA/snapshots/$app 2>/dev/null | wc -l")
  [ "$n" -le 3 ] && ok "$app keeps $n data snapshot(s) (at most 3)" || bad "$app snapshot retention" "$n snapshots"
done

rm -rf "$WORK"
echo
echo "passed $pass, failed $fail"
[ "$fail" -eq 0 ]
