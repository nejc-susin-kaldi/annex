# Goat Arcade: the demo client

Goat Arcade d.o.o. is a made-up client: eight pinball and retro arcades around Ljubljana, Maribor, Celje, Koper, Zagreb, Graz and Trieste. They have about 145 machines, 2,500 card-carrying members, technicians, weekly cash counts and monthly tournaments.

Their **production database is their own system**. Annex doesn't own it. Annex reads it through the **data gateway**, as a set of curated, read-only views without personal data. That's the setup a real client would have, which makes this the place to try vibe-coding an internal tool on a client's data. **Everything in it is synthetic.**

**Visiting?** [tour.md](tour.md) is a self-guided tour of this server: who's who, what to open in which order, what each part shows, and hints at the stories in the data.

## For the builder
You build a small internal tool (a web page, a dashboard or a scheduled job) with your coding agent, and deploy it to Annex. It runs behind Annex's login and reads Goat Arcade's data through the gateway.

1. **Get access.** Nejc gives you an Annex account (`annex users add`) and the `annex` CLI or MCP server on your machine ([setup](../setup.md), section 4). The CLI reaches the server over SSH, so for now that means Nejc's SSH setup, or building from his machine.
2. **Give your agent [agent-brief.md](agent-brief.md).** It explains the client, how an app reads the data, the query syntax, which view answers what, and some ideas. The agent also gets the Annex contract from `annex contract`.
3. **Columns** are in [data-dictionary.md](data-dictionary.md). A deployed app can also ask the gateway itself (`GET $ANNEX_GATEWAY_URL/`).
4. **Build, deploy, look.** `annex deploy <folder>`, then open the URL it prints. `annex status` and `annex logs` show what happened.

Something to start with: "a high-score wall for the Metelkova arcade", "a technician's morning list of broken and failing machines, emailed at 8", "which tables earn their floor space?". The data has stories hidden in it (the brief hints at them).

## Dashboards
Goat Arcade's staff also have **Metabase** ("Goat Arcade analytics", `metabase.` host, its own sign-in with their Annex password). In the "Goat Arcade" collection, one dashboard per team:

| Dashboard | Team collection | Shows |
|---|---|---|
| Goat Arcade at a glance | Management | last week's cash and games against the week before, active members, cash per venue over 26 weeks, revenue by era, the top 10 machines, a map of the venues |
| Venue performance | Venues | this week against last, games per day, a weekday-by-hour heatmap, machine earnings, idle machines; a **venue** filter on every card |
| Machine health | Technical | out of order now, open tickets by age, coil and switch faults, downtime by venue, time to repair by technician, tickets by symptom, parts cost |
| Cash and collections | Finance | weekly cash by venue, coins, tokens and card, takings per machine by era, counted cash against estimated plays |
| Players and tournaments | Community | sign-ups, active members per venue, most leaderboard spots, tournament entries, the last standings |

Everyone can open all of them and explore the views with the query builder (the keys between views are set, so it joins them). Each team edits its own collection; only analysts (`sam`) and admins write SQL. Periods count back from the latest day in the data, not today. The dashboards are kept as code (`platform/config/metabase/goat-arcade/`, D109): changes made in Metabase to them are overwritten at the next push, so save your own questions next to them. How to change one: [operations](../operations.md), "Metabase".

## For the platform team
| What | Where |
|---|---|
| The client's database | `demo/client-db/`: compose project `democlient` on the VM, `arcade-db`; schema and seed in `initdb/` |
| Run it | `scripts/push.sh` brings it up and links it (D112); by hand `scripts/democlient.sh up`, then `link`. Also `status`, `check` (the planted stories), `reseed` (data ends yesterday again), `psql`, `down` |
| The link | `platform/db/product/clientlink/link.sql`: postgres_fdw from product-db, the client's read-only login `annex_reader` |
| The views (the allowlist) | `platform/db/product/clientlink/api_views_arcade.sql`, with a comment on every column; after a change, `scripts/push.sh`, then `scripts/democlient.sh dictionary` |
| The people (Goat Arcade's staff) | [plan](../plans/goat-arcade-demo.md) 2.1; accounts: `annex users add ... --demo` ([setup](../setup.md) section 9, [operations](../operations.md) Accounts); Metabase groups: `platform/config/metabase/people.json`; tiles by team: `platform/config/teams.json` (D107) |
| Metabase: connection, permissions, dashboards | `platform/config/metabase/goat-arcade/` (`model.json`, `collections.json`, `dashboards/`), applied by `platform/vm/metabase-setup.sh` on every push (D109) |
| Tests | `tests/platform.sh` ("Demo client database"); one check in `tests/smoke.sh` |
| Why it's built this way | [decisions](../decisions.md) D77 (the link), D78 (the data); [plan](../plans/democlient.md) |
| A real client's side of it | [connect-your-database.md](connect-your-database.md) |

**Before handing it over:** `scripts/democlient.sh reseed` makes the data current: it ends yesterday. Then `scripts/democlient.sh check` should show seven `t` rows.
