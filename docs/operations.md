# Operations

## Day to day (from the Mac, in the repo)
| Task | Command |
|---|---|
| Change platform config | edit, then `scripts/push.sh` (add `--extras` to include Metabase and Dozzle) |
| Metabase/Dozzle on or off | `scripts/extras.sh on` / `scripts/extras.sh off` |
| Browser access | `scripts/tunnel.sh up`, then https://home.annex.home.arpa:8443 |
| Demo passwords | `scripts/show-users.sh` (the three demo users only) |
| User accounts | `./annex users list`, `./annex users add <login> --name '…' --email … [--groups users,builders,admins]` (emails an invitation), `./annex users set <login> [--groups …] [--disable\|--enable]`, `./annex users reset <login>`, `./annex users remove <login>` (all human only) |
| Deploy an app | `./annex deploy examples/client-lookup` |
| Inspect apps | `./annex list`, `./annex status <app>`, `./annex logs <app> -f` |
| Run a job now | `./annex run <app> <job>` |
| Releases of an app | `./annex releases <app>` |
| Undo a deploy | `./annex rollback <app> [--to rN]`; add `--with-data` to also restore its database and `/data` from that release's time |
| Is an app healthy? Any open incident? | `./annex status <app>` (health check, open incident, last run of each job) |
| All incidents, or the open ones | `ssh annex docker exec annex-ops-1 python -m annex_ops.ctl incidents [--open] [--app X]` |
| Hold alerts while you do planned work | `ssh annex docker exec annex-ops-1 python -m annex_ops.ctl maintenance app:<name> --minutes 30` (`--end` releases it) |
| Incoming webhook events of an app | `./annex hooks <app>` (metadata only); `./annex hooks show <id>` (headers and body: admins), `./annex hooks replay <app> --id N \| --failed \| --since 1h` (asks first) |
| Who uses what | `./annex usage` (every app, last 7 days), `./annex usage <app>` (by day), `./annex usage <app> --by-user` (per person: admins only) |
| Email and Slack sends of an app | `./annex sends <app> [-n 50]` (every attempt, rejections too; never the body) |
| Set a secret | `./annex secret set <app> KEY=VALUE` |
| Remove an app | `./annex remove <app> [--keep-data]` |
| Back up now | `scripts/backup.sh` (nightly at 02:00 anyway; 7 days kept) |
| Restore one app | `scripts/restore.sh <app> [archive]` (replaces its DB and `/data`) |
| Quick check after a push | `scripts/test-smoke.sh` (about 2 minutes: every part once) |
| All checks | `scripts/test-all.sh` (30+ minutes; `--slow` adds the 20-minute `test-alerts.sh`; `--only platform,apps` for a few). Only when a change concerns timings, retries or alerts |
| Host health as ops sees it | `ssh annex docker exec annex-ops-1 python -m annex_ops.ctl host` (disk, memory, newest backup's age) |
| Checks one by one | `scripts/test-platform.sh` (M0.1), `scripts/test-apps.sh` (M0.2), `scripts/test-jobs.sh` + `scripts/restore-test.sh` (M0.3), `scripts/test-rollback.sh` (M1.1), `scripts/test-notify.sh` (M1.2), `scripts/test-alerts.sh` (M1.3, 12-20 minutes), `scripts/test-usage.sh` (M1.4), `scripts/test-hooks.sh` (M1.5), `scripts/test-host.sh` (disk, memory, backup alerts), `scripts/test-users.sh` (accounts) |
| Add a gateway view | edit `db/product/api_views.sql`, run `scripts/push.sh`, and add it to the table in `cli/src/annex_cli/contract.md` |

`./annex` runs the CLI from the repo through uv. To put `annex` on your PATH, see `docs/setup.md` §6.

## Where things live on the VM (`/srv/annex`)
| Path | What |
|---|---|
| `platform/` | synced copy of the rendered repo bundle (compose, config, db, vm, ops, images); `platform/.env` holds the settings |
| `secrets/` | `master.env`, per-service env files (`ops.env`, `hooks-receiver.env`; `slack.env` once Slack is connected), `hooks/<app>.json` (an app's webhook signing secrets and token; owned by uid 10001, read by ops), `authelia/` (`users.yml`: the accounts, managed with `annex users`; Authelia rewrites it when someone resets or changes a password), `users.txt` (the demo users' generated passwords), `apps/<app>/` (db_password, gateway_token, ops_token, secrets.env, runtime.env) |
| `state/` | Postgres data directories, Authelia's sqlite, `mailpit/` (caught mail), `directory/users.json` (login → name, email, groups, no passwords; read by ops), `gatus/apps/<app>.yaml` (one health check per web app, written by `annex deploy`), `gatus/data` (Gatus's history), `ops/tuning.json` (only during tests; the usage settings and webhook retry delays can be overridden there too) |
| `deploy/<app>/` | app source (`src/`), generated `compose.yaml`, `deploy.json` (what's running now) |
| `apps/<app>/data` | app files, mounted at `/data`, backed up |
| `apps/<app>/releases.jsonl` | release history: image, manifest, who, when, git commit, result (backed up) |
| `snapshots/<app>/pre-r<N>/` | database dump and `/data` archive taken before release N went live; the last 3 are kept (not backed up) |
| `dumps/`, `backups/` | nightly pg_dump output and backup archives (7 days) |
| `certs/` | mkcert wildcard certificate (local TLS mode) |

## Logs
- Apps: `./annex logs <app>`, or Dozzle at `logs.` (admins).
- Gateway access per app: `ssh annex docker logs annex-product-db-1 2>&1 | grep annex-gateway`.
- Front door: `ssh annex docker logs annex-traefik-1` (JSON access log), `annex-authelia-1` (login decisions).

## Jobs
- Ofelia runs each `schedule` entry inside the app's container as uid 10001. See runs with `ssh annex docker logs annex-ofelia-1`, which shows start, stdout and duration.
- `./annex run <app> <job>` runs the same command now and prints its output and exit code.
- Ofelia reads job labels only at startup; the CLI restarts it after deploys that change schedules (D18).

## Releases and rollback
- Each `annex deploy` builds `annex-app/<app>:r<N>`. Before switching, it dumps the app's database and archives `/data` (if under 200 MB) into `snapshots/<app>/pre-r<N>`.
- **Health gate:** the new release must pass its health check within 60 s (`ANNEX_HEALTH_TIMEOUT` on the Mac changes this). If it doesn't, the previous release is switched back in automatically, with its manifest (views, storage, schedule), and the deploy fails with the new release's log lines.
- `annex rollback` does the same switch by hand and records it as a new release. `--with-data` also restores the snapshot from the target release's time. A snapshot of the current data is taken first, so a rollback can itself be undone with `annex rollback --to rN --with-data`.
- Kept per app: the images of the last 3 live releases and the last 3 snapshots. `annex remove` deletes both; with `--keep-data`, the release history and snapshots stay.

## Email and Slack (annex-ops)
- Apps call `ANNEX_OPS_URL` (`http://ops:8000`, edge network only, no public route) with their `ANNEX_OPS_TOKEN`. What a token may do comes from `notify:` in the app's `annex.yaml`; it's stored hashed in `annex_ops.app_grants` (database `annex_ops` in appdata-db, in the nightly backup).
- **Prototype mail goes to Mailpit** (`mail.` host, admins only): nothing leaves the VM. Mailpit keeps the last 500 messages in `state/mailpit`. To send to real addresses, a real SMTP relay replaces Mailpit (set `OPS_SMTP_HOST`/`OPS_SMTP_PORT` for ops in `compose.yaml`) and the client approves the recipient domains and addresses.
- **Who can receive:** logins from `state/directory/users.json` and addresses in `ANNEX_EMAIL_DOMAINS` (default `annex.example`) or `ANNEX_EMAIL_ALLOW` (exact extra addresses). Both are in `.env`; change them and run `scripts/push.sh`. A new demo user is picked up without a restart once `scripts/bootstrap.sh` has rewritten `users.json`.
- **Limits:** 60 emails and 60 Slack posts per hour per app (`OPS_EMAIL_PER_HOUR`, `OPS_SLACK_PER_HOUR` in ops's environment). Request bodies over 256 KB are refused.
- **The send log:** `./annex sends [app]`, or `ssh annex docker exec annex-ops-1 python -m annex_ops.ctl sends|grants|info`. It holds app, recipients, subject, time, status (`sent`, `rejected`, `failed`, `unavailable`) and the error, never a body.
- **Connect Slack:** `docs/setup.md` §7. Until then a post to an allowed channel returns 503 "isn't connected" and is logged as `unavailable`. Replacing the token is the same steps.
- **Ops logs:** `ssh annex docker logs annex-ops-1` (request lines only: no tokens, no bodies).

## Status page and alerts (M1.3)
- **Status page:** `status.` host (Gatus), visible to everyone signed in, plus a tile with live numbers on the discovery page. It lists every web app (checked every 60 s at `http://annex-app-<app>:<port><healthcheck>`) and the platform's front door (an anonymous request to the home page must answer 302 to the login). The check for an app is the file `state/gatus/apps/<app>.yaml`, written when the app deploys and deleted when it's removed. Gatus never notices a deleted file by itself, so the CLI restarts it when a file changes.
- **What raises an alert** (signals, in annex-ops): health check failing (2 checks in a row), container stopped for over 60 s, crash loop (3 non-zero exits in 10 min), container health check unhealthy, out of memory, error spike (at least 10 5xx and at least 5 % of an app's requests in 5 min), and a scheduled job failing (read from Ofelia's log: `annex run` is not an alert source, its caller sees the exit code).
- **One alert per problem:** signals attach to an *incident* per subject (`app:<name>`, `job:<app>/<job>`, `platform:<service>`). The first signal sends the alert, the rest are added silently, and "resolved" goes out when all have cleared (a stopped app that both Docker and Gatus notice is still one alert). A reminder follows after 1 h open, then every 24 h. Deploys, rollbacks, `secret set` and restarts that finish within 60 s alert nobody. Removing an app, or turning the extras off, closes its incidents without a message. Metabase and Dozzle being stopped is never alerted; being unhealthy while running is.
- **Who gets what:** app problems go to the app's owner (the `com.kaldi.annex.owner` label, turned into an email by the user directory), anyone in `alerts.to`, and every user in group `admins`. Platform problems go to admins only. With Slack connected, everything is also posted to `#annex-alerts`. An owner who isn't in the directory means the alert goes to admins with a note.
- **Maintenance:** `restore.sh` and `rollback --with-data` hold the app's alerts for up to 15 minutes while its container is stopped on purpose. For other planned work use `ctl maintenance` (see the table above): signals are collected and nothing is sent until it ends; any that are still active then open the incident.
- **Tuning:** thresholds are defaults in `ops/annex_ops/engine.py` (`Tuning`), overridable by `OPS_<NAME>` environment variables on ops and, at runtime, by `/srv/annex/state/ops/tuning.json` (`stopped_grace_s`, `crash_count`, `crash_window_s`, `crash_stable_s`, `error_window_s`, `error_min`, `error_ratio`, `reminder_first_s`, `reminder_repeat_s`, `tick_s`). Production never has the file; `scripts/test-alerts.sh` writes a short one and removes it. If an alert storm ever needs silencing, put a long `reminder_first_s` there, or open maintenance for the subject.
- **State:** the `annex_ops` tables `incidents` (with the full history of signals), `job_runs` (90 days), `log_cursors` (where ops stopped reading Traefik's and Ofelia's logs) and `maintenance`. ops restores open incidents at start and re-reads Docker's state, so a push (which recreates ops) doesn't lose or repeat alerts.
- **The VM itself** (D56): its disk (alert at 85 % used, cleared below 80 %), its memory (alert when under 256 MB stays available for 5 minutes, cleared above 400 MB) and the age of the newest backup (alert when over 26 hours; it's the archive folder's time, so a manual `scripts/backup.sh` counts) are sampled every minute and alerted to admins like any platform problem, with what to check in the message. `ctl host` shows the current numbers. Settings `disk_alert_pct`, `disk_clear_pct`, `mem_alert_mb`, `mem_alert_s`, `mem_clear_mb`, `backup_max_age_s`, `host_sample_s` in `Tuning`.
- **Limits:** if the VM itself dies, nothing on it can say so: that needs a check from outside the LAN (production item). Any container on `annex-edge` can read Gatus's API (which apps are up) without logging in.

## Usage stats and the weekly digest (M1.4)
- **What is counted:** a signed-in page request on an app's router, taken from Traefik's access log, which annex-ops already follows (one follower feeds both the alert engine and usage). Not counted: the app's health path (`com.kaldi.annex.healthcheck` label), static assets (`.js .mjs .css .map .png .jpg .jpeg .gif .svg .ico .webp .woff .woff2 .ttf`), anonymous requests (the login redirects) and platform routers. Apps change nothing.
- **Where it shows:** the app's discovery tile ("Users 7d" with a trend, and its status; job-only apps show "Last run"), `annex usage`, `annex status`, the **"Annex usage"** Metabase dashboard (collection "Annex"; the "Usage" tile on the discovery page leads to it), and the Monday digest.
- **Storage:** raw rows in `annex_ops.requests` for 30 days (`usage_retention_days`), never with a query string, deleted by the rollup. Daily and weekly summaries without logins (`usage_daily`, `usage_weekly`) are kept indefinitely; days and ISO weeks (Monday start) are in Europe/Ljubljana. The rollup runs every 10 minutes (`ctl rollup` runs it now). Removed apps disappear from `annex usage`, the tiles and the dashboard's table; their history stays in the summaries (`annex usage <app>` still shows it).
- **Logins** are in `requests` and nowhere else. They are shown only by `annex usage <app> --by-user` (personal data: admins; in the prototype, CLI access is SSH access). Metabase connects as `metabase_usage`, which can read the summaries and the `usage_apps` view and nothing else (no `requests`, `sends`, `incidents` or `app_grants`). The tile endpoint (`GET /tiles/<app>` on ops, called by Homepage from its own server) has no token and returns totals and status only; apps on the edge network can read it too.
- **Signals** (rules in `ops/annex_ops/usage.py`, written by the rollup to `app_signals`): *graduation candidate* = the last 4 complete weeks never fell, the latest had at least 5 users and more than the first; *dormant* = no signed-in request for 30 days and Annex first saw the app at least 30 days ago.
- **The digest:** Mondays at 08:00 (Europe/Ljubljana) by email: admins get every app, each owner their own apps. It covers the last complete week, with the 4-week trend, error rate, p95 and the last 7 days, and lists graduation candidates and dormant apps. It is sent once per week (`annex_ops.digests`), also if ops restarts that day; a fresh install on another weekday does not send one at once. Preview it without sending: `ssh annex docker exec annex-ops-1 python -m annex_ops.ctl digest [--to <login>] [--dormant-days N --min-age-days N]`; add `--send` to send it now.
- **Settings** (defaults in `ops/annex_ops/usage.py`, `UsageSettings`; override with `OPS_<NAME>` environment variables on ops or in `state/ops/tuning.json`): `usage_retention_days` 30, `graduation_min_users` 5, `graduation_weeks` 4 (2 to 4), `dormant_days` 30, `rollup_s` 600, `digest_weekday` 0, `digest_hour` 8.
- **Dashboard maintenance:** `push.sh` creates what's missing (connection, collection, cards, dashboard) from `config/metabase/usage-dashboard.json` and leaves existing cards alone. To rebuild them after editing the spec: `ssh annex ANNEX_DATA_DIR=/srv/annex bash /srv/annex/platform/vm/metabase-setup.sh --rebuild-usage`.
- **Demo history (optional):** `scripts/seed-usage.sh` writes 8 weeks of synthetic history (marked `synthetic`, weeks 3 to 10 back) so the dashboard has lines; `--clear` removes it. See the note in the script about graduation.

## Incoming webhooks (M1.5)
- **The hooks host:** `hooks.<domain>` (`ANNEX_HOST_HOOKS`) is the **only route without a login**. A webhook is `https://hooks.<domain>[:port]/h/<app>/<hook>`, declared in the app's `annex.yaml` (`webhooks:`); `annex deploy` and `annex status` print each URL. Traefik routes only `/h/` there (rate limit 20/s per source, burst 40; 1 MB body cap) to the **receiver**, its own container (`annex-hooks-1`, D55): the ops image with only the hooks files and a database role that may only insert events, so the internet-facing process holds no SMTP access, Slack token, user directory or Docker access. It refuses everything outside `/h/`. Answers on that host are terse (`{"error": "signature check failed"}`); the reasons are in `annex hooks` and the receiver's log (`docker logs annex-hooks-1`).
- **What happens to a call:** the receiver checks the provider's signature on the raw body (`trello`, `stripe`, `github`, `slack` or `shared-secret`; the secret is an app secret), stores the event and answers the provider at once; a worker in annex-ops forwards it to the app's endpoint with `X-Annex-Hook-Token`, `-Event`, `-Attempt` and `-Hook` headers. Bad signatures are rejected, logged (reason, no body) and never forwarded; at most 60 verified events per minute per hook.
- **Retries:** after 10 s, 30 s, 1 min, 5 min, 15 min, then hourly, until the event is 7 days old (then `failed`). Within one hook, events are delivered oldest first: a failing event holds back newer ones of that hook. Delivery is at least once, so apps dedupe by `X-Annex-Hook-Event`. If a hook's oldest event has been failing for 15 minutes, the app's incident gets a `webhook` signal (same alert and "resolved" as any other problem).
- **Retention and privacy:** event bodies are kept 7 days (needed for retries and replay), so they are in the nightly database dump for that long; then they are deleted. Bodies are shown only by `annex hooks show <id>` (human only); rejected requests have no body.
- **Replay:** `annex hooks replay <app> --id N` (or `--failed`, `--since 1h`) queues copies; the app sees the same `X-Annex-Hook-Event` and attempt 1 (it asks you to confirm: the app will process the events again).
- **Rotating a signing secret:** `annex secret set <app> TRELLO_SECRET=<new>`. It regenerates the hooks file the receiver and ops read (picked up on the next request) and restarts the app; then update the secret at the provider. Events signed with the old secret are rejected in between.
- **Settings:** retry steps (`hook_backoff_s`), the age after which events fail (`hook_max_age_s`) and the alert delay (`hook_alert_after_s`) are in `Tuning` (`ops/annex_ops/engine.py`) and overridable in `state/ops/tuning.json`.
- **Production:** this is the only internet-facing route. It will want a WAF or the providers' published IP ranges in front of it, and its own monitoring and alerting.

## Accounts: Annex's own users (D57)
Annex doesn't need a company SSO: it has its own accounts (Authelia's file backend), managed by an admin from the Mac. Company SSO can be put in front later without changing apps (they only see the `Remote-*` headers).
- **Add someone:** `./annex users add ana --name 'Ana Kovač' --email ana@client.com --groups users`. They get an invitation email (Authelia's "reset your password" message; Mailpit in the prototype) with a link to set their own password, valid 24 hours. No password passes through the CLI. `--print-password` prints a random one once instead, for someone without an inbox.
- **Groups:** `users` (use tools), `builders` (deploy tools; the convention, not enforced yet), `admins` (everything, incl. Mailpit, Dozzle, the gateway's page, admin alerts and digests). `./annex users set ana --groups users,builders` replaces them; a changed group applies at the person's next sign-in.
- **Disable, enable, remove:** `--disable` stops sign-in at once and takes them out of the user directory (no alerts or emails), reversibly; `remove` deletes the account. Apps they own keep running: give them a new `owner` and deploy again. The last active admin can't be removed, disabled or demoted.
- **Passwords:** anyone can reset a forgotten password from the sign-in page ("Reset password", by email) or change it in Authelia's settings (`auth.` host → settings; a one-time code by email confirms it). Minimum 12 characters. An admin can send a reset link (`./annex users reset ana`) or set a random password (`--print-password`). Authelia allows 30 reset or invitation emails per 10 minutes from one address.
- **Where it lives:** `secrets/authelia/users.yml` (password hashes; the source of truth) and `state/directory/users.json` (login, name, email, groups of active accounts; what annex-ops reads for alerts, digests and logins as email recipients). `annex users` keeps both in step, and `push.sh` re-syncs the directory, so a password change by the user never needs anything from you.
- **Metabase** keeps its own accounts (D9): add one there too if the person needs Metabase.
- **The three demo users** (`sam`, `cm`, `kaldi-admin`) have their passwords in `secrets/users.txt` for `scripts/show-users.sh` and the test suites. `annex users reset <demo-user> --print-password` updates that file; if a demo user changes or resets their own password by email, the file no longer matches.

