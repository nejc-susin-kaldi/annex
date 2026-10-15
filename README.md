# Kaldi Annex: M0 homelab prototype

A small platform where builders (and their AI coding agents) deploy internal tools behind login onto one client's server. This repo targets a disposable VM, `annex`, on Nejc's homelab. The brief is in [docs/brief.md](docs/brief.md), and decisions are in [docs/decisions.md](docs/decisions.md).

## Quick start (from the Mac)
```bash
cp .env.example .env          # then adjust; ANNEX_PUBLIC_PORT=8443 for tunnel testing
scripts/bootstrap.sh          # VM layout, secrets, demo users, mkcert cert (idempotent)
scripts/push.sh --extras      # sync and start the platform, incl. Metabase and Dozzle
scripts/test-platform.sh      # M0.1 checks through an SSH tunnel
scripts/show-users.sh         # demo user passwords
```
For browser access, see [docs/setup.md](docs/setup.md) §3. The management demo is in [docs/demo.md](docs/demo.md); teardown is in [docs/teardown.md](docs/teardown.md). Day-to-day commands are in [docs/operations.md](docs/operations.md).

## Deploying apps
```bash
./annex init python-web my-tool --owner sam   # copy a template
./annex deploy my-tool                         # validate, provision, build, run, register
./annex list
./annex releases my-tool                       # release history; ./annex rollback my-tool undoes a deploy
```
The app contract is in [docs/app-contract.md](docs/app-contract.md), or run `./annex contract`. Example apps: `examples/client-lookup` (web, gateway) and `examples/monthly-report` (web+job, Postgres, files). Templates: `python-web`, `python-job`.

## What runs
| Service | Host | Who |
|---|---|---|
| Homepage (discovery) | `home` | everyone signed in |
| Authelia (login) | `auth` | everyone |
| Metabase (extras) | `metabase` | everyone signed in; its own login (D9) |
| Dozzle (extras) | `logs` | admins |
| PostgREST gateway | `gateway` | apps with a JWT; admins via browser |
| Mailpit (caught outgoing email) | `mail` | admins |
| Gatus (status page and uptime checks) | `status` | everyone signed in |

Plus annex-ops (`ops/`: email and Slack for apps, incidents and alerts, usage stats and the weekly digest, no public route), product-db and appdata-db (Postgres 16), Ofelia (jobs), offen backup (nightly, 7 days) and two Docker socket proxies. Hostnames come from `ANNEX_HOST_PATTERN`, `{name}.annex.home.arpa` by default.

## Scripts
| Script | Does |
|---|---|
| `scripts/bootstrap.sh` | prepares `/srv/annex`, generates secrets on the VM, pushes the cert in `local` TLS mode |
| `scripts/push.sh [--extras]` | renders config, rsyncs to `/srv/annex/platform`, `docker compose up --wait`, applies gateway views, configures Metabase |
| `scripts/extras.sh on\|off` | Metabase and Dozzle on or off |
| `scripts/tunnel.sh up\|down` | SSH tunnel `127.0.0.1:8443 → annex:443` |
| `scripts/test-platform.sh` | M0.1 acceptance checks |
| `scripts/backup.sh` | run the nightly backup now |
| `scripts/restore.sh <app> [archive]` | restore one app's database and files |
| `scripts/test-apps.sh`, `test-jobs.sh`, `restore-test.sh` | M0.2/M0.3 checks |
| `scripts/test-rollback.sh` | M1.1 checks: automatic rollback, `annex rollback`, `--with-data` |
| `scripts/test-notify.sh` | M1.2 checks: email through Mailpit, Slack allowlist, send log, grants |
| `scripts/test-usage.sh` | M1.4 checks: a scripted session shows the same numbers in `annex usage`, the tiles and Metabase; privacy, retention, restart, digest |
| `scripts/seed-usage.sh [--clear]` | optional demo prep: 8 weeks of synthetic usage history for the dashboard |
| `scripts/test-alerts.sh` | M1.3 checks: stop, crash loop, 500 burst, failing job and platform outage each give one alert and one "resolved" (12-20 minutes) |
| `scripts/reset-demo.sh` | clean demo state: example apps redeployed, other apps removed, checks run (see docs/demo.md) |
| `scripts/wipe-vm.sh` | removes everything Annex from the VM (asks first) |
| `scripts/show-users.sh` | prints demo user passwords |
