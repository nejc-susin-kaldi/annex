# Kaldi Annex: build brief, M0 homelab prototype

> **Historical.** This is the brief as written on 14 Oct 2026, kept for the why and the ground rules (which CLAUDE.md repeats). What changed since: the product data is Goat Arcade's, not the wealth demo (D112); the milestones are done through M2 and the demo is `docs/demo.md`; the repo layout is D117 (`docs/architecture.md`, "The repository"), not the one suggested below. The current system is `docs/architecture.md`.

For a build agent. [[Kaldi Annex]] explains the why; this brief is the how for the first milestone. Draft, 14 Oct 2026, owner Nejc Sušin.

## Goal
A working Annex for one client, running on a **dedicated, disposable VM on Nejc's homelab**:
- one Docker Compose file for the platform;
- an `annex` CLI and MCP server on Nejc's Mac that let an AI coding agent deploy small internal tools to the VM.

It must be good enough to demo to Kaldi management in ten minutes, and to validate the app contract before any AWS work. In production each client gets their own server in their own AWS account, so this VM is one client's server. Builders deploy to it from their own laptop, which is exactly what this setup tests.

## Getting started (for the build agent)
1. You've been given this brief and an empty folder on Nejc's Mac. The folder is the git root: run `git init` there. Nejc adds the GitHub remote later.
2. Copy this brief into the repo as `docs/brief.md`, so the repo stands on its own.
3. Create `CLAUDE.md` with the ground rules below and the repo conventions you choose.
4. Check access:
   - `ssh annex true` must work (verified 14 Oct).
   - Write `docs/setup.md` with the command for the `annex-vm` Docker context (`docker context create annex-vm --docker host=ssh://annex`) and its undo. Ask Nejc to run it, then confirm `docker --context annex-vm version`.
5. Work through the milestones in order. Show Nejc the result of each one before starting the next.

## Ground rules for the build agent
- **Synthetic data only.** Never use, copy or connect to real client data or systems, Cadro included.
- **LAN only.** No port forwarding, tunnels to the internet or public DNS. SSH port forwards from the Mac for testing are fine.
- **Pin every image version.** No `:latest`. Images must support amd64; the VM is x86_64.
- **Where you may write:**
  - on the VM, only Docker and `/srv/annex`;
  - on the Mac, only the repo.
- **Ask Nejc before:**
  - adding a component that isn't in the stack below;
  - changing the app contract;
  - changing VM system config (packages, firewall, users, systemd).

  `annex` has passwordless sudo, but that doesn't make system changes allowed.
- **System changes:** anything that changes the Mac or the homelab is never run by you. That covers trusting the mkcert CA, the Docker context, registering the MCP server in Claude Code, `/etc/hosts`, and the existing Traefik on `dronky`. Write the exact commands, and their undo commands, into `docs/setup.md` and ask Nejc to run them.
- **Log decisions** in `docs/decisions.md`: what, why, alternatives considered.

## Target VM
Set up on 14 Oct by Nejc's homelab agent.

| Item | Value |
|---|---|
| Host | `dronky`: libvirt/KVM (QEMU 7.2, libvirt 9.0); libvirt domain `annex`, UUID `ca16ac15-e6c6-49bc-90cb-6601a73a1bd9` |
| Hostname / IP | `annex`, 192.168.1.30/24, static, on bridge `br0`; router DHCP reservation for MAC `52:54:00:a7:13:30` |
| OS / architecture | Ubuntu 24.04.5 LTS (minimal cloud image), x86_64, kernel 6.8 |
| Size | 2 vCPU, 4 GiB RAM (3.8 GiB usable), 2 GiB swap file, 30 GB thin disk (25 GB free) |
| SSH | `ssh annex` → `annex@192.168.1.30`, key-only, via jump host `neon@dronky`; key `~/.ssh/annex_ed25519`; groups `annex sudo docker` |
| Docker | Engine 29.9.0, Compose v5.6.0, official apt repo; log rotation json-file 10m × 3 |
| `ANNEX_DATA_DIR` | `/srv/annex`, owned by `annex`, mode 750, empty |
| Firewall | inbound 22, 80 and 443 (TCP, plus UDP 443) from 192.168.1.0/24 only. **Any other published Docker port is blocked, even from the LAN.** |
| Other | starts automatically with `dronky`; security-only unattended upgrades, no automatic reboots; cloud-init disabled |
| Snapshot | `clean-base`, internal, taken shut down after a clean install |

**The Mac is not on the home LAN.** It reaches the VM only over SSH through `dronky`. Docker's SSH transport uses the system `ssh` and `~/.ssh/config`, so the `annex-vm` context works through the jump host. Builds upload their context over two hops, so they're slower.

**The Mac** (checked 14 Oct):
- arm64
- Docker Desktop 28.5 with Compose v2.40
- mkcert 1.4.4
- Python 3.13
- uv 0.9

macOS ships `openrsync`. If `push.sh` hits its limits, ask Nejc to install GNU rsync with Homebrew.

**`dronky` is short on memory.** Its swap use rose to 6.6 GiB when the VM started, with about 3.6 GiB still available. Keep the VM's actual usage low; see the resource budget.

## Reaching the web UI
**Still being decided.** The plan is for Nejc's existing Traefik container on `dronky` to forward Annex hostnames to the VM's port 80 or 443. Open questions:
- which domain;
- whether that Traefik terminates HTTPS, and with what certificate;
- how Nejc reaches it from outside the LAN.

Don't block on this. Build for both outcomes, chosen by config in `.env`:

| Setting | Meaning |
|---|---|
| `ANNEX_HOST_PATTERN` | how hostnames are built, e.g. `{name}.annex.home.arpa` (nested) or `annex-{name}.example.com` (flat). Platform services use fixed names: `home`, `auth`, `metabase`, `logs`, `gateway`. |
| `ANNEX_COOKIE_DOMAIN` | Authelia's session cookie domain, derived from the pattern |
| `ANNEX_TLS_MODE=local` | the VM's Traefik terminates HTTPS on 443 with a mkcert wildcard certificate (the default until the route is decided) |
| `ANNEX_TLS_MODE=upstream` | the upstream proxy terminates HTTPS; the VM's Traefik serves plain HTTP on 80 and trusts `X-Forwarded-*` headers only from `dronky`'s LAN IP [TBD] |

**Until the route exists**, prove things with curl through an SSH tunnel. No system changes are needed:
```
ssh -N -L 8443:localhost:443 annex &
curl --resolve home.annex.home.arpa:8443:127.0.0.1 https://home.annex.home.arpa:8443/
```
The login flow can be proven with curl too: follow the redirect, post the credentials, reuse the cookie.

## How the pieces run
*Historical: this is the M0 plan. Apps now run through `ssh annex` and the VM's Compose (D1), and the current system is in `docs/architecture.md`.*

- **Platform:** `compose.yaml`, `config/` and `db/` are synced from the repo to `/srv/annex/platform` by `scripts/push.sh` (rsync over SSH). `docker compose` then runs there over SSH, using the VM's Compose v5.6, because bind mount paths must exist on the VM.
- **Apps:** built and run through the `annex-vm` Docker context, using the Mac's Compose. Their persistent paths are absolute VM paths under `/srv/annex/apps/<name>`.
- **Secrets:** generated on the VM, live only in `/srv/annex/secrets`, and never touch the Mac or the repo.
- **TLS in `local` mode:** the mkcert CA is trusted on the Mac by Nejc. The wildcard certificate is generated on the Mac and copied to `/srv/annex/certs`.

## Teardown
When the demo stage is over:

**1. Destroy the VM.** On `dronky`, as `neon`. This deletes the disk and its snapshot.
```
virsh -c qemu:///system destroy annex
virsh -c qemu:///system undefine annex --snapshots-metadata --remove-all-storage
```

**2. Clean up the Mac.**
- Delete the `Host annex` block from `~/.ssh/config`. A backup is at `~/.ssh/config.bak-20261014-annex`.
- Remove the throwaway key and the host key:
  ```
  rm ~/.ssh/annex_ed25519 ~/.ssh/annex_ed25519.pub
  ssh-keygen -R annex
  ```
- Undo everything listed in `docs/setup.md`:
  - remove the `annex-vm` Docker context
  - unregister the MCP server from Claude Code
  - `mkcert -uninstall`, if the CA isn't used for anything else
  - remove any `/etc/hosts` entries

**3. Clean up the homelab.**
- Remove the router's DHCP reservation for `52:54:00:a7:13:30`.
- Remove the Annex route from the Traefik on `dronky`, if one was added.

`docs/teardown.md` repeats all of this, plus anything the build adds.

**To reset without destroying**, use one of these:
- **Roll back to `clean-base`.** On `dronky`, as `neon`. This loses everything since the snapshot, including `/srv/annex`.
  ```
  virsh -c qemu:///system destroy annex
  virsh -c qemu:///system snapshot-revert annex clean-base
  virsh -c qemu:///system start annex
  ```
- **Run `scripts/wipe-vm.sh`.** It asks for confirmation, then removes:
  - all Annex compose projects, containers, networks, volumes and images;
  - everything under `/srv/annex`.

**Labels and naming:** every container, network, volume and image Annex creates gets the label `com.kaldi.annex=true`. Compose projects are named `annex` or `annex-app-<name>`. All persistent state is bind-mounted under `/srv/annex`.

## Stack (prototype)
| Concern | M0 choice | Production candidate (decided in the investigation) |
|---|---|---|
| Routing and TLS | Traefik v3, Docker labels; mkcert wildcard certificate or upstream TLS | Traefik, or Coolify-managed |
| Login | Authelia, file-based demo users | Authentik or Cloudflare Access with Google/Microsoft |
| Deploys | `annex` CLI on the Mac → Docker Compose on the VM via SSH context | same CLI/MCP; backend may become Coolify |
| Discovery page | Homepage (gethomepage.dev), Docker label discovery | same |
| Analytics | Metabase OSS | Metabase, Pro where data is sensitive |
| Scheduler | Ofelia, label-based jobs | same, or the platform's scheduled tasks |
| Data gateway | Postgres views + PostgREST with per-app JWT roles | same pattern against a read replica |
| App storage | shared Postgres (one DB per app) + one directory per app | same |
| Backups | offen/docker-volume-backup (with pg_dump hooks) to `/srv/annex/backups` | same, to S3 |
| Logs / oversight | Dozzle | Grafana + Loki |
| AI deploy kit | MCP server (Python) + Claude Code skill + template `AGENTS.md` | same; ChatGPT later |

**Resource budget.** The VM has 3.8 GiB of RAM and 25 GB of free disk, and its host is already swapping. Keep the platform under about 2.5 GiB of RAM, and lower when idle:
- Every platform container gets a memory limit.
- Metabase, the heaviest piece: JVM heap capped at 1 GiB (`JAVA_OPTS=-Xmx1g`), container limit 1.5 GiB.
- Both Postgres instances are tuned small, e.g. `shared_buffers=128MB`.
- Metabase and Dozzle sit in a compose profile that is **off by default**. Start them for verification and demos.
- Prune build cache and dangling images after deploys. Prefer slim base images.
- At the end of each milestone, record `docker stats` and `df -h /` in `docs/decisions.md`.

## Architecture
```
Mac: annex CLI / MCP ──ssh (via dronky)──▶ VM docker engine
browser ─▶ [dronky Traefik, TBD] ─▶ VM :80/:443 ─▶ traefik ─forward-auth─▶ authelia
                  ├─▶ homepage   (discovery page)
                  ├─▶ metabase ─────────────▶ product-db (annex_api views, read-only)
                  ├─▶ gateway (PostgREST) ──▶ product-db
                  ├─▶ dozzle     (admins only)
                  └─▶ apps (one compose project each) ─▶ appdata-db, /data, gateway
ofelia (jobs) · backup · socket-proxy
```

**Platform containers:**
- traefik, authelia, homepage, metabase, dozzle, ofelia, backup
- socket-proxy (one per permission level if needed)
- product-db: Postgres 16. A synthetic "client product", loosely wealth-management shaped: clients, accounts, holdings, transactions, advisors. Seeded on first start.
- gateway: PostgREST.
- appdata-db: Postgres 16, with one database and user per app, plus Metabase's own database.

**Networks:**

| Network | Members | Never on it |
|---|---|---|
| `edge` | traefik and everything it routes to | — |
| `product` | product-db, gateway, metabase | apps |
| `appdata` | appdata-db, apps, metabase, backup | — |
| `socket` | socket-proxy and the services that need the Docker API | apps |

**Demo users** (Authelia file backend; passwords generated at bootstrap):

| User | Group | Can |
|---|---|---|
| `sam` | builders | deploy tools |
| `cm` | users | use tools |
| `kaldi-admin` | admins | everything, including Dozzle and the Metabase admin |

## App contract v0 (starting point)
Principle: the builder or their AI writes a small folder, and Annex does the rest. The CLI turns the manifest into:
- routing and login
- the discovery page entry
- schedules and storage
- backups and data access

Builders never write Docker labels, login code or database credentials.

```
my-tool/
  annex.yaml    # manifest, required
  Dockerfile    # from a template; must run as non-root
  ...code
```

```yaml
name: monthly-report          # DNS-safe, unique; hostname built from ANNEX_HOST_PATTERN
owner: sam                    # required; shown on the discovery page
description: Monthly positions report
type: web                     # web | job | web+job
port: 8080                    # web: listen on 0.0.0.0:$PORT (internal only; never published)
healthcheck: /healthz         # web: returns 200 when ready
access: internal              # only value in v0 (behind login)
storage:
  postgres: true              # dedicated DB, injected as DATABASE_URL
  files: true                 # persistent directory at /data, backed up
data:
  views: [monthly_positions]  # gateway views requested; must exist in the allowlist
schedule:
  - name: monthly
    cron: "0 6 1 * *"
    command: ["python", "report.py"]
env:                          # non-secret config only
  REPORT_TITLE: Monthly positions
secrets: [SMTP_PASSWORD]      # names only; values via `annex secret set`
egress: []                    # declared, not enforced in v0
```

**Runtime rules**
- Stateless container. Persist only to `DATABASE_URL` or `/data`. Anything else is lost on redeploy and isn't backed up.
- **Identity** comes from the `Remote-User`, `Remote-Name`, `Remote-Email` and `Remote-Groups` headers set by the front door. Never implement your own login.
- **Product data** only through the gateway. No direct database access to the product.
- **Secrets** arrive as environment variables at runtime, never in code, the repo or the image.
- Apps never publish ports; only Traefik does.
- Log to stdout/stderr.
- Defaults: 256 MB RAM, 0.5 CPU, non-root user, no privileges.

**Injected by Annex:**
- `PORT`, `ANNEX_APP_NAME`, `ANNEX_BASE_URL`
- `DATABASE_URL`, if storage.postgres is set
- `DATA_DIR=/data`, if storage.files is set
- `ANNEX_GATEWAY_URL` and `ANNEX_GATEWAY_TOKEN`, if data.views is set
- each declared secret

**Templates:**
- `python-web` (FastAPI)
- `node-web` (Express)
- `static` (any build output plus a tiny server)
- `python-job` (no web)

Each template ships with a Dockerfile, `annex.yaml`, a health check, and an example that reads the identity headers and calls the gateway. Each also has an `AGENTS.md` that teaches the contract to coding agents.

## CLI and MCP
The `annex` CLI is Python, managed with uv, and runs on Nejc's Mac. It deploys to the VM through the `annex-vm` Docker context. Each app becomes its own compose project, `annex-app-<name>`, attached to the platform networks.

| Command | Does |
|---|---|
| `annex init <template> <name>` | copy a template |
| `annex deploy [path]` | validate, provision DB/role/token, build, run, register |
| `annex list` | list deployed apps |
| `annex status <app>` | show an app's state |
| `annex logs <app>` | show an app's logs |
| `annex run <app> <job>` | trigger a job now |
| `annex secret set <app> KEY=VALUE` | store a secret in `/srv/annex/secrets` on the VM, readable only by the platform |
| `annex remove <app> [--keep-data]` | remove an app |
| `annex contract` | print the contract, for agents |

Validation errors must say exactly what's wrong and how to fix it, because agents will read them.

**MCP server** (stdio, Python):
- Exposes `annex_contract`, `annex_init`, `annex_deploy`, `annex_list`, `annex_status`, `annex_logs` and `annex_run`.
- No `remove` or `secret set` over MCP in v0; those stay human-only.

**Claude Code skill** (`skills/annex/SKILL.md`): when to use it, a summary of the contract, and the workflow: init → build → deploy → check the URL.

SSH Docker access is root-equivalent. That's fine for the prototype; production needs a narrower deploy path.

## Data gateway v0
- **Allowlist:** the `annex_api` schema in product-db holds curated views, e.g. `client_summary`, `top_clients_by_aum`, `monthly_positions`. The list is code in `db/product/api_views.sql`, so adding a view is a reviewed change.
- **Per-app role:** on deploy, the CLI creates the role `app_<name>` with SELECT on exactly the requested views, grants it to PostgREST's authenticator role, and mints a JWT. The JWT is injected as `ANNEX_GATEWAY_TOKEN`.
- **Errors:** requesting a view that isn't in the allowlist fails the deploy with a clear message.
- **Logging:** every gateway request is logged with the app's role.
- **Metabase** gets its own read-only role on `annex_api` only.

## Backups v0
- **Schedule:** nightly, into `/srv/annex/backups`, kept for 7 days.
- **What's covered:** appdata-db, Metabase's DB, and every app's `/data` directory. product-db is seed data and can be re-seeded.
- **Restore:** `scripts/restore.sh <app>` restores one app's database and files.
- **Proof:** `scripts/restore-test.sh` writes marker data, backs up, destroys, restores and verifies.

## Example apps (synthetic data, mirroring real cases)
- `client-lookup` (python-web): searches clients through the gateway and greets the signed-in user by name. Shows login, identity and scoped data.
- `monthly-report` (python-job + a small page): runs monthly and on demand. Writes a CSV to `/data` and a history table to its DB, and serves the latest report behind login. Replaces Cadro's SQL → CSV → email routine.
- `vibe-dashboard` (static): built entirely by Claude Code through the MCP in M0.4, as the proof that AI deploys work.
- **Homepage tiles:** Metabase, Dozzle, and placeholder links for the optional add-ons Goat Mailer (email templating) and Bekafon (AI website helper with voice chat).

## Milestones and definition of done
1. **M0.1 Platform**
   - `docs/setup.md` lists the Mac and homelab steps, each with its undo command.
   - `scripts/bootstrap.sh` generates secrets on the VM and, in `local` TLS mode, pushes the certificate.
   - `scripts/push.sh` syncs the platform files and starts the platform, and every container is healthy.
   - All three demo users can log in and reach the discovery page. Prove it with curl through the SSH tunnel, and in a browser once the route on `dronky` exists.
   - The discovery page lists the platform services.
   - Metabase (profile on) is reachable behind login and connected read-only to `annex_api`.
   - A test proves the app network can't reach product-db.
   - `scripts/wipe-vm.sh` returns the VM to empty, and bootstrap plus push bring it back.
   - Idle RAM use is recorded.
2. **M0.2 Contract and CLI**
   - `annex deploy examples/client-lookup`, run from the Mac, puts it live behind login, greeting the user by name.
   - It reads its granted views and gets denied on any others.
   - It appears on the discovery page with its owner.
   - `list`, `status`, `logs`, `secret set` and `remove` work.
3. **M0.3 Jobs, storage, backups**
   - `monthly-report` runs on schedule and via `annex run`, and its output survives a redeploy.
   - Nightly backups run on the VM, and `restore-test.sh` passes.
4. **M0.4 AI deploy kit**
   - From an empty folder on the Mac, Claude Code builds and deploys `vibe-dashboard` to the VM using the template, the skill and the MCP, with no human edits to the code.
5. **M0.5 Demo**
   - `docs/demo.md` has a 10-minute walkthrough for management, plus `scripts/reset-demo.sh` back to a clean demo state.
   - `docs/teardown.md` is complete.

## Security must-haves, even in v0
- **Exposed ports:** only Traefik publishes ports, and only 80 and 443. The VM firewall already blocks everything else, and allows those two from 192.168.1.0/24 only. No database ports published.
- **Docker socket:** never mounted into apps. Platform services reach it through a socket proxy with the minimum permissions each one needs.
- **Header spoofing:** strip any incoming `Remote-*` headers before forward auth, and prove it with a curl test. In `upstream` TLS mode, trust `X-Forwarded-*` only from `dronky`.
- **App containers:**
  - non-root
  - `no-new-privileges`
  - `cap_drop: ALL`
  - memory limits
  - read-only root filesystem where the template allows
- **Secrets:** generated on the VM, kept in `/srv/annex/secrets`, never in the repo or on the Mac. `.env` files are gitignored.
- **Product data:** product-db is reachable only by the gateway and Metabase.
- **Throwaway access:** the passphrase-less SSH key and passwordless sudo are acceptable only because the VM is disposable, and both are removed at teardown.

## Out of scope for M0
- AWS and Terraform; multiple clients
- Real SSO and real client data
- Customer-facing or public apps
- Supabase and Lovable backends
- Egress enforcement
- n8n and connectors
- High availability
- Metabase Pro features
- A monitoring stack beyond logs and health checks
- Goat Mailer and Bekafon beyond placeholder tiles

## Suggested repo layout
```
annex/
  README.md  CLAUDE.md  .env.example  compose.yaml  .gitignore
  config/    traefik/ authelia/ homepage/ postgrest/
  db/        product/ (schema, seed, api_views) appdata/
  cli/       annex CLI (uv project)
  mcp/       MCP server
  templates/ python-web/ node-web/ static/ python-job/
  examples/  client-lookup/ monthly-report/
  skills/    annex/SKILL.md
  scripts/   bootstrap.sh push.sh wipe-vm.sh restore.sh restore-test.sh reset-demo.sh
  docs/      brief.md setup.md teardown.md app-contract.md operations.md decisions.md demo.md
```

## Open for later
- The browser route through `dronky`'s Traefik: domain, TLS and access from outside the LAN
- Production front door, and mapping its identity headers to the contract's `Remote-*` names
- Coolify vs this CLI as the deploy backend; the CLI and MCP interface stays the same either way
- Per-app network isolation and enforced egress allowlists
- Connectors (n8n or alternatives) and their licensing
- How to handle Lovable apps with Supabase backends
