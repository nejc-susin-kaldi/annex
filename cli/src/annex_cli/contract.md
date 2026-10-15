# Annex app contract v0

You write a small folder. Annex does the rest: routing and login, the discovery page entry, schedules, storage, backups and data access. **Never write Docker labels, login code or database credentials.**

```
my-tool/
  annex.yaml    # manifest, required
  Dockerfile    # from a template, or your own; must end with a non-root USER
  ...code
```

Start from a template: `annex init <template> <name> --owner <login>`. Templates: `python-web` (FastAPI), `python-job` (scheduled script, no web), `node-web` (Express), `static` (built files plus a tiny server). Another language or framework works too: see [Bring your own stack](#bring-your-own-stack). Deploy with `annex deploy <folder>`.

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
resources:
  memory: 512m                # optional; default 256m, 128m-512m
```
Reserved names: `home`, `auth`, `metabase`, `logs`, `gateway`, `echo-test`, `www`, `api`.

## Runtime rules
- **Stateless container.** Persist only to `DATABASE_URL` or `/data`. Everything else is lost on redeploy and isn't backed up.
- **Read-only root filesystem.** Only `/tmp` (64 MB, cleared on restart) and `/data` are writable.
- **Runs as uid 10001**, with no capabilities and `no-new-privileges`. The Dockerfile must still end with a non-root `USER`; the deploy refuses images that run as root.
- **Limits:** 256 MB RAM by default, 0.5 CPU. An app that needs more memory sets `resources.memory`, up to `512m`; the deploy notes it, and the platform team sees raised limits in `annex list`. Files in `/tmp` count against memory. More than 512 MB means the tool is too big for Annex: talk to the platform team.
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
- **Health gate.** Every deploy is a numbered release (`r1`, `r2`, …). A web app's new release must return 200 on its `healthcheck` within 60 s (a job-only app must keep running). If it doesn't, Annex switches back to the previous release automatically, and the deploy fails with the new release's last log lines. Your database and `/data` are left as they are, and so are any changes the failed release made to them.
- **Jobs** run inside the app's container with the same environment (`docker exec`). In `type: job`, the container idles (`sleep infinity`) between runs. Trigger a job by hand with `annex run <app> <job>`. A job's exit code and output are its result.

## Bring your own stack
The templates are the fast path, not a requirement. Any language or framework works if it runs as **one container** and follows the runtime rules above. Write your own `Dockerfile` and `annex.yaml` and check:

- **Web:** serve HTTP on `0.0.0.0:$PORT`. The `healthcheck` path must return 200 without a login. Annex calls it directly, not through the front door.
- **User:** end the Dockerfile with a non-root `USER`. Annex runs the app as uid 10001 regardless, so the files the app reads must be readable by any user. Files added with `COPY` are, by default.
- **Health:** a Docker `HEALTHCHECK` is optional but recommended, because `annex list` shows its state.
  - Use a command the image actually has; slim images have no `curl`.
  - If there is one, the deploy waits for it to report healthy.
- **Read-only filesystem:** set `ENV HOME=/tmp`, and point caches and temp files at `/tmp`.
- **Build in the image:** do everything at image build time: install dependencies, compile, bundle, collect static files. Nothing may be installed or built at startup.
- **Memory:** the default limit is 256 MB. Raise it with `resources.memory` (max `512m`) only if the app needs more. Size runtime heaps to fit, e.g. a JVM with `-Xmx` at about 70 % of the limit, or Node with `--max-old-space-size`.
- **Identity on the server:** read `Remote-*` headers in your server code. A browser-only app can't see request headers, so serve a small endpoint such as `/api/me` that returns them.
- **Gateway token stays on the server:** call the gateway from your server. Never send `ANNEX_GATEWAY_TOKEN` to the browser.
- **Storage:**
  - Postgres (`DATABASE_URL`) and files under `/data` are the only storage. A SQLite file is fine if it lives in `/data`.
  - There's no MySQL, MongoDB or Redis, and no second container.
  - If the tool needs those, it's a candidate to move to a Kaldi project.
- **Upload size:** the app folder is uploaded and built on the server (x86_64). Keep it under 50 MB compressed: add `node_modules`, `.venv`, `.next`, `dist` and data files to `.dockerignore`. Pin the base image version.

Common stacks:

| Stack | What to do |
|---|---|
| Vite / React / Vue single-page app (also Lovable exports) | Build to `dist/` and serve it with the `static` template's server: copy the build output into `public/`. You get `api/me` and `api/data/<view>` for free. Supabase calls must be replaced with those. The server has no fallback to `index.html`, so use hash routing (`/#/clients/42`) or a reload on a deep link returns 404. |
| Next.js | `output: "standalone"`, `next build` in the Dockerfile, run `node server.js`, `NEXT_TELEMETRY_DISABLED=1`. Runtime caches (image optimisation, ISR) write to `.next/cache`, which is read-only: turn them off (`images: {unoptimized: true}`). Usually needs `memory: 384m` or more. |
| Streamlit | `ENV HOME=/tmp`; run `streamlit run app.py --server.port $PORT --server.address 0.0.0.0 --server.headless true --browser.gatherUsageStats false`; `healthcheck: /_stcore/health`. Read `Remote-User` with `st.context.headers`. |
| Django / Flask | Run with gunicorn on `0.0.0.0:$PORT`. Run `collectstatic` in the Dockerfile, and serve static files with WhiteNoise. Set `PYTHONDONTWRITEBYTECODE=1`. |
| Go, Rust, .NET, Java | Multi-stage build; copy only the binary or app into a slim runtime image. |

If the deploy rejects something, the error says what to change.

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
| `annex releases <app>` | an app's releases, and which can be rolled back to |
| `annex rollback <app> [--to rN]` | switch back to an earlier release (default: the one before), with that release's manifest |
| `annex rollback <app> --with-data` | **human only**: also restore the database and `/data` from that release's time; writes since are lost |
| `annex secret set <app> KEY=VALUE` | **human only**: store a secret on the server |
| `annex remove <app> [--keep-data]` | **human only**: remove an app |
| `annex contract` | print this contract |

Add `--json` before the command for machine-readable output. When a deploy fails, the error says what to fix. Fix it and deploy again.
