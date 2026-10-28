#!/usr/bin/env bash
# M2.2 acceptance checks: logs that outlive containers (about 8 minutes).
#   1. what an app logs is stored, searchable by release, level, text and request id;
#   2. secrets written by a fixture never reach the table (fields, text, bearer headers);
#   3. a release that fails its health gate and is rolled back keeps all its lines;
#   4. gateway reads, platform lines and Authelia's sign-in lines are stored, the noise is not;
#   5. restarting annex-ops (and being down while the app logs) loses nothing and duplicates nothing;
#   6. a flood is capped and says so; a slow or dead annex-ops never blocks a deploy;
#   7. partitions roll (retention shortened through state/ops/tuning.json, removed on exit);
#   8. a job-only app that prints gets the logging warning in `annex status` after its first scheduled run;
#   9. releases are in the database.
# Deploys annex-selftest and two job-only variants of it (one that prints, one that logs properly) and removes them at the end.
source "$(dirname "$0")/../scripts/lib.sh"
source "$(dirname "$0")/lib.sh"
use_tunnel
ANNEX="$ROOT/annex"
APP=annex-selftest
JOB=annex-selftest-job      # a job-only variant that prints (plain text)
JOBOK=annex-selftest-jobok  # a job-only variant that logs in the convention
C_APP=annex-app-$APP
PATTERN=$(setting ANNEX_HOST_PATTERN)
URL="https://${PATTERN//\{name\}/$APP}$(setting ANNEX_PORT_SUFFIX)"
TUNING=$DATA/state/ops/tuning.json
WORK=$ROOT/.build/test-logs
RUN=$(date +%H%M%S)$RANDOM
SECRET="S3CRET-$RUN-value"
rm -rf "$WORK" && mkdir -p "$WORK"
SINCE=$(date -u +%Y-%m-%dT%H:%M:%S+00)   # the table keeps the history of earlier lifetimes of an app name
sql() { vm "docker exec annex-appdata-db-1 psql -U postgres -d annex_ops -Atc \"$1\""; }
ctl() { vm "docker exec annex-ops-1 python -m annex_ops.ctl $* --json"; }
py() { python3 -c "import json,sys; d=json.load(sys.stdin); print($1)"; }
variant() { # variant DIR MARKER [selftest.py options]: the self-test app with its own marker and fake secret
  local dir=$1 marker=$2; shift 2
  selftest_build "$WORK/$dir" --env "SELFTEST_MARKER=$marker" --env "SELFTEST_SECRET=$SECRET" "$@"
}
wait_for() { # wait_for SECONDS COMMAND...: 0 once the command succeeds
  local end=$((SECONDS + $1)); shift
  while [ $SECONDS -lt $end ]; do "$@" && return 0; sleep 2; done
  return 1
}
wait_sql() { # wait_sql SECONDS SQL MIN: 0 once the query's number is at least MIN
  local end=$((SECONDS + $1))
  while [ $SECONDS -lt $end ]; do [ "$(sql "$2")" -ge "$3" ] && return 0; sleep 2; done
  return 1
}
wait_lines() { # wait_lines SECONDS MIN ARGS...: 0 once `annex --json logs ARGS` returns at least MIN lines
  local end=$((SECONDS + $1)) min=$2; shift 2
  while [ $SECONDS -lt $end ]; do [ "$("$ANNEX" --json logs "$@" 2>/dev/null | py 'len(d)')" -ge "$min" ] && return 0; sleep 2; done
  return 1
}
cleanup() {
  vm "rm -f $TUNING; docker unpause annex-ops-1 >/dev/null 2>&1; docker start annex-ops-1 >/dev/null 2>&1" >/dev/null 2>&1 || true
  "$ANNEX" remove $APP --yes >/dev/null 2>&1 || true
  "$ANNEX" remove $JOB --yes >/dev/null 2>&1 || true
  "$ANNEX" remove $JOBOK --yes >/dev/null 2>&1 || true
}
on_exit cleanup

echo "Setup"
expect "sam logs in" 200 "$(login sam)"
variant v1 "marker-one-$RUN"
out=$(selftest_deploy "$WORK/v1") && ok "$APP deployed ($(grep -o 'r[0-9]* is live' <<<"$out"))" || { bad "$APP deployed" "$out"; finish; exit 1; }
R1=$("$ANNEX" --json status $APP | py 'd["release"]'); R1=${R1#r}
selftest_build "$WORK/job" --name $JOB --job-only --env SELFTEST_LOG=plain
out=$(selftest_deploy "$WORK/job") && ok "$JOB deployed (a job-only app that prints)" || bad "$JOB deployed" "$out"
selftest_build "$WORK/jobok" --name $JOBOK --job-only
selftest_deploy "$WORK/jobok" >/dev/null && ok "$JOBOK deployed (a job-only app that logs in the convention)" || bad "$JOBOK deployed" "failed"
has "a deploy of a job-only app has no logging warning yet (nothing has run)" "is live" "$out"

echo "1. What an app logs is stored and searchable"
hdr=$(c -D - -o /dev/null -b "$TMP/sam.jar" "$URL/emit?level=warn&msg=hello-$RUN")
RID=$(tr -d '\r' <<<"$hdr" | sed -n 's/^[Xx]-[Rr]equest-[Ii]d: //p')
expect "the probe answered with a request id" 16 "${#RID}"
live=$(vm "docker logs $C_APP 2>&1")
has "the live container really logged the secret (so the next checks mean something)" "$SECRET" "$live"
wait_lines 20 1 $APP --grep "hello-$RUN" && ok "annex logs --grep finds the line within seconds" || bad "annex logs --grep" "not stored in 20 s"
out=$("$ANNEX" logs $APP --grep "hello-$RUN")
has "it shows time, release, level and message" " r$R1 warn  hello-$RUN" "$out"
has "its other fields are shown (not the secret ones)" "marker=marker-one-$RUN" "$out"
out=$("$ANNEX" logs $APP --request "$RID")
has "--request finds every line of one request" "hello-$RUN" "$out"
has "  ...including the access line" "request  " "$out"
expect "  ...and nothing of another request" "" "$(grep -v "request_id=$RID" <<<"$out" | grep 'request_id=' || true)"
out=$("$ANNEX" logs $APP --level warn --since 10m)
has "--level warn shows warn lines" "hello-$RUN" "$out"
hasnt "  ...and not the info lines" " info  request" "$out"
has "--level error finds the traceback (a plain line, level guessed)" "ValueError: emit asked for a traceback (marker-one-$RUN)" "$("$ANNEX" logs $APP --level error --since 10m)"
expect "the traceback's lines keep their order" True "$("$ANNEX" logs $APP --since 10m -n 500 | python3 -c '
import sys
t = sys.stdin.read()
a, b, c = t.find("Traceback (most recent call last)"), t.find("File ", t.find("Traceback")), t.find("ValueError: emit asked")
print(0 <= a < b < c)')"
has "--release shows one release's lines" "started" "$("$ANNEX" logs $APP --release "r$R1" --grep "marker-one-$RUN")"
expect "--release r999 has none" "" "$("$ANNEX" logs $APP --release r999)"
has "an unknown level lists the valid ones" "invalid choice" "$("$ANNEX" logs $APP --level fatal 2>&1 || true)"
has "an unknown release says how to write one" "Use e.g. r4" "$("$ANNEX" logs $APP --release banana 2>&1 || true)"
has "-f can't be combined with filters" "can't be combined" "$("$ANNEX" logs $APP -f --grep x 2>&1 || true)"
js=$("$ANNEX" --json logs $APP --grep "hello-$RUN")
expect "--json gives rows with a structured flag" True "$(py 'd[0]["structured"] and d[0]["fields"]["count"] == 3' <<<"$js")"

echo "2. Secrets never reach the table"
n=$(sql "SELECT count(*) FROM annex_ops.logs WHERE logs::text LIKE '%$SECRET%'")
expect "the test secret is nowhere in annex_ops.logs (fields, text, bearer headers)" 0 "$n"
expect "its field names are gone from the stored fields" 0 "$(sql "SELECT count(*) FROM annex_ops.logs WHERE app='$APP' AND (fields ? 'password' OR fields ? 'token' OR fields->'nested' ? 'api_key')")"
expect "the harmless nested field is kept" yes "$(sql "SELECT fields->'nested'->>'keep' FROM annex_ops.logs WHERE app='$APP' AND msg='hello-$RUN' LIMIT 1")"
has "the plain line is kept, with the value redacted" "login failed for sam password=[redacted] Authorization: [redacted]" "$("$ANNEX" logs $APP --grep 'login failed' --since 10m)"

echo "3. A release that fails its health gate keeps its lines"
variant v2 "marker-failed-$RUN" --broken-health
out=$(selftest_deploy "$WORK/v2") && bad "the broken deploy fails" "it succeeded" || ok "the broken deploy fails"
has "it was rolled back" "rolled back to r$R1" "$out"
R2=$("$ANNEX" --json releases $APP | py '[r["release"] for r in d if r["result"] == "failed"][0]')
expect "the container of the failed release is gone" "r$R1" "r$(vm "docker inspect -f '{{index .Config.Labels \"com.kaldi.annex.release\"}}' $C_APP")"
wait_lines 20 1 $APP --release "r$R2" || true
out=$("$ANNEX" logs $APP --release "r$R2")
has "its lines are still there: annex logs --release r$R2" "started" "$out"
has "  ...with its marker" "marker=marker-failed-$RUN" "$out"
hasnt "  ...and none of the live release's" "marker-one-$RUN" "$out"
expect "the app is back on r$R1" "r$R1" "$("$ANNEX" --json status $APP | py 'd["release"]')"

echo "4. Gateway reads, platform lines and sign-in lines"
n0=$("$ANNEX" --json logs $APP --source gateway -n 5000 | py 'len(d)')
c -o /dev/null -b "$TMP/sam.jar" "$URL/"
wait_lines 30 $((n0 + 1)) $APP --source gateway -n 5000 && ok "a page that reads a view adds gateway lines for its app" || bad "gateway lines" "none new in 30 s"
has "a gateway line says which view" "[gateway]" "$("$ANNEX" logs $APP --source gateway -n 3)"
hasnt "the app's own logs don't include the gateway lines" "[gateway]" "$("$ANNEX" logs $APP -n 200)"
expect "gateway lines carry the app's release" True "$(py 'all(r["release"] for r in d)' <<<"$("$ANNEX" --json logs $APP --source gateway -n 5)")"
expect "annex-ops's own lines are stored" True "$([ "$(sql "SELECT count(*) FROM annex_ops.logs WHERE container='annex-ops-1'")" -gt 0 ] && echo True || echo False)"
expect "the receiver's lines are stored" True "$([ "$(sql "SELECT count(*) FROM annex_ops.logs WHERE container='annex-hooks-1'")" -gt 0 ] && echo True || echo False)"
expect "health-check lines are not" 0 "$(sql "SELECT count(*) FROM annex_ops.logs WHERE container IN ('annex-ops-1','annex-hooks-1') AND msg LIKE '%GET /healthz%200%'")"
expect "nothing is shipped from Traefik or the socket proxies" 0 "$(sql "SELECT count(*) FROM annex_ops.logs WHERE container ~ 'traefik|socket'")"
bad_login=$(c -o /dev/null -w '%{http_code}' -H 'Content-Type: application/json' --data "{\"username\":\"nobody-$RUN\",\"password\":\"wrong-$RUN\",\"keepMeLoggedIn\":false}" "$AUTH_URL/api/firstfactor")
expect "a failed sign-in is refused" 401 "$bad_login"
wait_sql 30 "SELECT count(*) FROM annex_ops.logs WHERE container='annex-authelia-1' AND msg LIKE '%nobody-$RUN%'" 1 \
  && ok "Authelia's failed sign-in line is stored" || bad "Authelia sign-in line" "not stored in 30 s"
expect "the anonymous 'not authorized' noise is not" 0 "$(sql "SELECT count(*) FROM annex_ops.logs WHERE container='annex-authelia-1' AND msg LIKE '%<anonymous>%'")"
expect "the wrong password isn't in the table" 0 "$(sql "SELECT count(*) FROM annex_ops.logs WHERE logs::text LIKE '%wrong-$RUN%'")"

echo "5. Restarting annex-ops: no duplicates, no gap"
BASE=$(sql "SELECT count(*) FROM annex_ops.logs WHERE app='$APP' AND msg='burst line'")
c -o /dev/null -b "$TMP/sam.jar" "$URL/burst?n=100"
wait_sql 20 "SELECT count(*) FROM annex_ops.logs WHERE app='$APP' AND msg='burst line'" $((BASE + 100)) || true
vm "docker stop annex-ops-1 >/dev/null"
c -o /dev/null -b "$TMP/sam.jar" "$URL/burst?n=100"
vm "docker start annex-ops-1 >/dev/null"
wait_healthy 90 >/dev/null || true
wait_sql 60 "SELECT count(*) FROM annex_ops.logs WHERE app='$APP' AND msg='burst line'" $((BASE + 200)) || true
expect "200 new lines: the 100 written while ops was down are there" $((BASE + 200)) "$(sql "SELECT count(*) FROM annex_ops.logs WHERE app='$APP' AND msg='burst line'")"
expect "no line is stored twice" 0 "$(sql "SELECT count(*) FROM (SELECT ts, ns, container, msg, fields FROM annex_ops.logs WHERE app='$APP' AND msg='burst line' GROUP BY 1,2,3,4,5 HAVING count(*) > 1) x")"
sleep 6
expect "and still no duplicates after the restart's replay settled" $((BASE + 200)) "$(sql "SELECT count(*) FROM annex_ops.logs WHERE app='$APP' AND msg='burst line'")"
hz=$(vm "docker exec annex-ops-1 python -c \"import urllib.request; print(urllib.request.urlopen('http://127.0.0.1:8000/healthz').read().decode())\"")
has "/healthz lists the log followers (B1)" '"name": "follow-ship:' "$(python3 -c 'import json,sys; print(json.dumps(json.load(sys.stdin)["threads"]))' <<<"$hz")"
has "  ...the log sink's counters" '"lost"' "$hz"
expect "the burst's lines came out in order" 0 "$(sql "SELECT count(*) FROM (SELECT (fields->>'i')::int - lag((fields->>'i')::int) OVER (ORDER BY ts, ns) AS d FROM (SELECT * FROM annex_ops.logs WHERE app='$APP' AND msg='burst line' ORDER BY ts DESC, ns DESC LIMIT 100) q) z WHERE d <> 1")"

echo "6. A flood is capped; a dead annex-ops never blocks a deploy"
c -o /dev/null -b "$TMP/sam.jar" "$URL/burst?n=3000"
wait_sql 40 "SELECT count(*) FROM annex_ops.logs WHERE app='$APP' AND msg LIKE 'annex-ops dropped%'" 1 \
  && ok "one line says how many lines were dropped" || bad "dropped-lines line" "none in 40 s"
has "  ...and what the limit is" "more than 200 lines in one second" "$("$ANNEX" logs $APP --level warn --grep dropped -n 3)"
mx=$(sql "SELECT max(c) FROM (SELECT count(*) c FROM annex_ops.logs WHERE app='$APP' AND msg='burst line' GROUP BY date_trunc('second', ts)) q")
expect "no second holds more than 200 burst lines" True "$([ "$mx" -le 200 ] && echo True || echo False)"
variant v3 "marker-paused-$RUN"
vm "docker pause annex-ops-1 >/dev/null"
t0=$SECONDS
out=$(selftest_deploy "$WORK/v3") && ok "a deploy succeeds with annex-ops paused" || bad "deploy with annex-ops paused" "$(tail -3 <<<"$out")"
dt=$((SECONDS - t0))
has "it warns that the old container's last lines weren't stored" "couldn't store annex-app-$APP's last log lines" "$out"
expect "  ...and wasn't held up for long (under 90 s, health gate included)" True "$([ "$dt" -lt 90 ] && echo True || echo False)"
vm "docker unpause annex-ops-1 >/dev/null"
wait_healthy 60 >/dev/null || true
out=$("$ANNEX" deploy "$WORK/v3" 2>&1) && ok "a normal redeploy drains cleanly" || bad "redeploy" "$out"
hasnt "  ...without a warning" "couldn't store" "$out"

echo "7. Partitions roll"
today=$(sql "SELECT (now() at time zone 'utc')::date")
expect "partitions exist for today and two days ahead" 3 "$(sql "SELECT count(*) FROM pg_inherits i JOIN pg_class c ON c.oid=i.inhrelid WHERE i.inhparent='annex_ops.logs'::regclass AND c.relname >= 'logs_' || to_char(now() at time zone 'utc','YYYYMMDD') AND c.relname <= 'logs_' || to_char((now() at time zone 'utc') + interval '2 days','YYYYMMDD')")"
sql "CREATE TABLE IF NOT EXISTS annex_ops.logs_19990101 PARTITION OF annex_ops.logs FOR VALUES FROM ('1999-01-01 00:00:00+00') TO ('1999-01-02 00:00:00+00')" >/dev/null
sql "INSERT INTO annex_ops.logs (ts, app, container, stream, msg, h) VALUES ('1999-01-01 10:00:00+00', 'old', 'x', 'stdout', 'ancient', md5('ancient')::uuid)" >/dev/null
expect "an ancient partition with a line is there" 1 "$(sql "SELECT count(*) FROM annex_ops.logs_19990101")"
vm "printf '%s' '{\"log_retention_days\": 2, \"log_housekeeping_s\": 5}' > $TUNING && chmod 644 $TUNING"; ops_state_seen
wait_wait() { [ "$(sql "SELECT count(*) FROM pg_class WHERE relname='logs_19990101'")" = 0 ]; }
wait_for 40 wait_wait && ok "housekeeping dropped it (a whole partition, with retention shortened to 2 days)" || bad "partition roll" "still there after 40 s"
expect "today's lines are untouched" True "$([ "$(sql "SELECT count(*) FROM annex_ops.logs WHERE app='$APP'")" -gt 0 ] && echo True || echo False)"
vm "rm -f $TUNING"
ok "tuning file removed"

echo "8. A job-only app that prints is warned about after its first run"
wait_lines 100 2 $JOB && ok "its first scheduled run's output is stored (found in Ofelia's log)" || bad "job output stored" "none in 100 s"
wait_lines 100 2 $JOBOK || true
has "the job's lines are shown, tagged as the job's" "[job:tick]" "$("$ANNEX" logs $JOB)"
has "annex status warns" "logs aren't structured" "$("$ANNEX" status $JOB 2>&1)"
has "  ...also in the JSON status an agent reads" "logs aren't structured" "$("$ANNEX" --json status $JOB 2>&1)"
hasnt "a job that logs properly gets no warning ($JOBOK)" "logs aren't structured" "$("$ANNEX" status $JOBOK 2>&1)"
expect "  ...its lines are structured (JSON fields)" True "$("$ANNEX" --json logs $JOBOK | py 'len(d) >= 2 and all(x["structured"] for x in d)')"

echo "9. Releases are in the database"
expect "every release of $APP is in annex_ops.releases" "$("$ANNEX" --json releases $APP | py 'len(d)')" "$(sql "SELECT count(*) FROM annex_ops.releases WHERE app='$APP' AND deployed_at >= '$SINCE'")"
expect "the failed release is recorded as failed, with why" True "$([ "$(sql "SELECT count(*) FROM annex_ops.releases WHERE app='$APP' AND deployed_at >= '$SINCE' AND release=$R2 AND result='failed' AND note LIKE 'rolled back to r$R1%'")" = 1 ] && echo True || echo False)"
expect "the backfill is idempotent" "$(sql "SELECT count(*) FROM annex_ops.releases")" "$(vm "python3 $DATA/platform/vm/annex_vm.py release-backfill >/dev/null; docker exec annex-appdata-db-1 psql -U postgres -d annex_ops -Atc 'SELECT count(*) FROM annex_ops.releases'")"

echo "Cleanup"
"$ANNEX" remove $APP --yes >/dev/null 2>&1 && ok "annex remove $APP" || bad "annex remove $APP" "failed"
"$ANNEX" remove $JOB --yes >/dev/null 2>&1 && ok "annex remove $JOB" || bad "annex remove $JOB" "failed"
"$ANNEX" remove $JOBOK --yes >/dev/null 2>&1 && ok "annex remove $JOBOK" || bad "annex remove $JOBOK" "failed"
has "a removed app's lines are still stored for the retention" "started" "$("$ANNEX" logs $APP --since 1h -n 3 2>&1)"
finish
