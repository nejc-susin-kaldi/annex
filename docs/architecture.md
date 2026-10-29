# Architecture

The system as it is after the hardening and control-plane steps (D79-D103), on one page. `docs/brief.md` is the historical brief (its diagram is the M0 one); `docs/decisions.md` has the reasons and the history, with a status index at its top. Where this page and a decision disagree, the code wins, then this page, then the older decision (check the index for what was revised).

One client's server: a single VM (x86_64, 4 GiB RAM) running Docker Compose. The platform is one Compose project (`annex`, from `/srv/annex/platform`); every app is its own project (`annex-app-<name>`). Builders deploy from their own machine with the `annex` CLI or the MCP server, which call `platform/vm/annex_vm.py` on the VM over SSH (D1); a deploy is one such call and runs on the VM (D101). The same compose files, scripts, CLI and `annex_vm.py` also run a **local stack** on the Mac's Docker Desktop (`ANNEX_ENV=local`, D99), a development lane and not a second environment ("Two places the platform runs"). Nothing on the VM talks to the internet except where listed under "Networks".

```
 Mac: annex CLI / MCP ──ssh (via dronky)──▶ VM: annex_vm.py, docker, compose
 browser ─▶ [dronky Traefik, optional] ─▶ VM :80/:443 ─▶ traefik ─forward-auth─▶ authelia
              traefik routes (labels) ─▶ homepage · console · mailpit · gateway · hooks (/h/, no login) ·
                                         [extras: metabase, dozzle] · each web app (own network)
 [ANNEX_ACCESS=headscale, D120] employee's device ─tailnet─▶ VM tailscale0 ─▶ :443 as above
              internet ─▶ VM :8443 (public entrypoint, no forward-auth) ─▶ headscale (vpn.) · hooks (/h/)

   edge        traefik · gateway · ops · hooks · [headscale]          (outbound allowed: ops reaches Slack)
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
| product-db | Postgres 16 | 384m | the gateway's database: the `annex_api` views over the client's DB (Goat Arcade), read through postgres_fdw; no data of its own (D112) | product, clientlink |
| gateway | PostgREST | 128m | the data gateway: per-app JWT roles over `annex_api` views | edge, product (+ app networks at runtime) |
| appdata-db | Postgres 16 | 640m | one database per app, plus `annex_ops` and Metabase's; `max_connections=60` (D84) | appdata, console (+ app networks at runtime) |
| ops | `annex-ops:local` (built on the VM) | 128m | email, Slack, send log, alert engine, health checks, usage, logs, errors, audit, host monitoring, webhook delivery; `/healthz` | edge, platform, appdata, socket-ro (+ app networks at runtime, as `ops`) |
| hooks | same image, `hooks_app` | 96m | the webhook receiver: the only route without a login, insert-only DB role (D55) | edge, appdata |
| console | same image, `console_app` | 96m | read-only web console; read-only DB role `annex_console`; no Docker, no secrets (D69, D76) | platform, console |
| mailpit | Mailpit | 64m | catches all mail (SMTP on `platform`) | platform |
| ofelia | Ofelia | 64m | runs each app's `schedule` entries by `exec` in the app's container | socket-exec |
| backup | offen/docker-volume-backup | 128m | nightly archive at 02:00 of `apps/`, `dumps/`, `secrets/`, `state/directory/` (and `state/headscale/` with it on); 7 days | socket-exec |
| headscale (`ANNEX_ACCESS=headscale`) | Headscale | 128m | Annex's own Tailscale coordination server: people's devices and the server on one tailnet, policy in `config/headscale/policy.hujson`; devices managed with `annex users device-*` (D120) | edge |
| metabase (extras) | Metabase | 1536m | Goat Arcade's dashboards on the `annex_api` views; its own login | platform, product, appdata |
| dozzle (extras) | Dozzle | 64m | live logs of platform containers, admins | platform, socket-ro |

The extras profile is off by default (`scripts/push.sh --extras`). Headscale comes with `ANNEX_ACCESS=headscale` (`compose.access-headscale.yaml`, plus `compose.access-oidc.yaml` for an OIDC sign-in), an instance setting like D118's. Per-container hardening: every platform container has a healthcheck; ops, hooks and console run as uid 10001, read-only root, no capabilities. The Docker socket is never mounted into anything that serves requests: the two proxies are the only holders, and only read-only access reaches ops.

Outside the platform: `demo/client-db/` is a separate Compose project (`democlient`, unlabelled) standing for a client's own database; its only network is `annex-clientlink`, shared with product-db (D77). `scripts/push.sh` brings it up and links it after the platform starts (D112).

Apps are separate containers, one per app, named `annex-app-<name>`: uid 10001, read-only root, `/tmp` as a 64 MB tmpfs, `cap_drop: ALL`, `no-new-privileges`, `pids_limit: 256`, 0.5 CPU, memory from `resources.memory` (default 256 MiB) (D16, D95). An app has no platform labels of its own: Annex writes them, and a deploy refuses an image that carries any (D83).

## Networks: who may reach what

All Annex networks carry `com.kaldi.annex=true` and are `internal` (no route out) except `annex-edge` and the optional egress networks.

- **Apps** (D88). Each app has its own internal network `annex-app-<name>`, created by `annex_vm.py networks-sync` with the lowest free /24 of `ANNEX_APP_SUBNETS` (default `10.213.0.0/16`). Platform containers are connected to it **at runtime** (`docker network connect`), only as needed: `annex-traefik-1` for web apps, `annex-ops-1` (alias `ops`) always, `annex-gateway-1` (alias `gateway`) when the app reads views, `annex-appdata-db-1` (alias `appdata-db`) when it has Postgres. Apps keep the URLs `http://gateway:3000`, `http://ops:8000` and `appdata-db:5432`. Two apps never share a network, so an app cannot reach another app, and identity headers (`Remote-User`) can only arrive through Traefik.
- **Egress.** `egress: []` means no internet. A non-empty list adds `annex-egress-<name>` (not internal, only that app on it): the app can then reach any host and the LAN. The names in the list are not filtered per hostname (a filtering proxy is a production item).
- **Platform networks.** `edge` holds Traefik, gateway, ops (for Slack) and hooks; no app is on it. `platform` holds the UIs Traefik forward-authenticates plus ops (Mailpit, tiles). `appdata` holds appdata-db with ops and hooks (and Metabase), no apps. `console` holds only the console and appdata-db, so the console cannot reach anything an app can. `product` holds product-db, the gateway and Metabase. `clientlink` holds product-db and the democlient database. `socket-ro` and `socket-exec` hold a proxy and its clients.
- **Runtime connections survive a restart, not a recreate.** `scripts/push.sh` runs `networks-sync` after its appdata-db step and again after the full start; after a manual recreate of traefik, ops, gateway or appdata-db, run it by hand (`docs/operations.md`, "App networks").
- **Front door.** Only Traefik publishes a port (80, and 443 in TLS mode). Every route except the hooks receiver's `/h/` sits behind Authelia forward-auth; `strip-identity` blanks any `Remote-*` header from a browser first.
- **Employee access** (`ANNEX_ACCESS=headscale`, D120). Traefik also publishes `ANNEX_BIND_PUBLIC` (8443), the `public` entrypoint: no forward-auth, and only `vpn.` (Headscale) and the hooks receiver's `/h/` are on it. The instance's security group admits only that port from the internet; employees reach 443 over the tailnet (the host's Tailscale client, `tailscale0`), where the policy allows their devices `tag:annex-server:443` and nothing else. Authelia still signs in every person. `annex users remove` takes their devices off the tailnet.

## Files on the VM (`/srv/annex`)

| Path | Content |
|---|---|
| `platform/` | synced repo bundle (compose, config, db, vm, ops); `.env` (settings), `VERSION` (commit, dirty flag, `.env` hash, contract version: `push.sh` refuses a checkout that lacks it, D85; a deploy compares the CLI's contract version with it, D103); its `platform/vm/` has the generated `annex_manifest.py` and `annex_logcheck.py` (D103) |
| `secrets/` | `master.env`, `headscale/oidc_client_secret` (a human's, with OIDC on), per-service env files (`ops.env`, `hooks-receiver.env`; `slack.env` once Slack is connected), `authelia/` (`users.yml`: the accounts, managed with `annex users`; Authelia rewrites it on a password reset or change), `users.txt` (the demo users' generated passwords), `hooks/<app>.json` (an app's webhook signing secrets and token; owned by uid 10001, read by ops), `apps/<app>/` (db password, gateway token, ops token, `secrets.env`, `runtime.env`); never in the repo |
| `state/` | Postgres data directories, Authelia's sqlite, `mailpit/` (caught mail), `directory/users.json` (login → name, email, groups, no passwords; read by ops), `ops/` (test mode; `tuning.json` overrides alert thresholds, usage settings and webhook retry delays), `headscale/` (Headscale's database and Noise key, with `ANNEX_ACCESS=headscale`) |
| `deploy/<app>/` | `src/` (uploaded source), `compose.yaml` (generated, JSON), `deploy.json` (what is live: type, postgres, views, egress, jobs; `run-job` reads its commands here), `.lock.guard` (an empty file the running operation holds an flock on; **never unlinked**, not even by `remove`, D102), `current.json` (the operation that holds the lock or held it last: pid, actor, kind, release, step, result; D101), `run.log` (that deploy's progress lines, one JSON object each; the run before is `run.log.1`), `run.err` (the worker's stderr), `build.log` (the last `docker build` output) |
| `apps/<app>/` | `data/` (mounted at `/data`, backed up), `releases.jsonl` (the release history: image, manifest, who, when, git commit, result, `pending` while its deploy runs; only the VM writes it, D92; backed up) |
| `snapshots/<app>/pre-r<N>/` | database dump and `/data` archive before release N; the last 3 are kept (not backed up) |
| `dumps/`, `backups/` | nightly `pg_dump` output with a `MARKER` (the time `dump-all.sh` finished); backup archives (7 days, mode 700: they contain `secrets/` and `state/directory/`, D87). The `annex_ops` dump leaves out the stored logs and health-check results (D74): restored, those tables come back empty |
| `certs/` | the mkcert wildcard certificate (local TLS mode) |

## The deploy sequence

`annex deploy <folder>` is a thin client in front of one process on the VM (D101). The CLI (`toolkit/cli/src/annex_cli/ops.py`, `deploy`) does only what is quick or only it can know; `platform/vm/annex_deploy.py` (stdlib, called by `platform/vm/annex_vm.py deploy <app>`, using `annex_vm.py`'s own functions for provision, snapshots and networks) does the rest. `annex rollback` has the same shape: `annex_vm.py rollback <app> [--to N] [--with-data]`, and the VM picks the target under the lock.

**On the builder's machine (one call).**
1. **Validate.** `annex.yaml` by `manifest.py` (name, `owner` login, env, egress hostnames, secrets declared) and the requested views against the live gateway allowlist (`annex_vm.py views`): fast, agent-readable errors before anything is uploaded.
2. **Meta and tar.** The release line (who, via, source folder, git commit, contract and runtime versions, the manifest), `cli` (the CLI checkout's commit and contract version, D103), and the three limit overrides a test may set; then the folder as a tar.gz (50 MB check).
3. **One `Remote.call`.** `annex_vm.py deploy <app> --actor <who> --via cli|mcp`, the meta as the first line on stdin and the tar after it. The call reads the VM's JSON lines as they arrive (`progress`, `note`, `heartbeat`, last `ok`), hands progress and notes to the caller, and returns the final `{"ok": true, ...}` object. The CLI adds the two warnings only it can give (the app's runtime copies against `toolkit/runtime/`, a raised memory limit) and prints.

**On the VM (one process).**
1. **Lock.** A non-blocking `flock` on `deploy/<app>/.lock.guard`, held by the process for its whole run, so it ends exactly when the process does. A second deploy, rollback, `secret set` or `remove` of the same app is refused, naming the holder from `current.json` (actor, release, step, age, pid). Different apps don't wait for each other.
2. **Settle.** A `pending` line in `releases.jsonl` whose lock was **not held** (its process was killed, or the VM rebooted) is reconciled at once by D92's rules: healthy and running the pending image: `live`; otherwise `failed`, nothing switched or removed. The step it stopped at comes from the previous `current.json`. A pre-D101 `.lock/` directory is removed with a note.
3. **Versions.** `check_versions` compares the CLI's contract version with the platform's (`VERSION`): a newer CLI is refused ("push the platform first"), an older one gets a note (D103).
4. **Number.** The release number is taken and `{"release": n, "result": "pending", ...}` appended to `releases.jsonl`. From here every failure ends the release as `failed`.
5. **Upload.** The tar on stdin is unpacked into `deploy/<app>/src` (via `src.new`, `filter="data"`); a truncated upload is a failed release "broke off at upload". `annex.yaml` is validated again with the same `annex_manifest.py` and must equal the manifest the CLI sent (D103).
6. **Detach.** The process forks. The worker calls `setsid()`, ignores SIGHUP and SIGPIPE, points stdin at `/dev/null` and stdout/stderr at `run.err`, writes its pid into `current.json` and every protocol line into `run.log`. The parent follows `run.log` to the caller (a heartbeat every 30 s of silence) and stops at the final line. **Ctrl-C, a closed terminal or a dropped SSH session do not stop the deploy**: the CLI says "the connection to the VM dropped at <step>; the deploy continues on the VM" and exits; the worker finishes. SIGTERM (`kill <pid>`) is the one way to stop it: the release is recorded `failed` at the step it reached.
7. **Build.** Refused with under 3 GB free on `/srv/annex`; `docker build` (output in `build.log`; labels `com.kaldi.annex` and `.app`) as image `annex-app/<app>:r<n>`; refused (and the image removed) above 2048 MB, when it runs as root, or when it carries platform labels (`traefik.`, `ofelia.`, `homepage.`, `docker-volume-backup.`, `com.kaldi.annex*` beyond the two the build adds; D83, D95).
8. **Snapshot** (if a release is live): the app's database and `/data` (under 200 MB) into `snapshots/`.
9. **Provision and networks.** DB role and database, gateway role with grants on exactly the requested views and a fresh JWT with `exp`, ops token, `/data`, secrets, webhook files and notify grants (D80, D96); `networks-sync` creates the app's network(s) and connects the platform containers it uses; the generated `compose.yaml` is written (own network only, `pids_limit`, label overrides); ops' stored-log follower drains the old container (D64).
10. **Switch.** `docker compose up -d --remove-orphans` in `deploy/<app>`.
11. **Gate.** Web app: the health path returns 200 from inside the Traefik container within 60 s, then the route answers with a login redirect and Docker health settles. Job-only app: the container is running. If the gate fails, the previous release's manifest is re-provisioned (grants, secrets, networks) and switched back, the release is recorded `failed` with `rolled_back_to`, and the deploy errors with the new container's log tail.
12. **Finish.** Ofelia is restarted if a schedule changed, `deploy.json` is written, older images and snapshots beyond the last 3 are pruned (a failure is a warning in the result), the pending line is rewritten as `live` and copied with an audit row to `annex_ops.releases` (never a pending line), the final `{"ok": true, ...}` line is written and the lock is released.

A VM reboot or `kill -9` mid-deploy leaves the pending line and a **free** lock (the kernel released it): the next deploy or rollback reconciles it at step 2, or `annex_vm.py deploy-reconcile <app>` does, without deploying. `restart: unless-stopped` brings back whatever Compose last started. While a deploy runs, `annex status <app>` (its `deploy` line), `annex_vm.py deploy-state <app>` and `deploy/<app>/run.log` show where it is; the outcome is in `annex releases <app>` (`docs/operations.md`, "Releases and rollback").

**The other container-replacing operations** (D102, `platform/vm/annex_apps.py`, also called by `annex_vm.py`). `annex secret set` (with `--recreate`: store, provision from `deploy.json`, drain, switch) and `annex remove` (drain, compose down, remove the image, deprovision: role, database, networks; the folder goes, `.lock.guard` stays) are each one process that takes the **same flock** while it replaces or removes the container, so none of them can interleave with a deploy or with each other. They take seconds, so they don't fork and have no `run.log`; they ignore SIGHUP and SIGPIPE, so a dropped session never leaves an app half-removed. `annex run <app> <job>` takes **no lock**: `annex_vm.py run-job` reads the job's command from `deploy.json` (what the last deploy declared, whatever CLI asks) and runs `docker exec`; a deploy that replaces the container ends the job.

## Two places the platform runs

The deploy, the scripts and the CLI don't know which machine they are on: `Remote` (`toolkit/cli/src/annex_cli/remote.py`) and `vm()` in `scripts/lib.sh` put one prefix in front of every command, and everything after it is the same.

| | VM lane (the default) | Local lane (`ANNEX_ENV=local`, D99) |
|---|---|---|
| Settings file | `.env` | `.env.local` (else the committed `.env.local.example`) |
| Where commands run | `ssh annex bash -c ...` (via dronky) | `bash -c ...` on the Mac, Docker context `desktop-linux` only |
| Data directory | `/srv/annex` | `~/.annex/local` |
| Render | `.build/vm/platform` | `.build/local/platform` |
| Front door | Traefik on :80/:443 (the Mac's browser through the tunnel on :8443) | Traefik on `127.0.0.1:9080`/`:9443`, hostnames `*.annex-local.home.arpa` |
| Images | amd64 | arm64 (D98) |

A third kind of lane is another instance (D118): `ANNEX_ENV=kaldi` reads `.env.kaldi` and is the VM lane in every other respect. Kaldi's own instance on EC2 runs that way with `ANNEX_DEMO=off` (no Goat Arcade; the gateway and product-db run empty), `ANNEX_TLS_MODE=acme` (Traefik's own Let's Encrypt wildcard certificate, DNS-01 in Route53), `ANNEX_AUTH_FACTORS=two` and `ANNEX_MAIL_RELAY=on` (Mailpit, on `edge` as well, relays every message to a real SMTP server); `docs/new-instance.md` section 8. `ANNEX_ACCESS=headscale` (D120) takes such an instance off the internet: employees reach it over Annex's own tailnet (`docs/new-instance.md` 8.7).

The lane is chosen per command and never a default. Builders don't use lanes: `./annex connect <ssh alias>` points their CLI and MCP server at one instance (`~/.config/annex/instance`), and `./annex` without `ANNEX_ENV` goes there (D119, `docs/install.md`). The local stack is a development tool: no backups worth keeping, no Slack, arm64 builds. Nothing proven only locally counts as proven for the VM.

## annex-ops threads

One process, many threads, each registered in `platform/ops/annex_ops/threads.py` with the longest time one pass may take (D81); `/healthz` answers 503 when one is `dead` or has not beaten for three periods. The console's Platform page shows the table.

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

annex-ops's schema is migrations (D93): `platform/db/appdata/bootstrap.sql` (roles, passwords, `CREATE DATABASE`; every push) then `platform/vm/migrate.py` applies `platform/db/appdata/migrations/NNN_*.sql` not yet in `annex_ops.schema_version`, each in one transaction. An applied file is never edited.

## The repository
Six folders at the root, one per job (D117):
```
platform/   what runs on the server: compose files, config/, db/, ops/, vm/ (push.sh syncs it as /srv/annex/platform)
toolkit/    what a builder installs: cli/, mcp/, skill/, templates/, runtime/
demo/       the synthetic client, Goat Arcade: client-db/ (its database), apps/ (its tools)
tests/      what proves the platform: the suites (smoke.sh, all.sh, <area>.sh), lib.sh, selftest.py, annex-selftest/ (the fixture)
scripts/    operator commands on the Mac: bootstrap, push, backup, restore, tunnel, demo
docs/       architecture, operations, setup, decisions, plans
```
`platform/` is synced flat, so a path inside it (`vm/annex_vm.py`, `db/appdata/`) is the same in the repo under `platform/` and on the VM under `/srv/annex/platform/`.

## Where to look next

- Reasons and history: `docs/decisions.md` (index with status at the top).
- Running it: `docs/operations.md`. Setting it up and undoing it: `docs/setup.md`, `docs/teardown.md`.
- The app contract (what a builder or their agent must follow): `annex contract`, `toolkit/cli/src/annex_cli/contract.md`.
- What changes before production: the list at the end of `docs/plans/hardening.md`.
