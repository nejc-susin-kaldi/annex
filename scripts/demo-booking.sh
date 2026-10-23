#!/usr/bin/env bash
# Demo beat: send one birthday-party request to party-bookings the way Goat Arcade's website form does, as a POST
# to the app's public webhook URL (https://hooks.<domain>/h/party-bookings/website) with the X-Annex-Hook-Secret header.
#
#   scripts/demo-booking.sh [--venue LJ1] [--date 2026-11-07] [--time 15:00] [--kids 8] [--age 7] [--child Luka] [--bad]
#   ANNEX_ENV=local scripts/demo-booking.sh ...      # the local lane
#
# The secret never comes to this Mac's shell or screen: the curl that sends the request runs where the secret lives
# (on the VM over ssh; on the local lane, the local stack's own secrets directory), reads it there and passes it to
# curl as a header file, not on the command line. Everything here is fictional; the customer's email is @example.com.
# --bad sends a request the app must reject (kids not a number, a date in the past). Needs party-bookings deployed and
# BOOKING_HOOK_SECRET set (`annex secret set party-bookings BOOKING_HOOK_SECRET`, human only).
source "$(dirname "$0")/lib.sh"

APP=party-bookings
HOOK=website
VENUE=LJ1 DATE="" TIME=15:00 KIDS="" AGE="" CHILD="" BAD=0
while [ $# -gt 0 ]; do
  case "$1" in
    --venue) VENUE=${2:?--venue needs a venue code, e.g. LJ1}; shift 2 ;;
    --date) DATE=${2:?--date needs YYYY-MM-DD}; shift 2 ;;
    --time) TIME=${2:?--time needs HH:MM}; shift 2 ;;
    --kids) KIDS=${2:?--kids needs a number}; shift 2 ;;
    --age) AGE=${2:?--age needs a number}; shift 2 ;;
    --child) CHILD=${2:?--child needs a first name}; shift 2 ;;
    --bad) BAD=1; shift ;;
    -h|--help) sed -n '2,13p' "$0"; exit 0 ;;
    *) echo "demo-booking: unknown option $1 (see --help)" >&2; exit 2 ;;
  esac
done

HOOKS_URL=$(setting ANNEX_URL_HOOKS)
[ -n "$HOOKS_URL" ] || { echo "demo-booking: no ANNEX_URL_HOOKS in the platform settings on $WHERE" >&2; exit 1; }
URL=$HOOKS_URL/h/$APP/$HOOK
HOSTPORT=${HOOKS_URL#https://}; HOSTPORT=${HOSTPORT#http://}

# The form, as the website would send it (built here: it holds no secret).
BODY=$(python3 - "$VENUE" "$DATE" "$TIME" "$KIDS" "$AGE" "$CHILD" "$BAD" <<'PY'
import json, random, sys
from datetime import date, timedelta
venue, day, time, kids, age, child, bad = sys.argv[1:8]
kids_names = ["Luka", "Zala", "Nik", "Ema", "Jakob", "Lana", "Tim", "Mia", "Filip", "Ajda", "Mark", "Neža", "Vid", "Lara"]
parents = ["Mojca", "Andrej", "Tina", "Gregor", "Nataša", "Rok", "Petra", "Matej", "Urška", "Blaž"]
surnames = ["Kovač", "Zupan", "Golob", "Kranjc", "Vidic", "Potočnik", "Lesjak", "Hribar", "Bizjak", "Kos"]
child = child or random.choice(kids_names)
parent, surname = random.choice(parents), random.choice(surnames)
if not day:  # the Saturday at least a week from today
    d = date.today() + timedelta(days=7)
    day = str(d + timedelta(days=(5 - d.weekday()) % 7))
age = int(age) if age else random.randint(6, 12)
note = f"It's for {child}'s {age}th birthday. " + random.choice(
    ["One of the kids is allergic to nuts.", "Could we have the room with the old pinball tables?",
     "A few parents will stay too.", "We'd bring our own cake if that's fine.", ""])
form = {"name": f"{parent} {surname}", "email": f"{parent.lower()}.{surname.lower()}@example.com".replace("š", "s")
        .replace("ž", "z").replace("č", "c"), "phone": f"+386 41 {random.randint(100, 999)} {random.randint(100, 999)}",
        "preferred_venue": venue, "date": day, "time": time,
        "kids": int(kids) if kids else random.randint(6, 14), "age": age, "note": note.strip()}
if bad == "1":  # what a broken form or a bot might send
    form.update(kids="lots", date="2020-02-30", email="not-an-address")
print(json.dumps(form, ensure_ascii=False))
PY
)
echo "Sending to $URL:"
python3 -c 'import json,sys; [print(f"  {k}: {v}") for k, v in json.loads(sys.argv[1]).items()]' "$BODY"

# How the sending host reaches the front door, and trusts its certificate.
if [ "$TARGET" = local ]; then
  CONNECT="::127.0.0.1:${ANNEX_PUBLIC_PORT:-9443}"
  CA_PEM=$(cat "$(mkcert -CAROOT)/rootCA.pem")   # a public certificate, not a secret
elif [ "${ANNEX_TLS_MODE:-local}" = upstream ]; then
  # TLS ends at the upstream proxy; Traefik on the VM speaks plain HTTP on :80
  URL=http://${HOSTPORT%%:*}/h/$APP/$HOOK CONNECT="::127.0.0.1:80" CA_PEM=""
else
  CONNECT="::127.0.0.1:443"
  CA_PEM=$(cat "$(mkcert -CAROOT)/rootCA.pem")
fi

# The script below runs on the host that holds the secret (vm: bash locally on the local lane, ssh on the VM). Its
# values are quoted here with %q; the secret is read there, by bash builtins, and handed to curl through a
# process-substitution file, so it shows up in no command line, log or output.
REMOTE=$(cat <<EOF
set -euo pipefail
f=$(printf %q "$DATA/secrets/apps/$APP/secrets.env")
line=\$(grep '^BOOKING_HOOK_SECRET=' "\$f" 2>/dev/null) || {
  echo "demo-booking: BOOKING_HOOK_SECRET is not set for $APP on $WHERE. A human sets it with: annex secret set $APP BOOKING_HOOK_SECRET" >&2
  exit 3
}
v=\${line#BOOKING_HOOK_SECRET=}; v=\${v#\\'}; v=\${v%\\'}
body=$(printf %q "$BODY")
ca=$(printf %q "$CA_PEM")
tls=()
if [ -n "\$ca" ]; then   # a file (curl on macOS can't read a CA from a pipe), under the data dir, removed on exit
  cafile=\$(mktemp $(printf %q "$DATA")/.demo-booking-ca.XXXXXX)
  trap 'rm -f "\$cafile"' EXIT
  printf '%s\n' "\$ca" > "\$cafile"
  tls=(--cacert "\$cafile")
fi
curl -sS --max-time 20 -w '\nHTTP %{http_code}\n' --connect-to $(printf %q "$CONNECT") \${tls[@]+"\${tls[@]}"} \\
  -H 'Content-Type: application/json' -H 'User-Agent: GoatArcadeWebsite/1.0 (party form)' \\
  -H @<(printf 'X-Annex-Hook-Secret: %s\n' "\$v") \\
  --data-binary @<(printf '%s' "\$body") $(printf %q "$URL")
EOF
)
OUT=$(vm "bash -s" <<<"$REMOTE")
echo "Annex answered: $OUT" | tr '\n' ' '; echo
case "$OUT" in
  *"HTTP 2"*) echo "Stored by Annex and forwarded to $APP. See it: annex hooks $APP, and the app's page." ;;
  *) echo "demo-booking: the request was not accepted" >&2; exit 1 ;;
esac
