# Goat Arcade on Annex: brief for a coding agent

Give this file to the coding agent (Claude Code, Codex, Cursor...) that builds an app on Goat Arcade's data. It is self-contained. The column-by-column reference is [data-dictionary.md](data-dictionary.md), and the gateway serves the same descriptions itself (below). **All data is synthetic.** Every venue, machine, title, maker and player is invented.

## The client
**Goat Arcade d.o.o.** runs eight retro arcades and pinball bars in Ljubljana (2), Maribor, Celje, Koper, Zagreb, Graz and Trieste. A ninth, in Nova Gorica, closed four months ago.

The floors hold about 145 machines:
- about 80% pinball, from 1960s electro-mechanical tables to modern LCD games;
- video cabinets;
- a few other games: air hockey, a claw machine and roll-a-ball lanes.

Members tap a card when they play, so their games carry a nickname; the rest are anonymous. Technicians keep the machines alive. Staff count the cash every week. Once a month or so there's a tournament.

Their production database records all of it. Annex reads it through the **data gateway**: a set of curated, read-only views. Personal data (names, emails, phones, birth dates, staff pay) never leaves the client's database.

## How your app gets the data
1. **Start the Annex way.** Read the app contract first (`annex contract`, or the MCP tool `annex_contract`). Then start from a template, for example `annex init python-web <name> --owner <login>`, and deploy with `annex deploy <folder>`. Never write login code, database credentials or Docker labels.
2. **Declare the views you read** in `annex.yaml`:
   ```yaml
   data:
     views: [machine_floor, leaderboard, service_log]
   ```
   The deploy checks them against the allowlist; a misspelt view fails with the list of valid names. A view you didn't declare answers 401 or 403.
3. **Read them from your server, never from the browser.** Annex injects `ANNEX_GATEWAY_URL` and `ANNEX_GATEWAY_TOKEN`; the token must never reach the browser. The templates have a helper:
   ```python
   from annex_runtime import gateway, GatewayError
   broken = gateway("machine_floor", out_of_order="is.true", order="venue_code")
   top = gateway("leaderboard", title="eq.Haunted Lighthouse", order="rank")
   ```
   ```js
   import { gateway, GatewayError } from "./annex_runtime.js";
   const week = await gateway("revenue_weekly", { order: "week_start.desc,amount_eur.desc", limit: 16 });
   ```
   With any other stack, call `GET $ANNEX_GATEWAY_URL/<view>?<filters>` with the header `Authorization: Bearer $ANNEX_GATEWAY_TOKEN`; the answer is a JSON array of rows.
4. **Look up the columns from the gateway itself.** `GET $ANNEX_GATEWAY_URL/` with the token returns an OpenAPI document of exactly your app's views, with a description of every column.
5. **You can't reach the gateway from your laptop.** It answers only apps running on Annex. To try a query, deploy (it takes a minute) and look at `annex logs <app>`, or add a small debug page to your app.

## Querying (the gateway is PostgREST)
Filters are query parameters, `column=operator.value`:

| Want | Write |
|---|---|
| equals, not equals | `venue_code=eq.MB`, `status=neq.closed` |
| greater / less | `games_7d=lt.5`, `day=gte.2026-10-01` |
| one of | `event_type=in.(coil_fault,switch_fault)` |
| text search (`*` is the wildcard) | `title=ilike.*dragon*` |
| null / true / false | `closed_at=is.null`, `out_of_order=is.true` |
| array contains | `toys=cs.{magnet}` (on `machine_models.toys`) |
| two conditions on one column | `and=(day.gte.2026-10-01,day.lt.2026-10-08)` |
| either / or | `or=(era.eq.EM,era.eq.SS)` |
| pick columns, rename, JSON field | `select=venue_code,venue_name,friday:opening_hours->fri` |
| sort, page | `order=score.desc,played_at.asc&limit=20&offset=40` |
| total count | header `Prefer: count=exact`, read `Content-Range: 0-19/144` |

What the gateway doesn't do:
- **No joins.** Read each view and join in your code. The keys are `venue_code`, `machine_id`, `model_id`, `nickname` (unique) and `tournament_id`. `handle` (three letters) is not unique.
- **No GROUP BY or SUM.** The views are already aggregated where it matters: per machine per day (`daily_plays`, `machine_events_daily`) and per venue per week (`revenue_weekly`). Total them further in your code.
- **No writes.** Everything is read-only. Your app's own data goes in its own database (`storage.postgres: true`, see the contract).

Some views are big if read whole, so filter them: `daily_plays` has about 23,000 rows, `hourly_plays` about 10,000, `machine_events_daily` about 45,000.

## Which view answers what
| Question | View |
|---|---|
| Where are the arcades, when are they open? | `venues` (with lat/lon for a map, `opening_hours` as JSON) |
| What games exist; which era, maker, toys? | `machine_models` |
| What stands where right now; what's broken, idle or busy? | `machine_floor` |
| High scores, a high-score wall | `leaderboard` (top 10 per model, members only) |
| Who are the regulars and the best players? | `player_profiles` (public profile: nickname, rating, activity) |
| Busy days, earnings per machine, trends | `daily_plays` |
| Busiest hours and weekdays (a weekday-by-hour heatmap) | `hourly_plays` (per venue, day and hour; `weekday` 1 = Monday) |
| Tilts, faults, jackpots | `machine_events_daily` |
| What broke, how fast it got fixed, what it cost | `service_log` |
| Cash per venue per week | `revenue_weekly` |
| Tournaments and their results | `tournaments`, `tournament_standings` |

Good to know:
- **Time.** The data covers the **180 days before the last reseed** and ends the day before it. Timestamps are ISO 8601 with an offset; `day` columns are local (Central European) dates. Don't assume today has data. Find the latest day with `daily_plays?select=day&order=day.desc&limit=1`, or show "no plays in the last 7 days" gracefully.
- **Scores** differ by orders of magnitude between eras: electro-mechanical tables score thousands, modern ones billions. Compare scores within one model only.
- **Money** is EUR. `daily_plays.revenue_eur` is credits times the price. `revenue_weekly.amount_eur` is the cash actually counted, which differs a little.
- **Eras:** EM (electro-mechanical, to 1977), SS (solid state, 1978-1990), DMD (dot-matrix display, 1991-2012), LCD (2013 on). `era` is NULL for video and other machines.
- **Machines move** between venues. `daily_plays.venue_code` is where the machine stood that day; `machine_floor` and `current_venue_code` are where it stands now.
- **Anonymous play** is most of it: only members' games count in `leaderboard`, `player_profiles` and `daily_plays.members`.

## There are stories in the data
The data was made with a few things worth finding. They make good demos:
- a machine that has been getting sicker for weeks;
- a player who dominates the high-score tables;
- a venue whose takings dropped after it lost two popular tables;
- weekly rhythms, including one venue's league night;
- what each era earns and how long its games last;
- a cabinet that tilts far more than it should;
- a tournament coming up.

## App ideas
- **High-score wall** for a venue's TV screen: top three per machine on the floor, refreshed every few minutes.
- **Technician's morning list:** machines out of order, open tickets by age, and machines with rising coil or switch faults. As a job, email it every morning (`notify:` in the contract).
- **Venue dashboard:** this week against last, per machine. Which tables earn their floor space, and which sit idle?
- **Player card:** search a nickname and see their rating, leaderboard spots, home venue and recent tournaments.
- **Tournament page:** past winners, standings and the next event, with its entries.
- **Map** of the arcades with what's on each floor: "find me a DMD table with a magnet near Graz".
- **Tilt watch:** a Slack nudge when a cabinet's tilt rate jumps (`notify.slack`).

## Rules
- Read data **only through the gateway**. Never try to reach a database directly.
- Show players by `nickname` or `handle`. The views hold no personal data; don't try to work it out from other data.
- Keep the token on the server, log with the template's `log` helper, and never log tokens.
- Need a view or a column that isn't there? Ask the platform team. Adding a view is a reviewed change on their side.
