# AGENTS.md: building an Annex app (python-web)

This folder is an **Annex app**: a small internal tool that runs behind the client's login on their Annex server. Read the full contract with `annex contract` (or the `annex_contract` MCP tool) before making structural changes.

## What you may change
- `main.py` and any new modules, templates or static files.
- `requirements.txt`: pin exact versions (`package==x.y.z`).
- `annex.yaml`: `description`, `storage`, `data.views`, `notify`, `schedule` (with `type: web+job`), `env`, `secrets`.

## What you must not do
- **No login code.** The user is already signed in. Read them with `current_user(request.headers)` from `annex_runtime.py` (`.login`, `.name`, `.email`, `.groups`).
- **No database credentials and no direct product database access.** Product data comes only from `gateway("<view>", **filters)`, for views listed under `data.views`. Your own data goes in Postgres via `DATABASE_URL` (set `storage.postgres: true`), or in files under `/data` (set `storage.files: true`).
- **No Docker labels, ports or compose files.** Annex generates them.
- **No secrets in code, files or the image.** Declare the name under `secrets:` and ask a human to run `annex secret set <app> KEY=VALUE`.
- **Don't change the Dockerfile's base image, `USER 10001` or `HEALTHCHECK`.** The app runs on a read-only filesystem; write only to `/tmp` and `/data`.

## Email and Slack
To email people or post to Slack, declare it in `annex.yaml` (`notify: {email: true, slack: ["#finance-reports"]}`) and use the helpers in `annex_runtime.py`. Never run an SMTP client or hold a Slack token of your own: Annex injects `ANNEX_OPS_URL` and a token that can do exactly what you declared.
```python
from annex_runtime import NotifyError, send_email, post_slack
send_email(["cm", "sam@annex.example"], "Report ready", template="notice",
           data={"title": "Report ready", "body": "June is done.", "link": "https://...", "link_text": "Open"})
post_slack("#finance-reports", "Report ready: https://...")
```
- Recipients are logins (`"cm"`, as in `current_user(...).login`) or company addresses. Anyone else is refused, and if one recipient is refused nothing is sent.
- Give `text`, `html` or `template="notice"`. **No attachments**: save the file under `/data` and email a link to it in your app.
- Limits: 60 emails and 60 Slack posts per hour per app. Slack only to the channels you declared.
- Failures raise `NotifyError` (`.status`, and a message that says what to fix). Decide whether a failed notification should fail your job.

## Rules the deploy checks
- Listen on `0.0.0.0:$PORT`, and make `GET /healthz` return 200 when ready.
- `name` is the hostname: lowercase letters, digits and `-`.
- Views must be in the gateway allowlist (see `annex contract`).

## Workflow
1. Edit the code. Test locally if you like: `pip install -r requirements.txt && PORT=8080 python main.py`. Identity headers and the gateway only exist on the server.
2. `annex deploy .` (or the `annex_deploy` MCP tool). Read any error, fix exactly what it names, and deploy again.
3. Open the URL it prints, then check `annex status <name>` and `annex logs <name>`.
