#!/usr/bin/env bash
# M1.2 acceptance checks for annex-ops, run from the Mac through the SSH tunnel:
# the grant follows annex.yaml, `annex run monthly-report monthly` emails cm (caught by Mailpit),
# outside recipients and other Slack channels are refused, every attempt is in `annex sends`
# without a body, and Mailpit's UI is for admins only.
# Deploys examples/monthly-report (notify: email + Slack) and examples/client-lookup (no notify).
source "$(dirname "$0")/lib.sh"
source "$(dirname "$0")/testlib.sh"
use_tunnel

MAIL_URL=$(setting ANNEX_URL_MAIL)
APP=monthly-report
OTHER=client-lookup

mail_count() { as kaldi-admin "$MAIL_URL/api/v1/messages" | python3 -c 'import json,sys; print(json.load(sys.stdin)["total"])'; }

echo "Deploy"
out=$("$ROOT/annex" deploy "$ROOT/examples/$APP" 2>&1) && ok "$APP deployed" || { bad "$APP deployed" "$out"; echo "passed $pass, failed $fail"; exit 1; }
has "deploy lists ANNEX_OPS_URL" ANNEX_OPS_URL "$out"
has "deploy lists ANNEX_OPS_TOKEN" ANNEX_OPS_TOKEN "$out"
out2=$("$ROOT/annex" deploy "$ROOT/examples/$OTHER" 2>&1) && ok "$OTHER deployed (no notify)" || { bad "$OTHER deployed" "$out2"; echo "passed $pass, failed $fail"; exit 1; }
[ -n "$(vm "docker exec annex-app-$APP printenv ANNEX_OPS_TOKEN")" ] && ok "$APP's container has ANNEX_OPS_TOKEN" || bad "$APP's container has ANNEX_OPS_TOKEN" "empty"
expect "$APP's container has ANNEX_OPS_URL" "http://ops:8000" "$(vm "docker exec annex-app-$APP printenv ANNEX_OPS_URL")"
if vm "docker exec annex-app-$OTHER printenv ANNEX_OPS_TOKEN" >/dev/null 2>&1; then bad "$OTHER has no ANNEX_OPS_TOKEN" "it has one"; else ok "$OTHER's container has no ANNEX_OPS_TOKEN"; fi

echo "Email from the monthly job"
expect "kaldi-admin logs in (for Mailpit)" 200 "$(login kaldi-admin)"
c -o /dev/null -X DELETE -b "$TMP/kaldi-admin.jar" "$MAIL_URL/api/v1/messages"
expect "Mailpit is empty before the run" 0 "$(mail_count)"
out=$("$ROOT/annex" run "$APP" monthly 2>&1) && ok "annex run $APP monthly exits 0" || bad "annex run $APP monthly exits 0" "$out"
has "the job logs that it emailed (logins, not addresses)" '"msg": "report emailed", "to": ["cm"]' "$out"
hits=$(as kaldi-admin "$MAIL_URL/api/v1/search?query=to:cm@annex.example")
expect "Mailpit has exactly one message to cm@annex.example" 1 "$(python3 -c 'import json,sys; print(json.load(sys.stdin)["messages_count"])' <<<"$hits")"
id=$(python3 -c 'import json,sys; print(json.load(sys.stdin)["messages"][0]["ID"])' <<<"$hits" 2>/dev/null || true)
msg=$(as kaldi-admin "$MAIL_URL/api/v1/message/$id")
field() { python3 -c "import json,sys; d=json.load(sys.stdin); print($1)" <<<"$msg"; }
has "subject says the report is ready" "is ready" "$(field 'd["Subject"]')"
has "the HTML links to the CSV" "/download/monthly-positions-" "$(field 'd["HTML"]')"
has "the plain-text part links to the CSV too" "/download/monthly-positions-" "$(field 'd["Text"]')"
has "From names the app" "monthly-report via Annex" "$(field 'd["From"]["Name"]')"
has "From is the configured sender" "$(setting ANNEX_EMAIL_FROM)" "$(field 'd["From"]["Address"]')"

echo "Refusals, from inside $APP"
before=$(mail_count)
cat > "$TMP/probe.py" <<'PY'
import json, os, urllib.request, urllib.error
from annex_runtime import NotifyError, send_email, post_slack

def show(name, fn):
    try:
        r = fn()
        print(f"{name}|200|{json.dumps(r)}")
    except NotifyError as e:
        print(f"{name}|{e.status}|{e}")

def raw(path, body):
    req = urllib.request.Request(os.environ["ANNEX_OPS_URL"] + path, data=json.dumps(body).encode(), method="POST",
                                 headers={"Authorization": "Bearer " + os.environ["ANNEX_OPS_TOKEN"], "Content-Type": "application/json"})
    try:
        urllib.request.urlopen(req, timeout=10)
        return "200", ""
    except urllib.error.HTTPError as e:
        return str(e.code), json.load(e).get("error", "")

show("external", lambda: send_email("someone@gmail.com", "Hi", text="hello"))
show("mixed", lambda: send_email(["cm", "someone@gmail.com"], "Hi", text="hello"))
show("unknown_login", lambda: send_email("nobody", "Hi", text="hello"))
status, msg = raw("/v1/email", {"to": "cm", "subject": "Hi", "text": "hello", "attachments": [{"name": "a.csv"}]})
print(f"attachments|{status}|{msg}")
show("two_bodies", lambda: send_email("cm", "Hi", text="a", html="<p>b</p>"))
show("slack_other", lambda: post_slack("#other", "hello"))
show("slack_granted", lambda: post_slack("#finance-reports", "hello from test-notify"))
PY
probe=$(vm "docker exec -i annex-app-$APP python -" < "$TMP/probe.py" 2>&1)
row() { grep "^$1|" <<<"$probe" | head -1; }
r=$(row external);       has "external address: 403 naming annex.example" "403|" "$r"; has "  ...says it is outside" "annex.example" "$r"
r=$(row mixed);          has "one bad recipient refuses the whole email (403)" "403|" "$r"
r=$(row unknown_login);  has "unknown login: 400 listing the logins" "400|" "$r"; has "  ...names a known login" "cm" "$r"
r=$(row attachments);    has "attachments: 400 with the link advice" "400|" "$r"; has "  ...says to email a link" "email a link" "$r"
r=$(row two_bodies);     has "text and html together: 400" "400|" "$r"
r=$(row slack_other);    has "Slack to #other: 403" "403|" "$r"; has "  ...names #finance-reports" "#finance-reports" "$r"
expect "nothing was sent by the refused emails" "$before" "$(mail_count)"
connected=$(vm "docker exec annex-ops-1 python -m annex_ops.ctl info --json" | python3 -c 'import json,sys; print(json.load(sys.stdin)["slack_connected"])')
r=$(row slack_granted)
if [ "$connected" = True ]; then
  echo "  (Slack is connected: a real post to #finance-reports was attempted)"
  has "Slack to #finance-reports is delivered" "200|" "$r"
else
  echo "  (Slack is not connected: the allowed post must say so)"
  has "Slack to #finance-reports: 503, not connected" "503|" "$r"; has "  ...says Slack isn't connected" "isn't connected" "$r"
fi

echo "Tokens, from inside $OTHER"
cat > "$TMP/anon.py" <<'PY'
import json, urllib.request, urllib.error
for name, headers in (("no_token", {}), ("made_up", {"Authorization": "Bearer not-a-real-token"})):
    req = urllib.request.Request("http://ops:8000/v1/email", method="POST", headers={"Content-Type": "application/json", **headers},
                                 data=json.dumps({"to": "cm", "subject": "x", "text": "x"}).encode())
    try:
        urllib.request.urlopen(req, timeout=10)
        print(name, 200)
    except urllib.error.HTTPError as e:
        print(name, e.code, json.load(e).get("error", ""))
PY
anon=$(vm "docker exec -i annex-app-$OTHER python -" < "$TMP/anon.py" 2>&1)
has "no token: 401" "no_token 401" "$anon"
has "made-up token: 401" "made_up 401" "$anon"
has "the 401 says how to fix it" "Declare notify:" "$anon"
expect "no email was sent by the unauthenticated calls" "$before" "$(mail_count)"

echo "Send log"
sends=$("$ROOT/annex" --json sends "$APP" 2>&1)
check() { python3 -c "import json,sys; rows=json.load(sys.stdin); $1" <<<"$sends"; }
expect "the email is logged as sent to cm@annex.example" True "$(check 'print(any(r["channel"]=="email" and r["status"]=="sent" and r["recipients"]==["cm@annex.example"] and "is ready" in r["subject"] for r in rows))')"
expect "the refused external send is logged as rejected, with the reason" True "$(check 'print(any(r["status"]=="rejected" and r["recipients"]==["someone@gmail.com"] and "annex.example" in r["error"] for r in rows))')"
expect "the unknown login is logged as rejected" True "$(check 'print(any(r["status"]=="rejected" and r["recipients"]==["nobody"] and "unknown login" in r["error"] for r in rows))')"
expect "the attachments attempt is logged as rejected" True "$(check 'print(any(r["status"]=="rejected" and "Attachments" in (r["error"] or "") for r in rows))')"
expect "the wrong Slack channel is logged as rejected" True "$(check 'print(any(r["channel"]=="slack" and r["status"]=="rejected" and r["recipients"]==["#other"] for r in rows))')"
want=$([ "$connected" = True ] && echo sent || echo unavailable)
expect "the allowed Slack post is logged as $want" True "$(check "print(any(r['channel']=='slack' and r['status']=='$want' and r['recipients']==['#finance-reports'] for r in rows))")"
expect "log rows carry only the metadata fields (no body)" True "$(check 'print(all(set(r) == {"id","ts","app","channel","recipients","subject","status","error"} for r in rows))')"
expect "no log row holds the message text" True "$(check 'print(not any("Your report is ready" in json.dumps(r) or "hello" in json.dumps(r) for r in rows))')"
expect "the log only has $APP's rows when asked for $APP" True "$(check 'print({r["app"] for r in rows} == {"'"$APP"'"})')"

echo "Grants"
grants=$(vm "docker exec annex-appdata-db-1 psql -U postgres -d annex_ops -Atc 'select app from annex_ops.app_grants order by 1'")
has "$APP has a grant" "$APP" "$grants"
if grep -qx "$OTHER" <<<"$grants"; then bad "$OTHER has no grant" "it has one"; else ok "$OTHER has no grant"; fi

echo "Mailpit UI"
expect "sam is forbidden" 403 "$(login sam >/dev/null; code -b "$TMP/sam.jar" "$MAIL_URL/")"
expect "kaldi-admin gets Mailpit" 200 "$(code -b "$TMP/kaldi-admin.jar" "$MAIL_URL/")"
finish
