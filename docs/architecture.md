# Architecture

The system as it is after the hardening steps (D79-D96), on one page. `docs/brief.md` is the historical brief (its diagram is the M0 one); `docs/decisions.md` has the reasons and the history, with a status index at its top. Where this page and a decision disagree, the code wins, then this page, then the older decision (check the index for what was revised).

One client's server: a single VM (x86_64, 4 GiB RAM) running Docker Compose. The platform is one Compose project (`annex`, from `/srv/annex/platform`); every app is its own project (`annex-app-<name>`). Builders deploy from their own machine with the `annex` CLI or the MCP server, which run commands on the VM over SSH (D1). Nothing on the VM talks to the internet except where listed under "Networks".

```
 Mac: annex CLI / MCP ──ssh (via dronky)──▶ VM: annex_vm.py, docker, compose
 browser ─▶ [dronky Traefik, optional] ─▶ VM :80/:443 ─▶ traefik ─forward-auth─▶ authelia
              traefik routes (labels) ─▶ homepage · console · mailpit · gateway · hooks (/h/, no login) ·
                                         [extras: metabase, dozzle] · each web app (own network)

   edge        traefik · gateway · ops · hooks                        (outbound allowed: ops reaches Slack)
   platform    traefik · authelia · homepage · ops · console · mailpit · metabase · dozzle   (internal)
   product     product-db · gateway · metabase                        (internal)
   clientlink  product-db · democlient-db                             (internal; the client's own database)
   appdata     appdata-db · ops · hooks · metabase                    (internal)
   console     appdata-db · console                                   (internal)
   socket-ro   socket-ro proxy · traefik · homepage · ops · dozzle    (internal)
   socket-exec socket-exec proxy · ofelia · backup                    (internal)

   annex-app-<name>      internal, one per app, a /24 of 10.213.0.0/16:
                         the app + traefik (web) + ops (always) + gateway (views) + appdata-db (Postgres)
   annex-egress-<name>   only if the app declares `egress:`; not internal; that app alone
```

## Containers

Compose project `annex` (`compose.yaml`; container names `annex-<service>-1`). Memory limits are `mem_limit`; the platform stays under about 2.5 GiB.

| Service | Image | Limit | What it does | Networks (compose) |
|---|---|---|---|---|
| socket-ro | docker-socket-proxy (read-only API) | 48m | Docker events, inspect and logs for traefik, homepage, ops, dozzle | socket-ro |
| socket-exec | docker-socket-proxy (exec allowed) | 48m | `exec` for Ofelia's jobs and the backup hooks | socket-exec |
| traefik | Traefik v3 | 128m | front door, TLS (mkcert) or plain behind an upstream proxy; routes from labels; forward-auth and `strip-identity` | edge, platform, socket-ro (+ each web app's network at runtime) |
| authelia | Authelia | 256m | login, groups; file backend (`secrets/authelia/users.yml`) | platform |
| homepage | Homepage | 256m | discovery page; tiles from app labels; config mounted `:ro` | platform, socket-ro |
| product-db | Postgres 16 | 384m | synthetic product data; `annex_api` views; reads the client's DB through postgres_fdw | product, clientlink |
| gateway | PostgREST | 128m | the data gateway: per-app JWT roles over `annex_api` views | edge, product (+ app networks at runtime) |
| appdata-db | Postgres 16 | 640m | one database per app, plus `annex_ops` and Metabase's; `max_connections=60` (D84) | appdata, console (+ app networks at runtime) |
| ops | `annex-ops:local` (built on the VM) | 128m | email, Slack, send log, alert engine, health checks, usage, logs, errors, audit, host monitoring, webhook delivery; `/healthz` | edge, platform, appdata, socket-ro (+ app networks at runtime, as `ops`) |
| hooks | same image, `hooks_app` | 96m | the webhook receiver: the only route without a login, insert-only DB role (D55) | edge, appdata |
| console | same image, `console_app` | 96m | read-only web console; read-only DB role `annex_console`; no Docker, no secrets (D69, D76) | platform, console |
| mailpit | Mailpit | 64m | catches all mail (SMTP on `platform`) | platform |
| ofelia | Ofelia | 64m | runs each app's `schedule` entries by `exec` in the app's container | socket-exec |
| backup | offen/docker-volume-backup | 128m | nightly archive at 02:00 of `apps/`, `dumps/`, `secrets/`, `state/directory/`; 7 days | socket-exec |
| metabase (extras) | Metabase | 1536m | curated product data; its own login | platform, product, appdata |
| dozzle (extras) | Dozzle | 64m | live logs of platform containers, admins | platform, socket-ro |

The extras profile is off by default (`scripts/push.sh --extras`). Per-container hardening: every platform container has a healthcheck; ops, hooks and console run as uid 10001, read-only root, no capabilities. The Docker socket is never mounted into anything that serves requests: the two proxies are the only holders, and only read-only access reaches ops.

Outside the platform: `democlient/` is a separate Compose project (`democlient`, unlabelled) standing for a client's own database; its only network is `annex-clientlink`, shared with product-db (D77).

Apps are separate containers, one per app, named `annex-app-<name>`: uid 10001, read-only root, `/tmp` as a 64 MB tmpfs, `cap_drop: ALL`, `no-new-privileges`, `pids_limit: 256`, 0.5 CPU, memory from `resources.memory` (default 256 MiB) (D16, D95). An app has no platform labels of its own: Annex writes them, and a deploy refuses an image that carries any (D83).

## Networks: who may reach what

All Annex networks carry `com.kaldi.annex=true` and are `internal` (no route out) except `annex-edge` and the optional egress networks.

- **Apps** (D88). Each app has its own internal network `annex-app-<name>`, created by `annex_vm.py networks-sync` with the lowest free /24 of `ANNEX_APP_SUBNETS` (default `10.213.0.0/16`). Platform containers are connected to it **at runtime** (`docker network connect`), only as needed: `annex-traefik-1` for web apps, `annex-ops-1` (alias `ops`) always, `annex-gateway-1` (alias `gateway`) when the app reads views, `annex-appdata-db-1` (alias `appdata-db`) when it has Postgres. Apps keep the URLs `http://gateway:3000`, `http://ops:8000` and `appdata-db:5432`. Two apps never share a network, so an app cannot reach another app, and identity headers (`Remote-User`) can only arrive through Traefik.
- **Egress.** `egress: []` means no internet. A non-empty list adds `annex-egress-<name>` (not internal, only that app on it): the app can then reach any host and the LAN. The names in the list are not filtered per hostname (a filtering proxy is a production item).
- **Platform networks.** `edge` holds Traefik, gateway, ops (for Slack) and hooks; no app is on it. `platform` holds the UIs Traefik forward-authenticates plus ops (Mailpit, tiles). `appdata` holds appdata-db with ops and hooks (and Metabase), no apps. `console` holds only the console and appdata-db, so the console cannot reach anything an app can. `product` holds product-db, the gateway and Metabase. `clientlink` holds product-db and the democlient database. `socket-ro` and `socket-exec` hold a proxy and its clients.
- **Runtime connections survive a restart, not a recreate.** `scripts/push.sh` runs `networks-sync` after its appdata-db step and again after the full start; after a manual recreate of traefik, ops, gateway or appdata-db, run it by hand (`docs/operations.md`, "App networks").
- **Front door.** Only Traefik publishes a port (80, and 443 in TLS mode). Every route except the hooks receiver's `/h/` sits behind Authelia forward-auth; `strip-identity` blanks any `Remote-*` header from a browser first.

## Files on the VM (`/srv/annex`)

| Path | Content |
|---|---|
| `platform/` | synced repo bundle (compose, config, db, vm, ops); `.env` (settings), `VERSION` (commit, dirty flag, `.env` hash: `push.sh` refuses a checkout that lacks it, D85) |
| `secrets/` | `master.env`, per-service env files, `authelia/`, `hooks/<app>.json`, `apps/<app>/` (db password, gateway token, ops token, `secrets.env`, `runtime.env`); never in the repo |
| `state/` | Postgres data directories, Authelia and Mailpit state, `directory/users.json`, `ops/` (test mode, tuning) |
| `deploy/<app>/` | `src/` (uploaded source), `compose.yaml` (generated), `deploy.json` (what is live: type, postgres, views, egress, ...), `.lock/` (+ `owner.json`) while a deploy or rollback runs, `.lock.guard` |
| `apps/<app>/` | `data/` (mounted at `/data`), `releases.jsonl` (the release history; only the VM writes it, D92) |
| `snapshots/<app>/pre-r<N>/` | database dump and `/data` archive before release N; the last 3 |
| `dumps/`, `backups/` | nightly `pg_dump` output with a `MARKER`; backup archives (mode 700; they contain `secrets/`, D87) |
| `certs/` | the mkcert wildcard certificate (local TLS mode) |

## The deploy sequence

> **Since D101 (control-plane V1)** steps 2–9 run in one process on the VM (`vm/annex_deploy.py`, called once as `annex_vm.py deploy <app>`), which holds an flock on `deploy/<app>/.lock.guard` for its whole run: no `.lock/` directory, token or 30-minute rule, `current.json` instead of `owner.json`, progress in `run.log`, and Ctrl-C detaches. The rewrite of this section is control-plane V4; `docs/operations.md` "Releases and rollback" is current.

`annex deploy <folder>` (`cli/src/annex_cli/ops.py`, `deploy`/`_deploy`, with `vm/annex_vm.py` on the VM) after D92. Every VM-side step is an `ssh annex` command; the CLI never needs Docker locally.

1. **Validate** (Mac). `annex.yaml` by `manifest.py` (name, `owner` login, description, env, egress hostnames, secrets declared); the requested views against the live gateway allowlist (`annex_vm.py views`).
2. **Begin** (`deploy-begin`). Takes `deploy/<app>/.lock` (a `mkdir`, with owner, token, step; a lock under 30 minutes old refuses and names its holder; an older one is taken over). **Reconciles** a `pending` line left by a dead deploy against what Docker says (healthy and running the pending image: `live`; otherwise `failed`, nothing switched or removed). Numbers the release and appends `{"release": n, "result": "pending", ...}` to `releases.jsonl`. From here an exception or Ctrl-C ends the release as `failed` and releases the lock.
3. **Upload.** The folder is tarred and unpacked into `deploy/<app>/src` (via `src.new`).
4. **Build** on the VM. Refused with under 3 GB free on `/srv/annex`; `docker build` (labels `com.kaldi.annex` and `.app`) as image `annex-app/<app>:r<n>`; refused (and the image removed) above 2048 MB, when it runs as root, or when it carries platform labels (`traefik.`, `ofelia.`, `homepage.`, `docker-volume-backup.`, `com.kaldi.annex*` beyond the two the build adds; D83, D95).
5. **Snapshot** (if a release is live). `annex_vm.py snapshot`: the app's database and `/data` (under 200 MB) into `snapshots/`.
6. **Provision and networks.** `annex_vm.py provision`: DB role and database, gateway role with grants on exactly the requested views and a fresh JWT with `exp`, ops token, `/data` directory, secrets, webhook files and notify grants (D80, D96). Then `networks-sync` creates the app's network(s) and connects the platform containers it uses. The generated `compose.yaml` is written (own network only, `pids_limit`, label overrides), and ops' stored-log follower drains the old container.
7. **Switch.** `docker compose up -d --remove-orphans` in `deploy/<app>`.
8. **Gate.** Web app: the health path returns 200 from inside the Traefik container within 60 s, then the route answers with a login redirect and Docker health settles. Job-only app: the container is running. If the gate fails, the previous release's manifest is re-provisioned (grants, secrets, networks) and switched back (`back-provision`, `back-switch`, `back-gate`), the release is recorded `failed` with `rolled_back_to`, and the deploy errors.
9. **Cleanup and end.** Ofelia is restarted if a schedule changed, `deploy.json` is written, older images and snapshots beyond the last 3 are pruned (a failure is a warning in the result, not silence), and `deploy-end` rewrites the pending line as `live` and copies it with an audit row to `annex_ops.releases` (never a pending line), then releases the lock.

`annex rollback` is the same machinery pointed at an older image and takes the same lock; `annex remove` deprovisions (drops role, database, networks) and does not take the lock. `annex migrate-networks` (human only) moves an app deployed before D88 onto its own network without its source.

## annex-ops threads

One process, many threads, each registered in `ops/annex_ops/threads.py` with the longest time one pass may take (D81); `/healthz` answers 503 when one is `dead` or has not beaten for three periods. The console's Platform page shows the table.

| Thread | Period | Does |
|---|---|---|
| `engine` | 30 s | the alert engine (pure rules, D39): signals to incidents; saves its timers (`engine_state`, D90) |
| `alert-sender` | 90 s | sends the engine's alert messages from the `outbox` table, with per-delivery retry and order (D90) |
| `watch-docker` | 150 s | Docker events and the reconcile pass; reports to the engine |
| `health-checker` | 90 s | HTTP checks of every web app, the front door and ops itself (`platform:ops`); results in `health_checks` (D73, D81) |
| `host-sampler` | `host_sample_s` + 30 s | disk, memory, the newest backup and its dump `MARKER`; `backup-stale` signal; a sample every 5 minutes (D56, D87) |
| `log-shipper` | 60 s | one follower thread per container (named `follow-ship:...`, 150 s each), restarted from the saved cursor if dead; rate cap before parsing (D89) |
| `log-sink` | 30 s | the only thread that inserts stored log rows; bounded buffer (D86, D89) |
| `log-keeper` | 120 s | partitions and retention of `annex_ops.logs` (D62) |
| `error-scanner` | `errors_scan_s` + 60 s | error groups from stored logs, error emails (D67) |
| `usage` | 60 s | Traefik access lines to usage rows; 10-minute rollup into `usage_rollup_apps` (D46, D82) |
| `digest` | 90 s | the weekly usage digest |
| `hooks-delivery` | 60 s | retried delivery of stored webhook events to apps (D53) |
| `auditor` | `audit_scan_s` + 60 s | copies sign-ins, webhook events and gateway aggregates into `audit_events` (D72, D82) |

Database access goes through a small own pool (`db.py`: ops 4 connections, console and hooks 2 each, `OPS_DB_POOL_MAX`; D84). Buffers that can grow have byte and row limits and drop the oldest first, counted in `/healthz` (D86).

## Database schema

annex-ops's schema is migrations (D93): `db/appdata/bootstrap.sql` (roles, passwords, `CREATE DATABASE`; every push) then `vm/migrate.py` applies `db/appdata/migrations/NNN_*.sql` not yet in `annex_ops.schema_version`, each in one transaction. An applied file is never edited.

## Where to look next

- Reasons and history: `docs/decisions.md` (index with status at the top).
- Running it: `docs/operations.md`. Setting it up and undoing it: `docs/setup.md`, `docs/teardown.md`.
- The app contract (what a builder or their agent must follow): `annex contract`, `cli/src/annex_cli/contract.md`.
- What changes before production: the list at the end of `docs/plans/hardening.md`.
