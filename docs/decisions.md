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

## D34. annex-ops: one service built from the repo (M1.2, 15 Oct)
**What.** `ops/` is a small FastAPI service (`annex-ops:local`, built on the VM from the synced bundle, the first platform image built from source). It owns everything the platform sends outward: email and Slack for apps now, and the base for M1.3 alerts, M1.4 usage and M2 audit. Code is split into small modules (`auth`, `email`, `slack`, `sends`, `directory`, `ctl`) with the DB and network kept out of the pure functions, so 47 unit tests run on the Mac. Its data is the `annex_ops` database in appdata-db (`db/appdata/annex_ops.sql`, in the nightly backup). A `docker exec ... python -m annex_ops.ctl` admin command is what the CLI uses (`annex sends`, the Slack warning). SMTP uses `smtplib` and Slack uses `urllib`: no extra dependencies. The numbering note: the plan said D33 onwards, but D33 was taken by the "bring your own stack" commit, so this is D34.
**Why.** Apps must hold no SMTP password or bot token, so something has to hold them; one service for all outbound traffic means one place to rate-limit, log and later audit.
**Alternatives.** A library inside each app (every app would need the credentials). Putting this in the CLI/VM scripts (it has to run continuously and answer apps on the network). Separate email and Slack services (more containers for the same RAM-poor VM).

## D35. Tokens are stored hashed in `app_grants` (M1.2, 15 Oct)
**What.** `annex_vm.py provision` creates a random token per app (`secrets/apps/<app>/ops_token`, injected as `ANNEX_OPS_TOKEN`) and writes only its sha256, with the app's email flag and Slack channels, to `annex_ops.app_grants`. ops looks the hash up on every request, so changing a grant takes effect at once, with no ops restart. A deploy without `notify:` (or a rollback to a release without it) deletes the row and the token. `Manifest.notify_email` and `notify_slack` have defaults, so manifests stored by older releases load (D30).
**Why.** A database leak or an ops log never exposes a usable token, and revocation is a row delete.
**Alternatives.** A JWT carrying the scopes (like the gateway): stateless, but revoking a grant means rotating the secret for everyone, and scopes are frozen into the token. Reading grants from files mounted into ops: no DB, but a stale-mount risk (D7) and nowhere to keep the send log next to them.

## D36. Networks for ops, and no public route (M1.2, 15 Oct)
**What.** ops is on `edge` (apps reach it, and it reaches Slack: `platform` and `appdata` are internal), `platform` (Mailpit's SMTP) and `appdata` (its database). It has no Traefik labels, so there is no hostname for it. Mailpit is on `platform` only; its UI is routed by Traefik behind forward-auth.
**Why.** Apps call ops directly and authenticate with their token, not through the login front door, so a public route would only add exposure. Apps can't reach Mailpit or appdata-db's `annex_ops` database directly: ops is the only door. Any app on `edge` can reach ops, but without a valid token it only gets a 401.
**Alternatives.** Routing ops through Traefik (needed only for webhooks, M1.5, which will get its own route). A dedicated `ops` network for apps (per-app networks are still open for later).

## D37. The user directory file (M1.2, 15 Oct)
**What.** `vm/bootstrap-secrets.sh` writes `state/directory/users.json` (login → name, email, groups; no password hashes) from the same `USERS` list that produces Authelia's file, on every run. ops mounts the **directory** read-only and re-reads the file when its mtime changes. Unlike everything under `secrets/`, the directory is world-readable, because ops runs as uid 10001.
**Why.** Apps know logins (from `Remote-User`), not addresses. Mounting a directory avoids the stale single-file mount (D7). Authelia's own file holds password hashes, so ops must not read it.
**Alternatives.** Querying Authelia (no API for this in file mode). Putting emails in `annex.yaml` (apps would hard-code addresses, and the client's user list is the source of truth).

## D38. Defaults chosen in the M1.2 plan (15 Oct)
1. **Slack without a workspace.** The endpoint, the channel allowlist and real `chat.postMessage` delivery are built. With no `SLACK_BOT_TOKEN` an allowed post returns 503 "Slack isn't connected yet" and is logged `unavailable`; the deploy warns when `notify.slack` is declared. Real delivery is untested until a human adds a token (setup.md §7): the tests run the refusal path and the "not connected" path, and `test-notify.sh` checks `ctl info` to report which one ran.
2. **Mailpit's UI is admins only** (`kaldi-admin`), with both Authelia rules, since it shows everyone's mail.
3. **Allowed recipients:** domains in `ANNEX_EMAIL_DOMAINS` (default `annex.example`), plus exact addresses in `ANNEX_EMAIL_ALLOW` (empty by default; in production the client approves each). All or nothing: one refused recipient sends nothing.
4. **Recipients can be logins**, resolved from the directory.
5. **One template, `notice`** (title, body, optional link; every value HTML-escaped, only http(s) links). Real templating is Goat Mailer's job.
6. **Rate limits:** 60 emails and 60 Slack posts per hour per app (`OPS_EMAIL_PER_HOUR`, `OPS_SLACK_PER_HOUR`), counted from `sent` rows.
7. **No attachments**: a 400 says to link to a file in the app instead.
8. **`monthly-report`:** a failed email fails the job; a failed Slack post only warns. Only the `monthly` job notifies; `refresh` (every 10 minutes) doesn't, so Mailpit isn't flooded.
9. **Schema by idempotent SQL**, applied by `push.sh` before the full `up` (ops's healthcheck needs the database). No migration framework; later milestones append to `annex_ops.sql`.
Also: every attempt past authentication is logged, including malformed or oversized bodies, with the reason and never the body. Reserved app names now include `mail`, `ops`, `status`, `hooks` and `console` (the last three for M1.3, M1.5 and M2). The deploy result has a generic `warnings` list; the memory warning from D33 is its first entry, so the earlier singular `warning` field is gone.

## Resource record: M1.2 (15 Oct)
Extras on, three apps (38–47 MiB each). **New: ops 46 MiB of 128, Mailpit 12 MiB of 64** (budgets were about 60–80 and 30). Others as before: Metabase 1.35 GiB, Authelia 164 MiB, Homepage 106 MiB, appdata-db 101 MiB, everything else under 40 MiB. VM: 2.7 GiB used, 1.2 GiB available, no swap. Disk: 8.1 GB of 29 GB; the ops image is 228 MB (it shares the Python base with the app images).

## D39. Incidents, not alerts (M1.3, 15 Oct)
**What.** The alert engine (`ops/annex_ops/engine.py`) turns *signals* into *incidents*. A signal (`down`, `stopped`, `crash-loop`, `unhealthy`, `oom`, `errors`, `job-failed`) belongs to a *subject*: `app:<name>`, `job:<app>/<job>` or `platform:<service>`. A subject has at most one open incident (a unique index enforces it). The first signal opens it and sends the alert; later ones are added without a message; "resolved" goes out once every signal has cleared; a reminder follows after 1 h, then every 24 h. This is what makes "stop an app", which both Docker and Gatus notice, produce exactly one alert.
**How it's built.** The engine is pure: it takes `(now, input)` and returns actions (`open`, `add_signal`, `clear_signal`, `resolve`, `remind`, `forget`), with no database, network or clock of its own, so every rule has a unit test (125 tests in `ops/tests`). One thread (`incidents.py`) is the only consumer of a queue fed by the Docker watcher, the log followers and the Gatus webhook, and the only writer, so there are no races. It persists incidents in `annex_ops.incidents` (with a history of every signal raised and cleared, for the M2 console) and sends the messages. On restart it reloads open incidents and a reconcile pass re-reads Docker (and Gatus's last results), so a push, which recreates ops, neither loses nor repeats an alert.
**Details that came up while building it.**
- Containers are tracked by Docker id, not name: a Compose recreate (new container, old one destroyed a moment later) must not look like a crash. A destroyed container takes its own signals with it. If a sibling container of the subject exists (a recreate), the app's owner hears "resolved"; if none is left (`annex remove`, `extras.sh off`), the incident, and the app's job incidents, close **without a message**.
- A single OOM kill is held until the container has been quiet for `crash_stable_s`, like a crash loop, instead of opening and resolving within seconds of its restart.
- A `down` or `errors` signal restored from the database after an ops restart is still evaluated, so it can't get stuck (found while writing the restart tests).
- Maintenance (below) suppresses opening and reminders. An incident that was already open still sends its "resolved", because its recipients were told about the problem.
- `_platform` alerts go through M1.2's `send_email`, so they count against the hourly limit of 60 emails. One email goes to all recipients; if one address is refused, each gets its own, so one bad address can't silence the rest.
**Alternatives.** One alert per signal with de-duplication by time: simpler, but "exactly one" then depends on timing. Giving each source (Gatus, Docker, logs) its own alerting: three channels to configure and three messages for one outage.

## D40. Recipients and the status page (M1.3, 15 Oct)
App problems go to the owner (the `com.kaldi.annex.owner` label, resolved to an email through the user directory), to logins and Slack channels in the app's `alerts.to` (contract v1, optional, default `[owner]`; label `com.kaldi.annex.alerts-to`) and to every user in group `admins`. Platform problems go to admins only. With Slack connected every alert is also posted to `#annex-alerts` (`ANNEX_ALERT_SLACK_CHANNEL`). An owner who isn't in the directory means admins get it with a note, and `annex deploy` warns about it (so does it for `alerts.to` logins, and for channels when Slack isn't connected). The status page is visible to every signed-in user (the existing catch-all Authelia rule): it shows app names and up/down, which the discovery page already shows. **Containers are the registry:** ops reads owner, recipients and job names from the app container's labels through the read-only socket proxy, so there is no registration API to keep in sync. `Manifest.alerts_to` has a default so manifests stored by older releases load for rollback (D30).

## D41. Spike results (M1.3, 15 Oct)
1. **Gatus reload.** A new YAML file in a mounted directory is picked up by itself after about 25–30 s (the API is unavailable for a few seconds during the reload). A *deleted* file is **not** noticed: its endpoint was still being monitored 90 s later. So the CLI restarts `annex-gatus-1` whenever an app's file changes (written with different content, or deleted by `remove` or a redeploy to a job-only manifest), and not when it's unchanged. The deploy writes the file after the health gate, because Gatus checks immediately when it restarts, and the app should be up by then.
2. **Gatus API.** `GET /health` returns `{"status":"UP"}`. `GET /api/v1/endpoints/<group>_<name>/statuses` returns `results[]` with `success`, `status`, `timestamp`, `errors` (only when failing); the key is `apps_<app>` or `platform_front-door`; an unknown key is a 404; `GET /api/v1/endpoints/statuses` lists all.
3. **Ofelia's lines** (ANSI colour codes, plus Ofelia's own timestamp): `ERROR [Job "alert-probe-check" (f4ee8fcf0adc)] Finished in "288.2ms", failed: true, skipped: false, error: error non-zero exit code: 1`; a good run is the same line at NOTICE with `failed: false ... error: none`. The error text can contain commas, so the parser takes everything after `error: `.
4. **Docker events through `socket-ro`.** `filters` with `type=container`, the Annex label and `event=die,start,stop,destroy,oom,health_status` work server-side: `die` excludes the noisy `exec_die`, and `health_status` matches `health_status: healthy`. `die` carries `exitCode` as a string; on `docker stop` the order is `kill`, `stop`, `die`; every event has the Compose project and service labels. The stream is chunked, so it has to be read through `HTTPResponse`, not the raw socket.
5. **Front door.** From a container on `annex-edge`, a request to Traefik's address with the home hostname answers 302 to the login **only if the request has an `Accept` header** (`*/*` or `text/html`); with none, which is what Gatus's Go client sends, Authelia answers 401 with the same `Location`. **This differs from the plan**, which expected 302: the Gatus endpoint sets `headers: {Accept: text/html}` and still expects 302.

## D42. Job failures come from Ofelia's log (M1.3, 15 Oct)
Docker `exec_die` events only carry the command, and `monthly-report`'s two jobs run the same command; they also can't tell a scheduled run from `annex run`, whose caller already sees the exit code. Ofelia's log names the job and says `failed: true/false`, so `follow_logs.py` follows `annex-ofelia-1` and maps the job key to `(app, job)` through the `ofelia.job-exec.<key>.schedule` labels (app and job names both contain `-`, so a key is never split). Runs are kept in `annex_ops.job_runs` for 90 days and shown by `annex status`. A job's incident resolves on the next good run of *that* job. (This deviates from the M1 plan's wording, which said exec events.) The follower is generic (a container name, a handler, a cursor in `annex_ops.log_cursors`) so M2.2 can reuse it to ship every container's logs. A first start reads the last 10 minutes; later starts resume from the cursor, which is saved the moment a line arrives (at most every 2 s), and a replayed job run isn't recorded twice.

## D43. Grace period, maintenance and thresholds (M1.3, 15 Oct)
- **60 s before "stopped".** Deploys, rollbacks, `secret set` and the Ofelia restart (D18) stop a container for a few seconds, and none may alert. Only `restore.sh` and `rollback --with-data` can take longer: annex_vm opens a maintenance window (`app:<app>`, 15 min) before stopping the container, and ends it afterwards (for `rollback --with-data` the CLI ends it once the release is running). A maintenance window on `app:x` also covers `job:x/*`. `ctl maintenance` is the manual form; ops syncs the table every tick.
- **Thresholds**, all overridable: down = 2 failed Gatus checks 60 s apart, cleared by 2 good ones; crash loop = 3 non-zero exits within 10 min, cleared after 10 min without one while running; error spike = at least 5 % 5xx **and** at least 10 in 5 min (from Traefik's access log, `RouterName` `app-<name>@docker`), cleared after 5 min below; reminders after 1 h and then every 24 h. Defaults live in `Tuning` (engine.py); `OPS_<NAME>` environment variables and `state/ops/tuning.json` (re-read when its mtime changes) override them. Production never has the file.
- **Extras:** Metabase and Dozzle being stopped is never alerted (`scripts/extras.sh off` does that on purpose); being unhealthy while running is.

## D44. Gatus: a derived image, the edge network, no external heartbeat (M1.3, 15 Oct)
- **Image.** `twinproduction/gatus:v5.37.0` has no shell or `wget` (checked on the VM: root, entrypoint `/gatus`, no healthcheck), but every platform container keeps a healthcheck (CLAUDE.md). `images/gatus/Dockerfile` copies a static `busybox:1.37.0-musl` in as `/bin/wget`. It's built on the VM like ops. The config is mounted under `/annex` with `GATUS_CONFIG_PATH`, never `/config`, where the image's sample config (7 endpoints) would be merged in.
- **Networks.** Gatus is on `annex-edge` so it can probe apps on their internal port, and on `annex-platform` for Traefik's route, Homepage's widget and its webhook to ops. Traefik has the home and auth hostnames as aliases on `edge`, so Gatus checks the front door by its real name. **Accepted:** any app (or any container on `annex-edge`) can read Gatus's API and so see which apps are up, without logging in. That is not data, and the discovery page shows it anyway.
- **Gatus's webhook** to `ops:8000/internal/gatus` carries `OPS_INTERNAL_TOKEN` (generated on the VM, never an app token); identifiers are in the URL and the errors in a plain-text body, because `[RESULT_ERRORS]` can contain quotes.
- **No external heartbeat.** If the VM dies, nothing on it can say so. That needs a check from outside the LAN, which the brief rules out; it's a production item (an external uptime monitor pointed at the status page or the front door).

## D45. Apps deployed before M1.3 (M1.3, 15 Oct)
Web apps get their Gatus check and the `alerts-to` label on their next deploy. `client-lookup`, `monthly-report` and the test probe were deployed during the M1.3 build. **`vibe-leads` was left as is** (not built by the build agent, like D32): it has no Gatus check until its next deploy. Docker events (stopped, crash loop, unhealthy) and Traefik's 5xx counts already cover it, and with no `alerts-to` label it alerts its owner and the admins by default.

## Resource record: M1.3 (15 Oct)
Extras on, three apps (38–48 MiB each). **New: Gatus 7.5 MiB of 64** (budget about 30), and ops is unchanged at 49 MiB of 128 with the engine and followers running (budget +10–20). Others: Metabase 1.14 GiB, Authelia 165 MiB, Homepage 117 MiB, appdata-db 102 MiB, Mailpit 21 MiB, everything else under 40 MiB. VM: 2.5 GiB used, 1.4 GiB available, no swap. Disk: 8.3 GB of 29 GB; the Gatus image is 80 MB (ops 229 MB, sharing the Python base with the apps).
**Test timing:** `scripts/test-alerts.sh` took about 19–21 minutes in full (stop 240 s, crash loop 50 s, 500 burst 140 s, failing job 160–190 s, platform stop 213 s, plus setup and teardown). Gatus's 60 s interval and the "exactly one" settle waits set the floor.
**Found by the tests and fixed:** a scheduled job fails while its app is stopped (Ofelia can't exec into it), which opened a second incident and a second alert for one outage. A job failure while its app is stopped or has an open incident is now ignored as a consequence (a real job bug alerts at its next run after the app is back).
**Slack:** `#annex-alerts` must exist and the Slack app must be invited to it; until then each alert's Slack post is logged as failed (`channel_not_found`, with the fix) and email is unaffected.

## D46. Usage stats: what counts, where it's kept, who sees what (M1.4, 15 Oct)
1. **Per-person usage is acceptable, admins only** (Nejc, 15 Oct; M2 plan). Totals appear on tiles, in Metabase, in the digest and in `annex usage`. Logins appear only in `annex usage <app> --by-user`, which prints a reminder that it's personal data. In the prototype CLI access means SSH access, which means admin.
2. **What counts:** a line on an `app-<name>@docker` router with a non-empty `request_Remote-User` (Traefik's access log already keeps that header). Not counted: the app's health path (new label `com.kaldi.annex.healthcheck`, so a custom `healthcheck` is honoured), static assets (by extension), anonymous requests (login redirects) and platform routers. The query string is dropped before storing (it can hold client names) and the path capped at 200 characters; `Duration` is converted from nanoseconds to ms.
3. **Storage:** raw rows in `annex_ops.requests` for 30 days, deleted by the rollup; `usage_daily` and `usage_weekly` (no logins) kept indefinitely; days and ISO weeks (Monday start) in Europe/Ljubljana. The rollup runs every 10 minutes and never overwrites `synthetic` rows. A unique index on the row's content plus `ON CONFLICT DO NOTHING` drops the lines a restart replays from the follower's cursor (which is saved at most every 2 s); ops flushes its buffer when it shuts down, so a restart neither loses nor repeats rows (tested). A hard crash can lose up to 5 s of buffered rows.
4. **Signals:** *graduation candidate* = the last 4 complete weeks never fell, the latest had at least 5 users and more than the first; *dormant* = no signed-in request for 30 days and Annex first saw the app at least 30 days ago. Both thresholds are settings. The rules live in Python (`usage.py`, pure and unit-tested at their edges); the rollup writes the verdict to `annex_ops.app_signals` and the `usage_apps` view reads it, so there is one place for the rules. With three demo users graduation can only show with real history.
5. **The digest** goes out Mondays 08:00 local time: admins get every app, each owner their own, never a login. It covers the last complete week (a Monday-morning digest summarises the week that just ended) with the 4-week trend as numbers (`3 → 4 → 4 → 6`), error rate, worst daily p95, users in the last 7 days, and the graduation and dormant sections. It is sent once per week (`annex_ops.digests`, the week is recorded before sending), also if ops restarts that day; a fresh install on a Wednesday doesn't send one at once, because it only catches up within 24 hours of the scheduled time.
6. **Tiles call ops without a token** (`GET /tiles/<app>`, totals and status, never logins). Homepage proxies the widget call on its own server, so the browser never calls ops. **Accepted:** apps (any container on `annex-edge`) can read these totals too.
7. **Metabase reads summaries only**, through the role `metabase_usage`: `SELECT` on `usage_daily`, `usage_weekly` and the view `usage_apps`, nothing else (checked: denied on `requests`, `sends`, `incidents` and `app_grants`). The view runs with its owner's rights, which is why it can use `requests` for rolling 7- and 30-day distinct users without the role having access. Normal Metabase users (`cm`, `sam`) can open the dashboard too: it holds only totals, so that matches "owners and the client see totals".
**Alternatives.** Per-request events pushed by apps (every app changes; not "zero effort"). Counting every request incl. assets (inflates numbers 10–50×). Computing signals in SQL (harder to test). Showing logins in Metabase (it would leak to every Metabase user).

## D47. M1.3's interfaces and the spikes (M1.4, 15 Oct)
**Step 0, what M1.3 actually delivered vs the plan's names.** The generic follower is `follow_logs.py` (`Follower`, `CursorStore` in `annex_ops.log_cursors`). The Traefik handler is `incidents.traefik_handler(mgr)`, and `follow_logs.parse_traefik` only returns `(app, ts, status)`, so M1.4 added `access_entry`, `entry_app` and `entry_ts` there and hooks usage into the same handler (one follower, the JSON parsed once into an entry that both the engine's request and `usage.parse_access_entry` read). `config.tuning()` returned only the engine thresholds, so it became a generic `_settings(cls)` that also serves `usage_settings()` (same `tuning.json`). Background threads start in `incidents.start_background()` from the FastAPI `lifespan` (off with `OPS_WATCHERS=0`); the usage and digest threads start there, and a new teardown flushes the usage buffer. `docker_api.containers()` gave labels; `ctl app-status` exists and now also returns the usage totals for `annex status`.
**Spike 1, Homepage `customapi` from Docker labels (v2.5.0):** works as written. `homepage.widget.type`, `.url`, `.refreshInterval` and `.mappings[N].label/.field` produce the widget in `/api/services`, and `/api/services/proxy?type=customapi&group=…&service=…&index=0` returns the data fetched on Homepage's server. A second field beside the number works with `homepage.widget.mappings[0].additionalField.field` (and `.color=adaptive`), so the tile shows "Users 7d 3 ↑2" and "Status ok". Tile rendering itself is client-side; the tests check the services list and the proxied JSON, not pixels.
**Spike 2, Metabase v0.64.2.2 API:** `POST /api/collection`, native `POST /api/card` (`dataset_query.native.query`), `POST /api/card/:id/query` (`data.rows`, `data.cols`), `POST /api/dashboard` and `PUT /api/dashboard/:id` with `dashcards` (`id: -1` for new ones) all work with the shapes in the plan. `GET /api/collection/:id/items` lists cards and dashboards, which makes the setup idempotent; deleting a dashboard or card is `DELETE` (204). Piping a script into `ssh annex bash` doesn't work with `docker compose exec -T` (it swallows stdin): the setup script runs from a file, and its `mb` helper now closes stdin for GET and DELETE.

## D48. Differences from the plan (M1.4, 15 Oct)
- **Removed apps** get `app_seen.removed_at` (set by the rollup when the app's container is gone, cleared if it returns). The `usage_apps` view hides them, so a removed test app isn't reported as dormant for ever; their history stays in the summaries and `annex usage <app>` still shows it. (Not in the plan: the plan said history stays, but not how to keep the dormant list honest.)
- **Rolling vs ISO windows:** tiles, `annex usage` and the dashboard's table use rolling 7 days for users and p95 (from the raw rows) and the last 7 calendar days of `usage_daily` for requests and errors; the digest uses ISO weeks. `users_prev_7d` (the 7 days before) drives the "↑2" trend. `p95_7d` is exact (from raw rows) rather than derived from daily summaries.
- **View `usage_apps`** also carries `users_prev_7d`, `errors_7d` and `week1`–`week4` (the last four complete weeks, oldest first), and takes `signal` from `app_signals`.
- **Digest options:** `ctl digest` shows the plan without `--send`; `--to`, `--dormant-days` and `--min-age-days` as in the plan.
- **Step 7 (`scripts/seed-usage.sh`) is written but not run**, as Nejc asked. The plan contradicts itself there: a graduation candidate needs the last four complete weeks (1 to 4 back) to rise, but the script seeds weeks 3 to 10 back and must not touch the last two, so seeded history alone can't produce one. The script follows the "never touch the last two weeks" rule and says so; making one app show as graduating would need either seeding weeks 1–2 as well (which would shadow real data, since the rollup never overwrites synthetic rows) or a demo-only fake app. Left for Nejc to decide.
- **Timezone:** the SQL view and Python use the constant `Europe/Ljubljana` (the `TZ` setting's default); changing `TZ` would not move usage days and weeks.

## D49. Apps deployed before M1.4 (M1.4, 15 Oct)
`client-lookup` and `monthly-report` were redeployed during the build and carry the health-path label and the tile widget. **`vibe-leads` was left as is** (not built by the build agent, like D32 and D45): it has no tile widget and no health-path label until its next deploy, so its own health checks (if any go through Traefik) would count as usage until then. Annex does see it (the reconcile pass records every app container in `app_seen`). Usage history of the throwaway test apps (`usage-probe`, `usage-idle`) stays in the summaries after `annex remove`; it is platform data, not app data.

## Resource record: M1.4 (15 Oct)
No new containers. Extras on, three apps (38–48 MiB each). **ops 49 MiB of 128** (the usage and digest threads add nothing measurable on top of M1.3's 49 MiB), Gatus 8 MiB, Metabase 1.15 GiB (it now has a second connection and 4 cards), Homepage 108 MiB. VM: 2.5 GiB used, 1.4 GiB available, no swap. Disk: 8.3 GB of 29 GB. The `annex_ops` database is **8.5 MB** (59 raw request rows, 3 daily and 3 weekly rows after the tests; raw rows are capped by the 30-day retention, at about 100 bytes per signed-in request).
**Tests:** `scripts/test-usage.sh` 52/52 (about 7 minutes: two static apps are built and deployed, the scripted session runs, ops is restarted once). The first run found a bug in the script itself (quote escaping in a helper), not in the platform; everything else passed on the first run.

## D50. Spike results (M1.5, 15 Oct)
1. **Authelia `bypass` through forward-auth for another host:** works. With a `bypass` rule for the hooks host first in `access_control.rules`, a request with no session reaches the backend (no 302, no 401) whether or not it has an `Accept` header (D41's difference doesn't apply to bypass). Client-sent `Remote-User`, `Remote-Groups` and `Remote_User` arrive at the backend **absent** (strip-identity ran, and with no session Authelia sets nothing).
2. **Traefik labels on a container:** a router `Host(...) && PathPrefix(`/h/`)` with router-level `ratelimit` (average 20, burst 40) and `buffering.maxRequestBodyBytes=1048576` middlewares sits on top of the entrypoint middlewares. On the hooks host, `/v1/me`, `/healthz`, `/internal/gatus`, `/tiles/...` and `/` are a **404 from Traefik** (no router matches); a 900 KB body passes and a 2 MB body gets **413**. Sequential curls through the tunnel are too slow to trip the rate limit (about 10/s); 300 requests with 60 in parallel gave 51 × 200 and 249 × 429.
3. **Signature formats, checked against each provider's current documentation (15 Oct 2026):**
   - **GitHub:** `X-Hub-Signature-256: sha256=<hex HMAC-SHA256(secret, body)>`. Published vector (secret `It's a Secret to Everybody`, payload `Hello, World!`) reproduces: `757107ea…043e17`.
   - **Slack:** `X-Slack-Signature: v0=<hex HMAC-SHA256(secret, "v0:<X-Slack-Request-Timestamp>:" + body)>`, replay window 5 minutes, compared with an HMAC compare function. The docs' worked example (secret `8f742231b10e8888abcd99yyyzzz85a5`, timestamp `1531420618`, the `/webhook-collect` body) reproduces `v0=a2114d57…b503` (pinned in the unit tests). A `url_verification` request has `type`, `challenge`, `token`; the endpoint answers 200 with the challenge as plain text.
   - **Stripe:** `Stripe-Signature: t=<unix>,v1=<hex>[,v1=...][,v0=...]`; signed payload `"<t>." + body`; HMAC-SHA256 with the endpoint secret; several `v1` appear while a secret is being rolled (any may match); only `v1` counts (`v0` is a test scheme, ignored to prevent downgrade); the libraries' default tolerance is 5 minutes. No published vector: the unit test pins one computed with `openssl`.
   - **Trello:** `X-Trello-Webhook: base64(HMAC-SHA1(secret, body + callbackURL))`, the URL exactly as given when the webhook was created; Trello sends a `HEAD` to the callback URL on creation and refuses to create the webhook without a 200. No published vector: pinned from `openssl`.

## D51. The hooks route: the only one without a login (M1.5, 15 Oct)
**What.** `hooks.<domain>` (`ANNEX_HOST_HOOKS`) is routed by Traefik labels on the ops container: `Host(...) && PathPrefix(`/h/`)` to port 8000, with `hooks-ratelimit` (20/s per source, burst 40) and `hooks-limit` (1 MB body) as router middlewares. Authelia has a `bypass` rule for that host, first in the list; `strip-identity` still runs, so no request there carries trusted `Remote-*` headers. ops adds a second layer: a host guard middleware answers 404 for **any path outside `/h/` when the `Host` is the hooks host**, so a routing mistake can't expose `/v1/*`, `/internal/gatus` or `/tiles` (tested directly against ops with that Host header, including path-traversal attempts). Everything answered on that host is terse (`{"error": "signature check failed"}`, `not found`, `slow down`): the reasons go to ops's log and `annex hooks`, and never include a body, a secret or a signature.
**Why two layers.** This is the one route without a login; either layer alone fails open on a mistake.
**Production note.** This hostname will be the only internet-facing endpoint. It will need a WAF or the providers' published IP ranges in front of it, and its own monitoring and alerting (the status page and alert engine here cover the platform, not an external probe of this route). In the prototype (LAN only) no real provider can reach it: test events are signed on the Mac and sent through the SSH tunnel.
**Alternatives.** A separate small receiver service (another container for the same code path; the host guard gets the same isolation inside ops). Authelia's per-path rules instead of a host (paths on the shared app hosts would be public, and an app could shadow them).

## D52. Signing secrets, tokens and what the app must check (M1.5, 15 Oct)
**What.** A webhook names a secret declared under `secrets:`; a human sets it with `annex secret set`. `annex_vm.py provision --hooks` copies the values into `/srv/annex/secrets/hooks/<app>.json` (`{token, port, hooks: {name: {verify, secret, path}}}`), written atomically and chowned to uid 10001 with mode 600; the directory stays owned by the `annex` user (755) so the next provision can replace the file, and it is mounted read-only into ops (the directory, not the file: D7). **Secrets never go in the database, ops logs or any CLI output** (the test greps for all three random values everywhere). A per-app `ANNEX_HOOK_TOKEN` is injected into the app (and written to the hooks file); the app checks `X-Annex-Hook-Token` with the runtime helper `hook_event(headers)` (`hookEvent(req)` in node), so nobody can reach the endpoint around Annex. (`ANNEX_OPS_TOKEN` can't be reused: ops stores only its hash, D35.) `annex secret set` re-provisions, so rotating a signing secret updates what ops checks; a release without `webhooks:` (or a rollback to one) deletes the file and token, and its URL is a 404. `Manifest.webhooks` has a default and `from_dict` rebuilds `Webhook` objects, so older releases still load (D30).
**Alternatives.** Secrets in `annex_ops` (rejected: D35's reasoning, plus bodies are already there); a JWT as the app token (stateless, but it can't be rotated per app without a signing key shared with every app).

## D53. Receive, store, deliver (M1.5, 15 Oct)
- **Answer first.** After the signature checks out, the event is stored (`annex_ops.hook_events`, `pending`) and the provider gets 200 `{"ok": true, "id": N}` at once; Slack's `url_verification` is answered with its challenge and not delivered. Signature headers are not stored or forwarded (Annex has checked them); only `Content-Type`, `User-Agent`, `X-GitHub-Event`, `X-GitHub-Delivery` and `X-Slack-Retry-Num` are. The raw body is read before anything parses it. `HEAD /h/<app>/<hook>` is 200 for a declared hook (Trello's registration check).
- **Delivery** is at least once, by one worker thread every 2 s: `POST http://annex-app-<app>:<port><path>` with a 10 s timeout, redirects not followed, headers `X-Annex-Hook`, `-Event` (the original id for a replay), `-Attempt` and `-Token`. 2xx = delivered. Otherwise the next attempt follows the backoff 10 s, 30 s, 1 min, 5 min, 15 min, then hourly, until the event is 7 days old (then `failed`). Within one hook, events go oldest first and a failing event holds back newer ones (tested with pure functions). One thread means one slow app can delay others by its 10 s timeout; fine at this volume.
- **Storage:** bodies are kept 7 days (retries and replay), so they are in the nightly dump for that long; events older than 7 days are deleted daily. Rejected requests are logged with the reason and **no body**. Pending events of an app that no longer declares the hook (or was removed) become `failed` with that reason.
- **Replay** (`annex hooks replay`, human only, asks first): copies of the stored events as new `pending` ones with `replay_of` set; the app sees the same `X-Annex-Hook-Event` and attempt 1.
- **Human-only CLI:** `annex hooks <app>` (metadata only), `annex hooks show <id>` (headers and the body, truncated to 64 KB, with a reminder that it can hold personal data) and `replay`. Nothing over MCP.

## D54. Alerting and differences from the plan (M1.5, 15 Oct)
- **The `webhook` signal.** If a hook's oldest pending event has been failing for more than 15 minutes (`hook_alert_after_s`), the delivery worker reports it to the M1.3 engine (`HookFailing`, repeated every cycle; `HookOk` when it recovers), giving the signal `webhook` on `app:<app>`: it joins the open incident of a stopped app (one alert, one "resolved", tested) or opens its own. Reports that stop for 60 s (the worker died, or ops restarted with a restored signal and the hook has since recovered) clear the signal, so it can't get stuck. Removing the app closes it quietly.
- **Differences from the plan.**
  - **Rate limit order:** the 60 events per minute per hook limit counts only *verified* events, and is checked after the signature (the plan had it before). Otherwise anyone could exhaust a hook's quota with junk and lock out the real provider. Rejected requests are answered 401 for free, and only 30 per minute per hook are written to the table, so a flood can't fill it; Traefik's per-source limit comes first.
  - `GET` on a hook URL is a terse 405; `ctl hooks` takes `list`, `show` and `replay`.
  - The plan's "rollback revokes" test deploys a copy of the fixture without `webhooks:` (a normal deploy; the rollback path uses the same provisioning with the older manifest).
  - The burst-over-the-limit check is in the suite and passes (Traefik answers 429 first; ops's own limit is unit-tested).
- **vibe-leads:** has no webhooks, so nothing about it changes.

## Resource record: M1.5 (16 Oct)
No new containers. Extras on, three apps (41–48 MiB each). **ops 50 MiB of 128** (the delivery worker and the new routes add about 1 MiB), Traefik 27 MiB, Gatus 6 MiB. VM: 2.5 GiB used, 1.4 GiB available, no swap. Disk: 8.3 GB of 29 GB. The `annex_ops` database is **8.7 MB** (after the tests: 9 delivered and 32 rejected webhook events from the last run; the earlier test runs' events of the removed probe stay until the 7-day purge).
**Tests:** `scripts/test-hooks.sh` 82/82 (about 5 minutes: three deploys of the probe, one stop and one longer stop). The first two runs stopped at once on a mistake in my script's helpers (a mangled `has` function from a sloppy edit), before reaching the platform; the third run passed everything.
**Spike evidence kept for later:** Traefik's rate limit is per source, and through the SSH tunnel every request comes from one source, so the suite's burst check exercises the limit, and a real provider's traffic would not be throttled by the tunnel test.

## D55. The webhook receiver runs in its own container (review follow-up, 16 Oct, approved by Nejc)
**What.** The hooks host is now served by `annex-hooks-1`: the annex-ops image with another command (`uvicorn annex_ops.hooks_app:app`), only the hooks files mounted (read-only), `OPS_WATCHERS=0`, networks `edge` (Traefik) and `appdata` (its database), and a new database role `annex_hooks` that may only `INSERT` into `hook_events` and read back the new id (`SELECT (id)`). It holds no SMTP access, Slack token, user directory, internal token or Docker access, runs no background threads, 96 MiB limit. Traefik's hooks router moved from ops to it; ops has no route at all again (as in D36) and keeps the delivery worker, retries, the `webhook` alert and `annex hooks`. The receive code moved unchanged from `app.py` to `hooks_app.py`, with its own catch-all guard (only `/h/`, and `/healthz` for its own healthcheck). New secret `APPDATA_DB_HOOKS_PASSWORD`, env file `secrets/hooks-receiver.env`.
**Why.** It's the only route without a login (D51), and in production the only one facing the internet. In M1.5 a bug in the receive path would have run in the process that holds every outbound credential and the alert engine; now its blast radius is "insert a webhook event". `test-hooks.sh` checks it: no Slack/SMTP/internal-token variables, only those two networks, no directory mounted, and the role is denied on bodies, the send log, grants and updates (92/92).
**Alternatives.** A second process inside the ops container (shares the environment and mounts). A separate code base (duplicates the verification code). Keeping D51's host guard alone (it was a second layer, not isolation).

## D56. The host and the backups are monitored (review follow-up, 16 Oct)
**What.** annex-ops samples the VM every minute (`host.py`) and the engine raises platform signals, alerted to admins only with the same incident rules as everything else: `platform:disk` (alert at 85 % used, cleared below 80 %), `platform:memory` (alert when under 256 MB is available for 5 minutes, cleared above 400 MB), `platform:backup` (alert when the newest backup is over 26 hours old). The gap between the alert and clear thresholds stops flapping. The messages say what to check (`docker system df`, `docker stats`, `scripts/backup.sh`). `ctl host` shows the numbers.
**How, without new privileges.** `/proc/meminfo` inside a container is the host's; `statvfs` on any bind mount reports the host filesystem. The backup's age is the **mtime of the backup container's `/archive` folder**, read with a `HEAD /containers/annex-backup-1/archive?path=/archive` through the read-only socket proxy: a stat, never a read. That covers the nightly run and `scripts/backup.sh` (which runs via `docker exec`, so it never appears in the container's log).
**Rejected.** Mounting `/srv/annex/backups` into ops: the archives are world-readable (644, written by offen as root) and hold every app's data, so ops would have been able to read them. Following offen's log: misses manual backups, and needs the last success to survive an ops restart. A node exporter: another component for three numbers.
**Tests.** `scripts/test-host.sh` (21 checks, about 4 minutes): ops's numbers match `df` and `/proc/meminfo` on the VM; disk, memory and backup each give admins exactly one alert and one "resolved", and the app owner nothing.

## D57. Annex's own user accounts (review follow-up, 16 Oct; Nejc: offer auth without company SSO)
**What.** Annex has its own accounts and doesn't need SSO; this is the mode for clients without one, and the demo's. Authelia's file backend stays the source of truth (`secrets/authelia/users.yml`); what was missing was managing it:
- `annex users list|add|set|remove|reset` (human only, CLI only; `annex_vm.py users-*`). **`add` emails an invitation**, which is Authelia's reset-password link (valid 24 h, `identity_validation.reset_password.jwt_lifespan`), so the person sets their own password and none passes through the CLI; `--print-password` prints a random one once instead. Passwords come from Authelia's own tool (`crypto hash generate argon2 --random`) so hashes match its settings. Fields are validated with fix-it messages; logins and email addresses are unique; **the last active admin can't be removed, disabled or demoted.**
- **Self-service**, enabled in Authelia: reset a forgotten password by email and change it in the settings page (a one-time code by email confirms it). Minimum length 12. The notifier is SMTP to Mailpit (it was a file).
- **The user directory** (`state/directory/users.json`, read by annex-ops) is now derived from `users.yml` by `annex_vm.py` on every change and by `push.sh` (`users-sync`); disabled accounts are left out, so they get no alerts or emails. `bootstrap-secrets.sh` writes it only on the first run, so a re-run no longer overwrites added users.
**Details found while building and testing it.**
- Authelia accepts a reset request only on its exact public URL including the port (`https://auth.annex.home.arpa:8443`), so `annex_vm.py` calls it through Traefik with that Host (`curl --connect-to …:8443:127.0.0.1:443`).
- Authelia rate-limits reset requests per source address (default 5 per 10 minutes), and every invitation comes from the VM's own address: raised to 30 per 10 minutes and 100 per hour (`server.endpoints.rate_limits.reset_password_start`), still limited against email bombing. The CLI explains a 429.
- **Writes to `users.yml` are atomic** (a new file renamed over it). An in-place rewrite let Authelia's watcher read a half-written file. And Authelia skips a change that arrives within about a second of its previous reload without catching up later (two quick `annex users` calls left a disabled user able to sign in), so `annex_vm.py` writes the same content a second time after 1.5 s. Both found by `scripts/test-users.sh`.
- Authelia writes the file in place itself (password changes), so it stays the `annex` user's, mode 600; its secrets folder is mounted read-write now.
- The users commands use PyYAML from Ubuntu's `python3-yaml` package (a cloud-init dependency, so present on every Ubuntu cloud image, AWS included); everything else in `annex_vm.py` stays stdlib.
- Invitations use Authelia's stock "Reset your password" email; customised templates are a later nicety. Metabase keeps its own accounts (D9).
**Tests.** `scripts/test-users.sh` (39 checks, about 1 minute): invitation → set password from the emailed link → sign in; self-service change with the emailed code; groups; disable/enable; admin reset both ways; the last-admin guards; removal; the directory and the file's owner and mode.
**Alternatives.** Company SSO (Authentik, Cloudflare Access): later, in front of the same headers, for clients that have one. Authelia's LDAP backend (another component). Managing users by editing YAML over SSH (what there was).

## D58. Review follow-ups: tests, runtime helpers, demo, docs (16 Oct)
From the M0/M1 review (Nejc: fix M0 and M1, but no wipe-and-rebuild before the AWS move; hold off connectors, the production shape and gateway realism; slim M2 approved).
- **Tests:** `scripts/testlib.sh` holds the helpers every suite had copied (results, the tunnel, logins, Mailpit), and the nine suites use it (184 duplicated lines gone). `scripts/test-all.sh` runs everything in order with a summary (`--slow` adds the 20-minute alert suite, `--only` picks some), including both unit test sets and the runtime check. **The Ofelia flake is fixed** instead of documented: `test-platform.sh` and `test-all.sh` wait for healthchecks to settle (`wait_healthy`) before asserting.
- **Runtime helpers have one source,** `runtime/annex_runtime.py` and `.js`. Templates, examples and fixtures keep copies (each folder must build on its own); `scripts/sync-runtime.sh` updates them and `--check` (in `test-all.sh`) fails if one differs. A deploy now warns when an app's copy differs from the platform's.
- **Demo:** the 5:00 line no longer claims the AI *can't* delete apps or set secrets: it says those aren't in the AI's toolkit, and that the server-side lock comes with the production deploy path. New 7:00 beat, `scripts/demo-bad-deploy.sh`: a broken version is rolled back by itself. setup.md §3 has an optional wildcard resolver (dnsmasq + `/etc/resolver`), so a live-built app with any name resolves without a hosts line.
- **Docs:** the build plans are in `docs/plans/` (`m1.md` is a copy of Nejc's M1 plan; `m1.2.md`–`m1.5.md` are the step-by-step plans the build agents followed).
- **Deferred, on purpose:** server-side enforcement of human-only actions and verified owner/deployer identity (they come with a deploy API, a later milestone), per-app networks and egress enforcement, connectors, the AWS shape and gateway realism, and the wipe-and-rebuild round trip (it happens with the AWS move). The M2 console must listen where only Traefik can reach it, because ops is reachable from apps on `annex-edge` (noted for the M2 plan).

## Resource record: review follow-ups (16 Oct)
Extras on, three apps. **New: the webhook receiver**, `annex-hooks-1`, 43 MiB of 96. ops unchanged at 51 MiB of 128 (the host sampler adds nothing measurable), Gatus 8 MiB, Authelia 165 MiB of 256 (after the test logins; argon2, D8/D20). VM: 2.6 GiB used, 1.3 GiB available, no swap. Disk: 8.3 GB of 29 GB (30 %).
