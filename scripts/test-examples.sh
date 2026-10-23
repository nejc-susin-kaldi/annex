#!/usr/bin/env bash
# Do the demo apps in examples/ still build, deploy and work? Run it before a demo, not as a platform check: the platform
# suites use annex-selftest and never touch these (they are stand-ins for client apps and change with the client).
# Covers client-lookup and monthly-report (until GA.6 removes them) and the six Goat Arcade apps in examples/goat-arcade/
# (venue-pulse, weekly-takings, tech-board, high-score-wall, league-desk, party-bookings): deploy, health check, the pages
# for the logins that use them, and the writes and jobs each one has. Repeatable on a stack where the apps already ran:
# sign-ups are withdrawn and claims released again; what stays behind is one party request (and its confirmation) from
# scripts/demo-booking.sh, and venue-pulse's saved venue for sam. Jobs only email demo logins, and Slack posts go to
# #annex-selftest because testlib switches test mode on while a suite runs (D74). It never runs monthly-report's
# `monthly` job (that one emails and posts to Slack: it is the demo's to run, on purpose). party-bookings needs its
# secret (`annex secret set party-bookings BOOKING_HOOK_SECRET`, human only): without it that block prints a skip line
# and the suite goes on. Works on both lanes (ANNEX_ENV=local).
# Leaves every app deployed, as the demo wants them.
source "$(dirname "$0")/lib.sh"
source "$(dirname "$0")/testlib.sh"
use_tunnel
ANNEX="$ROOT/annex"
url() { echo "https://$(setting ANNEX_HOST_PATTERN | sed "s/{name}/$1/")$(setting ANNEX_PORT_SUFFIX)"; }
text() { sed 's/<[^>]*>/ /g' | tr -s ' '; }
status_up() { expect "annex status $1: health check up" up "$("$ANNEX" --json status "$1" | python3 -c 'import json,sys; print(json.load(sys.stdin)["health_check"]["state"])')"; }
# deploy_app NAME DIR: deploys (output in $out); no warning at deploy except Slack not being connected, which a stack without Slack has
deploy_app() {
  out=$("$ANNEX" deploy "$2" 2>&1) && ok "deploys $1 ($(grep -o 'r[0-9]* is live' <<<"$out" | head -1))" || { bad "deploy $1" "$(tail -3 <<<"$out")"; return 1; }
  expect "no warnings at deploy ($1)" "" "$(grep 'warning:' <<<"$out" | grep -vi "Slack isn't connected" || true)"
}
# psql on the product database, through the VM (works on both lanes)
pq() { vm "docker exec -i annex-product-db-1 psql -XAt -U postgres -d product -c \"$1\""; }
GA="$ROOT/examples/goat-arcade"

expect "sam signs in" 200 "$(login sam)"
expect "maja signs in" 200 "$(login maja)"
for u in boris lena petra kaldi-admin; do expect "$u signs in" 200 "$(login "$u")"; done

echo "client-lookup"
out=$("$ANNEX" deploy "$ROOT/examples/client-lookup" 2>&1) && ok "deploys ($(grep -o 'r[0-9]* is live' <<<"$out" | head -1))" || bad "deploy client-lookup" "$(tail -3 <<<"$out")"
hasnt "no warnings at deploy" "warning:" "$out"
page=$(as sam "$(url client-lookup)/" | text)
has "greets the signed-in user" "Hello, Sam Novak" "$page"
has "reads its granted views" "client_summary : allowed" "$page"
has "is refused the ungranted one" "advisor_book : denied" "$page"
has "a search returns clients" "Novak" "$(as maja "$(url client-lookup)/?q=novak" | text)"
expect "annex status: health check up" up "$("$ANNEX" --json status client-lookup | python3 -c 'import json,sys; print(json.load(sys.stdin)["health_check"]["state"])')"

echo "monthly-report"
out=$("$ANNEX" deploy "$ROOT/examples/monthly-report" 2>&1) && ok "deploys ($(grep -o 'r[0-9]* is live' <<<"$out" | head -1))" || bad "deploy monthly-report" "$(tail -3 <<<"$out")"
has "its page answers" "Monthly positions" "$(as maja "$(url monthly-report)/" | text)"
jobs=$(vm "docker logs annex-ofelia-1 2>&1 | sed 's/\x1b\[[0-9;]*m//g'")
has "its monthly job is scheduled (and is not run here)" 'registered "monthly-report-monthly"' "$jobs"
has "so is the demo's refresh job" 'registered "monthly-report-refresh"' "$jobs"
out=$("$ANNEX" run monthly-report refresh 2>&1) && ok "annex run monthly-report refresh (writes the CSV, notifies nobody)" || bad "run refresh" "$(tail -3 <<<"$out")"
has "  ...it logged in the convention" '"msg": "job finished"' "$out"
expect "annex status: health check up" up "$("$ANNEX" --json status monthly-report | python3 -c 'import json,sys; print(json.load(sys.stdin)["health_check"]["state"])')"

echo "venue-pulse"
if deploy_app venue-pulse "$GA/venue-pulse"; then
  VP=$(url venue-pulse)
  page=$(as maja "$VP/?venue=LJ1" | text)
  has "a venue page names the venue" "Goat Arcade Metelkova" "$page"
  has "  ...games per day" "Games per day" "$page"
  has "  ...idle machines" "Idle machines" "$page"
  has "  ...rotation candidates" "Rotation candidates" "$page"
  has "a venue from a link is a look, not saved" "from the link (not saved)" "$(as maja "$VP/?venue=MB" | text)"
  has_re "maja's home page is the picker or her venue" "Which venue do you want to look at|your saved venue|your home venue" "$(as maja "$VP/" | text)"
  expect "choosing a venue redirects" 303 "$(code -b "$TMP/sam.jar" -X POST -d venue=MB "$VP/venue")"
  has "  ...and it is remembered" "your saved venue" "$(as sam "$VP/" | text)"
  expect "a cross-site POST is refused" 403 "$(code -b "$TMP/maja.jar" -X POST -H 'Sec-Fetch-Site: cross-site' -d venue=MB "$VP/venue")"
  status_up venue-pulse
fi

echo "weekly-takings"
if deploy_app weekly-takings "$GA/weekly-takings"; then
  WT=$(url weekly-takings)
  page=$(as petra "$WT/" | text)
  has "petra sees the takings" "Takings, week of" "$page"
  has "  ...all venues" "All venues" "$page"
  hasnt "  ...and no builder note" "For builders" "$page"
  has "sam, a builder, sees how to run it" "annex run weekly-takings weekly" "$(as sam "$WT/" | text)"
  before=$(mail_count petra@goat-arcade.example "Weekly takings: week of")
  out=$("$ANNEX" run weekly-takings weekly 2>&1) && ok "annex run weekly-takings weekly" || bad "run weekly" "$(tail -3 <<<"$out")"
  has "  ...it logged in the convention" '"msg": "job finished"' "$out"
  has "  ...and logged the email" "summary emailed" "$out"
  mail_wait petra@goat-arcade.example "Weekly takings: week of" $((before + 1)) 60 && ok "  ...petra got the summary" || bad "petra got the summary" "no new message in 60 s (was $before)"
  week=$(as petra "$WT/" | grep -o 'runs/[0-9-]*' | head -1 | sed 's|runs/||')
  has_re "the run history lists a week" '^[0-9]{4}-[0-9]{2}-[0-9]{2}$' "$week"
  has "the week's page offers the CSV" "Download CSV" "$(as petra "$WT/runs/$week" | text)"
  csv=$(as petra "$WT/download/weekly-takings-$week.csv")
  has "the CSV has its header" "week_start,week_end,venue_code" "$csv"
  has "  ...and a total row" ",TOTAL," "$csv"
  status_up weekly-takings
fi

echo "tech-board"
if deploy_app tech-board "$GA/tech-board"; then
  TB=$(url tech-board)
  page=$(as boris "$TB/" | text)
  has "boris sees the board" "Out of order now" "$page"
  has "  ...tickets, oldest first" "Open tickets, oldest first" "$page"
  has "  ...rising faults" "Rising faults" "$page"
  has "sam sees it too" "Tech board" "$(as sam "$TB/" | text)"
  # a ticket nobody holds (a held one shows a release form instead of a claim form)
  tid=$(as boris "$TB/" | python3 -c '
import re, sys
for m in re.finditer(r"<tr id=\"ticket-(\d+)\">(.*?)</tr>", sys.stdin.read(), re.S):
    if "action=\"claim\"" in m.group(2):
        print(m.group(1))
        break')
  if [ -n "$tid" ]; then
    tb_row() { as "$1" "$TB/" | python3 -c '
import re, sys
m = re.search(r"<tr id=\"ticket-" + sys.argv[1] + r"\">(.*?)</tr>", sys.stdin.read(), re.S)
print(re.sub(r"<[^>]*>", " ", m.group(1)) if m else "")' "$tid"; }
    on_exit "as boris -o /dev/null -X POST -d kind=ticket -d ref_id=$tid $TB/release"
    expect "boris claims ticket $tid" 303 "$(code -b "$TMP/boris.jar" -X POST -d kind=ticket -d ref_id="$tid" "$TB/claim")"
    row=$(tb_row sam)
    has "  ...sam sees who is on it" "Boris" "$row"
    has "  ...since when" " since " "$row"
    expect "a release gives it back" 303 "$(code -b "$TMP/boris.jar" -X POST -d kind=ticket -d ref_id="$tid" "$TB/release")"
    has "  ...it can be claimed again" "I'm on it" "$(tb_row sam)"
    expect "a cross-site claim is refused" 403 "$(code -b "$TMP/boris.jar" -X POST -H 'Sec-Fetch-Site: cross-site' -d kind=ticket -d ref_id="$tid" "$TB/claim")"
  else
    bad "an unclaimed ticket to claim" "every open ticket is already held (release one on the board)"
  fi
  expect "an unknown kind is refused" 400 "$(code -b "$TMP/boris.jar" -X POST -d kind=nothing -d ref_id=1 "$TB/claim")"
  before=$(mail_count boris@goat-arcade.example "Morning list")
  out=$("$ANNEX" run tech-board morning 2>&1) && ok "annex run tech-board morning" || bad "run morning" "$(tail -3 <<<"$out")"
  has "  ...it logged the email" "morning list emailed" "$out"
  mail_wait boris@goat-arcade.example "Morning list" $((before + 1)) 60 && ok "  ...boris got the morning list" || bad "boris got the morning list" "no new message in 60 s (was $before)"
  out=$("$ANNEX" run tech-board watch 2>&1) && ok "annex run tech-board watch" || bad "run watch" "$(tail -3 <<<"$out")"
  has "  ...it logged its outcome" "watch:" "$out"
  status_up tech-board
fi

echo "high-score-wall"
if deploy_app high-score-wall "$GA/high-score-wall"; then
  HW=$(url high-score-wall)
  has "lena sees the wall" "High-score wall" "$(as lena "$HW/")"
  has "a venue link has the venue's name slot" 'id="venue-name"' "$(as maja "$HW/?venue=LJ1")"
  has "the script refreshes every five minutes" "REFRESH_MS = 5 * 60 * 1000" "$(as maja "$HW/wall.js")"
  has "the venues come through the gateway" "Goat Arcade Metelkova" "$(as maja "$HW/api/data/venues?venue_code=eq.LJ1&select=venue_code,venue_name")"
  has "the machine floor does too" '"model_id"' "$(as maja "$HW/api/data/machine_floor?venue_code=eq.LJ1&select=machine_id,model_id,title,out_of_order&limit=3")"
  has_re "so does a leaderboard" '"rank": ?[0-9]' "$(as maja "$HW/api/data/leaderboard?select=model_id,rank,handle,nickname,score,played_at&order=rank&limit=1")"
  expect "a view that isn't granted is refused" 403 "$(code -b "$TMP/maja.jar" "$HW/api/data/daily_plays?limit=1")"
  status_up high-score-wall
fi

echo "league-desk"
if deploy_app league-desk "$GA/league-desk"; then
  LD=$(url league-desk)
  page=$(as lena "$LD/" | text)
  has "lena sees the open tournament" "places left" "$page"
  # a tournament open for registration with room, one of its official entrants, a member who isn't entered, a past tournament
  T=$(pq "select tournament_id from annex_api.tournaments where status='registration' and entries < max_players order by starts_on limit 1")
  E=$(pq "select nickname from annex_api.tournament_entries where tournament_id=$T order by nickname limit 1")
  N=$(pq "select nickname from annex_api.player_profiles p where nickname !~ '[^a-z0-9]' and not exists (select 1 from annex_api.tournament_entries e where e.tournament_id=$T and e.nickname=p.nickname) order by nickname limit 1")
  CT=$(pq "select tournament_id from annex_api.tournaments where status='completed' order by starts_on desc limit 1")
  if [ -n "$E" ]; then
    has "the official entrants are listed" "$E" "$page"
    body=$(as lena -X POST --data-urlencode "nickname=$E" -d tournament_id="$T" "$LD/signups" | text)
    has "an official entrant can't be signed up again" "is already entered in the club system" "$body"
    body=$(as lena -X POST --data-urlencode "nickname=$(tr a-z A-Z <<<"$E")" -d tournament_id="$T" "$LD/signups" | text)
    has "  ...whatever the case" "is already entered in the club system" "$body"
    has "a member's page shows placings" "Tournament placings" "$(as maja "$LD/players/$E" | text)"
  else
    bad "an official entrant of tournament ${T:-?}" "none found in annex_api.tournament_entries"
  fi
  [ -n "$CT" ] && expect "a past tournament's page answers" 200 "$(code -b "$TMP/maja.jar" "$LD/tournaments/$CT")"
  expect "an unknown member can't be signed up" 404 "$(code -b "$TMP/lena.jar" -X POST -d nickname=nosuchgoat99 -d tournament_id="$T" "$LD/signups")"
  if [ -n "$N" ] && [ -n "$T" ]; then
    hdr=$(as lena -D - -o /dev/null -X POST --data-urlencode "nickname=$N" -d tournament_id="$T" "$LD/signups")
    sid=$(grep -i '^location:' <<<"$hdr" | sed -n 's/.*signed=\([0-9]*\).*/\1/p' | head -1)
    expect "lena signs $N up" 303 "$(head -1 <<<"$hdr" | grep -o ' [0-9][0-9]*' | head -1 | tr -d ' ')"
    if [ -n "$sid" ]; then
      on_exit "as lena -o /dev/null -X POST $LD/signups/$sid/withdraw"
      has "  ...and the desk says so" "is signed up at the desk" "$(as lena "$LD/?signed=$sid" | text)"
      expect "  ...twice is refused" 409 "$(code -b "$TMP/lena.jar" -X POST -d nickname="$N" -d tournament_id="$T" "$LD/signups")"
      expect "a withdrawal redirects" 303 "$(code -b "$TMP/lena.jar" -X POST "$LD/signups/$sid/withdraw")"
      expect "  ...and twice is refused" 409 "$(code -b "$TMP/lena.jar" -X POST "$LD/signups/$sid/withdraw")"
    else
      bad "the sign-up's id in the redirect" "$(grep -i '^location:' <<<"$hdr")"
    fi
  else
    bad "a member to sign up and a tournament open for registration" "T='$T' N='$N'"
  fi
  expect "a foreign Origin is refused" 403 "$(code -b "$TMP/lena.jar" -X POST -H 'Origin: https://evil.example' -d nickname=x -d tournament_id="${T:-1}" "$LD/signups")"
  status_up league-desk
fi

echo "party-bookings"
pb_ok=1
if ! out=$("$ANNEX" deploy "$GA/party-bookings" 2>&1); then
  if grep -q BOOKING_HOOK_SECRET <<<"$out"; then
    echo "  skip: party-bookings needs its secret first. A human runs: annex secret set party-bookings BOOKING_HOOK_SECRET"
  else
    bad "deploy party-bookings" "$(tail -3 <<<"$out")"
  fi
  pb_ok=0
else
  ok "deploys party-bookings ($(grep -o 'r[0-9]* is live' <<<"$out" | head -1))"
  expect "no warnings at deploy (party-bookings)" "" "$(grep 'warning:' <<<"$out" | grep -vi "Slack isn't connected" || true)"
fi
if [ "$pb_ok" = 1 ]; then
  PB=$(url party-bookings)
  pb_ids() { as maja "$PB/" | grep -o '/r/[0-9]*"' | tr -dc '0-9\n' | sort -un; }
  has "maja sees the requests" "Birthday-party requests from the website" "$(as maja "$PB/" | text)"
  ids_before=$(pb_ids)
  req_before=$(mail_count maja@goat-arcade.example "Party request:")
  conf_before=$(mail_count maja@goat-arcade.example "Confirmed: party for")
  send=$("$ROOT/scripts/demo-booking.sh" --venue LJ1 2>&1) && sent=1 || sent=0
  if [ "$sent" = 0 ] && grep -q 'BOOKING_HOOK_SECRET is not set' <<<"$send"; then
    echo "  skip: BOOKING_HOOK_SECRET is not set. A human runs: annex secret set party-bookings BOOKING_HOOK_SECRET"
  elif [ "$sent" = 0 ]; then
    bad "scripts/demo-booking.sh sends a request" "$(tail -3 <<<"$send")"
  else
    has "the website request is accepted" "HTTP 200" "$send"
    bid="" end=$((SECONDS + 45))
    while [ $SECONDS -lt $end ]; do
      bid=$(comm -13 <(echo "$ids_before") <(pb_ids) | tail -1)
      [ -n "$bid" ] && break
      sleep 3
    done
    if [ -n "$bid" ]; then
      ok "the request reached the app (#$bid)"
      mail_wait maja@goat-arcade.example "Party request:" $((req_before + 1)) 60 && ok "  ...the manager was emailed" || bad "the manager was emailed" "no new message in 60 s (was $req_before)"
      day=$(python3 -c 'from datetime import date, timedelta; d = date.today() + timedelta(days=14); print(d + timedelta(days=(5 - d.weekday()) % 7))')
      has "its page has the actions" "Confirm" "$(as maja "$PB/r/$bid" | text)"
      expect "confirming redirects" 303 "$(code -b "$TMP/maja.jar" -X POST -d venue=LJ1 -d date="$day" -d time=16:00 -d anyway=1 "$PB/r/$bid/confirm")"
      mail_wait maja@goat-arcade.example "Confirmed: party for" $((conf_before + 1)) 60 && ok "  ...the confirmation summary arrived" || bad "the confirmation summary arrived" "no new message in 60 s (was $conf_before)"
      has "  ...and the request now says confirmed" "Confirmed" "$(as maja "$PB/r/$bid" | text)"
    else
      bad "the request reached the app" "no new request in the list after 45 s"
    fi
  fi
  expect "a cross-site confirm is refused" 403 "$(code -b "$TMP/maja.jar" -X POST -H 'Sec-Fetch-Site: cross-site' -d venue=LJ1 "$PB/r/1/confirm")"
  expect "an unknown request is a 404" 404 "$(code -b "$TMP/maja.jar" "$PB/r/999999999")"
  status_up party-bookings
fi

echo "kanboard (self-hosted, D111)"
if deploy_app kanboard "$GA/kanboard"; then
  KBU=$(url kanboard)
  expect "boris lands on Kanboard without a second login" 200 "$(as boris -o /dev/null -w '%{http_code}' "$KBU/")"
  has "  ...on his dashboard" "Dashboard for Boris" "$(as boris "$KBU/" | text)"
  expect "kaldi-admin is a Kanboard admin" 200 "$(as kaldi-admin -o /dev/null -w '%{http_code}' "$KBU/users")"
  expect "maja is a normal user" 403 "$(as maja -o /dev/null -w '%{http_code}' "$KBU/users")"
  expect "no session: the front door's redirect" 302 "$(code "$KBU/")"
  expect "a forged Remote-User from outside: still the redirect" 302 "$(code -H 'Remote-User: kaldi-admin' "$KBU/")"
  has "a forged Remote-User with maja's session stays maja" "Dashboard for Maja" "$(as maja -H 'Remote-User: kaldi-admin' "$KBU/" | text)"
  expect "Kanboard's login form is gone" 404 "$(as boris -o /dev/null -w '%{http_code}' "$KBU/login")"
  expect "its API is not reachable from outside" 403 "$(as boris -o /dev/null -w '%{http_code}' -X POST -d '{}' "$KBU/jsonrpc.php")"
  expect "a cross-site POST is refused" 403 "$(as boris -o /dev/null -w '%{http_code}' -X POST -H 'Origin: https://evil.example' "$KBU/")"
  out=$("$ANNEX" run kanboard seed 2>&1) && ok "annex run kanboard seed" || bad "run seed" "$(tail -3 <<<"$out")"
  has "  ...it logged its summary" '"msg":"seed finished"' "$out"
  out=$("$ANNEX" run kanboard seed 2>&1) && ok "annex run kanboard seed, again" || bad "run seed again" "$(tail -3 <<<"$out")"
  has "  ...and created nothing" '"projects_created":0,"cards_created":0' "$out"
  has "boris sees the repair boards" "Parts to order" "$(as boris "$KBU/" | text)"
  status_up kanboard
fi
finish
