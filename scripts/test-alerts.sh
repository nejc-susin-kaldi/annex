#!/usr/bin/env bash
# M1.3 acceptance checks for monitoring and alerts, run from the Mac through the SSH tunnel.
# Deploys tests/alert-probe (an app that can be told to crash, fail its job or return 500s), shortens the alert
# thresholds through state/ops/tuning.json (removed again on exit), and for each problem checks that the owner
# and the admin get exactly ONE alert and ONE "resolved" (and a reminder if it stays open). Mail is read from
# Mailpit. The health checker's interval is shortened through the same tuning file (5 s instead of 60 s), so it takes
# a few minutes: the scheduled job (cron: once a minute) and the "nothing more follows" waits set the floor.
source "$(dirname "$0")/lib.sh"
source "$(dirname "$0")/testlib.sh"
use_tunnel

MAIL_URL=$(setting ANNEX_URL_MAIL)
PATTERN=$(setting ANNEX_HOST_PATTERN)
APP=alert-probe
C_APP=annex-app-$APP
PROBE_URL="https://${PATTERN//\{name\}/$APP}${ANNEX_PUBLIC_PORT:+:$ANNEX_PUBLIC_PORT}"
SAM=sam@annex.example
ADMIN=admin@annex.example
TUNING=$DATA/state/ops/tuning.json
SETTLE=14   # seconds to wait after an expected message, to prove no second one follows (ticks are 5 s)
cleanup() {
  rm -rf "$TMP"
  vm "rm -f $TUNING; docker start annex-gateway-1 >/dev/null 2>&1; docker start $C_APP >/dev/null 2>&1" >/dev/null 2>&1 || true
}
on_exit cleanup


# --- mail helpers (Mailpit, as kaldi-admin) ---------------------------------------------
mail_count() { # mail_count RECIPIENT SUBJECT_PREFIX
  as kaldi-admin "$MAIL_URL/api/v1/messages?limit=500" | python3 -c '
import json, sys
rcpt, prefix = sys.argv[1], sys.argv[2]
print(sum(1 for m in json.load(sys.stdin)["messages"]
          if m["Subject"].startswith(prefix) and any(t["Address"] == rcpt for t in m["To"])))' "$@"
}
mail_total() { as kaldi-admin "$MAIL_URL/api/v1/messages?limit=500" | python3 -c 'import json,sys; print(json.load(sys.stdin)["total"])'; }
mail_subjects() { as kaldi-admin "$MAIL_URL/api/v1/messages?limit=500" | python3 -c 'import json,sys; [print("   ", m["Subject"]) for m in json.load(sys.stdin)["messages"]]'; }
wait_pair() { # wait_pair PREFIX N TIMEOUT: until the owner and the admin each have N such messages
  local end=$((SECONDS + $3))
  while [ $SECONDS -lt $end ]; do
    [ "$(mail_count $SAM "$1")" -ge "$2" ] && [ "$(mail_count $ADMIN "$1")" -ge "$2" ] && return 0
    sleep 3
  done
  return 1
}
expect_pair() { # expect_pair DESC PREFIX N TIMEOUT: exactly N to sam and to the admin, and no more after SETTLE s
  local t0=$SECONDS
  wait_pair "$2" "$3" "$4" || true
  sleep "$SETTLE"
  local s a; s=$(mail_count $SAM "$2"); a=$(mail_count $ADMIN "$2")
  if [ "$s" = "$3" ] && [ "$a" = "$3" ]; then ok "$1 (after $((SECONDS - t0 - SETTLE)) s)"
  else bad "$1" "expected $3 each, got sam=$s admin=$a for '$2'"; mail_subjects; fi
}
expect_count() { # expect_count DESC RECIPIENT PREFIX N
  expect "$1" "$4" "$(mail_count "$2" "$3")"
}
health_state() { # health_state KEY -> up | down | unknown | none  (KEY: app:<name> or platform:front-door)
  vm "docker exec annex-ops-1 python -m annex_ops.ctl health --json 2>/dev/null" \
    | python3 -c 'import json,sys
try: print(json.load(sys.stdin)[sys.argv[1]]["state"])
except Exception: print("none")' "$1"
}
wait_health() { # wait_health KEY STATE TIMEOUT
  local end=$((SECONDS + $3))
  while [ $SECONDS -lt $end ]; do [ "$(health_state "$1")" = "$2" ] && return 0; sleep 2; done
  return 1
}
scenario() { echo; echo "$1"; T0=$SECONDS; }
took() { echo "  (took $((SECONDS - T0)) s)"; }
ALERT="[Annex] $APP is down"
RESOLVED="[Annex] resolved: $APP is back"

# --- setup -----------------------------------------------------------------------------------
echo "Setup"
# the incident table keeps history across runs: remember where this run starts
BASE_ID=$(vm "docker exec annex-ops-1 python -m annex_ops.ctl incidents --limit 1 --json" 2>/dev/null | python3 -c 'import json,sys; r=json.load(sys.stdin); print(r[0]["id"] if r else 0)' 2>/dev/null || echo 0)
out=$("$ROOT/annex" deploy "$ROOT/tests/$APP" 2>&1) && ok "$APP deployed" || { bad "$APP deployed" "$out"; echo "passed $pass, failed $fail"; exit 1; }
vm "printf '%s' '{\"stopped_grace_s\": 10, \"crash_window_s\": 120, \"crash_stable_s\": 30, \"error_window_s\": 60, \"reminder_first_s\": 90, \"tick_s\": 5, \"check_interval_s\": 5, \"check_timeout_s\": 3}' > $TUNING && chmod 644 $TUNING"
ok "short thresholds written to state/ops/tuning.json (removed on exit)"
expect "sam logs in" 200 "$(login sam)"
expect "kaldi-admin logs in" 200 "$(login kaldi-admin)"
c -o /dev/null -X DELETE -b "$TMP/kaldi-admin.jar" "$MAIL_URL/api/v1/messages"
expect "Mailpit is empty" 0 "$(mail_total)"
wait_health "app:$APP" up 120 && ok "the health checker reports $APP up" || bad "the health checker reports $APP up" "state: $(health_state "app:$APP")"
vm "for i in \$(seq 30); do docker exec annex-ops-1 python -m annex_ops.ctl incidents --open --json | grep -q '^\[\]' && exit 0; sleep 2; done; exit 1" \
  && ok "no incident is open before the scenarios" || bad "no incident is open before the scenarios" "$(vm "docker exec annex-ops-1 python -m annex_ops.ctl incidents --open")"
sleep 8  # let the tuning file be picked up

# --- 1. stop the app ---------------------------------------------------------------------------------
scenario "1. docker stop $C_APP (Docker and the health checker both notice: still one alert)"
vm "docker stop $C_APP >/dev/null"
expect_pair "one alert to sam and to the admin: '$ALERT'" "$ALERT" 1 90
expect_pair "one reminder after the reminder time" "[Annex] reminder: $APP is down" 1 150
wait_health "app:$APP" down 60 && ok "the health checker now reports it down too" || bad "the health checker now reports it down too" "still $(health_state "app:$APP")"
# the checker confirms "down" after its second failed check: that must attach to the open incident, not alert again
end=$((SECONDS + 60)); sigs=""
while [ $SECONDS -lt $end ]; do
  sigs=$(vm "docker exec annex-ops-1 python -m annex_ops.ctl incidents --open --app $APP --json" | python3 -c 'import json,sys; r=json.load(sys.stdin); r = [i for i in r if i["subject"] == "app:'"$APP"'"]; print(" ".join(sorted(r[0]["signals"])) if r else "")')
  case "$sigs" in *down*) break ;; esac
  sleep 4
done
expect "the one open incident carries both signals" "down stopped" "$sigs"
sleep 10
expect_count "still exactly one alert after the health checker noticed (sam)" $SAM "$ALERT" 1
expect_count "still exactly one alert after the health checker noticed (admin)" $ADMIN "$ALERT" 1
vm "docker start $C_APP >/dev/null"
expect_pair "one 'resolved' to sam and to the admin" "$RESOLVED" 1 240
took

# --- 2. crash loop -------------------------------------------------------------------------------------
scenario "2. the app exits with an error 4 times in a row"
vm "docker exec $C_APP sh -c 'echo 4 > /data/crashes'"
expect_pair "one crash-loop alert" "[Annex] $APP is crash-looping" 1 90
expect_pair "one 'resolved' once it has been stable" "$RESOLVED" 2 150
took

# --- 3. burst of 500s ---------------------------------------------------------------------------------
scenario "3. 20 requests that answer 500"
for _ in $(seq 20); do code -b "$TMP/sam.jar" "$PROBE_URL/boom" >/dev/null; done
expect_pair "one error-spike alert" "[Annex] $APP is returning server errors" 1 90
expect_pair "one 'resolved' once the errors stop" "$RESOLVED" 3 240
took

# --- 4. a failing job -----------------------------------------------------------------------------------
scenario "4. the scheduled job exits 1"
JOB_ALERT="[Annex] $APP: job \"check\" failed"
vm "docker exec $C_APP touch /data/failjob"
expect_pair "one job-failed alert" "$JOB_ALERT" 1 150
sleep 65  # the job runs again, and fails again: no second alert
expect_count "no repeat alert while it keeps failing (sam)" $SAM "$JOB_ALERT" 1
vm "docker exec $C_APP rm -f /data/failjob"
expect_pair "one 'resolved' after the next good run" "[Annex] resolved: $APP job \"check\" is passing again" 1 150
took

# --- 5. a platform service -------------------------------------------------------------------------------
scenario "5. docker stop annex-gateway-1 (platform: admins only)"
vm "docker stop annex-gateway-1 >/dev/null"
GW="[Annex] platform: gateway stopped"
wait_pair "$GW" 1 90 || true
sleep "$SETTLE"
expect_count "the admin gets one alert" $ADMIN "$GW" 1
expect_count "sam gets none" $SAM "$GW" 0
vm "docker start annex-gateway-1 >/dev/null"
RGW="[Annex] resolved: platform gateway is back"
wait_pair "$RGW" 1 90 || true
sleep "$SETTLE"
expect_count "the admin gets one 'resolved'" $ADMIN "$RGW" 1
expect_count "sam gets none" $SAM "$RGW" 0
took

# --- status and API ------------------------------------------------------------------------------------
scenario "Status"
wait_health "app:$APP" up 60 || true
st=$("$ROOT/annex" status "$APP" 2>&1)
has "annex status shows the health check up" "health_check up" "$st"
has "annex status shows no open incident" "incident     none" "$st"
has "annex status shows the check job's last run" "job check" "$st"
has "  ...with its result" "last run" "$st"
keys=$(vm "docker exec annex-ops-1 python -m annex_ops.ctl health --json" | python3 -c '
import json, sys
print(" ".join(sorted(k + "=" + v["state"] for k, v in json.load(sys.stdin).items())))')
has "the health checker lists app:$APP up" "app:$APP=up" "$keys"
has "the health checker lists platform:front-door up" "platform:front-door=up" "$keys"
services=$(as sam "$HOME_URL/api/services")
has "the discovery page lists Status" '"name":"Status"' "$services"
expect "status page (the console's /status): sam sees it" 200 "$(code -b "$TMP/sam.jar" "$(setting ANNEX_URL_CONSOLE)/status")"
has "  ...and the app is up on it" "$APP" "$(as sam "$(setting ANNEX_URL_CONSOLE)/status" | sed 's/<[^>]*>/ /g')"
history=$(vm "docker exec annex-ops-1 python -m annex_ops.ctl incidents --app $APP --json")
expect "this run's incidents are all recorded and closed (4 for $APP: stopped, crash loop, errors, job)" "4 0" "$(python3 -c '
import json, sys
r = [i for i in json.load(sys.stdin) if i["id"] > '"$BASE_ID"']
print(len(r), sum(1 for i in r if not i["resolved_at"]))' <<<"$history")"
took

# --- removing an app closes its incident without a message ----------------------------------------------------
scenario "Teardown: annex remove while an incident is open"
c -o /dev/null -X DELETE -b "$TMP/kaldi-admin.jar" "$MAIL_URL/api/v1/messages"
vm "docker stop $C_APP >/dev/null"
wait_pair "$ALERT" 1 90 || true
expect_count "the stopped app alerted once (sam)" $SAM "$ALERT" 1
"$ROOT/annex" remove "$APP" --yes >/dev/null 2>&1 && ok "annex remove $APP" || bad "annex remove $APP" "failed"
sleep 45
expect "no 'resolved' or any other message after the removal" 1 "$(mail_total)"
expect "it is no longer checked (no result in the last 20 s)" 0 "$(vm "docker exec annex-appdata-db-1 psql -U postgres -d annex_ops -Atc \"select count(*) from annex_ops.health_checks where app = '$APP' and ts > now() - interval '20 seconds'\"")"
expect "its incident is closed" "[]" "$(vm "docker exec annex-ops-1 python -m annex_ops.ctl incidents --open --app $APP --json")"
took
finish
