# Setup: steps Nejc runs on the Mac and the homelab

The build agent never runs these. Each step has its undo; `docs/teardown.md` collects them.

## 1. Docker context `annex-vm` (optional)
Lets you run `docker --context annex-vm ps` from the Mac. The annex scripts and CLI don't need it: they use `ssh annex` (see decision D1).
```bash
docker context create annex-vm --docker host=ssh://annex
```
Check:
```bash
docker --context annex-vm version
```
Undo:
```bash
docker context rm annex-vm
```

## 2. mkcert CA: nothing to do
The mkcert root CA (`$(mkcert -CAROOT)/rootCA.pem`) was **already trusted** in the System keychain before Annex. `scripts/bootstrap.sh` only issues a certificate from it. **Don't run `mkcert -uninstall` at teardown**, because other things on this Mac may rely on it.

## 3. Browser access through the SSH tunnel (`/etc/hosts`)
Until the route through `dronky` exists. Port 443 on the Mac is already taken by another process (`node`), so the tunnel listens on **8443**, and `.env` sets `ANNEX_PUBLIC_PORT=8443` so every Annex URL and login redirect keeps the port.

Add the hostnames (one line, tagged `# annex`). Apps need their own names because `/etc/hosts` has no wildcards; the planned ones are included:
```bash
echo '127.0.0.1 home.annex.home.arpa auth.annex.home.arpa metabase.annex.home.arpa logs.annex.home.arpa gateway.annex.home.arpa client-lookup.annex.home.arpa monthly-report.annex.home.arpa vibe-dashboard.annex.home.arpa echo-test.annex.home.arpa # annex' | sudo tee -a /etc/hosts
```
Undo:
```bash
sudo sed -i '' '/# annex$/d' /etc/hosts
```
Mailpit (M1.2) needs its own name, `mail.annex.home.arpa`. Add it as a new line with the same tag, so the undo above removes it too:
```bash
echo '127.0.0.1 mail.annex.home.arpa # annex' | sudo tee -a /etc/hosts
```
Undo (the same command as above):
```bash
sudo sed -i '' '/# annex$/d' /etc/hosts
```
The status page (M1.3) needs `status.annex.home.arpa`, again as its own line with the same tag (the undo above removes it too):
```bash
echo '127.0.0.1 status.annex.home.arpa # annex' | sudo tee -a /etc/hosts
```
Incoming webhooks (M1.5) need `hooks.annex.home.arpa`, again as its own line with the same tag (the undo above removes it too). Only a browser or your own `curl` needs it: `scripts/test-hooks.sh` goes through `--connect-to` and works without it.
```bash
echo '127.0.0.1 hooks.annex.home.arpa # annex' | sudo tee -a /etc/hosts
```
Then, with no system change:
```bash
scripts/tunnel.sh up
```
Added on 14 Oct. Open https://home.annex.home.arpa:8443 and sign in with a demo user. `scripts/show-users.sh` prints the passwords. Stop the tunnel with `scripts/tunnel.sh down`.

## 4. Annex in Claude Code: MCP server and skill (M0.4)
Both are user-scoped, so Claude Code can use Annex from any folder, including an empty one.

**MCP server** (stdio, runs from this repo through uv):
```bash
claude mcp add --scope user annex -- uv run --quiet --project /Users/nejcsusin/git/annex/mcp annex-mcp
```
Check (it should say "Connected"):
```bash
claude mcp list
```
Undo:
```bash
claude mcp remove --scope user annex
```

**Skill** (a symlink, so edits in the repo apply right away):
```bash
mkdir -p ~/.claude/skills && ln -s /Users/nejcsusin/git/annex/skills/annex ~/.claude/skills/annex
```
Undo:
```bash
rm ~/.claude/skills/annex
```
The MCP server exposes `annex_contract`, `annex_init`, `annex_deploy`, `annex_list`, `annex_status`, `annex_logs`, `annex_run`, `annex_releases` and `annex_rollback` (without data). `remove`, `secret set` and `rollback --with-data` aren't exposed; they stay CLI-only. The MCP server picks up new tools when Claude Code restarts it (a new session).

## 5. Route through Traefik on `dronky` (open question)
Not decided yet; nothing to do. Annex supports two shapes, chosen in `.env`:
- **TLS passthrough, no change to Annex:** `dronky`'s Traefik routes TCP by SNI (`HostSNIRegexp`) for the Annex hostnames to `192.168.1.30:443` with `passthrough: true`. The VM keeps serving the mkcert certificate (`ANNEX_TLS_MODE=local`). Set `ANNEX_PUBLIC_PORT=` (empty) if it's reached on 443.
- **Upstream TLS:** `dronky`'s Traefik terminates HTTPS and forwards HTTP to `192.168.1.30:80`. Set `ANNEX_TLS_MODE=upstream` and `ANNEX_UPSTREAM_IP=<dronky's LAN IP>`, then `scripts/push.sh`.

Either way, the undo is to remove that router and service from `dronky`'s Traefik config.

## 6. `annex` on your PATH (optional)
`./annex` in the repo needs no installation. If you want `annex` everywhere:
```bash
uv tool install --editable ./cli
```
Undo:
```bash
uv tool uninstall annex-cli
```

## 7. Slack (optional, M1.2)
Without this, Annex works but Slack posts return a clear "Slack isn't connected" error (503) and are logged as `unavailable`; email through Mailpit is unaffected. Do it in a **test** Slack workspace, not a real one.

1. Create a Slack app at https://api.slack.com/apps ("From scratch"), in the test workspace. Under *OAuth & Permissions* add the **bot** scope `chat:write`, then *Install to Workspace* and copy the *Bot User OAuth Token* (`xoxb-...`).
2. In Slack, create `#finance-reports` and `#annex-alerts` and invite the app to both (`/invite @<app name>`). Once Slack is connected, **every alert** (app problems and platform problems, and their "resolved" messages) is also posted to `#annex-alerts` (setting `ANNEX_ALERT_SLACK_CHANNEL`), and apps can list their own channels under `alerts.to`.
3. Put the token on the VM. The command reads the token from the terminal, so it doesn't land in your shell history. Paste `SLACK_BOT_TOKEN=xoxb-...`, press Enter, then Ctrl-D:
   ```bash
   ssh annex 'umask 077; cat > /srv/annex/secrets/slack.env'
   ```
4. Apply it (ops reads the file at start):
   ```bash
   scripts/push.sh --extras
   ```
   Compose recreates ops when the token file appears. If `ctl info` (next step) still says `False`, force it with `ssh annex 'cd /srv/annex/platform && docker compose up -d --force-recreate ops'`. A plain `docker restart` does **not** re-read the env file.
5. Check: `ssh annex docker exec annex-ops-1 python -m annex_ops.ctl info` should say `slack_connected: True`, and `scripts/test-notify.sh` then posts for real to `#finance-reports`.

Undo (Annex goes back to "not connected"):
```bash
ssh annex rm /srv/annex/secrets/slack.env
```
```bash
ssh annex 'cd /srv/annex/platform && docker compose up -d --force-recreate ops'
```
Then delete the Slack app in the workspace (*Basic Information* → *Delete App*), or the test workspace. See `docs/teardown.md`.
