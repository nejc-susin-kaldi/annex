# M2 console overhaul: plan

For build agents, one step per session. Written 19 Oct 2026 against commit `952adcc`. It follows a UX review of the console (M2.4, D69–D70) and Nejc's answers of 19 Oct: **retire the Metabase "Annex usage" dashboard and the Homepage Usage tile, add a host-samples table, keep Dozzle.** No new component, no new container.

**Read first:** `CLAUDE.md` (it overrides everything), `docs/brief.md`, `docs/decisions.md` (D69–D74 for the console, health checks and the audit trail), `docs/plans/m2.md` (M2.4 and M2.6) and this file. Then do **only the step you were given**, and stop and report to Nejc when its "Done when" holds. Log each step's decisions in `docs/decisions.md` (what, why, alternatives).

## Status (19 Oct 2026)
All three steps are implemented and pushed to the VM (decision D76). Results: unit tests 305 (annex-ops), `test-smoke.sh` 51/51, `test-console.sh` 124/124, `test-audit.sh` 61/61, `test-usage.sh` 50/50. **Not run:** `test-host.sh` and `test-alerts.sh` (the sampler now also stores a sample; the alert path is untouched).
**Differences from this plan:**
- `metabase_usage` is **kept** for now (the plan allowed it): dropping it before Metabase's connection is removed would only fill Metabase's log with errors. `docs/setup.md` step 8 has the removal script and the commands to drop it; removing its lines from `annex_ops.sql`, `push.sh` and `bootstrap-secrets.sh` is the follow-up.
- The Overview's apps card shows the 6 most urgent apps and links to the Apps list (the same table component with filters), so the table has one definition.
- "Next run" of a job is not shown (the plan allowed leaving it out). The Releases tab shows the newest 25 (`?all=1` for all).
- The Summary's recent warnings are those of the **live** release only (older releases' lines are history on the Logs tab).
- "People, 7 days" counts distinct logins over the apps you may see (an aggregate; no login is shown).

## Why

The console shows the right data in the wrong shape for an admin:

- **Nothing says what needs attention.** `/` is an eight-column table in no particular order; Platform and Host are one-row tables beneath it; the newest backup's age isn't shown at all (D70).
- **The same thing lives in several places.** Health is on `/status`, in the fleet's Health column, in an app's Status section, on the Homepage Status tile and in `annex status`. Problems are split over Flags, Warnings, Status and Errors. Deploys are in an app's Releases and in the Audit page. Usage is in a fleet column, an app's Usage section, the Metabase dashboard and the Homepage Usage tile. The "read-only" notice and a CLI hint appear on every page and under almost every section.
- **An app page is one long scroll** (about 2,600 px with sample data; errors start 1,600 px down, below a 14-row usage table that is really a chart).
- **Details:** three time formats (local without a zone, UTC with `Z`, ISO with `T`), seven words for state drawn as the same outlined pill, no way to filter the fleet, Config as a 14-row key/value list, Audit with free-text filters and no paging.

## The rule: one home per thing

| Thing | Its one home | Everywhere else it is only |
|---|---|---|
| Health of apps, the front door, platform parts, host | **Overview** (admins and owners, scoped) and **Status** (every signed-in user): one render function, two scopes | a link, or a count |
| Everything wrong with one app (incident, failed job, warning, active error group) | the app's **Summary** tab, "Needs attention" | a count and a severity on the fleet row |
| Release history | the app's **Releases** tab | one feed line on Activity |
| Cross-app "who did what" (deploys, access, data reads, messages) | **Activity** (today's Audit page) | each row links to the app tab that explains it |
| Usage numbers and charts | the app's **Usage** tab | one KPI tile and a sparkline column on Overview |
| Stored app logs | the app's **Logs** tab | links from errors, releases and incidents |
| Live container logs, platform containers | **Dozzle** (kept), linked from Platform | — |
| Product-data analytics | **Metabase** (the curated product views only) | — |
| What the CLI would do | one collapsed "From the CLI" block per page | — |

Navigation: **Overview · Apps · Activity · Platform** for admins; **Overview · Apps** for owners; **Status** only for everyone else. The old `/` (fleet) becomes the Apps list; the Audit nav entry is renamed Activity (the CLI keeps `annex audit`).

## What does not change

- **No JavaScript, same CSP** (`default-src 'none'; style-src 'unsafe-inline'`). Sparklines and charts are inline SVG built on the server; filters are GET links and forms; tabs are separate URLs.
- **Read-only.** Every route stays GET-only (a POST is a 405).
- **Same data functions as the CLI** (`usage.usage_app`, `errors_run.list_groups`, `logstore.query_logs`, `incidents.list_incidents`, `audit.timeline`, `health`), so a number on a page is the CLI's number. Same authorisation (`can_see_app`; the same 403 for a missing app and someone else's for non-admins; per-person data admin-only).
- **Same container and networks** (D69): no Docker access, never `edge` or `appdata`. Memory stays under its 96 MiB limit (measure it; the budget was about 45).
- **State vocabulary:** one four-state scheme, **ok, warn, bad, off** (green, amber, red, grey), drawn as a solid dot plus a word; the existing words map onto it in one function (`up`/`ok`/`live` → ok, `unknown`/`warn` → warn, `down`/`failed`/`error`/`active` → bad, `quiet`/`none` → off). Dark mode stays.
- **One timezone** (Europe/Ljubljana, as today) everywhere, shown relative ("3 d ago") with the absolute time in the element's `title`. UTC stays only in log lines, which are copied into tickets.

## Order of work

| Step | Delivers | Depends on |
|---|---|---|
| M2.7 | The visual system and the Overview (admin and owner), Status on the same component, the Apps list | — |
| M2.8 | The app page as tabs: Summary, Logs, Errors, Releases, Usage (with charts), Config | M2.7 |
| M2.9 | Activity, Platform (host samples, backup age), and retiring the Metabase usage dashboard and Homepage Usage tile | M2.7 (M2.8 for the Usage tab's charts) |

---

## M2.7 Visual system and Overview

**Goal:** an admin opens the console and knows in five seconds whether anything needs them; an owner sees the same for their apps.

**Visual system** (`console_view.py`; keep it pure, escaped, one inline stylesheet):
- Tokens in `:root` for the four states, spacing, radius and a type scale; tabular numerals for numbers; cards for KPIs; a top nav with a clear active tab; breadcrumbs on app sub-pages; a mobile layout (cards stack, tables become labelled rows below 40 rem instead of scrolling sideways).
- Helpers: `state(word)` (the four-state dot), `rel_time(dt)` (relative text plus absolute `title`), `sparkline(values)` and `bars(values, labels)` (inline SVG with a text alternative: `role="img"` and a `<title>`), `kpi(label, value, note)`, `cli_block(*commands)` (a `<details>`; replaces `readonly_note` and the per-section hints), `tabs(app, active)`.
- The footer shrinks to one line (times, and the owner-is-a-claim caveat); the read-only notice lives in the CLI block's summary.

**Overview** (`/`, admins; owners get the same scoped to their apps, with no platform items):
1. **Needs attention**, ranked: down apps, open platform incidents, failed jobs, a failed release in the last 24 h, new error groups since yesterday, a backup or disk alert, apps with no health result. Each item is one line with a link to where to act. Empty state: "All clear" and the time of the newest health result (so a stopped checker doesn't read as all clear: if the newest result is older than three intervals, say so instead).
2. **Five KPI tiles:** apps up of total, active users 7d (with the change from the week before), open error groups, deploys 7d (with failures), disk and memory (admins).
3. **Apps table**, sorted by severity then name, columns **App** (owner under the name), **Health**, **Users 7d** (number, trend and sparkline from `usage_daily`), **Errors** (active groups), **Last deploy** (`r7 · 3 d ago`; "no release record" in muted text), **Signal** (one chip merging the usage signal, incident and warning count). Filters as links with a count: **All, Needs attention, Dormant, Graduating** (and **Mine** for admins), plus a text search as a GET form on app name and owner.
4. **Side column:** Platform status (front door, platform incidents, disk, memory) and the last eight Activity events (admins; the feed is the Activity page's query with `limit 8`).

**Status** (`/status`, everyone signed in): the same health component with names and up/down only, as D70 defines; it must not show owners, counts or details beyond what it shows today. Admins no longer get a Status nav entry (Overview contains it); `/status` still answers for them and for alert emails' links.

**Data** (`console_data.py`): add `attention(who)` (the ranked list, built from what `fleet()` already reads, no new queries beyond `usage_daily` for sparklines: one query for all apps, 14 days), and `kpis(who)`. Keep `fleet()`'s per-app loop but stop calling `warnings_for` once per app twice (it reads logs and job runs; compute once and pass it along).

**Done when:**
- with a down app, a failed job, an open platform incident and an overdue backup in the sample data, all four are in "Needs attention", in severity order, each linking to the right place; with none of them it says "All clear" and shows when health was last checked; with a stale checker it says so;
- the Apps table's users and trend equal `annex usage`'s (the existing test) and its sparkline's last value is today's `usage_daily` row;
- `sam` (owner) sees only his apps and no platform items; `cm` gets `/status` and a 403 on the rest; forged `Remote-*` headers change nothing (existing checks);
- nothing from an app is unescaped (the existing unit test extended to the new fields, including a `<script>` as an app's owner, a job's error and a signal);
- a width of 375 px shows no horizontal page scroll;
- `scripts/test-console.sh` is updated for the new markup (it greps `<span class="badge …">`) and passes, then `scripts/test-smoke.sh`.

---

## M2.8 The app page as tabs

**Goal:** one question per tab, and the answer to "is this app OK?" on the first screen.

**Routes** (same authorisation as today, via `authorised()`): `/apps/<app>` (Summary), `/apps/<app>/logs`, `/errors`, `/releases`, `/usage`, `/config`. The old `?release=` and `?request=` log links keep working.

**Header (every tab):** name, health state, owner, live release, type, memory, a link to the app's own URL, the tab bar with counts (Errors shows active groups).

**Summary:**
- KPI tiles: users 7d (with trend), requests 7d, server errors 7d, p95 (each with a 14-day sparkline from `usage_daily`).
- **Needs attention:** one list that merges the open incident and its signals, failed jobs, `warnings_for`, and active error groups (title, count, since which release), each with a link (logs, the error group, the release). This replaces the separate Status/Warnings sections.
- **Jobs:** one row per scheduled job: last run (relative), result, took, the next scheduled time if cheap to compute from the cron expression (leave it out if not), and a link to its logs (`source=all`, `job:<name>`).
- **Recent:** the last three releases and the last five error or warn log lines (links into the tabs).

**Logs:** today's page (filters, paging) unchanged in behaviour, with the filter form on top as a row of controls, a level colour on the line's left edge and the "Older lines" link.

**Errors:** groups with a state filter (Active, Quiet, All), title, kind, count, first and last seen, release range, the user and the request's log link (as today).

**Releases:** the current table, with failed releases visibly marked, the live one pinned first, git and note readable, and the logs link. The audit feed no longer shows an app's release rows twice: Activity lists a deploy once, as the event it is.

**Usage:** a 30-day SVG bar chart of users and one of requests with errors marked, the 4-week trend as bars, the table behind the chart collapsed in a `<details>`, per-person only for admins with the existing personal-data label.

**Config:** the manifest in groups: **Runtime** (type, port, health check, memory, schedule), **Storage** (Postgres, files), **Data access** (gateway views), **Notifications** (email allowed, Slack channels, alerts to), **Webhooks** (name and how verified), **Secrets and environment** (names only; `env` values from `annex.yaml` as today). Injected variable names stay names only. No secret value is ever read.

**Done when:**
- every number on Summary, Errors, Releases and Usage still equals `annex status`, `annex errors`, `annex releases` and `annex usage <app>` (extend the existing test's comparisons to the new tab routes);
- an app with a failing job, an active error group and a warning shows all three in "Needs attention" and nowhere else on Summary; an app with none shows "Nothing needs attention";
- the first screen of Summary at 1280×800 shows the header, the KPIs and the Needs attention list without scrolling;
- a non-owner non-admin still gets the same 403 on every tab, including a tab of an app that doesn't exist; admins get a 404;
- the old `/apps/<app>/logs?...` URLs and the links in alert emails still land on the right page;
- unit tests for each new view function; `test-console.sh` and `test-errors.sh` pass.

---

## M2.9 Activity, Platform, and retiring the usage dashboard

**Goal:** the last two admin questions have a home ("what happened?" and "is the server OK?"), and nothing duplicates the Usage tab.

**Activity** (`/audit`, admins; also `/activity`, the same page; `/audit` stays so existing links and tests keep working):
- filter chips instead of a free-text action: **All, Deploys, Access** (sign-ins, users, secrets), **Data reads** (gateway), **Messages** (email, Slack), **Webhooks**, **Failures**; an app and an actor field; a time-range chip row.
- the actor and app are links (the app to its Summary); relative times with absolute on hover; the details line kept short with a "more" `<details>` for the full JSON; paging (an "Older" link, 100 rows a page) instead of a fixed 300.
- incidents opening and closing, from `annex_ops.incidents` (readable by the console role), are merged into the timeline in `console_data` (not in the view; the `audit_timeline` view stays as D71 defines it). Mark them as platform events, not as actions by a person.
- the Overview's side feed and this page share one query function; the CLI's `annex audit` is unchanged.

**Platform** (`/platform`, admins):
- the front door and every platform incident (open, with their signals and since when);
- host: disk used and memory available as bars with the thresholds that raise the alert (from `config.tuning()`), the newest backup's age and result, and 7-day sparklines of disk and memory from the new table below;
- the platform's parts as a list with links: Dozzle ("Live container logs"), Metabase, Mail (Mailpit), the data gateway. Each says in a few words what it is for. **Dozzle stays** (Nejc, 19 Oct): the console holds stored app logs, Dozzle the live logs of platform containers.

**Host samples** (the one schema addition; Nejc approved it on 19 Oct). In `db/appdata/annex_ops.sql`, appended and idempotent like the others:

```sql
CREATE TABLE IF NOT EXISTS annex_ops.host_samples (
  ts              timestamptz NOT NULL DEFAULT now(),
  disk_used_pct   real,
  disk_free_gb    real,
  mem_available_mb real,
  backup_at       timestamptz
);
CREATE INDEX IF NOT EXISTS host_samples_ts ON annex_ops.host_samples (ts);
GRANT SELECT ON annex_ops.host_samples TO annex_console;
```

- `host.Sampler` already measures all four every `host_sample_s` (60 s); it additionally inserts a row **every 5 minutes** (not every sample: about 8,600 rows for 30 days), and an hourly housekeeping step deletes rows older than 30 days (the pattern of `health.housekeeping`). A value that couldn't be measured is NULL. A write failure is logged and never stops the sampler or the alert engine.
- The console shows the newest row; a row older than three sample intervals is "not sampled recently" instead of a stale number. The console no longer reads `/proc/meminfo` and `statvfs("/")` itself (`host_numbers` goes away), so the page and the alert engine can't disagree.
- The table can't change an existing table (CLAUDE.md); it is new, so note its columns in `docs/decisions.md`.
- `test-console.sh` runs the role's `SELECT` on `host_samples` and checks nothing else new is readable or writable.

**Retire the usage dashboard and tile** (Nejc, 19 Oct). The numbers and charts live on the Usage tab and the Overview; the weekly digest and the discovery page's per-app "Users 7d" tiles stay (they are not Metabase).
- Remove the Homepage `Usage` tile from `config/homepage/services.yaml.tmpl`; the smoke test's services check must still pass.
- Remove `config/metabase/usage-dashboard.json` and the "Annex usage" connection, collection and dashboard setup from `vm/metabase-setup.sh` (and its `--rebuild-usage` option), and the `metabase_usage` role's use: `APPDATA_DB_METABASE_USAGE_PASSWORD` in `vm/bootstrap-secrets.sh` and `scripts/push.sh`, the role and its grants in `annex_ops.sql`. **Dropping the role is a change to an existing database role: do it in `annex_ops.sql` as `DROP ROLE IF EXISTS` only after its grants are revoked, and say so in `docs/decisions.md` (D-number) with the reason (nothing reads it any more).** If that proves fiddly, leave the role in place with no connection and say so; it has `SELECT` on summaries only.
- **Removing the dashboard from the running Metabase is a destructive step on the VM** (the collection, dashboard and four cards in Metabase's own database, and the "Annex usage" connection). Do not run it. Write the exact steps and their effect in `docs/setup.md` and ask Nejc to go ahead; the repo changes alone leave the old dashboard in place, which is harmless.
- `scripts/test-usage.sh`: drop the Metabase-card comparison and the `metabase_usage` checks; keep the tile, privacy, retention, restart and digest checks. Update `scripts/seed-usage.sh` and `docs/demo.md` (the demo's usage lines now show on the console's Usage tab), `docs/operations.md` (the usage "Where it shows" and "Dashboard maintenance" paragraphs) and `README.md`.
- Add a line to the console's Usage tab and to `docs/operations.md` on where to explore beyond it: the CLI (`annex usage`) and the per-day table behind the chart.

**Done when:**
- Activity filters by chip, pages back past 100 rows, and an incident opening appears in it as a platform event; `annex audit` and the old `/audit?...` links behave as before;
- `/platform` shows disk, memory and backup age from `host_samples`, "not sampled recently" when the newest row is old, and links to Dozzle, Metabase and Mail; killing the backup container's mtime makes the backup age grow on the page and the existing alert still fires once;
- `host_samples` stays about 8,600 rows after 30 days (check the retention with a backdated row) and the sampler survives the table being unwritable;
- the Homepage services list has no Usage tile, `scripts/test-smoke.sh` and `scripts/test-usage.sh` pass, and a grep for `usage-dashboard` and `Annex usage` finds only `docs/decisions.md` history;
- the three suites that touch these pages pass: `test-console.sh`, `test-audit.sh`, `test-usage.sh`; then `test-smoke.sh`. (`test-alerts.sh` only if `host.py`'s alerting changed: tell Nejc first, it takes 20 minutes.)

---

## Decisions already made (Nejc, 19 Oct)
1. **Retire** the Metabase "Annex usage" dashboard and the Homepage Usage tile; Metabase stays for the curated product views.
2. **Add** the `host_samples` table (backup age and disk and memory trends on Platform).
3. **Keep** Dozzle, linked from Platform.

## Open for the build agent to raise, not decide
- Whether `Mine` and the text search on the Apps list are worth their markup (drop them if the page gets heavy).
- Any new component, route that changes something, or change to the app contract: ask first (CLAUDE.md).

## Risks
- **Test coupling:** `test-console.sh` and `test_console_view.py` assert on markup (badge classes, the usage sentence). M2.7 changes both; update them in the same step, never loosen a privacy or authorisation check to make them pass.
- **Two ways to see health** is the thing this plan removes: if a step leaves `/status` and Overview with different logic, it has failed its goal.
- **Size:** `console_view.py` is about 300 lines today and will roughly triple. Split by page (`console_view.py` for helpers and the shell, `console_pages_*.py` per area) rather than growing one file; keep views pure.
