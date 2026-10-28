#!/usr/bin/env bash
# M1.5 acceptance checks for incoming webhooks, run from the Mac through the SSH tunnel. No real provider is
# involved or ever pointed at the VM: every event is signed here (with random test secrets that only live in this
# script's memory) and sent with curl --connect-to to hooks.<domain>.
# Deploys annex-selftest, whose /hooks/* endpoints record what Annex forwards to it. Shortens the retry delays through
# state/ops/tuning.json (removed on exit). About 8-10 minutes.
source "$(dirname "$0")/../scripts/lib.sh"
source "$(dirname "$0")/lib.sh"
use_tunnel

MAIL_URL=$(setting ANNEX_URL_MAIL)
HOOKS_URL=$(setting ANNEX_URL_HOOKS)
APP=annex-selftest
SELFTEST_DIR=$ROOT/.build/selftest-hooks
C_APP=annex-app-$APP
SAM=sam@annex.example
ADMIN=admin@annex.example
TUNING=$DATA/state/ops/tuning.json
WORK=$ROOT/.build/test-hooks
cleanup() {
  rm -rf "$TMP" "$WORK"
  vm "rm -f $TUNING; docker start $C_APP >/dev/null 2>&1" >/dev/null 2>&1 || true
}
on_exit cleanup


# --- signing (on the Mac; the secret goes in through the environment, not the command line) ----------------------
sign() { # sign KIND SECRET FILE [CALLBACK_URL] -> the signature header value
  SECRET="$2" python3 - "$1" "$3" "${4:-}" <<'PY'
import base64, hashlib, hmac, os, sys
kind, path, callback = sys.argv[1:4]
secret, body = os.environ["SECRET"].encode(), open(path, "rb").read()
if kind == "github":
    print("sha256=" + hmac.new(secret, body, hashlib.sha256).hexdigest())
elif kind == "trello":
    print(base64.b64encode(hmac.new(secret, body + callback.encode(), hashlib.sha1).digest()).decode())
PY
}
post() { # post HOOK BODYFILE [curl args]: sets CODE and RESP
  local hook=$1 file=$2; shift 2
  CODE=$(c -o "$TMP/resp" -w '%{http_code}' -X POST --data-binary @"$file" "$@" "$HOOKS_URL/h/$APP/$hook")
  RESP=$(cat "$TMP/resp")
}
body() { printf '%s' "$2" > "$TMP/$1"; echo "$TMP/$1"; }
trello_event() { # trello_event NAME NONCE [extra curl args]: a correctly signed Trello event; sets CODE, RESP, EID
  local f; f=$(body "$1" "{\"action\":{\"type\":\"updateCard\",\"nonce\":\"$2\"}}")
  post trello "$f" -H "Content-Type: application/json" -H "X-Trello-Webhook: $(sign trello "$TRELLO_SECRET" "$f" "$HOOKS_URL/h/$APP/trello")" "${@:3}"
  EID=$(python3 -c 'import json,sys; print(json.loads(sys.argv[1]).get("id", ""))' "$RESP" 2>/dev/null || true)
}
hooks_json() { "$ROOT/annex" --json hooks "$APP" "$@"; }
row() { hooks_json -n 100 | python3 -c 'import json,sys; r=[x for x in json.load(sys.stdin) if x["id"] == int(sys.argv[1])]; print(json.dumps(r[0]) if r else "{}")' "$1"; }
field() { python3 -c 'import json,sys; print(json.loads(sys.argv[1]).get(sys.argv[2]))' "$(row "$1")" "$2"; }
wait_status() { # wait_status ID STATUS TIMEOUT
  local end=$((SECONDS + $3))
  while [ $SECONDS -lt $end ]; do [ "$(field "$1" status)" = "$2" ] && return 0; sleep 2; done
  return 1
}
events() { vm "docker exec $C_APP cat /data/events.jsonl 2>/dev/null" || true; }
event_count() { # event_count EVENT_ID -> lines the app recorded for that id
  events | python3 -c 'import json,sys; print(sum(1 for l in sys.stdin if l.strip() and json.loads(l)["event_id"] == sys.argv[1]))' "$1"
}
total_events() { events | grep -c . || true; }
last_event() { events | tail -1; }
mail_count() { # mail_count RECIPIENT SUBJECT_PREFIX
  c -b "$TMP/kaldi-admin.jar" "$MAIL_URL/api/v1/messages?limit=500" | python3 -c '
import json, sys
rcpt, prefix = sys.argv[1], sys.argv[2]
print(sum(1 for m in json.load(sys.stdin)["messages"] if m["Subject"].startswith(prefix) and any(t["Address"] == rcpt for t in m["To"])))' "$@"
}
wait_mail() { # wait_mail RECIPIENT PREFIX N TIMEOUT
  local end=$((SECONDS + $4))
  while [ $SECONDS -lt $end ]; do [ "$(mail_count "$1" "$2")" -ge "$3" ] && return 0; sleep 3; done
  return 1
}

# --- setup -----------------------------------------------------------------------------------------------------------
echo "Setup"
TRELLO_SECRET=$(python3 -c 'import secrets; print("trello-" + secrets.token_hex(12))')
GH_SECRET=$(python3 -c 'import secrets; print("gh-" + secrets.token_hex(12))')
PLAIN_SECRET=$(python3 -c 'import secrets; print("plain-" + secrets.token_hex(12))')
"$ROOT/annex" secret set "$APP" "TRELLO_SECRET=$TRELLO_SECRET" "GH_SECRET=$GH_SECRET" "PLAIN_SECRET=$PLAIN_SECRET" >/dev/null 2>&1 \
  && ok "signing secrets stored (random values, not printed)" || { bad "annex secret set" "failed"; exit 1; }
selftest_build "$SELFTEST_DIR"
deploy_out=$("$ROOT/annex" deploy "$SELFTEST_DIR" 2>&1) && ok "$APP deployed" || { bad "$APP deployed" "$deploy_out"; echo "passed $pass, failed $fail"; exit 1; }
has "the deploy lists the public Trello URL" "$HOOKS_URL/h/$APP/trello" "$deploy_out"
has "the deploy lists ANNEX_HOOK_TOKEN and ANNEX_HOOKS_URL" "ANNEX_HOOK_TOKEN" "$deploy_out"
has "  ...and ANNEX_HOOKS_URL" "ANNEX_HOOKS_URL" "$deploy_out"
vm "printf '%s' '{\"hook_backoff_s\": \"3,5,10\", \"tick_s\": 5}' > $TUNING && chmod 644 $TUNING"; ops_state_seen
ok "short retry delays written to state/ops/tuning.json (removed on exit)"
expect "kaldi-admin logs in (for Mailpit)" 200 "$(login kaldi-admin)"
# read in the ops container that uses it: on the Mac's bind mounts the host side always shows the Mac user (D98)
expect "the hooks file is owned by the ops user and private" "10001 600" "$(vm "docker exec annex-ops-1 stat -c '%u %a' /annex/hooks/$APP.json")"
sleep 8

# --- 2. Trello --------------------------------------------------------------------------------------------------------
echo "Trello"
expect "HEAD on a declared hook is 200 (Trello's registration check), with no login" 200 "$(code -I "$HOOKS_URL/h/$APP/trello")"
trello_event t1 "n-$RANDOM"
expect "a correctly signed event is accepted" 200 "$CODE"
has "  ...and answered with its id" '"ok":true' "$RESP"
wait_status "$EID" delivered 30 && ok "it is delivered to the app" || bad "it is delivered to the app" "status $(field "$EID" status): $(field "$EID" last_error)"
expect "the app recorded it exactly once" 1 "$(event_count "$EID")"
le=$(last_event)
has "  ...as the trello hook" '"hook": "trello"' "$le"
has "  ...with the token check passed (attempt 1)" '"attempt": 1' "$le"
has "  ...and the signature header was not forwarded" '"sig_header": false' "$le"
expect "annex hooks shows it delivered" delivered "$(field "$EID" status)"

# --- 3. bad signatures -----------------------------------------------------------------------------------------------------
echo "Bad signatures"
before=$(total_events)
f=$(body bad1 '{"action":{"type":"updateCard","nonce":"forged"}}')
post trello "$f" -H "X-Trello-Webhook: $(sign trello "not-the-secret" "$f" "$HOOKS_URL/h/$APP/trello")"
expect "a wrongly signed Trello event is refused" 401 "$CODE"
expect "  ...tersely" '{"error":"signature check failed"}' "$RESP"
post trello "$f"
expect "an unsigned one is refused too" 401 "$CODE"
post trello "$f" -H "X-Trello-Webhook: $(sign trello "$TRELLO_SECRET" "$f" "$HOOKS_URL/h/$APP/other")"
expect "a signature for another callback URL is refused" 401 "$CODE"
post gh "$f" -H "X-Hub-Signature-256: sha256=$(printf 'x%.0s' $(seq 64))"
expect "a wrong GitHub signature is refused" 401 "$CODE"
post plain "$f" -H "X-Annex-Hook-Secret: wrong"
expect "a wrong shared secret is refused" 401 "$CODE"
rejected=$(hooks_json --status rejected -n 20)
has "the rejections are logged, with a reason" '"last_error": "signature mismatch"' "$rejected"
has "  ...including the missing header" 'missing header X-Trello-Webhook' "$rejected"
rid=$(python3 -c 'import json,sys; print(json.load(sys.stdin)[0]["id"])' <<<"$rejected")
shown=$("$ROOT/annex" --json hooks show "$rid")
expect "a rejected request keeps no body" 0 "$(python3 -c 'import json,sys; print(json.loads(sys.argv[1])["body_bytes"])' "$shown")"
sleep 6
expect "nothing reached the app" "$before" "$(total_events)"
expect "a request with no session is answered (401), not redirected to login" 401 "$(code -X POST -d '{}' "$HOOKS_URL/h/$APP/trello")"

# --- 4. GitHub and shared secret --------------------------------------------------------------------------------------------
echo "GitHub and shared secret"
f=$(body gh1 "{\"ref\":\"refs/heads/main\",\"nonce\":\"g-$RANDOM\"}")
post gh "$f" -H "Content-Type: application/json" -H "X-GitHub-Event: push" -H "User-Agent: GitHub-Hookshot/test" -H "X-Hub-Signature-256: $(sign github "$GH_SECRET" "$f")"
expect "a correctly signed GitHub event is accepted" 200 "$CODE"
GID=$(python3 -c 'import json,sys; print(json.loads(sys.argv[1])["id"])' "$RESP")
wait_status "$GID" delivered 30 && ok "  ...and delivered" || bad "GitHub event delivered" "$(field "$GID" last_error)"
has "  ...with X-GitHub-Event forwarded" '"github_event": "push"' "$(last_event)"
hasnt "  ...and no signature header forwarded" '"sig_header": true' "$(last_event)"
f=$(body plain1 "{\"hello\":\"plain-$RANDOM\"}")
post plain "$f" -H "X-Annex-Hook-Secret: $PLAIN_SECRET"
expect "a shared-secret event is accepted" 200 "$CODE"
PID_=$(python3 -c 'import json,sys; print(json.loads(sys.argv[1])["id"])' "$RESP")
wait_status "$PID_" delivered 30 && ok "  ...and delivered" || bad "shared-secret event delivered" "$(field "$PID_" last_error)"

# --- 5. app stopped: delivered exactly once after it's back ----------------------------------------------------------------------------
echo "App stopped"
vm "docker stop -t 1 $C_APP >/dev/null"
trello_event t2 "stopped-$RANDOM"
SID=$EID
expect "an event sent while the app is stopped is still accepted (stored)" 200 "$CODE"
end=$((SECONDS + 20)); until [ "$(field "$SID" attempts)" -ge 1 ] 2>/dev/null || [ $SECONDS -ge $end ]; do sleep 2; done
expect "  ...and waits, pending" pending "$(field "$SID" status)"
[ "$(field "$SID" attempts)" -ge 1 ] && ok "  ...after at least one failed attempt: $(field "$SID" last_error | cut -c1-60)" || bad "a failed attempt was recorded" "attempts $(field "$SID" attempts)"
vm "docker start $C_APP >/dev/null"
wait_status "$SID" delivered 90 && ok "once the app is back it is delivered" || bad "delivered after the app came back" "status $(field "$SID" status)"
sleep 12
expect "it reached the app exactly once" 1 "$(event_count "$SID")"
expect "  ...after several attempts" true "$([ "$(field "$SID" attempts)" -ge 2 ] && echo true || echo false)"

# --- 6. replay -------------------------------------------------------------------------------------------------------------------------------
echo "Replay"
"$ROOT/annex" hooks replay "$APP" --id "$SID" --yes >/dev/null 2>&1 && ok "annex hooks replay queued the event" || bad "annex hooks replay" "failed"
end=$((SECONDS + 30)); until [ "$(event_count "$SID")" -ge 2 ] || [ $SECONDS -ge $end ]; do sleep 2; done
expect "the app received it again, with the same X-Annex-Hook-Event" 2 "$(event_count "$SID")"
has "  ...as attempt 1" '"attempt": 1' "$(last_event)"
has "  ...and it is a replay in the list" "of $SID" "$("$ROOT/annex" hooks "$APP" -n 5)"
has "annex hooks show prints a body (human only)" "stopped-" "$("$ROOT/annex" hooks show "$SID")"
hasnt "annex hooks <app> never prints a body" "stopped-" "$("$ROOT/annex" hooks "$APP")"

# --- 7. the route is tight ---------------------------------------------------------------------------------------------------------------------
echo "The route is tight"
for p in /v1/me /healthz /internal/anything /tiles/$APP /; do
  expect "hooks host $p is a 404 (GET)" 404 "$(code "$HOOKS_URL$p")"
done
expect "hooks host /v1/email is a 404 (POST)" 404 "$(code -X POST -d '{}' "$HOOKS_URL/v1/email")"
expect "an undeclared hook is a 404" 404 "$(code -X POST -d '{}' "$HOOKS_URL/h/$APP/nope")"
expect "an unknown app is a 404" 404 "$(code -X POST -d '{}' "$HOOKS_URL/h/no-such-app/trello")"
expect "path traversal is a 404" 404 "$(code --path-as-is -X POST -d '{}' "$HOOKS_URL/h/../v1/me")"
expect "HEAD on an undeclared hook is a 404" 404 "$(code -I "$HOOKS_URL/h/$APP/nope")"
trello_event t3 "spoof-$RANDOM" -H "Remote-User: kaldi-admin" -H "Remote-Groups: admins" -H "Remote_User: kaldi-admin"
expect "an event with spoofed identity headers is accepted (they are ignored)" 200 "$CODE"
wait_status "$EID" delivered 30 || true
has "  ...and reaches the app with an empty Remote-User" '"remote_user": ""' "$(last_event)"

# --- 8. limits ---------------------------------------------------------------------------------------------------------------------------------------------
echo "Limits"
head -c 2000000 /dev/zero | tr '\0' a > "$TMP/big"
expect "a 2 MB body is refused" 413 "$(code -X POST --data-binary @"$TMP/big" "$HOOKS_URL/h/$APP/trello")"
export CA HOOKS_URL APP PORT
burst=$(seq 300 | xargs -P 60 -I{} curl -sS -o /dev/null -w '%{http_code}\n' --cacert "$CA" --connect-to "::127.0.0.1:$PORT" -X POST -d '{}' "$HOOKS_URL/h/$APP/trello" | sort | uniq -c | tr -s ' ' | tr '\n' ';')
has "a burst of junk requests is rate limited (429)" " 429" "$burst"
sleep 8
trello_event t4 "after-burst-$RANDOM"
expect "after the burst, a signed event gets through again" 200 "$CODE"

# --- 9. revoking -------------------------------------------------------------------------------------------------------------------------------------------
echo "A release without webhooks revokes them"
selftest_build "$WORK/$APP" --drop webhooks
"$ROOT/annex" deploy "$WORK/$APP" >/dev/null 2>&1 && ok "a copy without webhooks: deployed" || bad "deploy without webhooks" "failed"
expect "the hooks file is gone" "" "$(vm "ls $DATA/secrets/hooks | grep -x '$APP.json' || true")"
expect "the app no longer has a hook token" 0 "$(vm "grep -c ANNEX_HOOK_TOKEN $DATA/secrets/apps/$APP/runtime.env || true")"
trello_event t5 "revoked-$RANDOM"
expect "a signed event now gets a 404" 404 "$CODE"
"$ROOT/annex" deploy "$SELFTEST_DIR" >/dev/null 2>&1 && ok "redeployed with webhooks" || bad "redeploy" "failed"
trello_event t6 "back-$RANDOM"
expect "and signed events work again" 200 "$CODE"
wait_status "$EID" delivered 30 && ok "  ...and are delivered" || bad "delivered after the redeploy" "$(field "$EID" last_error)"

# --- 10. alert ----------------------------------------------------------------------------------------------------------------------------------------------
echo "Alert"
c -o /dev/null -X DELETE -b "$TMP/kaldi-admin.jar" "$MAIL_URL/api/v1/messages"
vm "printf '%s' '{\"hook_backoff_s\": \"3,5,10\", \"tick_s\": 5, \"stopped_grace_s\": 10, \"hook_alert_after_s\": 20}' > $TUNING && chmod 644 $TUNING"; ops_state_seen
sleep 8
vm "docker stop -t 1 $C_APP >/dev/null"
trello_event t7 "alert-$RANDOM"
AID=$EID
expect "an event for the stopped app is accepted" 200 "$CODE"
ALERT="[Annex] $APP"
wait_mail $SAM "$ALERT" 1 90 && ok "the owner is alerted" || bad "the owner is alerted" "no mail"
end=$((SECONDS + 90)); sigs=""
while [ $SECONDS -lt $end ]; do
  sigs=$("$ROOT/annex" --json status "$APP" 2>/dev/null | python3 -c 'import json,sys; i=json.load(sys.stdin).get("incident"); print(" ".join(sorted(i["signals"])) if i else "")')
  case "$sigs" in *webhook*) break ;; esac; sleep 4
done
has "the incident carries the webhook signal (with stopped)" "webhook" "$sigs"
has "  ...joined to the stop, not a second incident" "stopped" "$sigs"
vm "docker start $C_APP >/dev/null"
wait_status "$AID" delivered 90 && ok "the pending event is delivered after the start" || bad "delivered after the start" "$(field "$AID" status)"
wait_mail $SAM "[Annex] resolved: $APP" 1 90 && ok "one 'resolved' arrives" || bad "one 'resolved' arrives" "no mail"
sleep 15
expect "exactly one alert reached sam" 1 "$(mail_count $SAM "$ALERT is")"
expect "exactly one alert reached the admin" 1 "$(mail_count $ADMIN "$ALERT is")"
expect "exactly one 'resolved' reached sam" 1 "$(mail_count $SAM "[Annex] resolved: $APP")"
expect "the event reached the app exactly once" 1 "$(event_count "$AID")"

# --- the receiver is its own, minimal container (D55) -------------------------------------------------------------------------------------
echo "The receiver is isolated"
R=annex-hooks-1
renv=$(vm "docker exec $R printenv" 2>/dev/null | cut -d= -f1)
for v in SLACK_BOT_TOKEN ANNEX_EMAIL_FROM OPS_SMTP_HOST; do
  hasnt "the receiver has no $v" "$v" "$renv"
done
rnets=$(vm "docker inspect -f '{{range \$k, \$v := .NetworkSettings.Networks}}{{\$k}} {{end}}' $R")
expect "the receiver is only on annex-edge and annex-appdata" "annex-appdata annex-edge" "$(tr ' ' '\n' <<<"$rnets" | grep . | sort | tr '\n' ' ' | sed 's/ $//')"
expect "the receiver has no user directory mounted" "" "$(vm "docker exec $R ls /annex/directory 2>/dev/null || true")"
rsql() { vm "docker exec $R python -c \"import os, psycopg
try:
    with psycopg.connect(os.environ['OPS_DATABASE_URL']) as c: c.execute('$1'); print('allowed')
except psycopg.errors.InsufficientPrivilege: print('denied')\""; }
expect "its database role can't read event bodies" denied "$(rsql 'SELECT body FROM annex_ops.hook_events LIMIT 1')"
expect "  ...or the send log" denied "$(rsql 'SELECT 1 FROM annex_ops.sends LIMIT 1')"
expect "  ...or the app grants" denied "$(rsql 'SELECT 1 FROM annex_ops.app_grants LIMIT 1')"
expect "  ...or change events" denied "$(rsql 'UPDATE annex_ops.hook_events SET attempts = 0 WHERE false')"

# --- secrets stay out of everything -----------------------------------------------------------------------------------------------------------------------
echo "Secrets"
leak=0
for s in "$TRELLO_SECRET" "$GH_SECRET" "$PLAIN_SECRET"; do
  out="$deploy_out$("$ROOT/annex" status "$APP" 2>&1)$("$ROOT/annex" hooks "$APP" -n 100 2>&1)$("$ROOT/annex" hooks show "$SID" 2>&1)"
  grep -qF "$s" <<<"$out" && leak=1
  grep -qF "$s" <<<"$(vm "docker logs annex-ops-1 2>&1; docker logs annex-hooks-1 2>&1")" && leak=1
  [ "$(vm "docker exec annex-appdata-db-1 psql -U postgres -d annex_ops -Atc \"SELECT count(*) FROM annex_ops.hook_events WHERE headers::text LIKE '%$s%' OR last_error LIKE '%$s%'\"")" != 0 ] && leak=1
done
expect "no signing secret appears in CLI output, ops or receiver logs, or the database" 0 "$leak"

# --- 11. teardown ----------------------------------------------------------------------------------------------------------------------------------------------
echo "Teardown"
"$ROOT/annex" remove "$APP" --yes >/dev/null 2>&1 && ok "annex remove $APP" || bad "annex remove $APP" "failed"
expect "its hooks file is gone" "" "$(vm "ls $DATA/secrets/hooks | grep -x '$APP.json' || true")"
trello_event t8 "gone-$RANDOM"
expect "its URL is a 404 now" 404 "$CODE"
finish
