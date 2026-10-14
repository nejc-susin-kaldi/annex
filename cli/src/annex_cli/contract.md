# Annex app contract v0

You write a small folder. Annex does the rest: routing and login, the discovery page entry, schedules, storage, backups and data access. **Never write Docker labels, login code or database credentials.**

```
my-tool/
  annex.yaml    # manifest, required
  Dockerfile    # from a template; must end with a non-root USER
  ...code
```

Start from a template: `annex init <template> <name> --owner <login>`. Templates: `python-web` (FastAPI), `python-job` (scheduled script, no web), `node-web` (Express), `static` (built files plus a tiny server). Deploy with `annex deploy <folder>`.

## annex.yaml
```yaml
name: monthly-report          # 2-32 chars: a-z, 0-9, '-'; starts with a letter. Becomes the hostname
owner: sam                    # required; the builder's login, shown on the discovery page
description: Monthly positions report
type: web                     # web | job | web+job
port: 8080                    # web: listen on 0.0.0.0:$PORT (internal only; never published)
healthcheck: /healthz         # web: returns 200 when ready
access: internal              # only value in v0 (behind login)
storage:
  postgres: true              # dedicated database, injected as DATABASE_URL
  files: true                 # persistent directory at /data (DATA_DIR), backed up nightly
data:
  views: [monthly_positions]  # gateway views to read; must be in the allowlist
schedule:                     # job and web+job only
  - name: monthly
    cron: "0 6 1 * *"         # minute hour day month weekday, Europe/Ljubljana; or @daily, @hourly
    command: ["python", "report.py"]
env:                          # non-secret config only
  REPORT_TITLE: Monthly positions
secrets: [SMTP_PASSWORD]      # names only; a human sets values with `annex secret set`
egress: []                    # hostnames the app calls; declared, not enforced in v0
```
Reserved names: `home`, `auth`, `metabase`, `logs`, `gateway`, `echo-test`, `www`, `api`.

## Runtime rules
- **Stateless container.** Persist only to `DATABASE_URL` or `/data`. Everything else is lost on redeploy and isn't backed up.
- **Read-only root filesystem.** Only `/tmp` (64 MB, cleared on restart) and `/data` are writable.
- **Runs as uid 10001**, with no capabilities and `no-new-privileges`. The Dockerfile must still end with a non-root `USER`; the deploy refuses images that run as root.
- **Limits:** 256 MB RAM, 0.5 CPU.
- **Identity** comes from the request headers set by the front door. Never implement your own login:
  - `Remote-User`: login, e.g. `sam`
  - `Remote-Name`: display name, e.g. `Sam Novak`
  - `Remote-Email`
  - `Remote-Groups`: comma-separated, e.g. `builders` or `admins,builders`

  Annex strips client-sent copies of these headers, so they can be trusted.
- **Product data only through the gateway**, never directly from a database. Call `GET $ANNEX_GATEWAY_URL/<view>` with `Authorization: Bearer $ANNEX_GATEWAY_TOKEN`. The gateway is PostgREST, so filters work as query parameters, e.g. `/client_summary?client_name=ilike.*novak*&order=aum_eur.desc&limit=20`. Requesting a view you didn't declare returns 401/403. Every call is logged with your app's identity.
- **Secrets** arrive as environment variables at runtime. Never put them in code, the repo or the image.
- **Never publish ports.** Only Annex's front door does.
- **Log to stdout/stderr.** Read logs with `annex logs <app>`.
- **Jobs** run inside the app's container with the same environment (`docker exec`). In `type: job`, the container idles (`sleep infinity`) between runs. Trigger a job by hand with `annex run <app> <job>`. A job's exit code and output are its result.

## Injected environment
| Variable | When |
|---|---|
| `PORT` | web, web+job |
| `ANNEX_APP_NAME`, `ANNEX_BASE_URL` | always (`ANNEX_BASE_URL` is the app's public URL) |
| `DATABASE_URL` | `storage.postgres: true` (`postgres://…@appdata-db:5432/app_<name>`) |
| `DATA_DIR=/data` | `storage.files: true` |
| `ANNEX_GATEWAY_URL`, `ANNEX_GATEWAY_TOKEN` | `data.views` is not empty |
| each declared secret | `secrets` |

## Gateway views (allowlist)
| View | Columns |
|---|---|
| `client_summary` | client_id, client_name, segment, risk_profile, country, advisor_name, account_count, aum_eur, onboarded_on |
| `top_clients_by_aum` | rank, client_id, client_name, segment, advisor_name, aum_eur (top 25) |
| `monthly_positions` | month_end, client_id, client_name, account_id, symbol, instrument_name, asset_class, quantity, price, market_value_eur |
| `advisor_book` | advisor_name, clients, aum_eur, avg_client_aum_eur |

All data is synthetic. Adding a view is a reviewed change by the platform team, in `db/product/api_views.sql`.

## Commands
| Command | Does |
|---|---|
| `annex init <template> <name> [--owner login]` | copy a template |
| `annex deploy [path]` | validate, provision DB/role/token, build, run, register |
| `annex list` | list deployed apps |
| `annex status <app>` | an app's state, config and recent logs |
| `annex logs <app> [-n 100] [-f]` | an app's logs |
| `annex run <app> <job>` | trigger a job now |
| `annex secret set <app> KEY=VALUE` | **human only**: store a secret on the server |
| `annex remove <app> [--keep-data]` | **human only**: remove an app |
| `annex contract` | print this contract |

Add `--json` before the command for machine-readable output. When a deploy fails, the error says what to fix. Fix it and deploy again.
