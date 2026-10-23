# league-desk

Goat Arcade's tournament desk. The next tournament (the Goat Arcade Masters in the data) with its date, venue, places,
the club system's entries (who, by nickname) and the sign-ups taken at the desk; past tournaments with their winners and standings; and a
player lookup by nickname.

Owner `lena` (Lena Gruber, community and tournaments, Graz). Goat Arcade data, all synthetic.

It shows that **the gateway is read-only while an app keeps its own data**: tournaments, standings and players come
from the gateway; sign-ups the club system doesn't have yet are the app's own rows in its Postgres.

- **`/`** the desk. The upcoming tournament(s) (status `registration`): date ("in N days", counted from the day after
  the data ends), venue, format, fee, places, entries (club system), pending desk sign-ups and places left counting
  both. A sign-up form, the pending list with a Withdraw button each, and past tournaments with their winners.
- **`/tournaments/<id>`** a completed tournament's standings: top 8, `?all=1` for everyone.
- **`/players?q=`** nickname search (first 25 by rating); **`/players/<nickname>`** rating, home venue, games in the
  last 180 days, leaderboard spots (top 10 shown, the rest folded) and past tournament placings, with a sign-up button.
- **Sign-ups** (`POST /signups`, `POST /signups/<id>/withdraw`; both pass the CSRF check) are stored in `signups`
  (nickname, tournament, `signed_up_by` = the `Remote-User` login, `signed_up_at`; a withdrawal keeps the row with
  `withdrawn_at` and `withdrawn_by`). A sign-up is refused when the nickname isn't a member's (`player_profiles`), when
  it's already among the club system's entries (`tournament_entries`, read fresh, 409), it's already pending for that tournament (also enforced by a unique index), when the tournament isn't open, or when
  no places are left (checked under a per-tournament lock). Each sign-up posts one line to Slack `#ga-community`; a
  failed post is logged as `slack post failed` (warn) and the sign-up stands.
- **Views:** `tournaments`, `tournament_entries`, `tournament_standings`, `player_profiles`, `leaderboard`, `venues` (home venue names).
  Reads are cached for a minute (not the sign-up check's entry read) (standings and venues for ten).

Tests for the pure part: `uv run --python 3.13 --with pytest pytest tests -q` in this folder.

## How it was built
Lena's request, as typed into Claude Code:

> Build me a tournament desk on Annex, owner lena. Show the past tournaments with who won, and let me open one to see
> the standings, top 8 is enough unless I ask for all. Up top I want the Masters: when, where, how many places and how
> many have entered so far. People keep signing up at the counter and the club system only catches up later, so let
> staff sign a player up by nickname right there: check the nickname is a real member, don't let anyone sign up twice,
> remember who signed them up and when, and let us withdraw a sign-up. Show those next to the official entries and
> count both in the places left. Post a line to #ga-community for every sign-up. And a quick player lookup: rating,
> home venue, how much they play, their high-score spots and how they did in past tournaments.

**Review pass** (after the first deploy):
- "10 days after the latest data" read oddly at a desk; the countdown is now "in N days", counted from the day after
  the data ends (the seed day), so it matches the calendar after a reseed.
- The `?signed=` / `?withdrawn=` notices only show when the row really is in that state, and the sign-up notice says
  when the Slack line couldn't be posted (there's no Slack on the local lane).
- Pending sign-up times are shown in Ljubljana time (the database stores UTC).
- A player with 40 leaderboard spots made a very long page: the top 10 show, the rest fold away.
