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
| Email and Slack sends of an app | `./annex sends <app> [-n 50]` (every attempt, rejections too; never the body) |
| Set a secret | `./annex secret set <app> KEY=VALUE` |
| Remove an app | `./annex remove <app> [--keep-data]` |
| Back up now | `scripts/backup.sh` (nightly at 02:00 anyway; 7 days kept) |
| Restore one app | `scripts/restore.sh <app> [archive]` (replaces its DB and `/data`) |
| Checks | `scripts/test-platform.sh` (M0.1), `scripts/test-apps.sh` (M0.2), `scripts/test-jobs.sh` + `scripts/restore-test.sh` (M0.3), `scripts/test-rollback.sh` (M1.1), `scripts/test-notify.sh` (M1.2) |
| Add a gateway view | edit `db/product/api_views.sql`, run `scripts/push.sh`, and add it to the table in `cli/src/annex_cli/contract.md` |

`./annex` runs the CLI from the repo through uv. To put `annex` on your PATH, see `docs/setup.md` §6.

## Where things live on the VM (`/srv/annex`)
| Path | What |
|---|---|
| `platform/` | synced copy of the rendered repo bundle (compose, config, db, vm, ops); `platform/.env` holds the settings |
| `secrets/` | `master.env`, per-service env files (`ops.env`; `slack.env` once Slack is connected), `authelia/`, `users.txt`, `apps/<app>/` (db_password, gateway_token, ops_token, secrets.env, runtime.env) |
| `state/` | Postgres data directories, Authelia's sqlite, `mailpit/` (caught mail), `directory/users.json` (login → name, email, groups, no passwords; read by ops) |
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
