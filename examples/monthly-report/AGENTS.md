# AGENTS.md: building an Annex app (python-web)

This folder is an **Annex app**: a small internal tool that runs behind the client's login on their Annex server. Read the full contract with `annex contract` (or the `annex_contract` MCP tool) before making structural changes.

## What you may change
- `main.py` and any new modules, templates or static files.
- `requirements.txt`: pin exact versions (`package==x.y.z`).
- `annex.yaml`: `description`, `storage`, `data.views`, `schedule` (with `type: web+job`), `env`, `secrets`.

## What you must not do
- **No login code.** The user is already signed in. Read them with `current_user(request.headers)` from `annex_runtime.py` (`.login`, `.name`, `.email`, `.groups`).
- **No database credentials and no direct product database access.** Product data comes only from `gateway("<view>", **filters)`, for views listed under `data.views`. Your own data goes in Postgres via `DATABASE_URL` (set `storage.postgres: true`), or in files under `/data` (set `storage.files: true`).
- **No Docker labels, ports or compose files.** Annex generates them.
- **No secrets in code, files or the image.** Declare the name under `secrets:` and ask a human to run `annex secret set <app> KEY=VALUE`.
- **Don't change the Dockerfile's base image, `USER 10001` or `HEALTHCHECK`.** The app runs on a read-only filesystem; write only to `/tmp` and `/data`.

## Rules the deploy checks
- Listen on `0.0.0.0:$PORT`, and make `GET /healthz` return 200 when ready.
- `name` is the hostname: lowercase letters, digits and `-`.
- Views must be in the gateway allowlist (see `annex contract`).

## Workflow
1. Edit the code. Test locally if you like: `pip install -r requirements.txt && PORT=8080 python main.py`. Identity headers and the gateway only exist on the server.
2. `annex deploy .` (or the `annex_deploy` MCP tool). Read any error, fix exactly what it names, and deploy again.
3. Open the URL it prints, then check `annex status <name>` and `annex logs <name>`.
