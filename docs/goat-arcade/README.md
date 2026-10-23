# Goat Arcade: the demo client

Goat Arcade d.o.o. is a made-up client: eight pinball and retro arcades around Ljubljana, Maribor, Celje, Koper, Zagreb, Graz and Trieste. They have about 145 machines, 2,500 card-carrying members, technicians, weekly cash counts and monthly tournaments.

Their **production database is their own system**. Annex doesn't own it. Annex reads it through the **data gateway**, as a set of curated, read-only views without personal data. That's the setup a real client would have, which makes this the place to try vibe-coding an internal tool on a client's data. **Everything in it is synthetic.**

## For the builder
You build a small internal tool (a web page, a dashboard or a scheduled job) with your coding agent, and deploy it to Annex. It runs behind Annex's login and reads Goat Arcade's data through the gateway.

1. **Get access.** Nejc gives you an Annex account (`annex users add`) and the `annex` CLI or MCP server on your machine ([setup](../setup.md), section 4). The CLI reaches the server over SSH, so for now that means Nejc's SSH setup, or building from his machine.
2. **Give your agent [agent-brief.md](agent-brief.md).** It explains the client, how an app reads the data, the query syntax, which view answers what, and some ideas. The agent also gets the Annex contract from `annex contract`.
3. **Columns** are in [data-dictionary.md](data-dictionary.md). A deployed app can also ask the gateway itself (`GET $ANNEX_GATEWAY_URL/`).
4. **Build, deploy, look.** `annex deploy <folder>`, then open the URL it prints. `annex status` and `annex logs` show what happened.

Something to start with: "a high-score wall for the Metelkova arcade", "a technician's morning list of broken and failing machines, emailed at 8", "which tables earn their floor space?". The data has stories hidden in it (the brief hints at them).

## For the platform team
| What | Where |
|---|---|
| The client's database | `democlient/`: compose project `democlient` on the VM, `arcade-db`; schema and seed in `initdb/` |
| Run it | `scripts/democlient.sh up`, then `link`. Also `status`, `check` (the planted stories), `reseed` (data ends yesterday again), `psql`, `down` |
| The link | `db/product/clientlink/link.sql`: postgres_fdw from product-db, the client's read-only login `annex_reader` |
| The views (the allowlist) | `db/product/clientlink/api_views_arcade.sql`, with a comment on every column; after a change, `scripts/push.sh`, then `scripts/democlient.sh dictionary` |
| The people (Goat Arcade's staff) | [plan](../plans/goat-arcade-demo.md) 2.1; accounts: `annex users add ... --demo` ([setup](../setup.md) section 10, [operations](../operations.md) Accounts); Metabase groups: `config/metabase/people.json`; tiles by team: `config/teams.json` (D107) |
| Tests | `scripts/test-platform.sh` ("Demo client database"); one check in `scripts/test-smoke.sh` |
| Why it's built this way | [decisions](../decisions.md) D77 (the link), D78 (the data); [plan](../plans/democlient.md) |
| A real client's side of it | [connect-your-database.md](connect-your-database.md) |

**Before handing it over:** `scripts/democlient.sh reseed` makes the data current: it ends yesterday. Then `scripts/democlient.sh check` should show seven `t` rows.
