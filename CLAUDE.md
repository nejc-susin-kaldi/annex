# Kaldi Annex: homelab prototype (M0 and M1)

The brief is `docs/brief.md`. Read it before changing anything. Later milestones' plans are in `docs/plans/` (`m1.md` is Nejc's M1 plan; `m1.2.md`–`m1.5.md` the step-by-step plans). Decisions and their reasons are in `docs/decisions.md`.

## Ground rules (from the brief)
- **Synthetic data only.** Never use, copy or connect to real client data or systems, Cadro included.
- **LAN only.** No port forwarding, tunnels to the internet or public DNS. SSH port forwards from the Mac for testing are fine.
- **Pin every image version.** No `:latest`. Images must support amd64; the VM is x86_64.
- **Where you may write:**
  - on the VM (`ssh annex`), only Docker and `/srv/annex`;
  - on the Mac, only this repo.
- **Ask Nejc before:**
  - adding a component that isn't in the brief's stack;
  - changing the app contract;
  - changing VM system config (packages, firewall, users, systemd). `annex` has passwordless sudo, but that doesn't make system changes allowed.
- **Never run system changes yourself.** That covers the Mac and the homelab: trusting the mkcert CA, the Docker context, registering the MCP server, `/etc/hosts`, and the Traefik on `dronky`. Write the exact commands and their undo commands into `docs/setup.md` and ask Nejc to run them.
- **Never touch `dronky`** beyond using it as the SSH jump host.
- **Log decisions** in `docs/decisions.md`: what, why, alternatives considered.
- Destructive VM operations (`scripts/wipe-vm.sh`) need Nejc's go-ahead.

## Layout
- `compose.yaml` (+ `compose.tls-local.yaml`): the platform. Runs on the VM from `/srv/annex/platform`.
- `config/`: Traefik, Authelia and Homepage config. `*.tmpl` files are rendered by `scripts/render.py` using `{{VAR}}` placeholders.
- `db/product/`: synthetic product DB (schema, seed, `api_views.sql` = the gateway allowlist). `db/appdata/`: app database init and backup hook; `annex_ops.sql` is annex-ops's schema (idempotent, applied by `push.sh` on every push, later milestones append to it; it can't change an existing table, so a changed table needs a note in `docs/decisions.md`).
- `ops/`: the annex-ops service (FastAPI: email via SMTP/Mailpit and Slack for apps, the send log, the alert engine (Docker/log watchers, Gatus webhook, incidents) and usage stats (`usage.py`, `digest.py`: what counts, signals, tiles, the weekly digest; logins never leave `annex usage <app> --by-user`); incoming webhooks (`hooks_*.py`: signature checks, storage, retried delivery; the **only** route without a login, served only under `/h/` by `hooks_app.py`, which runs in its **own container** (`hooks`, D55) with an insert-only DB role; secrets in `secrets/hooks/<app>.json`, never in the database); host and backup monitoring (`host.py`, D56); stored logs that outlive containers (`logship.py` follows and parses, `logstore.py` inserts, partitions and queries; D61-D66); later audit). The engine (`engine.py`) is pure: no DB, network or clock of its own. Built on the VM from the synced bundle. Unit tests: `uv run --python 3.13 --with-requirements ops/requirements.txt --with pytest pytest ops/tests -q`.
- `images/`: derived images built on the VM from a pinned upstream image (`gatus`: adds a `wget` for its healthcheck). `config/gatus/`: Gatus's platform config; each web app's check is written by `annex deploy` into `state/gatus/apps`. `tests/`: test fixtures (`alert-probe` for `scripts/test-alerts.sh`, `hook-probe` for `scripts/test-hooks.sh`, `log-probe` and `job-print-probe` for `scripts/test-logs.sh`; not example apps). `config/metabase/usage-dashboard.json`: the "Annex usage" dashboard spec (cards as SQL), applied by `vm/metabase-setup.sh`.
- `vm/`: scripts that run **on the VM** (secrets, Metabase setup). They are synced with the platform.
- `scripts/`: scripts that run **on the Mac**. Source `scripts/lib.sh` for `vm`, `setting`, `render`; test suites also source `scripts/testlib.sh` (results, the tunnel, logins, Mailpit, `wait_healthy`). `scripts/test-smoke.sh` is the quick check after a push; `scripts/test-all.sh` runs everything (long: ask first).
- `cli/`: the `annex` CLI (uv project). `ops.py` holds the operations shared with the MCP server, `manifest.py` validates `annex.yaml`, and `contract.md` is the app contract (`docs/app-contract.md` links to it). Run it with `./annex`.
- `vm/annex_vm.py`: the VM side of app provisioning (DB, gateway role and JWT, secrets) and of user accounts (`users-*`, D57: Authelia's `users.yml` and the user directory). Stdlib only, except PyYAML (Ubuntu's `python3-yaml`) for the users commands; prints JSON, never secret values (except a `--print-password` asked for by a human). Unit tests: `python3 -m unittest discover -s vm/tests`.
- `templates/<name>/`: app templates (`__APP_NAME__` and `__OWNER__` are replaced by `annex init`). `examples/`: demo apps. `runtime/`: the one source of `annex_runtime.py`/`.js`; templates, examples and fixtures carry copies, kept identical by `scripts/sync-runtime.sh` (`--check` runs in `test-all.sh`). Edit `runtime/`, then sync.
- `mcp/`: the MCP server (stdio, `mcp` 2.x), a thin layer over `annex_cli.ops`. `skills/annex/SKILL.md`: the Claude Code skill (symlinked into `~/.claude/skills`).
- `docs/demo.md` + `scripts/reset-demo.sh`: the management demo. Don't run `reset-demo.sh` without Nejc's go-ahead: it removes apps that aren't examples, with their data.

## Conventions
- Settings live in `.env` (gitignored; copy `.env.example`). `scripts/render.py` derives everything else into `.build/platform/.env`, which is also the platform's `.env` on the VM and the CLI's source of truth.
- Secrets are generated on the VM by `vm/bootstrap-secrets.sh` and live only in `/srv/annex/secrets`. Never commit them or write them into the repo.
- Everything Annex creates carries the label `com.kaldi.annex=true`. Compose projects are `annex` and `annex-app-<name>`. Networks are named `annex-<name>`.
- Every platform container has a memory limit and a healthcheck. Keep the platform under about 2.5 GiB of RAM.
- Shell: bash, `set -euo pipefail` (via `lib.sh`). Python: 3.12+ on the Mac via uv; VM-side scripts use bash, jq and the VM's stock python3 only.
- Apps: `./annex deploy <folder>`, then `scripts/test-apps.sh`. Validation errors must say what's wrong and how to fix it, because agents read them.
- User accounts are managed with `./annex users` (human only). Don't edit `secrets/authelia/users.yml` by hand while Authelia runs: `annex_vm.py` writes it atomically and twice (D57).
- Temporary files on the VM go under `/srv/annex`, never `/tmp` (the write rule above). One-off containers: `docker run --rm` with a command that exits by itself; never cut a server's output short with `| head`, which leaves it running (three stray Gatus containers ran for 18 hours that way).
- After changing platform files: `scripts/push.sh` (add `--extras` for Metabase and Dozzle), then **`scripts/test-smoke.sh`** (under a minute: every part once).
- **Long suites only when they're needed, and ask Nejc first.** Run the one suite for the part you changed (`scripts/test-<area>.sh`, a few minutes). `scripts/test-all.sh` (30+ minutes) and `scripts/test-alerts.sh` (20 minutes) only when the change concerns what they prove (timings, retries, "exactly one alert"), and tell Nejc before starting them.
- A change to a bind-mounted config file must trigger a recreate. `render.py` hashes `config/<service>` into `ANNEX_CONFIG_HASH_*` for this; add new services there when needed.
