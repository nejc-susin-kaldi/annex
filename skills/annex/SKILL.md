---
name: annex
description: Build and deploy small internal tools (web pages, dashboards, scheduled jobs) to the client's Kaldi Annex server, behind their login, using the annex MCP tools. Use when the user asks to make an internal tool, dashboard, report or job "on Annex", to deploy or redeploy an Annex app, or to check an Annex app's status or logs.
---

# Annex: build and deploy internal tools

Annex runs small internal tools on the client's own server, behind their login, with read-only access to curated product data. You write a small folder; Annex handles hosting, login, discovery page, storage, schedules, backups and data access.

## Tools (MCP server `annex`)
| Tool | Use |
|---|---|
| `annex_contract` | **Call first.** The rules, `annex.yaml` fields, injected env, data views |
| `annex_init(template, name, directory, owner)` | Start an app from a template in an empty folder (absolute path) |
| `annex_deploy(path)` | Validate, build and run the folder; returns the URL or exactly what to fix |
| `annex_list`, `annex_status(app)`, `annex_logs(app)` | Check what's running |
| `annex_run(app, job)` | Run a scheduled job now |
| `annex_releases(app)`, `annex_rollback(app, to_release)` | See releases; switch back to an earlier one (code and config only) |

Removing apps, setting secrets and restoring data are **human only**: tell the user the command (`annex remove <app>`, `annex secret set <app> KEY=VALUE`, `annex rollback <app> --with-data`).

## Pick a template
| The user wants | Template |
|---|---|
| A dashboard or page that shows data, no server logic | `static` (HTML/JS in `public/`, data via `fetch("api/data/<view>")`) |
| A web tool with server logic, forms, its own database | `python-web` (FastAPI) or `node-web` (Express) |
| Something that runs on a schedule (reports, syncs) | `python-job`; for a job with a page, use `python-web` with `type: web+job` |
| Another framework (Next.js, Streamlit, Django, Go…), or existing code to move to Annex | No template: write `annex.yaml` and a `Dockerfile` yourself, following **Bring your own stack** in `annex_contract`. A built React/Vite app goes into the `static` template's `public/` instead |

Prefer a template when the user hasn't asked for a specific stack: it already follows the rules.

## Workflow
1. `annex_contract`. Note the gateway views and their columns.
2. Ask for the **owner** (the builder's Annex login, e.g. `sam`) if you don't know it. Choose a short **name** (lowercase, `-`); it becomes the hostname.
3. `annex_init` into the current folder (or a new subfolder) using its **absolute** path. Read the returned AGENTS.md; it says which files you may change.
4. Build the app. Keep to the template's structure: don't touch the Dockerfile, server/runtime helpers, users or ports. Use only the views you list under `data.views`, and set `description`.
5. `annex_deploy` with the absolute folder path. If it fails, read the error, fix exactly that, and deploy again. Don't work around the platform. If the new version fails its health check, Annex has already switched back to the previous release; the error includes the new version's log lines.
   If a deploy succeeded but the user says the new version is wrong, `annex_rollback` brings back the previous one while you fix it.
6. Check `annex_status` shows healthy, and look at `annex_logs` for errors after the first page loads. Give the user the URL.

## Rules that matter
- **No login code.** The user is already signed in. Identity comes from `Remote-*` headers (python/node helpers), or from `api/me` in `static`.
- **Product data only through the gateway views**, never a database connection. Everything is synthetic, but treat it as client data anyway: don't paste rows into chat or commit them.
- **No secrets in code.** Declare names under `secrets:` and ask a human to set them.
- **Read-only filesystem.** Write only to `/tmp` or `/data` (`storage.files: true`), or use `DATABASE_URL` (`storage.postgres: true`).
- **Memory.** 256 MB by default. Only if the app needs more (it's killed for running out of memory, or the stack needs it, e.g. Next.js), set `resources: {memory: 384m}` or up to `512m`. Don't raise it by default.
- **Email and Slack** only through Annex, never an SMTP server or a Slack token of your own. Declare `notify: {email: true, slack: ["#channel"]}` in `annex.yaml`, then use the template's helpers (`send_email`, `post_slack`; `sendEmail`, `postSlack` in node). Recipients are logins (`"cm"`) or company addresses; anything else is refused, and one refused recipient means nothing is sent. No attachments: save the file under `/data` and email a link to it in your app. The `notice` template (title, body, link) covers most messages. Errors say what to fix. The deploy warns if Slack isn't connected yet.
- **Alerts.** The owner is emailed when the app is down, crash-loops, throws 5xx or a job fails. Keep `/healthz` honest and fast, and make jobs **exit non-zero on failure**. `annex_status` shows the health check, any open incident and each job's last run: check it after a deploy. `alerts: {to: [owner, cm, "#channel"]}` adds people.
- **Usage** is measured by the platform (signed-in page requests; not health checks or static files), so there is nothing to add to the app. Owners see "users in the last 7 days" on their tile and in `annex usage`; logins are never shown.
- **Webhooks** (Trello, Stripe, GitHub, Slack, shared-secret): declare `webhooks:` in `annex.yaml` with a `secret` listed under `secrets:`, and give the provider `$ANNEX_HOOKS_URL/<name>`. In the endpoint call `hook_event(headers)` first (401 on `HookError`), answer 2xx quickly and dedupe by `event_id`: delivery is at least once. Ask a human to set the signing secret (`annex secret set`); `annex hooks show` and `replay` are human only.
- **Pin versions** of libraries, including CDN URLs.

## Homelab note (M0 prototype)
The browser reaches Annex through an SSH tunnel on port 8443 and `/etc/hosts` entries. If the user can't open a new app's URL, they need to add its hostname to `/etc/hosts` (see `docs/setup.md` in the annex repo). Existing entries cover `client-lookup`, `monthly-report` and `vibe-dashboard`.
