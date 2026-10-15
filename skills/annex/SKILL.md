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
- **Pin versions** of libraries, including CDN URLs.

## Homelab note (M0 prototype)
The browser reaches Annex through an SSH tunnel on port 8443 and `/etc/hosts` entries. If the user can't open a new app's URL, they need to add its hostname to `/etc/hosts` (see `docs/setup.md` in the annex repo). Existing entries cover `client-lookup`, `monthly-report` and `vibe-dashboard`.
