# Shared helpers for scripts/test-*.sh. Source it after lib.sh:
#   source "$(dirname "$0")/lib.sh"
#   source "$(dirname "$0")/testlib.sh"
#   use_tunnel            # for suites that make HTTP requests through the SSH tunnel
#   ...checks...
#   finish                # prints "passed N, failed M" and sets the exit status
# Suites register their own cleanup with `on_exit "<command>"`; $TMP is removed automatically.

pass=0 fail=0
ok()  { pass=$((pass + 1)); printf '  \033[32mok\033[0m   %s\n' "$1"; }
bad() { fail=$((fail + 1)); printf '  \033[31mFAIL\033[0m %s (%s)\n' "$1" "$2"; }
expect() { if [ "$2" = "$3" ]; then ok "$1"; else bad "$1" "expected '$2', got '$3'"; fi; }
# has DESC TEXT HAYSTACK: a fixed string; has_re for a regular expression; hasnt for "must not contain"
has() { if grep -qF -- "$2" <<<"$3"; then ok "$1"; else bad "$1" "missing '$2' in: $(head -c 300 <<<"$3")"; fi; }
has_re() { if grep -qE -- "$2" <<<"$3"; then ok "$1"; else bad "$1" "no match for /$2/ in: $(head -c 300 <<<"$3")"; fi; }
hasnt() { if grep -qF -- "$2" <<<"$3"; then bad "$1" "found '$2'"; else ok "$1"; fi; }

finish() {
  echo
  echo "passed $pass, failed $fail"
  [ "$fail" -eq 0 ]
}

TMP=$(mktemp -d)
_on_exit=()
on_exit() { _on_exit+=("$1"); }
_run_exit() {
  local cmd
  for cmd in ${_on_exit[@]+"${_on_exit[@]}"}; do eval "$cmd" || true; done
  rm -rf "$TMP"
}
trap _run_exit EXIT

# Wait until every Annex container that has a healthcheck reports healthy (up to $1 seconds, default 90).
# A deploy restarts Ofelia (D18), whose healthcheck runs every 30 s, so a suite that runs right after one
# would otherwise see "health: starting". Prints what's still not healthy if the time runs out.
wait_healthy() {
  local limit=${1:-90} waited=0 left
  while :; do
    left=$(vm "docker ps --filter label=com.kaldi.annex=true --format '{{.Names}} {{.Status}}'" \
             | grep -E '\((health: starting|unhealthy)\)' || true)
    [ -z "$left" ] && return 0
    [ "$waited" -ge "$limit" ] && { echo "$left"; return 1; }
    sleep 5; waited=$((waited + 5))
  done
}

# HTTP through the SSH tunnel: every host:port is sent to 127.0.0.1:$PORT, so no /etc/hosts entries are needed.
use_tunnel() {
  render >/dev/null
  "$ROOT/scripts/tunnel.sh" up >/dev/null
  PORT=${ANNEX_PUBLIC_PORT:-8443}
  CA="$(mkcert -CAROOT)/rootCA.pem"
  HOME_URL=$(setting ANNEX_URL_HOME)
  AUTH_URL=$(setting ANNEX_URL_AUTH)
}
c() { curl -sS --cacert "$CA" --connect-to "::127.0.0.1:$PORT" "$@"; }
code() { c -o /dev/null -w '%{http_code}' "$@"; }
# demo users only: their generated passwords are in users.txt (D12)
password() { vm "awk -v u='$1' '\$1 == u {print \$2}' $DATA/secrets/users.txt"; }
# login USER [PASSWORD] -> cookie jar $TMP/USER.jar; prints the HTTP status (200 = signed in)
login() {
  local pw=${2:-}
  [ -n "$pw" ] || pw=$(password "$1")
  c -c "$TMP/$1.jar" -o "$TMP/$1.login" -w '%{http_code}' -H 'Content-Type: application/json' \
    --data "$(python3 -c 'import json,sys; print(json.dumps({"username": sys.argv[1], "password": sys.argv[2],
              "keepMeLoggedIn": False, "targetURL": sys.argv[3] + "/"}))' "$1" "$pw" "$HOME_URL")" \
    "$AUTH_URL/api/firstfactor"
}
as() { local u=$1; shift; c -b "$TMP/$u.jar" "$@"; }

# Mailpit, read as kaldi-admin through the tunnel (call `login kaldi-admin` first). Suites that define their own
# versions of these keep them (a later definition wins).
mail_count() { # mail_count RECIPIENT SUBJECT_PREFIX -> how many such messages
  as kaldi-admin "$(setting ANNEX_URL_MAIL)/api/v1/messages?limit=500" | python3 -c '
import json, sys
rcpt, prefix = sys.argv[1], sys.argv[2]
print(sum(1 for m in json.load(sys.stdin)["messages"]
          if m["Subject"].startswith(prefix) and any(t["Address"] == rcpt for t in m["To"])))' "$@"
}
mail_wait() { # mail_wait RECIPIENT SUBJECT_PREFIX N TIMEOUT -> 0 once there are N such messages
  local end=$((SECONDS + $4))
  while [ $SECONDS -lt $end ]; do [ "$(mail_count "$1" "$2")" -ge "$3" ] && return 0; sleep 3; done
  return 1
}
mail_clear() { c -o /dev/null -X DELETE -b "$TMP/kaldi-admin.jar" "$(setting ANNEX_URL_MAIL)/api/v1/messages"; }
mail_find() { # mail_find RECIPIENT SUBJECT_PREFIX -> the newest such message's text (plain text part)
  local id
  id=$(as kaldi-admin "$(setting ANNEX_URL_MAIL)/api/v1/messages?limit=500" | python3 -c '
import json, sys
rcpt, prefix = sys.argv[1], sys.argv[2]
ids = [m["ID"] for m in json.load(sys.stdin)["messages"]
       if m["Subject"].startswith(prefix) and any(t["Address"] == rcpt for t in m["To"])]
print(ids[0] if ids else "")' "$@")
  [ -n "$id" ] && as kaldi-admin "$(setting ANNEX_URL_MAIL)/api/v1/message/$id" | python3 -c 'import json,sys; print(json.load(sys.stdin)["Text"])'
}
