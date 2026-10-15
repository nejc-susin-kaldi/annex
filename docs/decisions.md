# Decisions

Newest last. Each entry: what, why, alternatives considered.

## D1. Apps run through `ssh annex` and the VM's Compose (14 Oct)
**What.** The CLI copies the app folder to `/srv/annex/deploy/<app>`, writes the generated compose file there and runs `docker compose` on the VM over SSH, the same way the platform runs. The brief said "built and run through the annex-vm Docker context, using the Mac's Compose".
**Why.** Compose reads `env_file` on the client. With the Mac's Compose, per-app secrets (DB password, gateway JWT, `annex secret set` values) would have to pass through the Mac. Nejc said it's fine for secrets to touch the Mac in this throwaway demo, and asked for the least complex option. One transport (SSH) for both platform and apps is simpler: no path translation, no Compose version skew, and env files stay on the VM.
**Alternatives.** (a) Mac Compose via the context, with secret values fetched over SSH and passed inline. Two transports, and values in the generated compose. (b) Build via the context, `up` via SSH. Two transports for no gain.
**Consequence.** The `annex-vm` context is optional (handy for inspection), not required.

## D2. Split the edge network: `annex-edge` + `annex-platform` (14 Oct, approved by Nejc)
**What.** `annex-edge`: Traefik, apps and gateway. `annex-platform` (internal): Traefik, Authelia, Homepage, Metabase, Dozzle.
**Why.** Dozzle trusts `Remote-User` from whoever calls it. On a shared edge network an app could call Dozzle directly with a spoofed header and read every log. Now apps reach platform UIs only through Traefik and forward-auth.
**Alternatives.** One edge network as in the brief. Per-app networks (open for later).

## D3. Pinned images (14 Oct)
All checked on Docker Hub on 14 Oct and verified amd64 on the VM.

| Component | Image |
|---|---|
| Routing | `traefik:v3.7.14` |
| Login | `authelia/authelia:4.39.28` |
| Discovery | `gethomepage/homepage:v2.5.0` |
| Analytics | `metabase/metabase:v0.64.2.2` |
| Logs | `amir20/dozzle:v11.3.2` |
| Scheduler | `mcuadros/ofelia:0.3.22` (0.4 is still beta) |
| Backups | `offen/docker-volume-backup:v2.49.1` |
| Databases | `postgres:16.15-alpine3.24` |
| Gateway | `postgrest/postgrest:v16.4` |
| Socket proxy | `tecnativa/docker-socket-proxy:v0.5.0` |
| Test/helper | `python:3.13.16-slim-trixie` (also the python templates' base), `alpine:3.24.2` (wipe helper) |

## D4. Two socket proxies (14 Oct)
`socket-ro` (containers, networks, info, events; no POST) for Traefik, Homepage and Dozzle. `socket-exec` (adds POST and exec) for Ofelia and the backup hooks. Each sits on its own internal network. **Caveat:** POST plus containers lets a caller create containers, so `socket-exec` is root-equivalent. That's acceptable for the prototype; production needs a narrower path.

## D5. Forward-auth on the entrypoint, plus header stripping (14 Oct)
Every router on the HTTPS entrypoint (HTTP in upstream mode) gets `strip-identity` and then `authelia` by default, so an app can't be exposed without login by mistake. `strip-identity` blanks any client-sent `Remote-*` header. Traefik's forward-auth then sets them from Authelia's answer. Authelia's own host is a `bypass` rule. `logs` and `gateway` are admins-only, with an explicit deny for everyone else. Without that deny, non-admins fell through to the catch-all rule; the test found this.

## D6. `ANNEX_PUBLIC_PORT` (14 Oct)
Port 443 on the Mac is already used by another process, so the browser tunnel listens on 8443. The optional `ANNEX_PUBLIC_PORT` is appended to every public URL (Authelia redirect, Homepage links, `ANNEX_BASE_URL`), so login redirects keep the port. Leave it empty once a route serves Annex on 443.

## D7. Config changes force a recreate (14 Oct)
Compose doesn't notice changed bind-mounted files. `render.py` hashes each service's config directory into `ANNEX_CONFIG_HASH_<SERVICE>`, which is passed as an env var, so a change recreates exactly that container. Config is mounted as directories, not single files, because rsync replaces files and a single-file bind mount would keep the old inode.

## D8. Authelia memory 256 MiB (14 Oct)
At 128 MiB Authelia was OOM-killed during three logins in a row: argon2id verification uses about 64 MiB per login. 256 MiB is enough for the demo.

## D9. Metabase keeps its own login (14 Oct)
Metabase OSS can't take identity from headers; that needs Pro (JWT/SAML SSO). It sits behind Authelia like everything else, and has its own accounts: `kaldi-admin` is the Metabase admin, while `sam` and `cm` are normal users. Each has the same password as in Authelia (set by `vm/metabase-setup.sh`). Metabase's product connection uses `metabase_ro`, which can only read `annex_api` and is limited to that schema in Metabase's sync settings. Sample content is disabled.

## D10. Gateway details (14 Oct)
PostgREST has no anonymous role, so every call needs an app JWT. Its public hostname is admins-only and mainly shows that it's locked. Each request is logged with the app's role through `db-pre-request = annex_gateway.log_request` (a `RAISE LOG` visible in product-db's logs), next to PostgREST's own access log. `push.sh` re-applies `api_views.sql` (idempotent) on every push and tells PostgREST to reload its schema.

## D11. Backup container is not on the `appdata` network (14 Oct)
The brief lists backup on `appdata`. It doesn't need to be: database dumps run *inside* appdata-db as an offen `archive-pre` exec hook (`db/appdata/hooks/dump-all.sh`) through `socket-exec`. The backup container only reads `/srv/annex/apps` and `/srv/annex/dumps`.

## D12. Demo passwords and tests (14 Oct)
Demo user passwords are generated on the VM (`/srv/annex/secrets/users.txt`). `scripts/show-users.sh` prints them for Nejc. `scripts/test-platform.sh` reads them over SSH into memory to drive the curl login. Per D1 this is accepted.

## D13. Platform service tiles are static (14 Oct)
Metabase, Logs and Gateway are in Homepage's `services.yaml` with a container status dot, so they're listed even while the extras profile is off. Apps are discovered through Docker labels. Goat Mailer and Bekafon are placeholder tiles.

## D14. Synthetic product data (14 Oct)
A deterministic seed (`setseed`) with 8 advisors, 300 clients, about 530 accounts, 30 fictional instruments, 24 month-end prices each, and about 4k transactions. Curated views: `client_summary`, `top_clients_by_aum`, `monthly_positions`, `advisor_book`. If a first-start init script fails, Postgres won't retry it. Wipe `state/product-db` (or the whole VM) and push again.

## Resource record: M0.1 (14 Oct)
Idle, a minute after a push, with the extras profile **on**:

| Container | Memory |
|---|---|
| metabase | 1.36 GiB / 1.5 GiB |
| authelia | 157 MiB / 256 MiB (after logins; argon2 memory stays mapped) |
| homepage | 104 MiB |
| appdata-db | 98 MiB |
| product-db | 36 MiB |
| traefik | 20 MiB |
| socket-ro, socket-exec | 17 MiB each |
| dozzle | 15 MiB |
| gateway | 11 MiB |
| backup, ofelia | 4 MiB, 2 MiB |
| **total** | **about 1.85 GiB; core without extras about 465 MiB** |

VM: 2.3 GiB used, 1.5 GiB available, no swap used. Disk: `/` 7.6 GB used of 29 GB (images 3.5 GB).

## D15. Wipe round trip not tested in M0.1 (14 Oct, Nejc)
`scripts/wipe-vm.sh` → `bootstrap.sh` → `push.sh` is deferred until the VM is actually wiped. It's a demo, and the test isn't worth the effort now. The script is written but has never been run.

## D16. App runtime hardening (14 Oct)
Every app container runs as **uid 10001** (`user:` in the generated compose), with a read-only root filesystem, `/tmp` as a 64 MB tmpfs, `cap_drop: ALL`, `no-new-privileges`, 256 MiB and 0.5 CPU. The Dockerfile must still declare a non-root `USER`; the deploy inspects the built image and refuses root. A fixed uid means `/srv/annex/apps/<app>/data` can be chowned once, without knowing the image's user. The contract documents this. There's no per-app override in v0.

## D17. How the CLI deploys (14 Oct)
`annex deploy` runs these steps:
1. Validates `annex.yaml` and checks the requested views against the live allowlist.
2. Tars the folder in Python, skipping `.git`, `node_modules`, `.venv` and similar, and unpacks it into `/srv/annex/deploy/<app>/src`.
3. Runs `vm/annex_vm.py provision` on the VM. This creates the DB role and database, the product-db role with grants on exactly the requested views, the JWT, and the data directory, then writes `secrets/apps/<app>/runtime.env`.
4. Writes `compose.yaml` and builds on the VM. The image is tagged `annex-app/<app>:<timestamp>`; older tags are removed after a successful deploy, and the build cache is capped at 1.5 GB.
5. Starts the container. For web apps it then waits for the healthcheck (probed from the Traefik container), for the route (a login redirect through Traefik on the VM) and for Docker health.

Each app's compose service is named after the app and the container is `annex-app-<app>`, so names are unique on the shared networks. `ANNEX_GATEWAY_URL` is `http://gateway:3000`. Apps join `annex-edge`, and `annex-appdata` only when `storage.postgres` is set. Secret values are written single-quoted, so Compose never interpolates `$`. Values with `'` or newlines are refused in v0.

## D18. Ofelia restarts on schedule changes (14 Oct)
Ofelia 0.3.22 reads job labels only at startup; a probe showed it ignores containers started later. The CLI restarts `annex-ofelia-1` after a deploy or remove that touches an app with schedules. Jobs are `job-exec` labels on the app container (`no-overlap`). `annex run` uses `docker exec` with the same command, so manual and scheduled runs behave the same. Job-only apps idle with `sleep infinity`. 5-field cron gets a leading `0` seconds field for Ofelia.

## D19. Missing secrets fail the deploy (14 Oct)
If `annex.yaml` declares a secret that hasn't been set, the deploy stops and names the exact `annex secret set` command. That command is human-only, not exposed over MCP. `annex secret set` on a deployed app regenerates its env file and recreates the container.

## D20. Authelia `GOMEMLIMIT=160MiB` (14 Oct)
After many logins Authelia sat at 222 of 256 MiB, because Go keeps argon2 buffers. A soft Go memory limit makes it collect and return memory earlier; it settles around 157 MiB.

## Resource record: M0.2 (14 Oct)
With extras on and `client-lookup` deployed: client-lookup 41 MiB, Authelia 157 MiB, Metabase 1.32 GiB, other services as in M0.1. VM: 2.5 GiB used, 1.4 GiB available, no swap. Disk: 7.8 GB used of 29 GB.

## D21. Jobs run as the app user (14 Oct)
Ofelia's `job-exec` defaults to **root**. A root job in a container with `cap_drop: ALL` couldn't write to `/data`, which is owned by 10001, and would leave root-owned files otherwise. The CLI sets `ofelia.job-exec.<job>.user=10001`. Ofelia logs each scheduled run, with its stdout and duration.

## D22. `monthly-report` has a demo schedule (14 Oct)
Besides `monthly` (`0 6 1 * *`), the example has `refresh` (`*/10 * * * *`), so the scheduler can be seen working during a demo. Both run the same `report.py`. A rerun for the same month overwrites that month's CSV and adds a history row. Remove `refresh` from `examples/monthly-report/annex.yaml` for a realistic setup.

## D23. Backups and restore (14 Oct)
- **Nightly backup:** offen runs at 02:00 Europe/Ljubljana. Before archiving, its `archive-pre` hook runs `dump-all.sh` inside appdata-db: `pg_dump -Fc` of every database (apps and Metabase) plus globals. The archive `annex-<timestamp>.tar.gz` holds `/backup/apps/<app>/data/…` and `/backup/dumps/current/*.dump`. Retention is 7 days (`BACKUP_RETENTION_DAYS`), and offen logs each prune. `scripts/backup.sh` runs the same thing now.
- **Restore:** `scripts/restore.sh <app> [archive]` calls `annex_vm.py restore`. It extracts only that app's dump and files, stops the container, drops and recreates the app database, runs `pg_restore`, replaces `/srv/annex/apps/<app>/data` (chowned to 10001), then starts the container again. The app must be deployed, because restore brings back data, not code or config.
- **Proof:** `scripts/restore-test.sh` passes 12/12 on `monthly-report`.
- **Not covered:** product-db (seed data, re-created from `db/product`) and platform state (Authelia's sqlite, regenerable). Backups stay on the VM's own disk, so they don't survive losing the VM. Production goes to S3.

## Resource record: M0.3 (14 Oct)
Two apps deployed (41 MiB each), extras on. VM: 2.5 GiB used, 1.4 GiB available, no swap. Disk: 7.9 GB of 29 GB. Backups: 772 KiB for three archives.

## D24. MCP server on the 2.x SDK (14 Oct)
`mcp==2.3.0`, where FastMCP is now `MCPServer`. The server (`mcp/`) is a thin layer over `annex_cli.ops`, so CLI and MCP behave identically. Blocking calls run in a worker thread. Known failures are raised as `ToolError`; the SDK hides the message of any other exception ("Error executing tool"), and agents need the message to fix things. `annex_deploy` returns the deploy steps with the result. `remove` and `secret set` aren't exposed. Registration is user-scoped (setup.md §4), so it works from an empty folder.

## D25. `static` template has a tiny server with two endpoints (14 Oct)
A purely static site can't read identity headers or call the gateway, and the token must not reach the browser. The template's `server.py` (stdlib, `python:3.13.16-slim-trixie`) serves `public/` plus two endpoints: `api/me` (identity) and `api/data/<view>` (gateway proxy with the app's token; filters pass through, and the gateway still enforces the app's views). So a dashboard is plain HTML/JS with same-origin `fetch`. There's no build step on the server: build locally and commit the output to `public/`.

## D26. `node-web` template (14 Oct)
`node:24.21.0-alpine3.24` and Express 5.3.0, pinned exactly in `package.json`. There's no lockfile in the template, so `npm install` resolves transitive dependencies at build time. That's acceptable for the prototype. Same helpers as python-web (`annex_runtime.js`), same healthcheck pattern.

## D27. The skill lives in the repo, symlinked (14 Oct)
`skills/annex/SKILL.md` covers when to use it, the tool table, template choice, the workflow, the rules and a homelab note about `/etc/hosts`. It's symlinked into `~/.claude/skills/annex` by Nejc (setup.md §4), so repo edits apply without reinstalling.

## D28. Demo reset (15 Oct)
`scripts/reset-demo.sh` brings the VM to a known demo state:
- platform with extras;
- `client-lookup` redeployed;
- `monthly-report` removed and redeployed with empty history, then run once;
- every other app removed with its data, unless `--keep <app>`;
- tunnel up, `test-platform.sh` and `test-apps.sh` run.

It shows the plan and asks before changing anything. It doesn't touch secrets, passwords, backups or the platform's data. **It was written but not run (Nejc's call, to avoid disturbing the current setup).** It only chains commands that were each tested in M0.1–M0.3 (`push.sh --extras`, `annex remove --yes`, `annex deploy`, `annex run`, `tunnel.sh up`, the test suites). Its own bash syntax was checked.
`vibe-dashboard` is built live during the demo by Claude Code in a fresh empty folder, so the default reset removes it. A pre-built copy can be kept as a fallback with `--keep`.

## D29. M0.4 proof run is part of the demo (15 Oct)
The M0.4 acceptance (Claude Code builds and deploys `vibe-dashboard` from an empty folder through the MCP and skill, with no human code edits) hasn't been recorded by the build agent. The demo's live-build step is that proof. As of 15 Oct an app `vibe-leads` (owner sam, python-web with Postgres, deployed 15 Oct 09:30) is also on the VM; it wasn't deployed by the build agent. Keep it with `scripts/reset-demo.sh --keep vibe-leads` if it's wanted.

## Resource record: M0.5 (15 Oct)
Three apps (client-lookup 41 MiB, monthly-report 48 MiB, vibe-leads 44 MiB), extras on. Metabase 1.34 GiB, Homepage 120 MiB, Authelia 29 MiB (after a quiet night), everything else under 90 MiB each. VM: 2.4 GiB used, 1.45 GiB available. Disk: 8.2 GB of 29 GB.

## D30. Releases and the health gate (M1.1, 15 Oct)
- **Releases.** Every deploy is release `r<N>`, image `annex-app/<app>:r<N>`. The M1 plan wrote `annex/<app>:r<N>`; keeping the M0 repository name means `remove`, `wipe-vm.sh` and the image cleanup needed no change. Each release is one line in `/srv/annex/apps/<app>/releases.jsonl`: image, the validated manifest, who (`$ANNEX_USER` or user@host), via (cli/mcp), source folder, git commit (if the folder is tracked), data snapshot and result (`live` or `failed`). The file sits next to `data/`, so the nightly backup includes it and `remove --keep-data` keeps it.
- **Build before the switch.** The image is built with `docker build` before anything changes, and the generated compose file only references it (`pull_policy: never`). In M0, `docker compose build` used the compose file, which was already rewritten for the new version. Now a build failure leaves the running version and its compose file untouched.
- **Health gate:** 60 s (`ANNEX_HEALTH_TIMEOUT`) for the healthcheck to return 200, polled through Traefik's container as before. It fails early if the container stops. Job-only apps must still be running after 3 s. On failure, the previous live release is re-activated **from its stored manifest** (provision with its views, storage and secrets, then compose generated by the current CLI). The failed image is deleted and the deploy error names the reason and includes the last 40 log lines. Data isn't touched; that's what `--with-data` is for.
- **Rollback is a new release** that points at the older image, so the history stays linear and a rollback can be undone (`--to rN` also rolls forward). The default target is the last live release before the current one. A rollback that fails the gate switches back to where it started (and restores the data it replaced, with `--with-data`).
- **Alternatives.** Re-using each release's stored compose file instead of regenerating it from the manifest: simpler, but rolling back would also drop platform labels added later (e.g. M1.3's). Marking an old release as live again instead of appending one: loses the audit trail.

## D31. Data snapshots before each switch (M1.1, 15 Oct)
Before release N goes live (deploy or rollback), `annex_vm.py snapshot` writes `/srv/annex/snapshots/<app>/pre-r<N>/`: `pg_dump -Fc` of the app's database if it has one, and a tar of `/data` if it's under 200 MB (larger: skipped and noted; the nightly backup covers it). `meta.json` records which release was live (`from_release`). `rollback --with-data` to rK restores the newest snapshot taken while rK was live, with the same restore code as `restore.sh`. Snapshots are outside `apps/`, so they aren't in the nightly backup: they're short-term undo, not backups. The last 3 per app are kept; pruning runs after a deploy or rollback finishes, so the snapshot a rollback needs is never pruned first. `--with-data` is CLI-only and asks for confirmation; the MCP's `annex_rollback` never touches data.

## D32. Apps deployed before M1.1 (15 Oct)
They have no release history, so their first deploy after M1.1 becomes r1 and, like any first deploy, has nothing to roll back to if it fails the gate. `client-lookup` and `monthly-report` were redeployed during the M1.1 tests. **`vibe-leads` was left as is** (not built by the build agent); its next deploy starts its history. Its old image (`annex-app/vibe-leads:20261015-093009`) is cleaned up then.

## Resource record: M1.1 (15 Oct)
No new containers. Three apps (41–44 MiB each), extras on. VM: 2.5 GiB used, 1.4 GiB available. Disk: 8.0 GB of 29 GB; images 3.6 GB. Release images share the base layers, so each kept release adds only its own layers (tens of MB for the examples). Snapshots: 84 KiB.

## D33. Bring your own stack, and `resources.memory` (15 Oct, approved by Nejc)
- **Templates are the fast path, not a requirement.** The deploy already accepted any `Dockerfile` (it only refuses images that run as root), but the contract read as if only the four templates existed. The contract now has a "Bring your own stack" section: a checklist (`$PORT`, a health check that works without a login, non-root `USER`, read-only filesystem with `HOME=/tmp`, everything built in the image, identity and the gateway token on the server, Postgres and `/data` only, one container, 50 MB upload) and notes for common stacks (Vite/React SPA via the `static` server, Next.js, Streamlit, Django/Flask, compiled languages). The skill points agents at it when the user wants another framework or brings existing code.
- **`resources.memory`** (contract change, optional): 128m–512m, default 256m. Next.js, JVMs and heavier Python data apps don't fit in 256 MB, which in practice ruled them out. The cap of 512m keeps one app from taking a large share of the VM's 3.8 GiB; anything bigger is a graduation candidate. A raised limit is not an approval step: the deploy succeeds with a warning, the container carries `com.kaldi.annex.memory`, and `annex list` shows a memory column so the platform team sees it (the M2 console's fleet view will too). `Manifest.memory_mb` defaults to 256, so manifests stored with older releases still load for rollback.
- **Alternatives.** Approving raised limits per app (a platform-side allowlist): safer for the VM, but it adds a human step to every Next.js deploy, and the cap already bounds the risk. CPU as well as memory: not needed yet, because the CPU limit throttles rather than kills, and builds aren't limited. Allowing sizes like `1g`: keeps the format simple to only `<n>m`, which the error spells out.
