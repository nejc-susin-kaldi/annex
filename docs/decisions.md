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
