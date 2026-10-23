# Setup: steps Nejc runs on the Mac and the homelab

The build agent never runs these. Each step has its undo; `docs/teardown.md` collects them.

## Prerequisites
Nothing here is installed by Annex; check that each is present.

**On the Mac:**
- `uv` (runs the CLI, the MCP server and the unit tests) and Python 3.12 or newer (uv fetches one; the scripts also use the system `python3`, 3.9+ is enough for them).
- `mkcert` with its root CA trusted (section 2); `scripts/bootstrap.sh` issues the wildcard certificate with it.
- `rsync` and `ssh` (the platform bundle is synced over SSH; `ssh annex` must work without a password prompt).
- `node` (`scripts/test-smoke.sh` runs the runtime's logging tests with it and skips them when it is missing).
- `docker`, only for the optional context in section 1, and Docker Desktop for the local stack (section 9).

**On the VM (`annex`):**
- Docker with the Compose plugin, `jq` and `python3-yaml` (PyYAML, used by `vm/annex_vm.py users-*`). Installing packages is a system change: if one is missing, ask Nejc.

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
The status page (M1.3) needed `status.annex.home.arpa`; since M2.6 the status page is the console's `/status`, so **that hosts line is no longer needed**. If you added it, it is harmless; remove it with the undo above (which removes every line tagged `# annex`) and add back the others you want.
The console (M2.4) needs `console.annex.home.arpa`, again as its own line with the same tag (the undo above removes it too). Only a browser needs it: `scripts/test-console.sh` goes through `--connect-to` and works without it. The Homepage tiles "Console" and "Status" point at it.
```bash
echo '127.0.0.1 console.annex.home.arpa # annex' | sudo tee -a /etc/hosts
```
Incoming webhooks (M1.5) need `hooks.annex.home.arpa`, again as its own line with the same tag (the undo above removes it too). Only a browser or your own `curl` needs it: `scripts/test-hooks.sh` goes through `--connect-to` and works without it.
```bash
echo '127.0.0.1 hooks.annex.home.arpa # annex' | sudo tee -a /etc/hosts
```
**Optional, recommended before a demo: a wildcard resolver instead of a line per name.** `/etc/hosts` has no wildcards, so every new app name (e.g. one an AI picks during the live build) needs a line. A local `dnsmasq` answering `*.annex.home.arpa` with 127.0.0.1 removes that. macOS sends only that domain to it (`/etc/resolver`); everything else resolves as before. Entries in `/etc/hosts` keep working and take precedence.
```bash
brew install dnsmasq
```
```bash
echo 'address=/annex.home.arpa/127.0.0.1 # annex' >> "$(brew --prefix)/etc/dnsmasq.conf"
```
```bash
sudo brew services start dnsmasq
```
```bash
sudo mkdir -p /etc/resolver && echo 'nameserver 127.0.0.1' | sudo tee /etc/resolver/annex.home.arpa
```
Check (any name should give 127.0.0.1): `dscacheutil -q host -a name anything.annex.home.arpa`.
Undo:
```bash
sudo rm /etc/resolver/annex.home.arpa
```
```bash
sudo brew services stop dnsmasq
```
```bash
sed -i '' '/# annex$/d' "$(brew --prefix)/etc/dnsmasq.conf"
```
```bash
brew uninstall dnsmasq
```
(Skip the last two if you used dnsmasq before Annex.)

Then, with no system change:
```bash
scripts/tunnel.sh up
```
Added on 14 Oct. Open https://home.annex.home.arpa:8443 and sign in with a demo user. `scripts/show-users.sh` prints the passwords. Stop the tunnel with `scripts/tunnel.sh down`.

## 4. Annex in Claude Code: MCP server and skill (M0.4)
Both are user-scoped, so Claude Code can use Annex from any folder, including an empty one.

**MCP server** (stdio, runs from this repo through uv):
```bash
claude mcp add --scope user annex -- uv run --quiet --project "$(pwd)/mcp" annex-mcp
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
mkdir -p ~/.claude/skills && ln -s "$(pwd)/skills/annex" ~/.claude/skills/annex
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
2. In Slack, create `#finance-reports`, `#annex-alerts` and **`#annex-selftest`** and invite the app to all three (`/invite @<app name>`). `#annex-selftest` is where every test run posts (test mode, docs/operations.md): nobody should be in it who doesn't want to see tests. Once Slack is connected, **every alert** (app problems and platform problems, and their "resolved" messages) is also posted to `#annex-alerts` (setting `ANNEX_ALERT_SLACK_CHANNEL`), and apps can list their own channels under `alerts.to`.
3. Put the token on the VM. The command reads the token from the terminal, so it doesn't land in your shell history. Paste `SLACK_BOT_TOKEN=xoxb-...`, press Enter, then Ctrl-D:
   ```bash
   ssh annex 'umask 077; cat > /srv/annex/secrets/slack.env'
   ```
4. Apply it (ops reads the file at start):
   ```bash
   scripts/push.sh --extras
   ```
   Compose recreates ops when the token file appears. If `ctl info` (next step) still says `False`, force it with `ssh annex 'cd /srv/annex/platform && docker compose up -d --force-recreate ops'`. A plain `docker restart` does **not** re-read the env file.
5. Check: `ssh annex docker exec annex-ops-1 python -m annex_ops.ctl info` should say `slack_connected: True`, and `scripts/test-notify.sh` then posts for real to `#annex-selftest`.

Undo (Annex goes back to "not connected"):
```bash
ssh annex rm /srv/annex/secrets/slack.env
```
```bash
ssh annex 'cd /srv/annex/platform && docker compose up -d --force-recreate ops'
```
Then delete the Slack app in the workspace (*Basic Information* → *Delete App*), or the test workspace. See `docs/teardown.md`.

## 8. Retire Metabase's "Annex usage" dashboard (M2.9, only when you are ready)
M2.9 moved usage to the console (an app's **Usage** tab; the **Overview** shows people and trends). The repo no longer sets the dashboard up, but an existing Metabase still has it: the "Annex usage" dashboard, its four cards, the "Annex" collection and the "Annex usage" connection. They are harmless; leaving them is fine. To remove them (this deletes things in Metabase's own database):
```bash
ssh annex 'ANNEX_DATA_DIR=/srv/annex bash /srv/annex/platform/vm/metabase-retire-usage.sh'
```
Run it after `scripts/push.sh --extras` (the script is synced with the platform). It is idempotent. The product-data connection, its questions and the demo users are not touched.

The database role `metabase_usage` (read-only on the usage summaries) stays: nothing connects as it any more, and dropping it while Metabase still holds a connection that uses it would only make Metabase log errors. **After** the script above has run, you can drop it for good:
```bash
ssh annex 'docker exec -i annex-appdata-db-1 psql -U postgres -d annex_ops -v ON_ERROR_STOP=1' <<'SQL'
DROP OWNED BY metabase_usage;
DROP ROLE metabase_usage;
SQL
```
and then tell the build agent to remove the role, `APPDATA_DB_METABASE_USAGE_PASSWORD` and `usage_pw` from `db/appdata/bootstrap.sql`, `scripts/push.sh` and `vm/bootstrap-secrets.sh` (until then a push would create the role again).

Undo: check out the commit before M2.9 and run `scripts/push.sh --extras`; `vm/metabase-setup.sh` of that commit creates the connection, cards and dashboard again (the history in the usage tables is untouched, so the charts fill in at once).

## 9. Local stack: the platform on Docker Desktop (D99)
A second, private copy of the platform on the Mac, for development and the suites (control-plane plan, lane "local"). It has its own settings, data, hostnames and ports, so it runs alongside the VM's stack and the tunnel. Nothing here is a system change except the optional `/etc/hosts` lines at the end.

**Selection is per command.** Every script and `./annex` take `ANNEX_ENV=local` in front; without it they talk to the VM as before. Don't export it in your shell profile.
```bash
ANNEX_ENV=local scripts/bootstrap.sh && ANNEX_ENV=local scripts/push.sh && ANNEX_ENV=local scripts/test-smoke.sh
```
```bash
ANNEX_ENV=local ./annex list
```
The push also brings up Goat Arcade's database (compose project `democlient`) and links product-db to it, as on the VM (D112); on a fresh stack its first start seeds it, about a minute more.

**Prerequisites** (on top of the Mac list at the top):
- Docker Desktop running, with the context `desktop-linux` (`docker --context desktop-linux info` answers). The scripts and the CLI set `DOCKER_CONTEXT=desktop-linux` for their own commands only; your current context is never switched, and a context that points at the VM is refused.
- Memory for Docker Desktop: at least 4 GiB (Settings → Resources); the core platform is about 1.4 GiB (D97), each app 20–50 MiB.
- `python3` with PyYAML (`python3 -c 'import yaml'`): the scripts run `vm/annex_vm.py users-sync` with it, as the VM does with `python3-yaml`. `./annex` uses its own Python (uv), which has it.
- `mkcert`, `jq`, `rsync`, `openssl`, `uv`: `scripts/bootstrap.sh` checks them.

**Settings.** `.env.local.example` is committed and used as it is; copy it to `.env.local` (gitignored) only to change something. It sets `ANNEX_TARGET=local` (commands run here with `bash -c` instead of `ssh annex`), `ANNEX_DATA_DIR=$HOME/.annex/local` (outside the repo, under your home folder so Docker Desktop shares it), hostnames `*.annex-local.home.arpa` (separate from the VM's, so a browser can be signed in to both), and Traefik on `127.0.0.1:9443` (HTTPS) and `127.0.0.1:9080` (HTTP), loopback only. The renders are apart too: `.build/vm/platform` and `.build/local/platform`. `scripts/tunnel.sh` says "local mode: no tunnel" and does nothing. The mkcert certificate is issued into `~/.annex/local/certs` by `bootstrap.sh`; the mkcert CA is already trusted (section 2).

**Browser access (optional, a system change: Nejc runs it).** The suites don't need it (`curl --connect-to`). Then open https://home.annex-local.home.arpa:9443.
```bash
echo '127.0.0.1 home.annex-local.home.arpa auth.annex-local.home.arpa console.annex-local.home.arpa mail.annex-local.home.arpa hooks.annex-local.home.arpa gateway.annex-local.home.arpa metabase.annex-local.home.arpa logs.annex-local.home.arpa annex-selftest.annex-local.home.arpa # annex-local' | sudo tee -a /etc/hosts
```
Undo (only the local stack's lines; the VM's `# annex` lines stay):
```bash
sudo sed -i '' '/# annex-local$/d' /etc/hosts
```
With the dnsmasq resolver from section 3, one more line and resolver file cover every app name instead:
```bash
echo 'address=/annex-local.home.arpa/127.0.0.1 # annex' >> "$(brew --prefix)/etc/dnsmasq.conf" && sudo brew services restart dnsmasq
```
```bash
echo 'nameserver 127.0.0.1' | sudo tee /etc/resolver/annex-local.home.arpa
```
Undo: `sudo rm /etc/resolver/annex-local.home.arpa`; the dnsmasq line goes with section 3's undo (it carries the `# annex` tag).

**Stop it** (keeps the data; `ANNEX_ENV=local scripts/push.sh` starts it again, Goat Arcade's database too):
```bash
cd ~/.annex/local/platform && DOCKER_CONTEXT=desktop-linux docker compose --profile extras stop
```
```bash
ANNEX_ENV=local scripts/democlient.sh down
```

**Wipe it** (everything of the local stack: containers, networks, volumes, Annex's images, data and secrets; nothing of the VM, and none of your other Docker images). `scripts/wipe-vm.sh` refuses a local target, because it prunes every image.
```bash
export DOCKER_CONTEXT=desktop-linux
(cd ~/.annex/local/platform && docker compose --profile extras down -v --remove-orphans)
for d in ~/.annex/local/deploy/*/; do [ -f "$d/compose.yaml" ] && (cd "$d" && docker compose down -v --remove-orphans); done
docker ps -aq --filter label=com.docker.compose.project=democlient | xargs docker rm -f 2>/dev/null
docker ps -aq --filter label=com.kaldi.annex=true | xargs docker rm -f 2>/dev/null
docker network ls -q --filter label=com.kaldi.annex=true | xargs docker network rm 2>/dev/null
docker image ls -q --filter label=com.kaldi.annex=true | xargs docker rmi -f 2>/dev/null
rm -rf ~/.annex/local
unset DOCKER_CONTEXT
```
`ANNEX_ENV=local scripts/bootstrap.sh && ANNEX_ENV=local scripts/push.sh` brings it back with new secrets and passwords, and Goat Arcade's database seeded afresh. The pinned platform images stay in Docker Desktop's cache; `docker --context desktop-linux image rm <image>` removes one if you want the space.

## 10. Goat Arcade's people (GA.1, D107)
Human-only commands: they create accounts and print passwords. Run them from the repo on the Mac. For the local stack put `ANNEX_ENV=local` in front of every `./annex` and `scripts/` command (per command, never exported).

**Before the first command**, `.env` (and `.env.local`) must list the new address domain, or Annex's emails to these people are refused:
```
ANNEX_EMAIL_DOMAINS=annex.example,goat-arcade.example
```
Then `scripts/push.sh --extras` (it brings the new config to the instance and recreates Homepage and Authelia for the branding).

**Add the people.** `--demo` prints a random password once and keeps it in `secrets/users.txt` (`scripts/show-users.sh` prints that file; the test suites and the Metabase setup read it). No invitation email is sent.
```bash
./annex users add maja   --name 'Maja Horvat'   --email maja@goat-arcade.example   --groups users --demo
./annex users add boris  --name 'Boris Zajc'    --email boris@goat-arcade.example  --groups users --demo
./annex users add petra  --name 'Petra Kralj'   --email petra@goat-arcade.example  --groups users --demo
./annex users add lena   --name 'Lena Gruber'   --email lena@goat-arcade.example   --groups builders --demo
./annex users add tomaz  --name 'Tomaž Vidmar'  --email tomaz@goat-arcade.example  --groups users --demo
```
`sam` and `kaldi-admin` stay as they are. An account added earlier without `--demo` gets its line with `./annex users reset <login> --demo` (new password, printed once).

**Create their Metabase accounts and groups** (Metabase must be running): `scripts/push.sh --extras` again, or only the setup script on the VM:
```bash
ssh annex 'ANNEX_DATA_DIR=/srv/annex bash /srv/annex/platform/vm/metabase-setup.sh'
```
It is idempotent and skips anyone without a line in `users.txt`.

**Remove `cm` last**, after the suites pass with `maja` (it also drops `cm`'s line from `users.txt`):
```bash
./annex users remove cm --yes
```
Then deactivate the old Metabase account `cm@annex.example` (Metabase, Admin, People). Apps that name `cm` as a recipient or alert target (`monthly-report`'s `REPORT_RECIPIENTS`) need `maja` first.

Undo: `./annex users remove <login> --yes` for each new account (the Metabase accounts stay until deactivated in Admin, People); to bring `cm` back, `./annex users add cm --name 'Maja Horvat' --email cm@annex.example --groups users --demo`.

The branding (Homepage title, logo and groups; Authelia's logo and favicon) needs nothing from you beyond the push. Homepage groups an app's tile from `config/teams.json` when the app is next deployed.
