#!/usr/bin/env bash
# M2.3 acceptance checks: error groups and their emails (about 6 minutes).
#   1. 50 identical errors (only a number differs) are one group and one email, to the owner and the admins only;
#   2. another kind of error is another group and another email; more than 5 kinds an hour become one summary;
#   3. a redeploy that fixes it shows the group as quiet in that release, no email;
#   4. it comes back in a newer release: one email, "error is back";
#   5. a browser error report (what /annex-errors.js sends) is one group, with the user and request id, rate limited;
#   6. a traceback an app printed (plain text, several lines) is one group, by exception type and last frame;
#   7. removing an app removes its groups.
# Scan times are shortened through state/ops/tuning.json (removed on exit). Deploys annex-selftest, annex-selftest-boom (for the printed traceback) and err-static (the static template); removes all three.
source "$(dirname "$0")/lib.sh"
source "$(dirname "$0")/testlib.sh"
use_tunnel
ANNEX="$ROOT/annex"
PATTERN=$(setting ANNEX_HOST_PATTERN)
SUFFIX=$(setting ANNEX_PORT_SUFFIX)
url() { echo "https://${PATTERN//\{name\}/$1}$SUFFIX"; }
TUNING=$DATA/state/ops/tuning.json
WORK=$ROOT/.build/test-errors
SAM=sam@annex.example
ADMIN=admin@annex.example
CM=cm@annex.example
mkdir -p "${WORK:?}" && find "${WORK:?}" -mindepth 1 -delete
py() { python3 -c "import json,sys; d=json.load(sys.stdin); print($1)"; }
cleanup() {
  vm "rm -f $TUNING" >/dev/null 2>&1 || true
  for a in annex-selftest err-static annex-selftest-boom; do "$ANNEX" remove $a --yes >/dev/null 2>&1 || true; done
}
on_exit cleanup
errs() { "$ANNEX" --json errors "$@"; }
emails() { mail_count "$1" "$2"; }          # emails RECIPIENT SUBJECT_PREFIX
err_mails() { # how many error emails (new, back, summary) of annex-selftest sam has
  as kaldi-admin "$(setting ANNEX_URL_MAIL)/api/v1/messages?limit=500" | python3 -c '
import json, re, sys
print(sum(1 for m in json.load(sys.stdin)["messages"]
          if re.match(r"\[Annex\] (new error in|error is back in) annex-selftest|\[Annex\] annex-selftest: \d+ more kind", m["Subject"])
          and any(t["Address"] == "sam@annex.example" for t in m["To"])))'
}
subjects() { as kaldi-admin "$(setting ANNEX_URL_MAIL)/api/v1/messages?limit=500" | python3 -c 'import json,sys; print("\n".join(m["Subject"] for m in json.load(sys.stdin)["messages"]))'; }
wait_errors() { # wait_errors SECONDS APP N: 0 once the app has N groups
  local end=$((SECONDS + $1))
  while [ $SECONDS -lt $end ]; do [ "$(errs "$2" | py 'len(d)')" -ge "$3" ] && return 0; sleep 2; done
  return 1
}

variant() { # variant DIR: the self-test app (its /raise endpoint fails on request; a redeploy is a new release of the same code)
  selftest_build "$WORK/${1:?}"
}

echo "Setup"
expect "sam logs in" 200 "$(login sam)"
expect "kaldi-admin logs in" 200 "$(login kaldi-admin)"
vm "printf '%s' '{\"errors_scan_s\": 4, \"errors_settle_s\": 4}' > $TUNING && chmod 644 $TUNING"
ok "short scan times written to state/ops/tuning.json (removed on exit)"
sleep 8
variant v1
out=$(selftest_deploy "$WORK/v1") && ok "annex-selftest deployed ($(grep -o 'r[0-9]* is live' <<<"$out" | head -1))" || { bad "annex-selftest deployed" "$out"; finish; exit 1; }
sleep 6
mail_clear
P="[Annex] new error in annex-selftest"

echo "1. Fifty identical errors are one group and one email"
for i in $(seq 1 50); do
  c -o /dev/null -b "$TMP/sam.jar" "$(url annex-selftest)/raise?q=boom$i" &
  [ $((i % 10)) -eq 0 ] && wait
done
wait
mail_wait $SAM "$P: RuntimeError in handle_raise" 1 60 && ok "the owner got an email for it" || bad "owner email" "none in 60 s"
sleep 16
expect "exactly one email to the owner" 1 "$(emails $SAM "$P")"
expect "one to the admin (the same message)" 1 "$(emails $ADMIN "$P")"
expect "none to anyone else (cm)" 0 "$(emails $CM "[Annex]")"
g=$(errs annex-selftest)
expect "one group" 1 "$(py 'len(d)' <<<"$g")"
expect "  ...seen 50 times" 50 "$(py 'd[0]["count"]' <<<"$g")"
expect "  ...with the release, user and kind" "r1 sam server active" "$(py '"r%d %s %s %s" % (d[0]["last_release"], d[0]["user"], d[0]["kind"], d[0]["state"])' <<<"$g")"
rid=$(py 'd[0]["request_id"]' <<<"$g")
expect "  ...and a request id that finds the lines" True "$("$ANNEX" logs annex-selftest --request "$rid" | grep -q 'unhandled error' && echo True || echo False)"
text=$(mail_find $SAM "$P")
has "the email says where to look" "annex logs annex-selftest --request" "$text"
has "  ...and what the error was" "cannot load client" "$text"

echo "2. Another kind is another email; more than 5 an hour are summarised"
c -o /dev/null -b "$TMP/sam.jar" "$(url annex-selftest)/raise?q=oops"
mail_wait $SAM "$P: KeyError in handle_raise" 1 60 && ok "a KeyError is a second group with its own email" || bad "second email" "none in 60 s"
expect "two groups now" 2 "$(errs annex-selftest | py 'len(d)')"
for n in 1 2 3 4 5 6 7 8; do c -o /dev/null -b "$TMP/sam.jar" "$(url annex-selftest)/raise?q=kind$n" & done
wait
wait_errors 60 annex-selftest 10 && ok "eight more kinds are ten groups in all" || bad "ten groups" "$(errs annex-selftest | py 'len(d)') after 60 s"
sleep 20
total=$(err_mails)
expect "exactly 5 error emails an hour for the app: the limit held (2 + 2 + one summary)" 5 "$total"
has "the rest came as one summary" "more kinds of error" "$(subjects)"

echo "3. A redeploy that fixes it: quiet in that release, no email"
before=$(err_mails)
variant v2
out=$(selftest_deploy "$WORK/v2") && ok "the fixed version deployed ($(grep -o 'r[0-9]* is live' <<<"$out" | head -1))" || bad "fixed deploy" "$out"
sleep 12
g=$(errs annex-selftest)
expect "every group is quiet since the new release" "quiet" "$(py 'set(x["state"] for x in d).pop() if len({x["state"] for x in d}) == 1 else "mixed"' <<<"$g")"
expect "  ...and says which release is live now" 2 "$(py 'd[0]["current_release"]' <<<"$g")"
expect "no email for it" "$before" "$(err_mails)"
expect "--state active lists none" 0 "$("$ANNEX" --json errors annex-selftest --state active | py 'len(d)')"

echo "4. It comes back in a newer release: one email"
variant v3
selftest_deploy "$WORK/v3" >/dev/null && ok "the bug is back in r3" || bad "v3 deploy" "failed"
for i in 1 2 3 4 5; do c -o /dev/null -b "$TMP/sam.jar" "$(url annex-selftest)/raise?q=boom$i"; done
sleep 25
has "'error is back' was sent, or the hourly limit held it back for the summary" "annex-selftest" "$(subjects | grep -E 'error is back in annex-selftest|more kinds? of error' | head -1)"
g=$(errs annex-selftest)
expect "the group is active again, in r3" "active 3" "$(py '[x["state"] + " " + str(x["last_release"]) for x in d if x["title"] == "RuntimeError in handle_raise"][0]' <<<"$g")"
expect "  ...counted once per error (55 in all)" 55 "$(py '[x["count"] for x in d if x["title"] == "RuntimeError in handle_raise"][0]' <<<"$g")"

echo "5. A browser error"
has "a Python app serves the script too (the self-test app's routes)" "unhandledrejection" "$(as sam "$(url annex-selftest)/annex-errors.js")"
expect "  ...and takes a report" 204 "$(as sam -o /dev/null -w '%{http_code}' -X POST -d '{"msg": "py app browser error"}' "$(url annex-selftest)/api/_errors")"
S=err-static
dir="$WORK/static"
mkdir -p "$dir" && cp -R "$ROOT/templates/static/." "$dir/"
sed -i.bak "s/__APP_NAME__/$S/g; s/__OWNER__/sam/g" "$dir/annex.yaml" "$dir/public/index.html" && rm "$dir/annex.yaml.bak" "$dir/public/index.html.bak"
"$ANNEX" deploy "$dir" >/dev/null 2>&1 && ok "a static app deployed" || bad "static deploy" "failed"
has "the page includes the error script" 'src="annex-errors.js"' "$(as sam "$(url $S)/")"
has "the script is served by the app" "unhandledrejection" "$(as sam "$(url $S)/annex-errors.js")"
payload() { printf '{"msg": "Uncaught TypeError: x is undefined at row %s", "stack": "TypeError: x\\n    at render (app.js:%s:5)", "url": "/page"}' "$1" "$1"; }
codes=""
for i in $(seq 1 14); do codes="$codes $(as sam -o /dev/null -w '%{http_code}' -X POST -H 'Content-Type: application/json' -d "$(payload $i)" "$(url $S)/api/_errors")"; done
has "reports are accepted (204)" "204" "$codes"
has "  ...and rate limited per user (429 after 10 a minute)" "429" "$codes"
expect "a report that isn't JSON is refused" 400 "$(as sam -o /dev/null -w '%{http_code}' -X POST -d 'nope' "$(url $S)/api/_errors")"
expect "a huge one too" 413 "$(head -c 9000 /dev/zero | tr '\0' x | as sam -o /dev/null -w '%{http_code}' -X POST --data-binary @- "$(url $S)/api/_errors")"
mail_wait $SAM "[Annex] new error in $S: browser error: Uncaught TypeError: x is undefined at row <n>" 1 60 && ok "the owner got an email for the browser error" || bad "browser email" "none in 60 s"
sleep 14
expect "one email for all the reports" 1 "$(emails $SAM "[Annex] new error in $S")"
g=$(errs $S)
expect "one group, kind browser, with the user" "1 browser sam" "$(py '"%d %s %s" % (len(d), d[0]["kind"], d[0]["user"])' <<<"$g")"
expect "  ...the ten accepted reports counted" 10 "$(py 'd[0]["count"]' <<<"$g")"

echo "6. A traceback an app printed"
L=annex-selftest-boom   # a second self-test app (the first one has used up its five emails an hour in section 2)
selftest_build "$WORK/boom" --name $L --drop webhooks,notify --no-postgres
selftest_deploy "$WORK/boom" >/dev/null && ok "$L deployed" || bad "$L deployed" "failed"
for i in 1 2 3; do c -o /dev/null -b "$TMP/sam.jar" "$(url $L)/boom"; done
mail_wait $SAM "[Annex] new error in $L: ZeroDivisionError in handle_boom" 1 60 && ok "one email for the printed traceback, by exception type and last frame" || bad "traceback email" "none in 60 s: $(errs $L | head -c 300)"
sleep 14
expect "still one" 1 "$(emails $SAM "[Annex] new error in $L")"
expect "one group, three times, kind server" "1 3 server" "$(errs $L | py '"%d %d %s" % (len(d), d[0]["count"], d[0]["kind"])')"

echo "7. Removing an app removes its groups"
for a in annex-selftest err-static annex-selftest-boom; do "$ANNEX" remove $a --yes >/dev/null 2>&1 && ok "removed $a" || bad "remove $a" "failed"; done
expect "no groups left for them" 0 "$(errs | py 'len([x for x in d if x["app"] in ("annex-selftest", "err-static", "annex-selftest-boom")])')"
finish
