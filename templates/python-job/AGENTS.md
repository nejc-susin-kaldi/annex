# AGENTS.md: building an Annex app (python-job)

This folder is an **Annex app**: a small internal tool that runs behind the client's login on their Annex server. Read the full contract with `annex contract` (or the `annex_contract` MCP tool) before making structural changes.

## What you may change
- `job.py` and any new modules.
- `requirements.txt`: pin exact versions (`package==x.y.z`).
- `annex.yaml`: `description`, `storage`, `data.views`, `schedule` (with `type: web+job`), `env`, `secrets`.

## What you must not do
- **No web server.** This is `type: job`. If you need a page too, use `type: web+job` with the python-web template's server, and add `port` and `healthcheck`.
- **No database credentials and no direct product database access.** Product data comes only from `gateway("<view>", **filters)`, for views listed under `data.views`. Your own data goes in Postgres via `DATABASE_URL` (set `storage.postgres: true`), or in files under `/data` (set `storage.files: true`).
- **No Docker labels, ports or compose files.** Annex generates them.
- **No secrets in code, files or the image.** Declare the name under `secrets:` and ask a human to run `annex secret set <app> KEY=VALUE`.
- **Don't change the Dockerfile's base image or `USER 10001`.** The image needs `sleep` (the base image has it), because the container idles between runs. The filesystem is read-only; write only to `/tmp` and `/data`.

## Rules the deploy checks
- `schedule` needs at least one job: `name`, a 5-field `cron` (Europe/Ljubljana time) and a `command` list.
- Exit 0 on success and non-zero on failure, and print a short summary. That output is what `annex run` shows.
- `name` is the hostname: lowercase letters, digits and `-`.
- Views must be in the gateway allowlist (see `annex contract`).

## Workflow
1. Edit the code. The gateway and `DATA_DIR` only exist on the server.
2. `annex deploy .` (or the `annex_deploy` MCP tool). Read any error, fix exactly what it names, and deploy again.
3. `annex run <name> <job>` (or `annex_run`) to run it now and see its output, then `annex status <name>`.
