# Operations

## Day to day (from the Mac, in the repo)
| Task | Command |
|---|---|
| Change platform config | edit, then `scripts/push.sh` (add `--extras` to include Metabase and Dozzle) |
| Metabase/Dozzle on or off | `scripts/extras.sh on` / `scripts/extras.sh off` |
| Browser access | `scripts/tunnel.sh up`, then https://home.annex.home.arpa:8443 |
| Demo passwords | `scripts/show-users.sh` |
| Deploy an app | `./annex deploy examples/client-lookup` |
| Inspect apps | `./annex list`, `./annex status <app>`, `./annex logs <app> -f` |
| Run a job now | `./annex run <app> <job>` |
| Releases of an app | `./annex releases <app>` |
| Undo a deploy | `./annex rollback <app> [--to rN]`; add `--with-data` to also restore its database and `/data` from that release's time |
| Is an app healthy? Any open incident? | `./annex status <app>` (health check, open incident, last run of each job) |
| All incidents, or the open ones | `ssh annex docker exec annex-ops-1 python -m annex_ops.ctl incidents [--open] [--app X]` |
| Hold alerts while you do planned work | `ssh annex docker exec annex-ops-1 python -m annex_ops.ctl maintenance app:<name> --minutes 30` (`--end` releases it) |
| Email and Slack sends of an app | `./annex sends <app> [-n 50]` (every attempt, rejections too; never the body) |
| Set a secret | `./annex secret set <app> KEY=VALUE` |
| Remove an app | `./annex remove <app> [--keep-data]` |
| Back up now | `scripts/backup.sh` (nightly at 02:00 anyway; 7 days kept) |
| Restore one app | `scripts/restore.sh <app> [archive]` (replaces its DB and `/data`) |
| Checks | `scripts/test-platform.sh` (M0.1), `scripts/test-apps.sh` (M0.2), `scripts/test-jobs.sh` + `scripts/restore-test.sh` (M0.3), `scripts/test-rollback.sh` (M1.1), `scripts/test-notify.sh` (M1.2), `scripts/test-alerts.sh` (M1.3, 12-20 minutes) |
| Add a gateway view | edit `db/product/api_views.sql`, run `scripts/push.sh`, and add it to the table in `cli/src/annex_cli/contract.md` |

`./annex` runs the CLI from the repo through uv. To put `annex` on your PATH, see `docs/setup.md` §6.

## Where things live on the VM (`/srv/annex`)
| Path | What |
|---|---|
| `platform/` | synced copy of the rendered repo bundle (compose, config, db, vm, ops, images); `platform/.env` holds the settings |
| `secrets/` | `master.env`, per-service env files (`ops.env`; `slack.env` once Slack is connected), `authelia/`, `users.txt`, `apps/<app>/` (db_password, gateway_token, ops_token, secrets.env, runtime.env) |
| `state/` | Postgres data directories, Authelia's sqlite, `mailpit/` (caught mail), `directory/users.json` (login → name, email, groups, no passwords; read by ops), `gatus/apps/<app>.yaml` (one health check per web app, written by `annex deploy`), `gatus/data` (Gatus's history), `ops/tuning.json` (only during tests) |
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
- **Limits:** if the VM itself dies, nothing on it can say so: that needs a check from outside the LAN (production item). Any container on `annex-edge` can read Gatus's API (which apps are up) without logging in.
