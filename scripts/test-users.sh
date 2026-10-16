#!/usr/bin/env bash
# Annex's own user accounts (D57): `annex users` add (with an emailed invitation), set, disable/enable, reset and
# remove; self-service password reset and change through Authelia (emails caught by Mailpit); the guards that
# keep an admin; and the user directory annex-ops reads. Uses a throwaway login and removes it. About 1 minute.
source "$(dirname "$0")/lib.sh"
source "$(dirname "$0")/testlib.sh"
use_tunnel
ANNEX="$ROOT/annex"
MAIL_URL=$(setting ANNEX_URL_MAIL)
U="tst-$RANDOM"
EMAIL="$U@annex.example"
PW1="Correct-Horse-$RANDOM-battery"
PW2="Another-Horse-$RANDOM-staple"
on_exit "\"$ANNEX\" users remove $U --yes >/dev/null 2>&1"
login kaldi-admin >/dev/null
# Authelia reloads users.yml a moment after it changes: wait for a login to give the expected answer
login_becomes() { # login_becomes CODE USER PASSWORD
  local end=$((SECONDS + 20)) got
  while :; do got=$(login "$2" "$3"); [ "$got" = "$1" ] || [ $SECONDS -ge $end ] && break; sleep 2; done
  echo "$got"
}
directory() { vm "docker exec annex-ops-1 python -m annex_ops.ctl users --json"; }
in_list() { "$ANNEX" --json users list | python3 -c 'import json,sys; print(any(u["login"] == sys.argv[1] for u in json.load(sys.stdin)))' "$1"; }
link_token() { # link_token SUBJECT_PREFIX -> the token in the newest such email to $EMAIL
  mail_find "$EMAIL" "$1" | tr -d '\r' | grep -o 'step2?token=[^ ]*' | head -1 | sed 's/step2?token=//'
}
set_password_from_link() { # set_password_from_link SUBJECT_PREFIX PASSWORD: what the reset page does
  local token; token=$(link_token "$1")
  [ -n "$token" ] || { echo "no link"; return; }
  c -c "$TMP/reset.jar" -o /dev/null -H 'Content-Type: application/json' -d "{\"token\":\"$token\"}" \
    "$AUTH_URL/api/reset-password/identity/finish"
  c -b "$TMP/reset.jar" -o /dev/null -w '%{http_code}' -H 'Content-Type: application/json' \
    -d "{\"password\":\"$2\"}" "$AUTH_URL/api/reset-password"
}

echo "Add, with an invitation"
out=$("$ANNEX" users add "$U" --name "Test User" --email "$EMAIL" --groups users 2>&1) \
  && ok "annex users add $U" || bad "annex users add" "$out"
has "the output says an invitation went out" "invitation sent to $EMAIL" "$out"
hasnt "and prints no password" "shown once" "$out"
mail_wait "$EMAIL" "[Annex] Reset your password" 1 30 && ok "the invitation email arrived" || bad "invitation email" "none"
expect "following its link sets a password" 200 "$(set_password_from_link "[Annex] Reset your password" "$PW1")"
expect "$U signs in with it" 200 "$(login "$U" "$PW1")"
expect "  ...and reaches the discovery page" 200 "$(code -b "$TMP/$U.jar" "$HOME_URL/")"
expect "  ...but not the admins-only Mailpit" 403 "$(code -b "$TMP/$U.jar" "$MAIL_URL/")"
has "annex-ops's user directory knows them (alerts, logins as recipients)" "\"$U\"" "$(directory)"
expect "annex users list shows them" True "$(in_list "$U")"
has "a duplicate login is refused" "already exists" "$("$ANNEX" users add "$U" --name X --email "x$EMAIL" 2>&1)"
has "a bad login is refused, saying how to fix it" "lowercase" "$("$ANNEX" users add Bad --name X --email x@annex.example 2>&1)"
has "a bad email is refused" "is not an address" "$("$ANNEX" users add tst-bad --name X --email nope 2>&1)"
expect "the users file is still the annex user's, mode 600" "annex 600" "$(vm "stat -c '%U %a' $DATA/secrets/authelia/users.yml")"

echo "Self-service password change (Authelia's settings page: a code by email, then the new password)"
c -b "$TMP/$U.jar" -c "$TMP/$U.jar" -o /dev/null -X POST "$AUTH_URL/api/user/session/elevation"
mail_wait "$EMAIL" "[Annex] Confirm your identity" 1 30 && ok "a one-time code arrived by email" || bad "one-time code" "none"
otc=$(mail_find "$EMAIL" "[Annex] Confirm your identity" | tr -d '\r' | grep -E '^\s*[A-Z0-9]{8,}\s*$' | head -1 | tr -d ' ')
c -b "$TMP/$U.jar" -c "$TMP/$U.jar" -o /dev/null -X PUT -H 'Content-Type: application/json' -d "{\"otc\":\"$otc\"}" \
  "$AUTH_URL/api/user/session/elevation"
change=$(python3 -c 'import json,sys; print(json.dumps({"username": sys.argv[1], "old_password": sys.argv[2], "new_password": sys.argv[3]}))' "$U" "$PW1" "$PW2")
expect "with it, they change their password" 200 "$(code -b "$TMP/$U.jar" -H 'Content-Type: application/json' -d "$change" "$AUTH_URL/api/change-password")"
expect "the new password works" 200 "$(login "$U" "$PW2")"
expect "the old one doesn't" 401 "$(login "$U" "$PW1")"
has "annex users still reads the file Authelia rewrote" "Test User" "$("$ANNEX" users list)"

echo "Groups, disable, enable"
"$ANNEX" users set "$U" --groups users,admins >/dev/null && ok "made an admin"
login "$U" "$PW2" >/dev/null
end=$((SECONDS + 20)); until [ "$(code -b "$TMP/$U.jar" "$MAIL_URL/")" = 200 ] || [ $SECONDS -ge $end ]; do sleep 2; login "$U" "$PW2" >/dev/null; done
expect "  ...after signing in again, Mailpit opens" 200 "$(code -b "$TMP/$U.jar" "$MAIL_URL/")"
"$ANNEX" users set "$U" --groups users >/dev/null
"$ANNEX" users set "$U" --disable >/dev/null && ok "disabled"
expect "  ...a disabled user can't sign in" 401 "$(login_becomes 401 "$U" "$PW2")"
hasnt "  ...and isn't in the directory (no alerts or emails to them)" "\"$U\"" "$(directory)"
"$ANNEX" users set "$U" --enable >/dev/null && ok "enabled again"
expect "  ...signs in again with the same password" 200 "$(login_becomes 200 "$U" "$PW2")"

echo "Admin reset"
out=$("$ANNEX" --json users reset "$U" --print-password)
PW3=$(python3 -c 'import json,sys; print(json.load(sys.stdin)["password"])' <<<"$out")
[ ${#PW3} -ge 20 ] && ok "annex users reset --print-password gives a new random password" || bad "reset --print-password" "$out"
expect "  ...which works" 200 "$(login_becomes 200 "$U" "$PW3")"
expect "  ...and the previous one doesn't" 401 "$(login "$U" "$PW2")"
"$ANNEX" users reset "$U" >/dev/null && ok "annex users reset emails a link instead"
mail_wait "$EMAIL" "[Annex] Reset your password" 2 30 && ok "  ...the reset email arrived" || bad "reset email" "none"

echo "An admin always remains"
only_admin=$("$ANNEX" --json users list | python3 -c 'import json,sys; a=[u["login"] for u in json.load(sys.stdin) if "admins" in u["groups"] and not u["disabled"]]; print(a[0] if len(a) == 1 else "")')
if [ -n "$only_admin" ]; then
  has "the last admin can't lose the admins group" "no active admin" "$("$ANNEX" users set "$only_admin" --groups builders 2>&1)"
  has "  ...or be disabled" "no active admin" "$("$ANNEX" users set "$only_admin" --disable 2>&1)"
  has "  ...or removed" "no active admin" "$("$ANNEX" users remove "$only_admin" --yes 2>&1)"
  expect "  ...and still signs in" 200 "$(login "$only_admin")"
else
  echo "  (skipped: there is more than one admin)"
fi

echo "Remove"
"$ANNEX" users remove "$U" --yes >/dev/null && ok "annex users remove $U"
expect "  ...they can't sign in" 401 "$(login_becomes 401 "$U" "$PW3")"
expect "  ...they're gone from the list" False "$(in_list "$U")"
hasnt "  ...and from the directory" "\"$U\"" "$(directory)"
finish
