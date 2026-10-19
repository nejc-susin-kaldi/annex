# Kaldi Annex: homelab prototype (M0 and M1)

A small platform where builders (and their AI coding agents) deploy internal tools behind login onto one client's server. This repo targets a disposable VM, `annex`, on Nejc's homelab. The brief is in [docs/brief.md](docs/brief.md), the later build plans in [docs/plans/](docs/plans/), and decisions are in [docs/decisions.md](docs/decisions.md).

## Quick start (from the Mac)
```bash
cp .env.example .env          # then adjust; ANNEX_PUBLIC_PORT=8443 for tunnel testing
scripts/bootstrap.sh          # VM layout, secrets, demo users, mkcert cert (idempotent)
scripts/push.sh --extras      # sync and start the platform, incl. Metabase and Dozzle
scripts/test-smoke.sh         # quick check that every part works (under a minute)
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
The app contract is in [docs/app-contract.md](docs/app-contract.md), or run `./annex contract`. Example apps: `examples/client-lookup` (web, gateway) and `examples/monthly-report` (web+job, Postgres, files). Templates: `python-web`, `python-job`, `node-web`, `static`; any other stack works too (the contract's "Bring your own stack").

People sign in with Annex's own accounts (no company SSO needed): `./annex users add ana --name 'Ana Kovač' --email ana@client.com` emails them an invitation. See [docs/operations.md](docs/operations.md#accounts-annexs-own-users-d57).

## What runs
| Service | Host | Who |
|---|---|---|
| Homepage (discovery) | `home` | everyone signed in |
| Authelia (login, Annex's own accounts, password reset and change) | `auth` | everyone |
| Metabase (extras) | `metabase` | everyone signed in; its own login (D9) |
| Dozzle (extras) | `logs` | admins |
| PostgREST gateway | `gateway` | apps with a JWT; admins via browser |
| Mailpit (caught outgoing email) | `mail` | admins |
| Console (read-only: an overview of what needs attention, a page per app with Summary, Logs, Errors, Releases, Usage and Config tabs, activity and platform for admins; its own container) | `console` | signed in: `/status` for everyone, an app's pages for its owner and admins |
| Incoming webhooks (the receiver, its own container; only `/h/`) | `hooks` | **nobody logs in**: callers are checked by signature |

Plus annex-ops (`ops/`: email and Slack for apps, incidents and alerts, host and backup monitoring, usage stats and the weekly digest, webhook delivery, stored logs and error groups), product-db and appdata-db (Postgres 16), Ofelia (jobs), offen backup (nightly, 7 days) and two Docker socket proxies. Hostnames come from `ANNEX_HOST_PATTERN`, `{name}.annex.home.arpa` by default.

## Scripts
| Script | Does |
|---|---|
| `scripts/bootstrap.sh` | prepares `/srv/annex`, generates secrets on the VM, pushes the cert in `local` TLS mode |
| `scripts/push.sh [--extras]` | renders config, rsyncs to `/srv/annex/platform`, `docker compose up --wait`, applies gateway views, configures Metabase |
| `scripts/extras.sh on\|off` | Metabase and Dozzle on or off |
| `scripts/tunnel.sh up\|down` | SSH tunnel `127.0.0.1:8443 → annex:443` |
| `scripts/test-smoke.sh` | **after every push**: every part once (login, deploy, gateway, job, email, ops, host, webhooks), under a minute |
| `scripts/test-examples.sh` | before a demo: do `examples/client-lookup` and `examples/monthly-report` still build, deploy and work? Never runs the notifying job. The platform suites don't touch them: they use `annex-selftest` |
| `scripts/test-all.sh [--slow] [--only a,b]` | every check below in order, with a summary (30+ minutes; only when needed) |
| `scripts/test-platform.sh` | M0.1 acceptance checks |
| `scripts/backup.sh` | run the nightly backup now |
| `scripts/restore.sh <app> [archive]` | restore one app's database and files |
| `scripts/test-apps.sh`, `test-jobs.sh`, `restore-test.sh` | M0.2/M0.3 checks |
| `scripts/test-rollback.sh` | M1.1 checks: automatic rollback, `annex rollback`, `--with-data` |
| `scripts/test-notify.sh` | M1.2 checks: email through Mailpit, Slack allowlist, send log, grants |
| `scripts/test-logs.sh` | M2.2 checks (about 4 minutes): stored logs searchable by release, level, text and request id; secrets never stored; a failed release keeps its lines; gateway, platform and sign-in lines; ops restart without gap or duplicates; the rate cap; a paused annex-ops never blocks a deploy; partitions roll; the job-only logging warning |
| `scripts/test-audit.sh` | M2.5 checks (about 3 minutes): a scripted session (secret, deploy, broken deploy, rollback, gateway reads, an email, an account, failed and good sign-in, removal) is in the trail once each, in order, with the right actor, and no secret; the console's Activity page is admins only; copies are idempotent |
| `scripts/test-console.sh` | M2.4 checks (about 2 minutes): who sees what, forged identity headers, no network path from an app, the container's and the database role's limits, numbers equal to the CLI's, escaping, Homepage tiles |
| `scripts/test-errors.sh` | M2.3 checks (about 4 minutes): 50 identical errors are one group and one email, another kind another email, the hourly limit and its summary, quiet after a fix, back in a newer release, browser reports, printed tracebacks, groups removed with the app |
| `scripts/test-hooks.sh` | M1.5 checks: signed events reach the app, bad ones don't, retries after a stop, replay, a tight route |
| `scripts/test-usage.sh` | M1.4 checks: a scripted session shows the same numbers in `annex usage`, the tiles and the console; privacy, retention, restart, digest |
| `scripts/seed-usage.sh [--clear]` | optional demo prep: 8 weeks of synthetic usage history for the dashboard |
| `scripts/test-alerts.sh` | M1.3 checks: stop, crash loop, 500 burst, failing job and platform outage each give one alert and one "resolved" (12-20 minutes) |
| `scripts/test-host.sh` | disk, memory and backup alerts: one alert and one "resolved" each, admins only |
| `scripts/test-users.sh` | accounts: invitation, self-service reset and change, groups, disable, last-admin guard, removal |
| `scripts/sync-runtime.sh [--check]` | copies `runtime/annex_runtime.*` into templates, examples and fixtures (or checks they match) |
| `scripts/demo-bad-deploy.sh` | demo beat: a broken version of client-lookup is rolled back by itself |
| `scripts/reset-demo.sh` | clean demo state: example apps redeployed, other apps removed, checks run (see docs/demo.md) |
| `scripts/wipe-vm.sh` | removes everything Annex from the VM (asks first) |
| `scripts/show-users.sh` | prints the demo users' passwords |
