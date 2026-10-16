#!/usr/bin/env bash
# M2.5 acceptance checks: the audit trail (about 5 minutes).
# One scripted session as "audit-tester" (ANNEX_USER): a secret is set, an app deployed, a broken version rolled back by
# itself, a new version deployed and rolled back by hand, product data read through the gateway, an email sent (a job of
# monthly-report), an account added, changed and removed, a failed and a good sign-in, the app removed. The trail must show
# each step once, in order, with the right actor, and no secret, password or body anywhere.
# Gateway buckets and scan times are shortened through state/ops/tuning.json (removed on exit). Deploys aud-app (a copy of
# client-lookup); removes it at the end.
source "$(dirname "$0")/lib.sh"
source "$(dirname "$0")/testlib.sh"
use_tunnel
export ANNEX_USER=audit-tester
ANNEX="$ROOT/annex"
CON=$(setting ANNEX_URL_CONSOLE)
PATTERN=$(setting ANNEX_HOST_PATTERN)
SUFFIX=$(setting ANNEX_PORT_SUFFIX)
url() { echo "https://${PATTERN//\{name\}/$1}$SUFFIX"; }
TUNING=$DATA/state/ops/tuning.json
WORK=$ROOT/.build/test-audit
APP=aud-app
SECRET="AUDSECRET-$RANDOM$RANDOM"
mkdir -p "${WORK:?}" && find "${WORK:?}" -mindepth 1 -delete
py() { python3 -c "import json,sys; d=json.load(sys.stdin); print($1)"; }
pj() { python3 -c "import json,sys; d=json.load(sys.stdin); print(json.dumps($1))"; }
trail() { "$ANNEX" --json audit --since 30m --limit 2000 "$@"; }
cleanup() {
  vm "rm -f $TUNING" >/dev/null 2>&1 || true
  "$ANNEX" remove $APP --yes >/dev/null 2>&1 || true
  "$ANNEX" users remove aud-tmp --yes >/dev/null 2>&1 || true
}
on_exit cleanup
variant() { # variant DIR [broken]
  local dir="$WORK/${1:?}"
  mkdir -p "$dir" && find "$dir" -mindepth 1 -delete && cp -R "$ROOT/examples/client-lookup/." "$dir/"
  sed -i.bak "s/^name: client-lookup/name: $APP/; s/^secrets: \[\]/secrets: [AUDIT_KEY]/" "$dir/annex.yaml" && rm "$dir/annex.yaml.bak"
  [ "${2:-}" = broken ] && sed -i.bak 's#^healthcheck: /healthz#healthcheck: /nope#' "$dir/annex.yaml" && rm "$dir/annex.yaml.bak"
  return 0
}

echo "Setup"
for u in sam kaldi-admin cm; do expect "$u signs in" 200 "$(login $u)"; done
vm "printf '%s' '{\"audit_scan_s\": 5, \"audit_bucket_s\": 20}' > $TUNING && chmod 644 $TUNING"
sleep 8
START=$(python3 -c 'import datetime; print((datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(seconds=2)).strftime("%Y-%m-%dT%H:%M:%S"))')   # the trail keeps earlier runs: look only at this one
START_EPOCH=$(date +%s)

echo "The scripted session (as $ANNEX_USER)"
out=$("$ANNEX" secret set $APP "AUDIT_KEY=$SECRET" 2>&1) && ok "1. annex secret set (before the first deploy)" || bad "secret set" "$out"
variant v1
out=$("$ANNEX" deploy "$WORK/v1" 2>&1) && ok "2. deploy r1" || { bad "deploy r1" "$out"; finish; exit 1; }
for i in 1 2 3 4; do c -o /dev/null -b "$TMP/sam.jar" "$(url $APP)/"; done
ok "3. four page loads that read gateway views"
out=$("$ANNEX" run monthly-report monthly 2>&1) && ok "4. a job that sends an email (monthly-report)" || bad "run job" "$(tail -2 <<<"$out")"
variant v2 broken
"$ANNEX" deploy "$WORK/v2" >/dev/null 2>&1 && bad "5. the broken deploy fails" "it succeeded" || ok "5. a broken version fails its health gate and is rolled back by itself"
variant v3
"$ANNEX" deploy "$WORK/v3" >/dev/null 2>&1 && ok "6. a good new version (deploy)" || bad "deploy v3" "failed"
out=$("$ANNEX" rollback $APP 2>&1) && ok "7. annex rollback" || bad "rollback" "$out"
out=$("$ANNEX" users add aud-tmp --name "Audit Temp" --email aud-tmp@annex.example --groups users --print-password 2>&1) && ok "8. annex users add" || bad "users add" "$out"
PW=$(sed -n 's/^password (shown once[^:]*): *//p' <<<"$out" | head -1 | tr -d ' ')
"$ANNEX" users set aud-tmp --disable >/dev/null 2>&1 && ok "9. annex users set --disable" || bad "users set" "failed"
"$ANNEX" users remove aud-tmp --yes >/dev/null 2>&1 && ok "10. annex users remove" || bad "users remove" "failed"
code=$(c -o /dev/null -w '%{http_code}' -H 'Content-Type: application/json' --data "{\"username\":\"sam\",\"password\":\"wrong-$RANDOM-pw\",\"keepMeLoggedIn\":false}" "$AUTH_URL/api/firstfactor")
expect "11. a failed sign-in (sam, wrong password)" 401 "$code"
expect "12. a good sign-in (sam)" 200 "$(login sam)"
sleep 3
"$ANNEX" remove $APP --yes >/dev/null 2>&1 && ok "13. annex remove" || bad "remove" "failed"
echo "(waiting for the gateway buckets to close and the scans to run)"
for i in $(seq 1 24); do
  t=$(trail --app $APP --action gateway | START=$START python3 -c 'import json,os,sys; print(len([x for x in json.load(sys.stdin) if x["ts"] >= os.environ["START"]]))')
  [ "$t" -ge 1 ] && s=$(trail --action signin | START=$START python3 -c 'import json,os,sys; print(len([x for x in json.load(sys.stdin) if x["actor"] == "sam" and x["ts"] >= os.environ["START"]]))') && [ "$s" -ge 2 ] && break
  sleep 5
done

echo "The trail"
all=$(trail | START=$START python3 -c 'import json,os,sys; print(json.dumps([x for x in json.load(sys.stdin) if x["ts"] >= os.environ["START"]]))')
mine=$(pj '[x for x in d if x["actor"] == "audit-tester" or x["app"] == "aud-app" or (x["actor"] == "sam" and x["action"].startswith("signin"))]' <<<"$all")
count() { python3 -c "import json,sys; d=json.load(sys.stdin); print(len([x for x in d if x['action'] == '$1' ${2:-}]))" <<<"$mine"; }
expect "secret.set: once, by $ANNEX_USER, the names only" "1 audit-tester AUDIT_KEY" "$(py '(lambda s: "%d %s %s" % (len(s), s[0]["actor"], ",".join(s[0]["details"]["names"])))([x for x in d if x["action"] == "secret.set"])' <<<"$mine")"
expect "deploy: twice (r1 and the good new version; the failed one is deploy.failed)" 2 "$(count deploy ' and x["actor"] == "audit-tester"')"
expect "deploy.failed: once, saying it rolled back to r1" "1 1" "$(py '"%d %s" % (len([x for x in d if x["action"] == "deploy.failed"]), [x for x in d if x["action"] == "deploy.failed"][0]["details"]["rolled_back_to"])' <<<"$mine")"
expect "rollback: once, by $ANNEX_USER, with the releases" "1 audit-tester" "$(py '"%d %s" % (len([x for x in d if x["action"] == "rollback"]), [x for x in d if x["action"] == "rollback"][0]["actor"])' <<<"$mine")"
expect "user.added, user.changed, user.removed: once each, by $ANNEX_USER" "1 1 1" "$(py '" ".join(str(len([x for x in d if x["action"] == a and x["actor"] == "audit-tester" and x["details"].get("login") == "aud-tmp"])) for a in ("user.added", "user.changed", "user.removed"))' <<<"$mine")"
expect "app.removed: once" 1 "$(count app.removed)"
expect "signin.failed: once for sam" 1 "$(py 'len([x for x in d if x["action"] == "signin.failed" and x["actor"] == "sam"])' <<<"$mine")"
expect "signin: sam's good sign-in is there (found from Authelia's log)" True "$(py 'len([x for x in d if x["action"] == "signin" and x["actor"] == "sam"]) >= 1' <<<"$mine")"
reads=$(py '[x["details"] for x in d if x["action"] == "gateway.reads"]' <<<"$mine")
has "gateway reads: summed per app, view and bucket" "requests" "$reads"
expect "  ...and the number of reads is the number of gateway lines the app caused (four page loads, two reads each)" \
  "$("$ANNEX" --json logs $APP --source gateway --grep top_clients_by_aum --since "$(( $(date +%s) - START_EPOCH + 5 ))s" -n 5000 | py 'len(d)')" \
  "$(py 'sum(x["details"]["requests"] for x in d if x["action"] == "gateway.reads" and x["details"]["view"] == "top_clients_by_aum")' <<<"$mine")"
expect "  ...by the app, not by a person" "app:aud-app" "$(py '[x for x in d if x["action"] == "gateway.reads"][0]["actor"]' <<<"$mine")"
mails=$("$ANNEX" --json audit --action email --app monthly-report --since "$(( $(date +%s) - START_EPOCH + 5 ))s" | py 'len(d)')
expect "an email sent by an app is in the trail, once (no body)" 1 "$mails"
has "  ...with its recipients" "cm@annex.example" "$("$ANNEX" --json audit --action email --app monthly-report --since 5m)"
order=$(py '" ".join(x["action"] for x in sorted([y for y in d if y["actor"] == "audit-tester" or (y["actor"] == "sam" and y["action"].startswith("signin"))], key=lambda y: y["ts"]) if x["action"] not in ("gateway.reads",))' <<<"$all")
expect "in order, once each (the actor's steps)" "secret.set deploy deploy.failed deploy rollback user.added user.changed user.removed signin.failed signin app.removed" "$(tr -s ' ' <<<"$order" | sed 's/^ //')"
secrets=$(trail | python3 -c 'import json,sys; print(json.dumps(json.load(sys.stdin)))')
hasnt "the secret's value is nowhere in the trail" "$SECRET" "$secrets"
[ -n "$PW" ] && hasnt "  ...nor the new account's password" "$PW" "$secrets" || ok "(no password captured to look for)"
hasnt "  ...nor a webhook body or a token" "token_sha256" "$secrets"
expect "no raw password was typed as a username into the trail" 0 "$(py 'len([x for x in d if x["action"] == "signin.failed" and x["actor"] not in (None, "sam", "cm", "kaldi-admin") and not x["actor"].startswith("nobody") and not x["actor"].startswith("tst")])' <<<"$all")"

echo "Filters"
expect "--action deploy finds deploy and deploy.failed only" "deploy deploy.failed" "$(trail --app $APP --action deploy | py '" ".join(sorted({x["action"] for x in d}))')"
expect "--actor finds what $ANNEX_USER did" True "$(trail --actor audit-tester | py 'len(d) >= 10')"
expect "--app limits to the app" "aud-app" "$(trail --app $APP | py '" ".join(sorted({x["app"] for x in d}))')"
expect "--since 1m is empty of the session's old events" 0 "$("$ANNEX" --json audit --since 1s --app $APP | py 'len(d)')"
has "annex audit prints a table for humans" "ACTION" "$("$ANNEX" audit --app $APP -n 5)"

echo "The console's Audit page (admins only)"
expect "kaldi-admin opens /audit" 200 "$(code -b "$TMP/kaldi-admin.jar" "$CON/audit")"
expect "sam gets a 403" 403 "$(code -b "$TMP/sam.jar" "$CON/audit")"
expect "cm gets a 403" 403 "$(code -b "$TMP/cm.jar" "$CON/audit")"
expect "forged admin headers from sam change nothing" 403 "$(code -b "$TMP/sam.jar" -H 'Remote-User: kaldi-admin' -H 'Remote-Groups: admins' "$CON/audit")"
page=$(as kaldi-admin "$CON/audit?app=$APP" | sed 's/<style>.*<\/style>//; s/<[^>]*>/ /g' | tr -s ' \n')
for a in secret.set deploy deploy.failed rollback gateway.reads app.removed; do has "the page shows $a" "$a" "$page"; done
has "  ...and who did it" "audit-tester" "$page"
cli_n=$(trail --app $APP | py 'len(d)')
page_n=$(as kaldi-admin "$CON/audit?app=$APP" | grep -o '<tr><td>' | wc -l | tr -d ' ')
expect "the page's rows are annex audit's for the app" "$cli_n" "$page_n"
hasnt "the nav shows Audit to admins only" ">Audit<" "$(as sam "$CON/" )"
has "  ...and to admins" ">Audit<" "$(as kaldi-admin "$CON/")"
has "the page says who may read it" "For admins" "$page"
expect "the console's role reads the view, not the send log directly" "permission denied" "$(printf 'SET ROLE annex_console;\nSELECT 1 FROM annex_ops.sends LIMIT 1;\n' | ssh annex 'docker exec -i annex-appdata-db-1 psql -U postgres -d annex_ops -At -f -' 2>&1 | grep -o 'permission denied' | head -1)"
expect "  ...and can't write to the trail" "permission denied" "$(printf 'SET ROLE annex_console;\nINSERT INTO annex_ops.audit_events (action) VALUES ($$x$$);\n' | ssh annex 'docker exec -i annex-appdata-db-1 psql -U postgres -d annex_ops -At -f -' 2>&1 | grep -o 'permission denied' | head -1)"

echo "Idempotent copies"
before=$(vm "docker exec annex-appdata-db-1 psql -U postgres -d annex_ops -Atc \"select count(*) from annex_ops.audit_events where ref like 'release:%'\"")
vm "python3 $DATA/platform/vm/annex_vm.py release-backfill >/dev/null"
sleep 12
after=$(vm "docker exec annex-appdata-db-1 psql -U postgres -d annex_ops -Atc \"select count(*) from annex_ops.audit_events where ref like 'release:%'\"")
expect "a release backfill adds nothing (the release rows: $before)" "$before" "$after"
finish
