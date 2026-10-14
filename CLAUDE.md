# Kaldi Annex: M0 homelab prototype

The brief is `docs/brief.md`. Read it before changing anything. Decisions and their reasons are in `docs/decisions.md`.

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
- `db/product/`: synthetic product DB (schema, seed, `api_views.sql` = the gateway allowlist). `db/appdata/`: app database init and backup hook.
- `vm/`: scripts that run **on the VM** (secrets, Metabase setup). They are synced with the platform.
- `scripts/`: scripts that run **on the Mac**. Source `scripts/lib.sh` for `vm`, `setting`, `render`.
- `cli/`: the `annex` CLI (uv project). `ops.py` holds the operations shared with the MCP server, `manifest.py` validates `annex.yaml`, and `contract.md` is the app contract (`docs/app-contract.md` links to it). Run it with `./annex`.
- `vm/annex_vm.py`: the VM side of app provisioning (DB, gateway role and JWT, secrets). Stdlib only; prints JSON, never secret values.
- `templates/<name>/`: app templates (`__APP_NAME__` and `__OWNER__` are replaced by `annex init`). `examples/`: demo apps.
- `mcp/`, `skills/`: M0.4.

## Conventions
- Settings live in `.env` (gitignored; copy `.env.example`). `scripts/render.py` derives everything else into `.build/platform/.env`, which is also the platform's `.env` on the VM and the CLI's source of truth.
- Secrets are generated on the VM by `vm/bootstrap-secrets.sh` and live only in `/srv/annex/secrets`. Never commit them or write them into the repo.
- Everything Annex creates carries the label `com.kaldi.annex=true`. Compose projects are `annex` and `annex-app-<name>`. Networks are named `annex-<name>`.
- Every platform container has a memory limit and a healthcheck. Keep the platform under about 2.5 GiB of RAM.
- Shell: bash, `set -euo pipefail` (via `lib.sh`). Python: 3.12+ on the Mac via uv; VM-side scripts use bash, jq and the VM's stock python3 only.
- Apps: `./annex deploy <folder>`, then `scripts/test-apps.sh`. Validation errors must say what's wrong and how to fix it, because agents read them.
- After changing platform files: `scripts/push.sh` (add `--extras` for Metabase and Dozzle), then `scripts/test-platform.sh`.
- A change to a bind-mounted config file must trigger a recreate. `render.py` hashes `config/<service>` into `ANNEX_CONFIG_HASH_*` for this; add new services there when needed.
