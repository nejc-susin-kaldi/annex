# Demo client database plan: Goat Arcade (pinball)

For build agents, one step per session. Written 21 Oct 2026 against commit `20252bb` (M2 and the console overhaul done). **Done (DC.1–DC.5, 21 Oct); DC.6 not done.** What was built differs from this plan in a few places, which D77 and D78 in `docs/decisions.md` record:
- the company is **Goat Arcade** (Nejc's choice);
- `pinball_models` became **`machine_models`**, because it holds video and other machines too;
- `games` and `coin_collections` carry the venue where the machine stood;
- the actual scale: 61 models, 151 cabinets, about 245k games, 86k events, 670 tickets;
- the handoff is a folder, `docs/goat-arcade/`: a README, an agent brief, the generated data dictionary and the client-side recipe.

Nejc approved the shape on 21 Oct:
- the client database is a **separate compose project on the VM** (option A), not a container on `dronky`;
- the domain is an **arcade chain with an emphasis on pinball**;
- a **second Postgres** (the client's database) and the **`postgres_fdw`** extension (already in the official image) are approved as additions to the brief's stack.

**Read first:** `CLAUDE.md` (ground rules; it overrides everything), `docs/brief.md` (the "Data gateway v0" section), `docs/decisions.md` (D10 and D14 on the gateway and the existing product data, D75 on the `annex-selftest` fixture), and this whole file. Then do **only the step you were given**, and stop and report to Nejc when its "Done when" holds.

## Why
The existing product database (wealth management, D14) is generic and lives inside the platform. This one is the demo of the real engagement: **a client's production database that already exists, outside Annex**, which Annex connects to through the gateway. A colleague then vibe-codes apps on it without ever seeing a credential or a raw table. The domain should be fun enough that people want to build things on it.

## The setup

```
                       VM (one Docker engine)
 ┌──────────────── project "annex" ────────────────┐      ┌── project "democlient" ──┐
 │ apps ─▶ gateway (PostgREST) ─▶ product-db        │      │  arcade-db (Postgres 16) │
 │                     annex_api views              │      │  schema "arcade"         │
 │                       └─▶ src_arcade (foreign) ─┼──────┼─▶ role annex_reader (RO)  │
 │ metabase ─▶ product-db                           │ annex-clientlink (internal)     │
 └──────────────────────────────────────────────────┘      └───────────────────────────┘
```

- **The client's side** (`democlient/` in the repo, `/srv/annex/democlient` on the VM): compose project `democlient`, one service `arcade-db`, image `postgres:16.15-alpine3.24` (the image product-db already uses). It deliberately has **no `com.kaldi.annex` label**: Annex doesn't manage, back up, monitor or ship logs from it. A client's database is the client's business.
- **What the client gives Annex:** one login, `annex_reader`, with `CONNECT` and `SELECT` on schema `arcade` and nothing else. Annex never creates anything in the client's database.
- **Annex's side:** product-db gets `postgres_fdw`, a foreign server `goat_arcade` and a user mapping **for the view owner (`postgres`) only**. `IMPORT FOREIGN SCHEMA arcade FROM SERVER goat_arcade INTO src_arcade`. `src_arcade` has no grants for anyone. The curated views in `annex_api` read from `src_arcade`, so per-app roles, JWTs, request logging, `data.views` validation and Metabase all work unchanged. **The app contract doesn't change.**
- **The link:** network `annex-clientlink`, `internal: true`, defined in Annex's `compose.yaml` (Annex owns the connection, so it carries the label) and used as an `external` network by `democlient/compose.yaml`. Only product-db and arcade-db are on it. arcade-db is on no other network: no egress, and no app, Metabase or gateway route to it.
- **Lifecycle:** the client database isn't started by `push.sh`. `scripts/democlient.sh` runs it. If it's down, the platform still works and only the Goat Arcade views fail (with a clear Postgres error through the gateway).

### Views run as their owner (important)
The `annex_api` views must stay **`security_invoker = false`** (the default), so foreign-table access is checked as the view owner (`postgres`), which has the only user mapping. App roles get `SELECT` on the view and nothing on `src_arcade`, and need no user mapping. A test proves this (step DC.4).

## The domain: Goat Arcade d.o.o.
A fictional chain of retro arcades and pinball bars (an arcade is where the ball drains; the name is a placeholder, Nejc can change it). **Everything is fictional:** venue names, machine titles, manufacturers, designers and players. Real cities are fine, but no real arcade, manufacturer, title or person.

**Scale** (about 60–80 MB on disk; product-db + arcade-db stay well under 300 MiB):
- 8 venues: Ljubljana (2), Maribor, Celje, Koper, Zagreb, Graz, Trieste;
- ~70 machine models, ~150 cabinets, ~80% pinball:
  - eras: electro-mechanical, early solid state, DMD and modern LCD;
  - the rest: video cabinets, a few oddities (air hockey, a claw machine);
- ~2,500 players, ~250k games over the last 180 days;
- ~150k machine events, ~900 service tickets, 12 tournaments.

### Raw tables (schema `arcade`, written like a real, slightly messy production schema)
| Table | Contents |
|---|---|
| `venues` | name, city, country, lat/lon, timezone, opened_on, `closed_on` (one closed venue), monthly_rent_eur, opening hours as JSONB |
| `manufacturers` | fictional makers, founded year, country |
| `machine_models` | title, manufacturer, year, `era` (text, not an enum), theme, designer, flipper count, max multiball, `features` JSONB (ramps, toys, modes), `type` (pinball, video, other) |
| `machines` | the cabinets: serial, model, current venue, installed_at, `condition` (text with inconsistent casing), price_per_play_cents, `cab_no` (legacy, mostly NULL), `is_deleted` (soft delete) |
| `machine_moves` | history of cabinets moving between venues and storage |
| `players` | handle (3 initials, as on a high-score table), nickname, **full_name, email, phone, birth_date** (PII), home venue, member_since, rating |
| `games` | machine, player (NULL for anonymous play, about 60%), started_at, duration_s, score (bigint), balls_played, extra_balls, multiballs, `tilted`, credits_used |
| `machine_events` | machine, at, type: tilt, slam_tilt, ball_search, coil_fault, switch_fault, jackpot, replay, match |
| `technicians` | staff: name, **email, phone, hourly_rate** (PII and pay) |
| `service_tickets` | machine, opened_at, closed_at, symptom (free text: "left flipper weak", "ball stuck in VUK"), resolution, technician, parts_cost_eur, `status` history as JSONB |
| `parts`, `ticket_parts` | rubbers, coils, flipper rebuild kits, displays |
| `coin_collections` | per machine per week: coins, tokens, card plays, `amount_eur` (the money) |
| `tournaments`, `tournament_entries`, `tournament_matches` | match-play rounds, groups of four, points per game |

### Planted stories (so demos have something to find)
The seed must make these visible in the views, and step DC.1 checks each one:
1. **A dying machine.** One table in Maribor has coil faults that get more frequent over six weeks, then its plays drop. It still has an open ticket.
2. **The champion.** One player handle holds the top score on more than ten models across three venues.
3. **The venue that lost its stars.** Two popular tables moved out of Celje and its weekly revenue dropped.
4. **Rhythm:** evenings and weekends are busy, and Monday mornings are dead. One venue has a Thursday league night.
5. **Era taste:** modern LCD tables earn the most per game, but some electro-mechanical classics get the longest sessions.
6. **A tilt-happy cabinet,** whose tilt rate is three times the model's average (its tilt is set too sensitive).
7. **An upcoming tournament** (dates after the seed's anchor date), with entries but no matches yet.

### Dates and determinism
The seed uses `setseed`, so the shape and every story are identical on each run. **Dates are relative to an anchor**, by default the seed day: data ends "yesterday". `scripts/democlient.sh reseed` before a demo makes the data current. This trades D14's fixed dates for freshness, because "last 7 days" apps are the most likely thing to get vibe-coded.

### Curated views (`annex_api`, the allowlist)
No prefix: the names don't clash with the four wealth views. Every view and every column gets a `COMMENT`, which PostgREST shows in its OpenAPI output. That's the data dictionary a colleague's coding agent reads.

| View | Contents | Leaves out |
|---|---|---|
| `venues` | open venues, city, lat/lon, opening hours, machine count | rent |
| `pinball_models` | catalog: title, maker, year, era, theme, designer, features | — |
| `machine_floor` | each cabinet now: venue, model, era, condition (normalised), price per play, plays in the last 7 days, last service, open ticket yes/no | `cab_no`, soft-deleted rows |
| `leaderboard` | top 10 scores per model, all time, with venue and date | anything but the handle |
| `player_profiles` | handle, nickname, home venue, member since, games played, best rank, rating | full name, email, phone, birth date |
| `daily_plays` | per day per machine: games, credits, average duration, tilts | — |
| `machine_events_daily` | per day per machine per event type: counts | — |
| `service_log` | tickets: machine, venue, symptom, resolution, opened/closed, downtime hours, parts cost | technician contact and pay (name only) |
| `revenue_weekly` | per venue per week (and per machine): `amount_eur` | — |
| `tournaments` | tournaments, venue, dates, status, entry count | — |
| `tournament_standings` | per tournament: handle, points, position | — |

`GRANT SELECT` to `metabase_ro`, as `api_views.sql` does. Per-app grants are made by `annex_vm.py` on deploy, unchanged.

**Things a vibe-coded app could do with these:** a high-score wall per venue, a machine-of-the-week email, a technician dashboard ranking machines by trouble, a Slack nudge when a cabinet's tilt rate jumps, a venue map, a tournament bracket viewer, a "find me a space-themed DMD table near Graz" search, a revenue dashboard per era.

## Ground rules that bite here
- **Synthetic only.** Every name is invented. No scraping real machine databases (IPDB, OPDB or similar) for titles or rules.
- **Writes on the VM:** Docker and `/srv/annex` only: `/srv/annex/democlient` (compose and SQL), `/srv/annex/state/democlient-db` (data), `/srv/annex/secrets/democlient.env`.
- **Pinned images**; the client DB gets a memory limit (`192m`, `shared_buffers=64MB`) and a healthcheck, like every container here.
- **Tests:** `scripts/push.sh` then `scripts/test-smoke.sh` after each change; run `scripts/test-platform.sh` for the gateway part. Ask Nejc before `test-all.sh`.
- **Don't run** `scripts/wipe-vm.sh` or `scripts/reset-demo.sh`; don't touch the app `vibe-leads`.

## Order of work
| Step | Delivers | Depends on |
|---|---|---|
| DC.1 | Schema, seed, `annex_reader` role; the planted stories checked | — |
| DC.2 | The client container, the link network, secrets, `scripts/democlient.sh` | DC.1 |
| DC.3 | FDW link in product-db, the Goat Arcade views with comments | DC.2 |
| DC.4 | Tests: gateway reads, allowlist, isolation, smoke check | DC.3 |
| DC.5 | Docs: decisions, setup and teardown, the colleague handoff | DC.4 |
| DC.6 (optional) | A Metabase "Goat Arcade" collection with a few questions | DC.3 |

### DC.1 Schema and seed
- `democlient/initdb/00-roles.sh`: `annex_reader` with its password from the env file; `REVOKE CONNECT ... FROM PUBLIC`.
- `democlient/initdb/01-schema.sql`: the `arcade` tables with their production mess.
- `democlient/initdb/02-seed.sql`: deterministic (`setseed`), anchored dates, the seven stories.
- `democlient/initdb/03-grants.sql`: `annex_reader` gets `USAGE` on `arcade` and `SELECT` on its tables, including future ones (`ALTER DEFAULT PRIVILEGES`).
- `democlient/checks.sql`: one query per story, each returning a single `ok` boolean.

**Done when:** the seed runs in a one-off `docker run --rm` Postgres on the VM (under `/srv/annex`) in under two minutes. Row counts match the scale above (±10%). Every row in `checks.sql` returns true. Two runs with the same anchor give identical `md5` checksums of `games` and `machine_events`.

### DC.2 The client container
- `democlient/compose.yaml`: project name `democlient`; service `arcade-db` with the pinned image, `mem_limit: 192m`, a healthcheck, its data dir and initdb mounted read-only, and only the external network `annex-clientlink`.
- `compose.yaml`: add the `clientlink` network (`name: annex-clientlink`, internal, labelled) and attach product-db to it.
- `vm/bootstrap-secrets.sh`: generate `democlient.env` (`POSTGRES_PASSWORD`, `ANNEX_READER_PASSWORD`) if it's missing, like the other secrets.
- `scripts/democlient.sh`: `up | down | reseed [--anchor YYYY-MM-DD] | status | psql`. It syncs `democlient/` to the VM; `reseed` drops the state dir after confirming and starts again; `status` prints row counts and the anchor date. `down` keeps the data.

**Done when:**
- `democlient.sh up` brings `arcade-db` up healthy;
- `status` shows the counts;
- `docker network inspect annex-clientlink` lists exactly product-db and arcade-db;
- `annex_reader` can `SELECT` from `arcade.games` but can't `INSERT` or create a table;
- `push.sh` and `test-smoke.sh` pass, with and without the client database running.

### DC.3 The FDW link and the views
- `db/product/clientlink/link.sql` (idempotent):
  - `CREATE EXTENSION IF NOT EXISTS postgres_fdw`;
  - the server `goat_arcade` (`host 'arcade-db'`, `dbname 'arcade'`, `fetch_size '10000'`, `use_remote_estimate 'true'`);
  - the user mapping for `postgres`;
  - `src_arcade` recreated by `IMPORT FOREIGN SCHEMA`, with `REVOKE ALL` from `PUBLIC`.
- The password for the user mapping isn't in the repo: `democlient.sh link` reads it from `secrets/democlient.env` on the VM and passes it as a psql variable, never printing it.
- `db/product/clientlink/api_views_arcade.sql`: the eleven views and their comments, plus the `metabase_ro` grant and `NOTIFY pgrst, 'reload schema'`.
- Who applies them: `democlient.sh link` runs `clientlink.sql` and then the views. `push.sh` re-applies the views file only if `src_arcade` exists (one guarded `psql` call), so a push never fails because the client database is down.

**Done when:**
- `annex_vm.py`'s view list includes the new views;
- every view returns rows through a test JWT in under a second (time each one; if one is slow, rewrite it so the aggregate pushes down, or tell Nejc);
- the stories from DC.1 are visible through the views;
- an app role gets "permission denied" on `src_arcade.games`;
- the gateway's OpenAPI output shows the column comments.

### DC.4 Tests
- `scripts/selftest.py`: add `--views a,b` (the fixture's `data.views`) next to `--no-views`.
- `scripts/test-platform.sh`, a new "client database" block:
  1. a selftest app with `--views leaderboard,machine_floor` reads both;
  2. it gets 401/403 on `player_profiles`, which it didn't request;
  3. its role can't reach `src_arcade` (from inside product-db as that role);
  4. every app network can't resolve or reach `arcade-db`, the same `probe` as for product-db;
  5. `player_profiles` has no `email`, `phone`, `full_name` or `birth_date` column (from OpenAPI);
  6. `annex-clientlink` has exactly two members.

  Skip the block with a clear message if `arcade-db` isn't running.
- `scripts/test-smoke.sh`: one check, that `machine_floor` returns rows through the gateway (skipped with a note if the client database is down).

**Done when:** `test-platform.sh` and `test-smoke.sh` pass; the suite removes its app at the end.

### DC.5 Docs
- `docs/decisions.md`:
  - **D77** the external client database: option A over B, FDW over a second PostgREST, why views run as their owner, why it's unlabelled and not backed up;
  - **D78** the Goat Arcade data: scale, anchored dates (versus D14's fixed ones), the stories, the PII left out of the views.
- `docs/setup.md` and `docs/teardown.md`: `democlient.sh up` / `link`, and the undo (`democlient.sh down`, drop `src_arcade` and the server, remove the state dir).
- `CLAUDE.md`: one layout line for `democlient/`.
- `docs/demo.md`: an optional "client database" beat.
- **`docs/goat-arcade/`, for the colleague:**
  - the story of Goat Arcade in a paragraph;
  - the views and what they hold;
  - five app ideas;
  - how to request views in `annex.yaml`;
  - a reminder that the data is fictional and current up to yesterday.

  Written for a person and their coding agent, without platform internals.

**Done when:** Nejc has read the handoff doc.

### DC.6 Metabase (optional)
An "Goat Arcade" collection with four questions: plays per venue per week, revenue by era, the trouble ranking, and the leaderboard. Set it up through the existing Metabase setup script, if it supports collections, or skip it.

## Decided by Nejc (21 Oct)
- The company name: **Goat Arcade**.
- View names have no prefix.
- A reseed moves the dates so the data ends yesterday.
