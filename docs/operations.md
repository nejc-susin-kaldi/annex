# Operations

## Day to day (from the Mac, in the repo)
| Task | Command |
|---|---|
| Change platform config | edit, then `scripts/push.sh` (add `--extras` to include Metabase and Dozzle) |
| Metabase/Dozzle on or off | `scripts/extras.sh on` / `scripts/extras.sh off` |
| Browser access | `scripts/tunnel.sh up`, then https://home.annex.home.arpa:8443 |
| Demo passwords | `scripts/show-users.sh` |
| Deploy an app | `./annex deploy examples/client-lookup` |
| Inspect apps | `./annex list`, `./annex status <app>`, `./annex logs <app> -f` |
| Run a job now | `./annex run <app> <job>` |
| Set a secret | `./annex secret set <app> KEY=VALUE` |
| Remove an app | `./annex remove <app> [--keep-data]` |
| Checks | `scripts/test-platform.sh` (M0.1), `scripts/test-apps.sh` (M0.2) |
| Add a gateway view | edit `db/product/api_views.sql`, run `scripts/push.sh`, and add it to the table in `cli/src/annex_cli/contract.md` |

`./annex` runs the CLI from the repo through uv. To put `annex` on your PATH, see `docs/setup.md` §6.

## Where things live on the VM (`/srv/annex`)
| Path | What |
|---|---|
| `platform/` | synced copy of the rendered repo bundle (compose, config, db, vm); `platform/.env` holds the settings |
| `secrets/` | `master.env`, per-service env files, `authelia/`, `users.txt`, `apps/<app>/` (db_password, gateway_token, secrets.env, runtime.env) |
| `state/` | Postgres data directories and Authelia's sqlite |
| `deploy/<app>/` | app source (`src/`), generated `compose.yaml`, `deploy.json` (last deploy record) |
| `apps/<app>/data` | app files, mounted at `/data`, backed up |
| `dumps/`, `backups/` | nightly pg_dump output and backup archives (7 days) |
| `certs/` | mkcert wildcard certificate (local TLS mode) |

## Logs
- Apps: `./annex logs <app>`, or Dozzle at `logs.` (admins).
- Gateway access per app: `ssh annex docker logs annex-product-db-1 2>&1 | grep annex-gateway`.
- Front door: `ssh annex docker logs annex-traefik-1` (JSON access log), `annex-authelia-1` (login decisions).
