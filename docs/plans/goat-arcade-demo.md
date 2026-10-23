# Goat Arcade demo environment plan

Written 21 Oct 2026 on branch `goat-arcade` (commit `a4964d2`: the Goat Arcade database behind the gateway, D77–D78), merged into `main` on 22 Oct: work from `main`. Hardening (D79–D97) and control-plane (D98–D105) landed since; D106 records what that changed for Goat Arcade. **Not started; Nejc's choices are in (§4, 21 Oct); implementation comes later.**

**Goal.** Turn this instance into one complete, believable example of Annex in practice: Goat Arcade's own server. Its staff sign in once and find their tools. Analysts have dashboards on curated data. A few tools were "vibe-coded" by staff. One off-the-shelf open-source tool is self-hosted behind the same login. Kaldi and the client oversee all of it. Everything generic or wealth-management-shaped goes.

**Read first:** `CLAUDE.md`, `docs/brief.md`, `docs/decisions.md` (D9 Metabase accounts, D14 product data, D57 users, D74–D78), `docs/goat-arcade/` and this file. One step per session; stop and report when its "Done when" holds.

## 1. Inventory: what's there now
| Thing | Where | What it is | Fate |
|---|---|---|---|
| `client-lookup` | `examples/client-lookup`, deployed | web: search wealth clients via the gateway (identity, granted vs denied views) | remove; its demo role goes to `venue-pulse` and `high-score-wall` |
| `monthly-report` | `examples/monthly-report`, deployed | web+job: monthly positions CSV, run history, email to `cm`, Slack `#finance-reports`, a demo job every 10 min | remove; replaced by `weekly-takings` |
| `vibe-leads` | deployed only: source in `/srv/annex/deploy/vibe-leads/src`, **not in the repo** | web: capture a client lead, hand it to a teammate (Slack or email preview); own Postgres; no gateway | archive the source, then remove. Its form-and-hand-off idea is reused by `party-bookings` |
| Wealth data | `db/product/01-schema.sql`, `02-seed.sql`, `api_views.sql` (4 views); D14 | the brief's "loosely wealth-management shaped" product DB | remove the data and its views. product-db stays as the gateway database (roles, log hook, FDW link) |
| Metabase "Product" connection | `vm/metabase-setup.sh` | product-db `annex_api` as `metabase_ro`; already sees the Goat views | rename it "Goat Arcade" |
| Metabase "Annex usage" dashboard, 4 cards, DB connection | Metabase (M1.4), retired in the repo (D76) | the old usage dashboard; still live | remove with `vm/metabase-retire-usage.sh` (`docs/setup.md` step 8, **Nejc runs it**) |
| Metabase users | `admin@`, `sam@`, `cm@annex.example` | demo accounts (D9: Metabase has its own) | keep; add the new personas |
| Annex accounts | `sam` (Sam Novak, builder), `cm` (Maja Horvat, user), `kaldi-admin` | demo and test users; the test suites depend on them | keep `sam` and `kaldi-admin`; **`cm` becomes `maja`** (§2.1); add more |
| Demo script and helpers | `docs/demo.md`, `scripts/reset-demo.sh`, `scripts/demo-bad-deploy.sh`, `scripts/seed-usage.sh`, `scripts/test-examples.sh` | the 10-minute wealth demo | rewrite for Goat Arcade |
| Tests on wealth views | `tests/annex-selftest` (`GRANTED`, `UNGRANTED`), `scripts/test-apps.sh`, `scripts/test-audit.sh`, `test-smoke.sh` page checks | platform tests read `client_summary`, `top_clients_by_aum`, `advisor_book`, `monthly_positions` | switch to Goat views (decision B) |
| Templates and agent docs | `templates/*` (sample code reads `top_clients_by_aum`), `runtime/` docstrings, `skills/annex/SKILL.md`, manifest error examples, contract allowlist, README | what agents copy | switch the examples to Goat views |
| Discovery page | `config/homepage/` (title "Annex", tiles "curated product data", placeholder add-ons Goat Mailer and Bekafon) | the front page | rebrand (§2.6); keep the add-on tiles |
| Hostnames | `docs/setup.md` §3 (`client-lookup`, `monthly-report`, `vibe-dashboard`...) | `/etc/hosts` entries on the Mac | new names; **Nejc's change** (or the wildcard resolver) |
| Slack | `secrets/slack.env` (connected); `#finance-reports`; test mode posts to `#annex-selftest` | | new channels (§2.5) |

Memory today: about 2.4 GiB used of 3.9 GiB, 1.5 GiB available. Metabase uses 0.95 GiB, arcade-db 85 MiB, each small app 35–45 MiB.

## 2. The target environment

### 2.1 The people (fictional Goat Arcade staff)
| Login | Name | Role | Annex groups | Uses |
|---|---|---|---|---|
| `kaldi-admin` | Kaldi Admin | Kaldi's platform admin | admins, builders | console, everything |
| `sam` | Sam Novak | operations manager, the tech-savvy one: builds the tools | builders | builds `tech-board`, `weekly-takings`; Metabase analyst |
| `maja` (was `cm`) | Maja Horvat | venue manager, Metelkova (LJ1) | users | `venue-pulse`, `party-bookings`, Venue dashboard |
| `boris` | Boris Zajc | lead technician (he's in the data: `technicians`) | users | `tech-board`, the self-hosted tool, Machine health dashboard |
| `petra` | Petra Kralj | finance and admin | users | `weekly-takings` email, Cash dashboard |
| `lena` | Lena Gruber | community and tournaments, Graz | builders | builds `league-desk` and `high-score-wall`: a second builder, so ownership shows |
| `tomaz` | Tomaž Vidmar | managing director | users | the At-a-glance dashboard, the weekly digest |

New accounts use `@goat-arcade.example` (a reserved domain); `maja` too (`maja@goat-arcade.example`). `sam` keeps `sam@annex.example`, and `kaldi-admin` keeps `admin@annex.example`.

**Renaming `cm` to `maja`** (decision C) touches about 80 places in the integration suites:
- `scripts/test-*.sh`: the `cm.jar` cookie files, `login cm`, `mail_count cm@annex.example`, the 403 checks on cm;
- the selftest fixture (`SELFTEST_RECIPIENT: cm`);
- `vm/bootstrap-secrets.sh` (the demo `USERS` list);
- `vm/metabase-setup.sh`;
- `docs/demo.md`.

The ops unit tests use `cm` only as an arbitrary login string: leave them.

On the running VM, the steps are:
1. Nejc adds `maja` (`annex users add maja ... --print-password`).
2. Her password goes into `secrets/users.txt`, which the suites read. A small `annex_vm.py` helper does it, so nobody edits the file by hand.
3. Her Metabase account is created.
4. `cm` is removed last, after the suites pass with `maja`.

`annex users` is human-only, so the step prints the commands and Nejc runs them.

### 2.2 Data
- **Wealth data removed** (§1). The gateway then serves Goat Arcade only.
- **One more view, `hourly_plays`** (day, weekday, hour, venue_code, games, credits): the weekday-by-hour heatmap Metabase and the apps want. Others only if a dashboard needs one; each is a reviewed addition to `api_views_arcade.sql`, with comments.
- **Freshness** (decision D: nothing advanced). This instance is demoed a few times and then retired, so `reset-demo.sh` reseeds arcade-db before each demo and the data ends the day before. No scheduled reseed, no live trickle.

### 2.3 Metabase: dashboards the staff would use
- **The connection:** rename "Product" to "Goat Arcade" (same product-db `annex_api`, read-only). Use the API to set semantic types: lat/lon as coordinates, `*_eur` as EUR currency, keys (`venue_code`, `machine_id`, `model_id`, `nickname`) as entity and foreign keys. Metabase can then join views in the query builder, even though the gateway can't.
- **Groups and collections**, to show governance: Management, Venues, Technical, Finance, Community. One collection per team. Everyone can explore the curated views; only `sam` and admins get the SQL editor.
- **Dashboards as code:** JSON specs in `config/metabase/goat-arcade/`, applied idempotently by `vm/metabase-setup.sh` through the API (the approach the retired usage dashboard used). A reset rebuilds them.

| Dashboard | Collection | Cards |
|---|---|---|
| Goat Arcade at a glance | Management | last week's cash, games and active members (with the change against the week before); weekly cash per venue over 26 weeks; revenue by era; top 10 machines by revenue over 30 days; map of venues sized by cash |
| Venue performance (filter: venue) | Venues | games per day; weekday × hour heatmap (`hourly_plays`); machine earnings table; idle machines (`games_7d` low); this week against last |
| Machine health | Technical | out of order now; open tickets by age; coil and switch faults per day; downtime hours by venue; mean time to repair by technician; tickets by symptom; parts cost per month |
| Cash and collections | Finance | weekly cash stacked by venue; coins, tokens and card split; cash per machine by era; **counted cash against estimated plays per venue** (the gap a finance person checks) |
| Players and tournaments | Community | member sign-ups per month; active members per venue; most leaderboard spots; tournament entries over time; the last tournament's standings |

- **Site name** "Goat Arcade analytics". Metabase accounts for every persona, with their Annex password (D9: separate accounts, as now).

### 2.4 The vibe-coded tools (Annex apps, `examples/goat-arcade/<app>`)
Each tool is built the way a staff member would build it. Claude Code gets the `annex` skill, `docs/goat-arcade/agent-brief.md` and one short prompt in the person's words. Then a review pass, and the prompt is kept in the app's README. Each tool shows one platform capability clearly.

| App | Owner | Type | What | Shows |
|---|---|---|---|---|
| `venue-pulse` | cm | web | my venue this week against last: earnings per machine, idle machines, rotation candidates ("move X, it earns more in Zagreb") | identity (the venue follows the user), gateway, charts |
| `tech-board` | sam (for boris) | web+job | the morning list: out of order, open tickets by age, rising faults. "I'm on it" claims in its own Postgres, by login. 07:30 email to the technicians; Slack `#ga-tech` when a machine goes out of order | identity, own database, jobs, email, Slack |
| `weekly-takings` | sam (for petra) | web+job | Monday 07:00: last week's cash per venue as CSV and a summary email to finance, Slack `#ga-finance`; run history page | jobs, files and backups, run history. **Replaces monthly-report** |
| `league-desk` | lena | web | tournaments: past standings, the upcoming Masters with its entries (gateway), and **new sign-ups stored in the app's own database**; Slack `#ga-community` on a sign-up | read-only gateway plus app-owned writes |
| `high-score-wall` | lena | web (static/node) | full-screen TV mode per venue: top scores of the machines on the floor, rotating every 15 s | a builder's second app; the kiosk look |
| `party-bookings` | cm | web | birthday-party requests arrive **by webhook** from the (fictional) public website form; staff confirm, pick a venue and slot, and the venue's manager gets a summary to forward (Annex never emails outside addresses, so the customer isn't emailed by the app). Based on vibe-leads | webhooks (the only route from outside), email, own database |

Plus a script, `scripts/demo-booking.sh`: it posts a signed test booking, as the website would.

**The live build in the demo** (built from an empty folder, as today): *"Build tilt-watch on Annex, owner sam: for the last 30 days, each pinball machine's tilt rate next to its model's average. Flag anything over 3x, worst first. Deploy it and give me the URL."* It finds planted story 6 (the Graz Cyber Samurai) live on stage. Fallback: a pre-built copy kept by `--keep`.

### 2.5 Email, Slack and webhooks
- Slack channels `#ga-tech`, `#ga-finance` and `#ga-community` (Nejc creates them in the test workspace; test mode still sends test runs to `#annex-selftest`).
- Mailpit shows the mail people would get: the technicians' morning list, the Monday takings, booking confirmations and the weekly digest.

### 2.6 The front door
- **Discovery page:** title "Goat Arcade · internal tools", a goat logo (an inline SVG, made here), tiles grouped by team (Venues, Technical, Finance, Community, Platform). The add-on placeholders stay.
- **Authelia's login page** says "Goat Arcade". Metabase's site name likewise.
- Platform tiles describe Goat Arcade's data ("Analytics on Goat Arcade's curated data").

### 2.7 The self-hosted tool: Kanboard (decision A)
An off-the-shelf open-source tool that Goat Arcade runs on its own server, behind the same login, next to the vibe-coded ones. It shows "Annex isn't only for AI-built tools: bring the tools you'd otherwise pay a SaaS for". It runs as an Annex app under the contract: its own Postgres or `/data`, backups, health, the console. An image that runs as root or writes outside `/data` gets a thin wrapper Dockerfile (`annex contract stack`).

| Option | What Goat Arcade uses it for | Sign-in | Storage | Memory | Fit |
|---|---|---|---|---|---|
| **Kanboard** | the technicians' repair board: a card per broken machine, parts to order, swimlanes per venue | **native reverse-proxy auth** (trusts `Remote-User`): one login, no extra setup | Postgres | ~50 MB | best fit for the platform as it is; plain looks |
| **Wiki.js** | the repair manual and venue handbook: how to rebuild a flipper, opening checklists, per-model notes | OIDC, which needs **Authelia's OIDC provider switched on** (new platform capability); otherwise its own accounts | Postgres | ~250 MB | the prettiest demo; the OIDC work pays off for later tools |
| **Homebox** | spare parts and cabinet inventory, with locations per venue and QR labels | its own accounts only (a second login) | SQLite in `/data` | ~40 MB | tiny and charming; the second login weakens the story |
| **Vikunja** | projects and tasks: a venue refit, tournament prep | OIDC (as Wiki.js) or its own accounts | Postgres | ~60 MB | good all-rounder; overlaps with tech-board |
| **n8n** | automations: a booking means a Slack post and a calendar entry; a weekly export to a sheet | its own accounts | Postgres | ~350 MB | shows power-user automation; **conflicts with "email and Slack only through Annex"** (it holds its own credentials and calls out), so it needs rules |

**Chosen: Kanboard** (Nejc, 21 Oct): the technicians' repair board, at `kanboard.`, owned by `sam`, used by `boris` and the venue managers. The other options stay here for the record.

Kanboard specifics for GA.5:
- **Image:** the official `kanboard/kanboard` image, version pinned (amd64; check it's at least two weeks old).
- **Sign-in:** reverse-proxy authentication. `REVERSE_PROXY_AUTH=true` and `REVERSE_PROXY_USER_HEADER=Remote-User`, which Annex strips from clients and sets from the session, so the header can be trusted (contract). Also `REVERSE_PROXY_EMAIL_HEADER=Remote-Email`, and `REVERSE_PROXY_DEFAULT_ADMIN=kaldi-admin`. Kanboard creates a user the first time someone arrives. Turn its own login form and password reset off.
- **Storage:** Postgres through `DATABASE_URL`, split into `DB_DRIVER`, `DB_HOSTNAME` and the other `DB_*` settings by the wrapper's entrypoint. Attachments and plugins go in `/data`.
- **Contract fit (the risk):** the image runs nginx and php-fpm, with root at the start, and writes to `/var/www/app/data`, `/var/run` and `/var/lib/nginx`. Expect a wrapper Dockerfile:
  - drop to uid 10001 and listen on `$PORT` above 1024;
  - send the writable paths to `/data` and `/tmp`;
  - add a health check path.

  If that fights the image too hard, build from the release tarball on a small PHP base instead. Record either in `docs/decisions.md`.
- **Content:** a project per venue, plus a "Parts to order" project. Swimlanes: reported, diagnosing, waiting for parts, fixed. Seed the open tickets from `service_log` once, with a one-off job that reads the gateway (Kanboard's JSON-RPC API, using a token stored as an Annex secret), so the board matches the data on day one. Optional, if time allows: `tech-board` links each machine to its card.
- **Tests:** `test-examples.sh` checks that Kanboard is healthy, that a signed-in `boris` lands on his board without a second login, and that a forged `Remote-User` from outside doesn't work (the existing spoofing checks cover the front door; add one for this app).

### 2.8 Making it feel lived-in
- **Usage history** (decision E): `scripts/seed-usage.sh` (synthetic, marked as such) for the new apps and Kanboard, so the console's Usage tabs and the weekly digest have shape. No synthetic sign-ins or "staff day" job.
- **One incident:** `demo-bad-deploy.sh` breaks `venue-pulse` instead of client-lookup.
- **Docs for visitors:** `docs/goat-arcade/tour.md`, a self-guided tour: who's who, what to click, what each part demonstrates. Someone can explore the instance without Nejc.

## 3. Steps
Removal comes late, so a working demo exists at every point.

| Step | Delivers | Depends on |
|---|---|---|
| GA.0 | Nejc's choices (§4); `vibe-leads` source archived to `examples/archive/vibe-leads`; the Metabase usage dashboard retired (Nejc runs `vm/metabase-retire-usage.sh`) | — |
| GA.1 | People and branding: the `annex users add` commands for Nejc (including `maja`); `cm` renamed to `maja` in the suites, the fixture, bootstrap and Metabase setup, then `cm` removed; Metabase users and groups; the Homepage, Authelia and Metabase names and logo | GA.0 |
| GA.2 | Data: `hourly_plays`; the freshness option; the Metabase connection renamed, with semantic types | GA.0 |
| GA.3 | Metabase: the five dashboards as code, collections and permissions | GA.1, GA.2 |
| GA.4 | Vibe tools, one session each: venue-pulse, tech-board, weekly-takings, league-desk, high-score-wall, party-bookings (+ `demo-booking.sh`); `test-examples.sh` covers them | GA.1 |
| GA.5 | Kanboard: wrapper, header sign-in, Postgres, seeded boards, tests; a decision entry | GA.0, GA.1 |
| GA.6 | Wealth removed: apps (Nejc runs the removals), examples, wealth SQL and D14's data, templates, runtime docstrings, skill, contract, README; the tests switched to Goat views; `bootstrap.sh` and `push.sh` bring up and link democlient; the switch logged in `docs/decisions.md` (the brief stays as is) | GA.3, GA.4 |
| GA.7 | The demo: `docs/demo.md` rewritten around the personas; `reset-demo.sh` (keeps the Goat examples, reseeds arcade-db, runs weekly-takings, sends a booking); `demo-bad-deploy.sh`; `seed-usage.sh`; the live-build prompt and fallback; hostnames in `docs/setup.md` §3 for Nejc; `docs/goat-arcade/tour.md` | GA.4–GA.6 |
| GA.8 | Verification: `test-smoke.sh` and the area suites at each step; **ask Nejc before `test-all.sh`**; a resource record (memory with everything up) in `docs/decisions.md` | all |

**Done when (whole plan):**
- `scripts/reset-demo.sh` brings up the full Goat Arcade instance from a clean state;
- every persona signs in and finds their tools;
- the five dashboards render with current data;
- the live build works from an empty folder;
- no wealth-management name is left outside `docs/decisions.md` and the old plans;
- everything fits in today's VM (3.9 GiB; the platform within its 2.5 GiB budget), with no new swap pressure on `dronky`.

## 4. Decided (Nejc, 21 Oct)
- **A. Self-hosted tool:** Kanboard (§2.7).
- **B. Tests:** switch to Goat Arcade views. `bootstrap.sh` and `push.sh` bring up and link democlient on this instance, so the suites can rely on it.
- **C. Personas:** keep `sam` (and `kaldi-admin`). Rename `cm` to `maja` (Maja Horvat, venue manager) and update the suites (§2.1). Add the five new accounts.
- **D. Freshness:** nothing advanced. `reset-demo.sh` reseeds before a demo. The instance is demoed a few times, then retired.
- **E. Synthetic activity:** seeded usage history only.
- **F. The brief:** log the switch from wealth data to Goat Arcade as its own entry in `docs/decisions.md`; leave the brief unchanged.
- **G. Memory:** stay within today's VM. GA.8 records the memory with everything up; if it gets tight, the first thing to cut is the number of vibe apps deployed at once, not the VM's size.
