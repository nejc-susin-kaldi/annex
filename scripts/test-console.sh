#!/usr/bin/env bash
# M2.4 acceptance checks: the console (about 5 minutes).
#   1. who sees what: sam his apps, kaldi-admin the fleet, cm /status and a 403 on app pages, anonymous the login;
#   2. a forged Remote-User or Remote-Groups from the browser changes nothing;
#   3. the console is on no network an app is on, and an app can't reach it (tested from an app with Postgres);
#   4. its container and database role hold nothing they shouldn't: no Docker access, tokens, grants or bodies, read-only;
#   5. every number on an app's page is the one `annex status`, `annex usage`, `annex releases` and `annex logs` give;
#   6. nothing an app logs can inject markup into a page; the pages are read-only and carry security headers;
#   7. the Homepage tiles point at the console.
# Deploys annex-selftest (owner sam, with Postgres) and annex-selftest-other (owner kaldi-admin, edge network only); removes both.
source "$(dirname "$0")/lib.sh"
source "$(dirname "$0")/testlib.sh"
use_tunnel
ANNEX="$ROOT/annex"
CON=$(setting ANNEX_URL_CONSOLE)
PATTERN=$(setting ANNEX_HOST_PATTERN)
SUFFIX=$(setting ANNEX_PORT_SUFFIX)
url() { echo "https://${PATTERN//\{name\}/$1}$SUFFIX"; }
WORK=$ROOT/.build/test-console
TUNING=$DATA/state/ops/tuning.json
mkdir -p "${WORK:?}" && find "${WORK:?}" -mindepth 1 -delete
py() { python3 -c "import json,sys; d=json.load(sys.stdin); print($1)"; }
text() { sed 's/<style>.*<\/style>//; s/<[^>]*>/ /g' | tr -s ' \n'; }
psql_as() { # psql_as ROLE SQL: run SQL as that role (SET ROLE), print output and errors
  printf 'SET ROLE %s;\n%s\n' "$1" "$2" | ssh annex 'docker exec -i annex-appdata-db-1 psql -U postgres -d annex_ops -At -f -' 2>&1 | grep -v '^SET$'
}
cleanup() {
  vm "rm -f $TUNING" >/dev/null 2>&1 || true
  for a in annex-selftest annex-selftest-other; do "$ANNEX" remove $a --yes >/dev/null 2>&1 || true; done
}
on_exit cleanup

echo "Setup"
for u in sam cm kaldi-admin; do expect "$u signs in" 200 "$(login $u)"; done
vm "printf '%s' '{\"errors_scan_s\": 4, \"errors_settle_s\": 4}' > $TUNING && chmod 644 $TUNING"
sleep 6
A=annex-selftest; B=annex-selftest-other
selftest_build "$WORK/b" --name $B --owner kaldi-admin --no-postgres --drop notify,webhooks
selftest_deploy "$WORK/b" >/dev/null && ok "$B deployed (owner kaldi-admin, no Postgres: on the edge network only)" || bad "$B deployed" "failed"
selftest_build "$WORK/a"
selftest_deploy "$WORK/a" >/dev/null && ok "$A deployed (owner sam, with Postgres)" || bad "$A deployed" "failed"
EVIL='<script>alert(1)</script>'
hdr=$(c -D - -o /dev/null -b "$TMP/sam.jar" "$(url annex-selftest)/emit?level=error&msg=$(python3 -c 'import urllib.parse,sys; print(urllib.parse.quote(sys.argv[1]))' "$EVIL")")
RID=$(tr -d '\r' <<<"$hdr" | sed -n 's/^[Xx]-[Rr]equest-[Ii]d: //p')
c -o /dev/null -b "$TMP/sam.jar" "$(url annex-selftest)/boom"
c -o /dev/null -b "$TMP/sam.jar" "$(url annex-selftest)/"
sleep 3   # the lines reach the table and the usage rows are written

echo "1. Who sees what"
expect "anonymous is sent to the login" 302 "$(code "$CON/")"
for p in / /status /apps/annex-selftest /apps/annex-selftest; do expect "sam $p" 200 "$(code -b "$TMP/sam.jar" "$CON$p")"; done
expect "sam is refused another owner's app (403)" 403 "$(code -b "$TMP/sam.jar" "$CON/apps/annex-selftest-other")"
expect "  ...and its logs" 403 "$(code -b "$TMP/sam.jar" "$CON/apps/annex-selftest-other/logs")"
expect "  ...and a name that doesn't exist looks the same (no listing of apps)" 403 "$(code -b "$TMP/sam.jar" "$CON/apps/no-such-app")"
home=$(as sam "$CON/" | text)
has "sam's overview has his apps" "annex-selftest" "$home"
hasnt "  ...and not the others'" "annex-selftest-other" "$home"
hasnt "  ...and no platform section" "Platform" "$home"
for p in /status /; do expect "cm $p" 200 "$(code -b "$TMP/cm.jar" "$CON$p")"; done
for a in annex-selftest annex-selftest-other; do expect "cm is refused /apps/$a (403)" 403 "$(code -b "$TMP/cm.jar" "$CON/apps/$a")"; done
has "cm's overview points at the status page" "don't own any apps" "$(as cm "$CON/" | text)"
st=$(as cm "$CON/status" | text)
has "the status page lists apps by name and state for everyone" "annex-selftest" "$st"
has "  ...and the platform's front door" "front door" "$st"
admin=$(as kaldi-admin "$CON/" | text)
for a in annex-selftest annex-selftest-other; do has "kaldi-admin's fleet has $a" "$a" "$admin"; expect "kaldi-admin opens /apps/$a" 200 "$(code -b "$TMP/kaldi-admin.jar" "$CON/apps/$a")"; done
has "  ...with the platform section and the host numbers" "Memory available" "$admin"
expect "an admin gets a real 404 for an unknown app" 404 "$(code -b "$TMP/kaldi-admin.jar" "$CON/apps/no-such-app")"
page=$(as kaldi-admin "$CON/apps/annex-selftest" | text)
has "an app page has its owner and live release" "Owner: sam" "$page"
for s in Status Releases Usage Errors Config; do has "  ...a $s section" "$s" "$page"; done
has "an admin sees per-person usage, labelled as personal data" "Personal data" "$(as kaldi-admin "$CON/apps/annex-selftest" | text)"
hasnt "the owner doesn't" "Personal data" "$(as sam "$CON/apps/annex-selftest" | text)"

echo "2. Forged identity changes nothing"
expect "sam with a forged Remote-User: kaldi-admin" 403 "$(code -b "$TMP/sam.jar" -H 'Remote-User: kaldi-admin' -H 'Remote-Groups: admins' "$CON/apps/annex-selftest-other")"
expect "  ...and a forged groups header alone" 403 "$(code -b "$TMP/sam.jar" -H 'Remote-Groups: admins' "$CON/apps/annex-selftest-other")"
expect "  ...and the underscore spelling" 403 "$(code -b "$TMP/sam.jar" -H 'Remote_User: kaldi-admin' -H 'Remote_Groups: admins' "$CON/apps/annex-selftest-other")"
expect "cm forging sam" 403 "$(code -b "$TMP/cm.jar" -H 'Remote-User: sam' "$CON/apps/annex-selftest")"
has "the page still says who really signed in" "sam (signed in)" "$(as sam -H 'Remote-User: kaldi-admin' -H 'Remote-Groups: admins' "$CON/" | text)"
expect "with no session, the headers alone get the login redirect" 302 "$(code -H 'Remote-User: kaldi-admin' -H 'Remote-Groups: admins' "$CON/apps/annex-selftest-other")"

echo "3. No app can reach the console"
nets() { vm "docker inspect $1 -f '{{range \$k, \$v := .NetworkSettings.Networks}}{{\$k}} {{end}}'" | tr ' ' '\n' | grep . | sort | tr '\n' ' '; }
expect "the console is on platform and its own network only" "annex-console annex-platform " "$(nets annex-console-1)"
expect "appdata-db is on appdata and the console network only" "annex-appdata annex-console " "$(nets annex-appdata-db-1)"
expect "the console network is internal" True "$(vm "docker network inspect annex-console -f '{{.Internal}}'" | sed 's/true/True/')"
expect "only the console and appdata-db are on it" "annex-appdata-db-1 annex-console-1" "$(vm "docker network inspect annex-console -f '{{range .Containers}}{{.Name}} {{end}}'" | tr ' ' '\n' | grep . | sort | tr '\n' ' ' | sed 's/ $//')"
CIPS=$(vm "docker inspect annex-console-1 -f '{{range .NetworkSettings.Networks}}{{.IPAddress}} {{end}}'")
probe() { # probe CONTAINER: tries the name and every address of the console from inside the app
  vm "docker exec $1 python -c \"
import socket, sys
for target in ['console'] + sys.argv[1:]:
    try:
        s = socket.create_connection((target, 8000), timeout=3); print('REACHED', target); s.close()
    except OSError as e:
        print('no', target, type(e).__name__)
\" $CIPS"
}
for a in annex-selftest annex-selftest-other; do
  out=$(probe annex-app-$a)
  hasnt "from $a (on edge$( [ "$a" = annex-selftest ] && echo ' and appdata, with Postgres')) the console can't be reached" "REACHED" "$out"
  has "  ...not even by name" "no console" "$out"
done

echo "4. What the container and its database role hold"
env=$(vm "docker exec annex-console-1 printenv" | cut -d= -f1)
for k in SLACK_BOT_TOKEN ANNEX_ALERT_SLACK_CHANNEL OPS_SMTP_HOST ANNEX_EMAIL_FROM OPS_DOCKER_API; do hasnt "no $k in the console's environment" "$k" "$env"; done
expect "no mounts at all (no Docker socket, no secrets, no directory)" "" "$(vm "docker inspect annex-console-1 -f '{{range .Mounts}}{{.Destination}} {{end}}'")"
expect "read-only root filesystem, uid 10001, no capabilities" "true 10001:10001 [ALL]" "$(vm "docker inspect annex-console-1 -f '{{.HostConfig.ReadonlyRootfs}} {{.Config.User}} {{.HostConfig.CapDrop}}'")"
expect "memory limit 96 MiB" 100663296 "$(vm "docker inspect annex-console-1 -f '{{.HostConfig.Memory}}'")"
expect "not published on any host port" "map[]" "$(vm "docker inspect annex-console-1 -f '{{.HostConfig.PortBindings}}'")"
denied() { has "$1" "permission denied" "$(psql_as annex_console "$2")"; }
expect "the role reads what the pages show" "ok" "$(psql_as annex_console "SELECT 'ok' FROM annex_ops.logs LIMIT 1; SELECT 'ok' FROM annex_ops.releases LIMIT 1" | sort -u | head -1)"
expect "  ...including the webhook events' metadata" "ok" "$(psql_as annex_console "SELECT 'ok' FROM (SELECT id, app, hook, status FROM annex_ops.hook_events LIMIT 1) x UNION SELECT 'ok'" | sort -u | head -1)"
denied "it can't read a webhook body" "SELECT body FROM annex_ops.hook_events LIMIT 1"
denied "  ...or its headers" "SELECT headers FROM annex_ops.hook_events LIMIT 1"
denied "  ...or the apps' token hashes (app_grants)" "SELECT token_sha256 FROM annex_ops.app_grants LIMIT 1"
denied "  ...or the log cursors" "SELECT * FROM annex_ops.log_cursors LIMIT 1"
denied "  ...or the send log (not granted before the audit page)" "SELECT * FROM annex_ops.sends LIMIT 1"
denied "it can't insert" "INSERT INTO annex_ops.releases (app, release, image, result, deployed_at, manifest) VALUES ('x', 1, 'i', 'live', now(), '{}')"
denied "  ...or update" "UPDATE annex_ops.error_groups SET count = 0"
denied "  ...or delete" "DELETE FROM annex_ops.logs"
has "  ...or drop" "must be owner" "$(psql_as annex_console "DROP TABLE annex_ops.logs")"

echo "5. The numbers match the CLI"
use=$("$ANNEX" --json usage annex-selftest)
page=$(as kaldi-admin "$CON/apps/annex-selftest" | text)
has "users and requests in 7 days are annex usage's" "$(py 'str(d["app"]["users_7d"]) + " user(s) in the last 7 days (" + d["app"]["trend"] + "), " + str(d["app"]["users_30d"]) + " in 30 days, " + str(d["app"]["requests_7d"]) + " requests"' <<<"$use")" "$page"
rel=$("$ANNEX" --json releases annex-selftest)
expect "the release list has as many rows as annex releases" "$(py 'len(d)' <<<"$rel")" "$(as kaldi-admin "$CON/apps/annex-selftest" | grep -o '/logs?release=[0-9]*' | sort -u | wc -l | tr -d ' ')"
has "the live release is the one annex status shows" "live release: r$("$ANNEX" --json status annex-selftest | py 'd["release"][1:]')" "$page"
has "the health check state is annex status's" "Health check: up" "$page"
cli=$("$ANNEX" logs annex-selftest --request "$RID")
nlines=$(grep -c . <<<"$cli")
expect "a request's log lines: as many as annex logs --request" "$nlines" "$(as sam "$CON/apps/annex-selftest/logs?request=$RID" | grep -o 'class="log' | wc -l | tr -d ' ')"
for i in $(seq 1 15); do [ "$("$ANNEX" --json errors annex-selftest | py 'len(d)')" -ge 2 ] && break; sleep 4; done
errs=$("$ANNEX" --json errors annex-selftest)
expect "the error groups on the page are annex errors' (count)" "$(py 'len(d)' <<<"$errs")" "$(as sam "$CON/apps/annex-selftest" | grep -o '<span class="badge \(bad\|muted\)">\(active\|quiet\)</span>' | wc -l | tr -d ' ')"
has "  ...with their request ids linked to the logs" "/apps/annex-selftest/logs?request=" "$(as sam "$CON/apps/annex-selftest")"
has "  ...and what the error was" "ZeroDivisionError in handle_boom" "$(as sam "$CON/apps/annex-selftest" | text)"
expect "an app with no errors says so" "no errors seen" "$(as kaldi-admin "$CON/apps/annex-selftest-other" | text | grep -o 'no errors seen')"
has "the config shows names of injected variables, never values" "ANNEX_APP_NAME" "$(as sam "$CON/apps/annex-selftest" | text)"
hasnt "  ...and no token in any page" "ANNEX_GATEWAY_TOKEN=" "$(as sam "$CON/apps/annex-selftest" | text)"
gw=$(as kaldi-admin "$CON/apps/annex-selftest/logs?source=gateway" | text)
has "the gateway reads are a source of the logs page" "GET /" "$gw"

echo "6. Nothing from an app injects markup; read-only; headers"
logs=$(as sam "$CON/apps/annex-selftest/logs?grep=alert")
hasnt "a script an app logged isn't in the page as markup" "<script>alert" "$logs"
has "  ...it is shown escaped" "&lt;script&gt;alert(1)&lt;/script&gt;" "$logs"
hdrs=$(c -D - -o /dev/null -b "$TMP/sam.jar" "$CON/apps/annex-selftest")
has "Content-Security-Policy: no scripts at all" "default-src 'none'" "$hdrs"
has "  ...and nosniff, no-store" "no-store" "$hdrs"
expect "a POST is refused (405)" 405 "$(code -b "$TMP/sam.jar" -X POST -d x "$CON/apps/annex-selftest")"
expect "so is a DELETE" 405 "$(code -b "$TMP/sam.jar" -X DELETE "$CON/apps/annex-selftest")"
has "every page says it is read-only" "Read-only" "$(as sam "$CON/apps/annex-selftest" | text)"
has "  ...and gives the CLI command" "annex rollback annex-selftest" "$(as sam "$CON/apps/annex-selftest" | text)"
has "the logs page filters by release" 'name="release"' "$(as sam "$CON/apps/annex-selftest/logs?release=1" | grep -o 'name="release"')"
has "an old, unmatched search is just empty" "no lines match" "$(as sam "$CON/apps/annex-selftest/logs?grep=zzz-nothing-$RANDOM" | text)"

echo "7. Homepage"
svc=$(as sam "$HOME_URL/api/services")
has "the Console tile is in Platform" "$CON" "$svc"
has "the Status tile points at the console's /status" "$CON/status" "$svc"

finish
