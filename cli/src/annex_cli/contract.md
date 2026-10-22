# Annex app contract v0

You write a small folder. Annex does the rest: routing and login, the discovery page entry, schedules, storage, backups and data access. **Never write Docker labels, login code or database credentials.**

```
my-tool/
  annex.yaml    # manifest, required
  Dockerfile    # from a template, or your own; must end with a non-root USER
  ...code
```

Start from a template: `annex init <template> <name> --owner <login>`. Templates: `python-web` (FastAPI), `python-job` (scheduled script, no web), `node-web` (Express), `static` (built files plus a tiny server). Another language or framework works too: `annex contract stack`. Deploy with `annex deploy <folder>`.

## annex.yaml
```yaml
name: monthly-report          # 2-32 chars: a-z, 0-9, '-'; starts with a letter. Becomes the hostname
owner: sam                    # required; the builder's login, shown on the discovery page
description: Monthly positions report
type: web                     # web | job | web+job
port: 8080                    # web: listen on 0.0.0.0:$PORT (internal only; never published)
healthcheck: /healthz         # web: returns 200 when ready
access: internal              # only value in v0 (behind login)
storage:
  postgres: true              # dedicated database, injected as DATABASE_URL
  files: true                 # persistent directory at /data (DATA_DIR), backed up nightly
data:
  views: [monthly_positions]  # gateway views to read; must be in the allowlist
notify:                       # optional: what the app may send, through ANNEX_OPS_URL
  email: true                 # may send email (to company addresses only)
  slack: ["#finance-reports"] # Slack channels it may post to
webhooks:                     # optional, web apps only, at most 10: calls from outside services (see Webhooks)
  - name: trello              # part of the public URL
    path: /hooks/trello       # the app's own endpoint (POST)
    verify: trello            # trello | stripe | github | slack | shared-secret
    secret: TRELLO_SECRET     # a name from secrets:
alerts:                       # optional: who hears when the app has a problem (default: the owner)
  to: [owner, cm, "#finance-alerts"]  # 'owner', logins and Slack channels; admins always hear too
schedule:                     # job and web+job only
  - name: monthly
    cron: "0 6 1 * *"         # minute hour day month weekday, Europe/Ljubljana; or @daily, @hourly
    command: ["python", "report.py"]
env:                          # non-secret config only
  REPORT_TITLE: Monthly positions
secrets: [SMTP_PASSWORD]      # names only; a human sets values with `annex secret set`
egress: []                    # hostnames the app calls; declared, not enforced in v0
resources:
  memory: 512m                # optional; default 256m, 128m-512m
```
Reserved names: `home`, `auth`, `metabase`, `logs`, `gateway`, `mail`, `ops`, `status`, `hooks`, `console`, `echo-test`, `www`, `api`.

## Runtime rules
- **Stateless container.** Persist only to `DATABASE_URL` or `/data`. Everything else is lost on redeploy and isn't backed up.
- **Read-only root filesystem.** Only `/tmp` (64 MB, cleared on restart) and `/data` are writable.
- **Runs as uid 10001**, with no capabilities and `no-new-privileges`. The Dockerfile must still end with a non-root `USER`; the deploy refuses images that run as root.
- **Limits:** 256 MB RAM by default, 0.5 CPU. An app that needs more memory sets `resources.memory`, up to `512m`; the deploy notes it, and the platform team sees raised limits in `annex list`. Files in `/tmp` count against memory. More than 512 MB means the tool is too big for Annex: talk to the platform team.
- **Identity** comes from the request headers set by the front door. Never implement your own login:
  - `Remote-User`: login, e.g. `sam`
  - `Remote-Name`: display name, e.g. `Sam Novak`
  - `Remote-Email`
  - `Remote-Groups`: comma-separated, e.g. `builders` or `admins,builders`

  Annex strips client-sent copies of these headers, so they can be trusted.
- **Product data only through the gateway**, never directly from a database. Call `GET $ANNEX_GATEWAY_URL/<view>` with `Authorization: Bearer $ANNEX_GATEWAY_TOKEN`. The gateway is PostgREST, so filters work as query parameters, e.g. `/client_summary?client_name=ilike.*novak*&order=aum_eur.desc&limit=20`. Requesting a view you didn't declare returns 401/403. Every call is logged with your app's identity.
- **Email and Slack only through Annex.** Never run an SMTP client or hold a Slack token: declare `notify:` in `annex.yaml` and use the template helpers (`send_email`, `post_slack`). Recipients are logins or company addresses; no attachments. Details: `annex contract notify`.
- **Incoming webhooks** (Trello, Stripe, GitHub, Slack, a shared secret) reach a tool that stays behind login: `annex contract webhooks`.
- **Alerts.** Annex watches every app, and the owner (plus every admin, plus anyone in `alerts.to`) gets one email when:
  - the health check fails (checked every minute, 2 failures in a row);
  - the container stops, or exits with an error 3 times in 10 minutes (crash loop), or runs out of memory;
  - a scheduled job exits non-zero;
  - 5xx responses spike (at least 10 in 5 minutes and at least 5 % of requests).

  There is one alert per problem however many signals notice it, a reminder after an hour and then daily, and a "resolved" message when it recovers. So keep `/healthz` honest and fast (it must not depend on slow calls), and make a job **exit non-zero when it fails**: a job that catches its own error and exits 0 looks successful. Redeploys and rollbacks don't alert. `annex status <app>` shows the health check, any open incident and the last run of each job; everyone signed in can see every app's up/down on the console's status page (`console.` host, `/status`, also the Status tile on the discovery page); owners and admins see each app's page there. Slack channels in `alerts.to` only work once Slack is connected.
- **Usage** is counted for you from the front door's access log; the app does nothing. What counts: signed-in page requests to the app. Not counted: health checks, static files (`.js`, `.css`, images, fonts), anonymous requests, and anything a person did not request. Owners see totals on the app's discovery tile ("Users 7d" with a trend, status) and with `annex usage [app]`; Kaldi and the client see all apps in the "Annex usage" Metabase dashboard and a weekly email digest, which flags tools that are rising (graduation candidates) and ones nobody uses (dormant). Logins are never shown there: per-person counts exist only for admins (`annex usage <app> --by-user`), and raw request records, which never contain query strings, are deleted after 30 days.
- **Secrets** arrive as environment variables at runtime. Never put them in code, the repo or the image.
- **Never publish ports.** Only Annex's front door does.
- **Logging:** one JSON object per line on stdout through the template's `log` helper (`log.info("report written", rows=12)`), with the right level (`debug`, `info`, `warn`, `error`), and never secrets, tokens or personal data beyond the login. `annex logs <app>` searches 14 days of them, also after a rollback; errors are grouped and the owner is emailed once per new kind. Details: `annex contract logging`.
- **Health gate.** Every deploy is a numbered release (`r1`, `r2`, …). A web app's new release must return 200 on its `healthcheck` within 60 s (a job-only app must keep running). If it doesn't, Annex switches back to the previous release automatically, and the deploy fails with the new release's last log lines. Your database and `/data` are left as they are, and so are any changes the failed release made to them.
- **Jobs** run inside the app's container with the same environment (`docker exec`). In `type: job`, the container idles (`sleep infinity`) between runs. Trigger a job by hand with `annex run <app> <job>`. A job's exit code and output are its result.

## Email and Slack
Never run an SMTP client or hold a Slack token. Declare `notify:` in `annex.yaml`; Annex injects `ANNEX_OPS_URL` and an `ANNEX_OPS_TOKEN` that can do exactly what you declared. The python and node templates ship helpers (`send_email`, `post_slack` / `sendEmail`, `postSlack`); the raw API is below.
- `POST $ANNEX_OPS_URL/v1/email` with `Authorization: Bearer $ANNEX_OPS_TOKEN` and JSON `{"to": ["cm", "sam@annex.example"], "subject": "Report ready", "template": "notice", "data": {"title": "...", "body": "...", "link": "https://...", "link_text": "Open"}}`. Instead of `template`+`data`, give `text` or `html` (exactly one of the three). Answers `{"id", "to": [resolved addresses]}`.
- `POST $ANNEX_OPS_URL/v1/slack` with `{"channel": "#finance-reports", "text": "...", "blocks": [...]}`. The channel must be one you declared; `text` is required (it's also the notification fallback when `blocks` is set). Answers `{"id", "channel", "ts"}`.
- `GET $ANNEX_OPS_URL/v1/me` shows what your token may do and whether Slack is connected.
- **Recipients** are logins (`"cm"`, as in `Remote-User`) or company addresses. Anything outside the client's domains is refused (403), and if one recipient is refused, **nothing is sent**. 1-20 recipients; subject 1-200 characters on one line; body up to 256 KB.
- **No attachments** (400). Save the file under `/data` and email a link to it in your app: it stays behind login and downloads are logged.
- **Limits:** 60 emails and 60 Slack posts per hour per app (429 when exceeded). Slack answers 503 if the platform team hasn't connected a workspace yet.
- Every attempt is logged (app, recipients, subject, time, result), never the body: `annex sends <app>`.
- Errors are JSON `{"error": "..."}` and say what to fix; the helpers raise `NotifyError` with that message and `.status`.

## Webhooks
Outside services (Trello, Stripe, GitHub, Slack, anything with a shared secret) can call your tool while the tool itself stays behind login. Declare the hooks in `annex.yaml` (block above); each needs a secret listed under `secrets:`, whose value a human sets with `annex secret set <app> TRELLO_SECRET=...`.
- **The public URL** is `$ANNEX_HOOKS_URL/<name>`, i.e. `https://hooks.<domain>/h/<app>/<name>`: register that with the provider. It is the only address on Annex that needs no login, and it accepts only the hooks you declare.
- **What Annex does:** it checks the provider's signature on the raw body (`trello`: HMAC-SHA1 of body plus this URL; `stripe`, `slack`: HMAC-SHA256 with a 5-minute timestamp window; `github`: HMAC-SHA256; `shared-secret`: the header `X-Annex-Hook-Secret`), rejects and logs anything else, stores the event, answers the provider at once, and forwards it to your `path` with `POST`. Slack's URL verification challenge is answered by Annex.
- **What your endpoint must do:** call `hook_event(headers)` first (`hookEvent(req)` in node). It raises `HookError` with status 401 unless the request carries your `ANNEX_HOOK_TOKEN`, so nobody can reach the endpoint around Annex. It returns `.hook`, `.event_id` and `.attempt`. Then answer **2xx quickly** (do slow work afterwards), and **dedupe by `event_id`**: delivery is at least once, and a retry or replay carries the same id.
- **Retries:** a non-2xx answer or a connection error is retried after 10 s, 30 s, 1 min, 5 min, 15 min, then hourly, for 7 days; events of one hook are delivered oldest first, so a failing event holds back newer ones of that hook. If events stay undeliverable for 15 minutes the owner is alerted. Events (with their bodies) are kept 7 days.
- **Limits:** 1 MB per request; 60 events per minute per hook.
- **Seeing and replaying:** `annex hooks <app>` lists events (never their bodies). **Human only:** `annex hooks show <id>` (headers and body, which can hold personal data) and `annex hooks replay <app> --id N | --failed | --since 1h`. Rotate a signing secret with `annex secret set`, which updates what Annex checks.

## Logging
Every app logs the same way, so anyone can debug any app, and Annex can store and search the lines.
- **One JSON object per line on stdout** (stderr is fine for crashes). Required keys: `ts` (ISO 8601 with time zone), `level` (`debug`, `info`, `warn` or `error`) and `msg`. Anything else is free-form (`rows`, `ms`, `view`, ...).
- **Inside a request** every line also has `request_id` and `user` (the `Remote-User` login). The app reuses an incoming `X-Request-Id` or makes one (16 hex characters), **returns it as the `X-Request-Id` response header** and puts it on every line of that request. Annex's access log keeps the header too, so one id finds the front-door record, the app's lines and an error.
- **`LOG_LEVEL`** is injected (default `info`); set another under `env:` (`env: {LOG_LEVEL: debug}`) to get `debug` lines. Choose levels like this: `debug` for detail only you need, `info` for what happened, `warn` for something odd that was handled, `error` for something that failed.
- **Never log** secrets, tokens, passwords, cookies, request bodies or full product records. IDs and counts are fine, and so is the user's login; other personal data isn't (log `matches=3`, not the search text).
- **Jobs** log a start line and an end line with counts and duration, log errors with context, and **exit non-zero on failure** (that is what alerts the owner).
- **The helpers do it for you.** Python: `from annex_runtime import log, request_context` (`log.info("report written", rows=12, ms=340)`, `log.exception(...)` inside `except`; the template's middleware sets the context and the header). Node: `import { log, annexRequest }` (`log.info("report written", { rows: 12 })`, `app.use(annexRequest)`). The `static` template's server logs its requests itself. `print()` still works but isn't structured; prefer `log`. Your own stack must write the same format.
- **Annex checks, and only warns:** after a web app passes its health gate, `annex deploy` reads its first lines; if none follow the convention, the result says "logs aren't structured". A job-only app is checked on its first scheduled run: `annex status` shows the same warning. The deploy still succeeds.
- **Errors are grouped and emailed.** Annex reads the stored logs for errors: lines at level `error` (an unhandled exception, `log.exception`) and Python tracebacks an app printed, in web apps and in scheduled jobs. Errors that differ only in numbers, ids, quoted values or file paths are one *kind*; a traceback's kind is its exception type plus the last frame's file and function. The first time a kind appears, the owner (and `alerts.to`, and admins) get **one email**; more of the same are counted, not emailed. You hear again if it returns after a quiet day (`error_quiet_s`, 24 h) or in a newer release than it was last seen in. At most 5 error emails an hour per app; the rest come in one summary. `annex errors <app>` lists the kinds, `active` or `quiet` (a newer release is live and it hasn't come back). It is a notification, not an incident: nothing "resolves".
- **Browser errors:** a page that includes `<script src="/annex-errors.js"></script>` reports uncaught JavaScript errors and unhandled promise rejections (at most 5 a page load) to `POST /api/_errors` on its own app, which logs one `error` line with `source=browser`, the user's login and the request id, so they are grouped like server errors. `/annex-errors.js` and `/api/_errors` are reserved paths of the web templates (keep them; the `static` template has them in `server.py` and `public/`). The report holds the message, the stack and the page's path, never its query string.
- **Annex keeps your logs for 14 days**, also after the container is gone (a rolled-back release's lines, last week's errors). `annex logs <app>` searches them: by release (`--release r4`), level (`--level warn` means warn and error), text in the message or fields (`--grep`), request id (`--request`) and age (`--since 2h`). A scheduled job's output is kept too. Before a field is stored, anything whose name looks secret (`password`, `token`, `secret`, `authorization`, `cookie`, `api_key`, ...) is removed and `password=...` style text is redacted: a safety net, not permission to log them. Lines over 8 KB are cut, and a container writing more than 200 lines in a second keeps the first 200 (one line says how many were dropped). The last moments of a container that is being replaced can be missed.

## Bring your own stack
The templates are the fast path, not a requirement. Any language or framework works if it runs as **one container** and follows the runtime rules above. Write your own `Dockerfile` and `annex.yaml` and check:

- **Web:** serve HTTP on `0.0.0.0:$PORT`. The `healthcheck` path must return 200 without a login. Annex calls it directly, not through the front door.
- **User:** end the Dockerfile with a non-root `USER`. Annex runs the app as uid 10001 regardless, so the files the app reads must be readable by any user. Files added with `COPY` are, by default.
- **Health:** a Docker `HEALTHCHECK` is optional but recommended, because `annex list` shows its state.
  - Use a command the image actually has; slim images have no `curl`.
  - If there is one, the deploy waits for it to report healthy.
- **Logging:** write JSON lines to stdout in the format of the Logging section above (`ts`, `level`, `msg`), and return an `X-Request-Id` header. Libraries' plain-text startup lines are fine as long as your own lines follow it.
- **Read-only filesystem:** set `ENV HOME=/tmp`, and point caches and temp files at `/tmp`.
- **Build in the image:** do everything at image build time: install dependencies, compile, bundle, collect static files. Nothing may be installed or built at startup.
- **Memory:** the default limit is 256 MB. Raise it with `resources.memory` (max `512m`) only if the app needs more. Size runtime heaps to fit, e.g. a JVM with `-Xmx` at about 70 % of the limit, or Node with `--max-old-space-size`.
- **Identity on the server:** read `Remote-*` headers in your server code. A browser-only app can't see request headers, so serve a small endpoint such as `/api/me` that returns them.
- **Gateway token stays on the server:** call the gateway from your server. Never send `ANNEX_GATEWAY_TOKEN` to the browser. The token changes on every deploy and expires after a year, so read it from the environment at startup; never store it in a file, the database or the code.
- **Storage:**
  - Postgres (`DATABASE_URL`) and files under `/data` are the only storage. A SQLite file is fine if it lives in `/data`.
  - There's no MySQL, MongoDB or Redis, and no second container.
  - If the tool needs those, it's a candidate to move to a Kaldi project.
- **Upload size:** the app folder is uploaded and built on the server (x86_64). Keep it under 50 MB compressed: add `node_modules`, `.venv`, `.next`, `dist` and data files to `.dockerignore`. Pin the base image version.

Common stacks:

| Stack | What to do |
|---|---|
| Vite / React / Vue single-page app (also Lovable exports) | Build to `dist/` and serve it with the `static` template's server: copy the build output into `public/`. You get `api/me` and `api/data/<view>` for free. Supabase calls must be replaced with those. The server has no fallback to `index.html`, so use hash routing (`/#/clients/42`) or a reload on a deep link returns 404. |
| Next.js | `output: "standalone"`, `next build` in the Dockerfile, run `node server.js`, `NEXT_TELEMETRY_DISABLED=1`. Runtime caches (image optimisation, ISR) write to `.next/cache`, which is read-only: turn them off (`images: {unoptimized: true}`). Usually needs `memory: 384m` or more. |
| Streamlit | `ENV HOME=/tmp`; run `streamlit run app.py --server.port $PORT --server.address 0.0.0.0 --server.headless true --browser.gatherUsageStats false`; `healthcheck: /_stcore/health`. Read `Remote-User` with `st.context.headers`. |
| Django / Flask | Run with gunicorn on `0.0.0.0:$PORT`. Run `collectstatic` in the Dockerfile, and serve static files with WhiteNoise. Set `PYTHONDONTWRITEBYTECODE=1`. |
| Go, Rust, .NET, Java | Multi-stage build; copy only the binary or app into a slim runtime image. |

If the deploy rejects something, the error says what to change.

## Injected environment
| Variable | When |
|---|---|
| `PORT` | web, web+job |
| `ANNEX_APP_NAME`, `ANNEX_BASE_URL` | always (`ANNEX_BASE_URL` is the app's public URL) |
| `LOG_LEVEL` | always (default `info`; the one injected name you may override under `env:`: debug, info, warn or error) |
| `DATABASE_URL` | `storage.postgres: true` (`postgres://…@appdata-db:5432/app_<name>`) |
| `DATA_DIR=/data` | `storage.files: true` |
| `ANNEX_GATEWAY_URL`, `ANNEX_GATEWAY_TOKEN` | `data.views` is not empty |
| `ANNEX_OPS_URL`, `ANNEX_OPS_TOKEN` | `notify.email` is true or `notify.slack` is not empty |
| `ANNEX_HOOKS_URL`, `ANNEX_HOOK_TOKEN` | `webhooks` is not empty (`ANNEX_HOOKS_URL` is the public base: `…/h/<app>`) |
| each declared secret | `secrets` |

## Gateway views (allowlist)
Two synthetic data sets. `GET $ANNEX_GATEWAY_URL/` with your token returns an OpenAPI description of your app's views, with every column explained (Goat Arcade's views have one).

**Wealth management** (the product database):

| View | Columns |
|---|---|
| `client_summary` | client_id, client_name, segment, risk_profile, country, advisor_name, account_count, aum_eur, onboarded_on |
| `top_clients_by_aum` | rank, client_id, client_name, segment, advisor_name, aum_eur (top 25) |
| `monthly_positions` | month_end, client_id, client_name, account_id, symbol, instrument_name, asset_class, quantity, price, market_value_eur |
| `advisor_book` | advisor_name, clients, aum_eur, avg_client_aum_eur |

**Goat Arcade** (a chain of pinball and retro arcades; the client's own database, read through the gateway; the data covers the last 180 days):

| View | Columns |
|---|---|
| `venues` | venue_code, venue_name, city, country, lat, lon, opened_on, opening_hours (JSON by weekday), machines, pinball_machines |
| `machine_models` | model_id, title, manufacturer, manufacturer_country, release_year, era (EM, SS, DMD, LCD), era_name, machine_type (pinball, video, other), theme, designer, flippers, max_multiball, ramps, modes, toys, cabinets_on_floor |
| `machine_floor` | machine_id, serial_no, venue_code, venue_name, city, model_id, title, manufacturer, release_year, era, machine_type, condition, price_per_play_eur, installed_on, games_7d, games_30d, last_played_at, open_ticket, out_of_order, last_service_at |
| `leaderboard` | model_id, title, rank (1-10), handle, nickname, score, venue_code, played_at, machine_id |
| `player_profiles` | player_id, handle, nickname, home_venue_code, member_since, rating, games_180d, venues_played, last_played_at, leaderboard_spots, best_rank |
| `daily_plays` | day, venue_code, machine_id, title, era, machine_type, games, credits, revenue_eur, avg_duration_s, tilts, members |
| `machine_events_daily` | day, machine_id, title, current_venue_code, event_type (tilt, slam_tilt, ball_search, coil_fault, switch_fault, jackpot, replay, match), events |
| `service_log` | ticket_id, machine_id, serial_no, title, current_venue_code, opened_at, closed_at, status, symptom, resolution, reported_by, took_machine_off_floor, downtime_hours, technician, labour_hours, parts_cost_eur, parts |
| `revenue_weekly` | week_start, venue_code, venue_name, city, amount_eur, coins_eur, tokens, card_plays, machines |
| `tournaments` | tournament_id, name, venue_code, venue_name, starts_on, format, status, entry_fee_eur, max_players, entries, winner_handle, winner_nickname |
| `tournament_standings` | tournament_id, tournament_name, starts_on, position, handle, nickname, points, games_played, best_score |

Views can't be joined on the gateway: read each one and join in your code, on venue_code, machine_id, model_id or nickname. The gateway doesn't group or sum either, so the views come pre-aggregated (per day, per week); filter them (`?day=gte.2026-10-01`) rather than reading everything.

All data is synthetic. Adding a view is a reviewed change by the platform team, in `db/product/api_views.sql` (wealth) or `db/product/clientlink/api_views_arcade.sql` (Goat Arcade).

## Commands
| Command | Does |
|---|---|
| `annex init <template> <name> [--owner login]` | copy a template |
| `annex deploy [path]` | validate, provision DB/role/token, build, run, register |
| `annex list` | list deployed apps |
| `annex status <app>` | an app's state, health check, open incident, last job runs, config and recent logs |
| `annex errors [app] [--state active\|quiet]` | the kinds of error seen in the logs: state, count, first and last seen, release, sample |
| `annex logs <app> [-n 100] [--since 2h] [--release r4] [--level warn] [--grep text] [--request id] [-f]` | an app's stored logs (14 days, also of containers that are gone); `-f` follows the live container |
| `annex run <app> <job>` | trigger a job now |
| `annex hooks <app>` | incoming webhook events of an app (metadata only); **human only:** `annex hooks show <id>`, `annex hooks replay <app> ...` |
| `annex usage [app]` | users, requests, errors and response times per app (totals; with an app, by day) |
| `annex sends [app] [-n 50]` | email and Slack send log: every attempt, rejections too, no bodies |
| `annex releases <app>` | an app's releases, and which can be rolled back to |
| `annex rollback <app> [--to rN]` | switch back to an earlier release (default: the one before), with that release's manifest |
| `annex rollback <app> --with-data` | **human only**: also restore the database and `/data` from that release's time; writes since are lost |
| `annex secret set <app> KEY=VALUE` | **human only**: store a secret on the server |
| `annex remove <app> [--keep-data]` | **human only**: remove an app |
| `annex users list\|add\|set\|remove\|reset` | **human only**: Annex's own user accounts (an app's `owner` must be one of them) |
| `annex contract` | print this contract |

Add `--json` before the command for machine-readable output. When a deploy fails, the error says what to fix. Fix it and deploy again.
