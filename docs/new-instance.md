# A new instance: from an empty VM to a running platform

For a developer who has this repo and an empty virtual machine, and has to bring up a second Annex without
Nejc. It ends with the platform running, the smoke test green and a browser signed in. Day-to-day commands are in
`docs/operations.md`; what the system is, in `docs/architecture.md`.

The scripts assume nothing about the homelab: every address, name and mode comes from `.env`, and on the VM they
write only to Docker and `ANNEX_DATA_DIR`. What they do assume is a VM prepared as in section 2 and a machine to
run them from as in section 3. Those two are not scripted; this page is the script.

This is the prototype's access model: one SSH key that is root-equivalent on the VM (the `docker` group), secrets
on the VM's disk, builds on the VM. Fine for a disposable instance on a LAN; what changes before a client runs it
is listed at the end of `docs/plans/hardening.md`.

## 1. Decide first: names, certificates, the path to the browser

Three choices shape `.env` and the VM. Decide them before section 2.

**Hostnames.** Every service and app gets a name from `ANNEX_HOST_PATTERN`, e.g. `{name}.annex.example.lan`
gives `home.annex.example.lan`, `auth.annex.example.lan` and `<app>.annex.example.lan`. The browser machines must
resolve every such name to the VM, including names that don't exist yet (each deploy makes one). That needs a
**wildcard**: a LAN DNS server with `*.annex.example.lan → <VM IP>`, or a `dnsmasq` per browser machine, or
`/etc/hosts` lines per name as a fallback (`docs/setup.md` §3 has both for a Mac). Authelia's session cookie is set
on everything after `{name}.`, so the names must share that suffix.

**Certificates** (`ANNEX_TLS_MODE`):

| Mode | Who terminates HTTPS | What it needs | Status |
|---|---|---|---|
| `local` (default) | Traefik on the VM, port 443, a wildcard certificate from `mkcert` | `mkcert` on the operator machine; its root CA trusted on **every browser machine** | the proven path: the demo, the suites and CI all run it |
| `upstream` | an existing reverse proxy in front of the VM, which forwards plain HTTP to the VM's port 80 | `ANNEX_UPSTREAM_IP` = that proxy's IP (the only source trusted for `X-Forwarded-*`); the proxy owns DNS and certificates | rendered and configured, **never tested against a real proxy**; the test suites don't cover it (they speak TLS through the tunnel) |

Take `local` unless you already run a proxy and will test the login redirects yourself.

**Port.** With `local` mode and browsers on the VM's LAN, Traefik is reached on 443 and `ANNEX_PUBLIC_PORT` stays
empty. From a machine that can't reach the LAN (only SSH), `scripts/tunnel.sh` forwards `127.0.0.1:8443` to the
VM's 443; then `ANNEX_PUBLIC_PORT=8443`, because every URL and login redirect carries the port. The suites always
go through the tunnel, whatever the browsers do.

## 2. The VM

What the prototype runs on: Ubuntu 24.04 LTS, x86_64, 2 vCPU, 4 GiB RAM, 30 GB disk, a fixed IP (static or a
DHCP reservation). The platform uses about 2.5 GiB with Metabase; a smaller VM runs the core only
(`scripts/push.sh` without `--extras`). It was built with Docker Engine 29.9 and Compose 5.6; newer ones should
work, older Compose may not (the scripts use `up --wait`).

`vm/prepare-vm.sh` does the preparation: one idempotent script, run once as root on the fresh VM. Make the
operator's key first (section 3), then send the script over SSH from the repo, with an account that has sudo on
the new VM:
```bash
ssh <you>@<vm-ip> "sudo bash -s -- --lan 192.168.1.0/24 --pubkey '$(cat ~/.ssh/annex_ed25519.pub)'" < vm/prepare-vm.sh
```
`--lan` is the network allowed in; `--user` and `--data-dir` change the defaults `annex` and `/srv/annex` (then
set the same in `.env`); `--no-firewall` leaves the host firewall alone where a security group does that job. It
prints one line per step, `already:` for what was in place, and ends with what the VM now has. Running it again
changes nothing. What it does, and the undo of each (the usual undo is to destroy the VM):

| Step | Undo |
|---|---|
| Docker Engine and the Compose plugin from Docker's apt repository, as on the homelab VM; `jq` (the push's JSON) and `python3-yaml` (the user commands of `vm/annex_vm.py`) | `sudo apt-get purge -y docker-ce docker-ce-cli containerd.io docker-compose-plugin jq python3-yaml && sudo rm /etc/apt/sources.list.d/docker.list /etc/apt/keyrings/docker.asc` |
| `/etc/docker/daemon.json`: each container's own log capped at 3 × 10 MB (the stored logs live in Postgres). An existing file is left alone, with a note | `sudo rm /etc/docker/daemon.json && sudo systemctl restart docker` |
| the `annex` user in the `docker` and `sudo` groups. The scripts log in as it and never call sudo; sudo is for this script and the restore procedure in `docs/operations.md` ("Restore a whole VM"). Passwordless sudo is a convenience, not a requirement | `sudo deluser --remove-home annex` |
| the data directory, owned by `annex`, mode 750 (its backups contain the secrets) | `sudo rm -rf /srv/annex` (everything Annex, secrets and backups included) |
| the operator's key in `~annex/.ssh/authorized_keys`; password logins off in `sshd` (`/etc/ssh/sshd_config.d/60-annex.conf`) | `sudo rm /etc/ssh/sshd_config.d/60-annex.conf && sudo systemctl reload ssh` |
| `ufw`: deny incoming, allow 22, 80 and 443 TCP from `--lan`. Nothing in Annex uses UDP or HTTP/3, and only Traefik publishes ports (the smoke test checks this), so this is a second line, not the first. Docker publishes ports through its own `iptables` chains, past `ufw`'s input rules; the homelab VM also blocked other published ports, but how is not in this repo. If that matters, add rules to the `DOCKER-USER` chain or use `ufw-docker` | `sudo ufw disable && sudo apt-get purge -y ufw` |

The script has been written from the homelab VM's spec and the scripts' needs; it has not yet been run on a fresh
machine. The first new VM is its test: read its output.

**Not in the script, as on the homelab VM, optional:** security-only unattended upgrades without automatic reboots
(`sudo apt-get install -y unattended-upgrades`, keep the default of security updates only, and leave
`Unattended-Upgrade::Automatic-Reboot` unset); a 2 GiB swap file; the VM starting with its host.

**Check** (from the operator machine, once section 3 is done):
```bash
ssh annex 'id; docker info --format "{{.ServerVersion}} {{.Architecture}}"; docker compose version; jq --version; python3 -c "import yaml; print(\"pyyaml ok\")"; test -w /srv/annex && echo "data dir ok"'
```
It must print the `docker` group, `x86_64`, a Compose version, `pyyaml ok` and `data dir ok`, with no sudo prompt.

## 3. The operator machine

Where the repo is checked out and the scripts run. The docs say "the Mac", but a Linux workstation is the same
(CI runs `bootstrap.sh` and `push.sh` on an Ubuntu runner); only the `sed -i ''` lines in `docs/setup.md` are
macOS-specific.

**Tools:** `git`, `ssh`, `rsync` (macOS ships `openrsync`, which has been enough; GNU rsync from Homebrew if it
isn't), `curl`, `python3` (3.9 or newer for the scripts; uv fetches the CLI's own), `uv` (the CLI, the MCP server,
the ops unit tests), `mkcert` (`local` mode: the certificate, and the suites' `curl --cacert`), `node` (optional:
one test in `test-smoke.sh`, skipped without it), `docker` only for the local lane (`docs/setup.md` §8).

**SSH key and config.** One key for this instance, no passphrase (the scripts and the CLI open many connections):
```bash
ssh-keygen -t ed25519 -f ~/.ssh/annex_ed25519 -N '' -C annex
```
`vm/prepare-vm.sh` puts `~/.ssh/annex_ed25519.pub` on the VM (section 2); then add the block to `~/.ssh/config`. The scripts and the
CLI only ever run `ssh annex` (the alias is `ANNEX_SSH_HOST` in `.env`), so everything about the path to the VM
lives here:
```
Host annex
  HostName 192.168.1.30
  User annex
  IdentityFile ~/.ssh/annex_ed25519
  IdentitiesOnly yes
  ServerAliveInterval 30
  # Only when the VM is reachable through another host (the homelab reaches it through dronky):
  # ProxyJump you@jumphost
```
Check: `ssh annex true` with no prompt. Undo: delete the block, `ssh-keygen -R annex`,
`rm ~/.ssh/annex_ed25519 ~/.ssh/annex_ed25519.pub`.

**mkcert's CA** (`local` mode). `mkcert -install` creates the CA on first use and trusts it in this machine's
stores, so the browser here accepts the certificate. The suites don't need the trust store: they pass
`$(mkcert -CAROOT)/rootCA.pem` to curl. Undo: `mkcert -uninstall` (only if nothing else on the machine uses it).

## 4. `.env`

```bash
cp .env.example .env
```
`.env` is gitignored and is the one file to edit; `scripts/render.py` derives everything else. What to set:

| Key | Set it to |
|---|---|
| `ANNEX_SSH_HOST` | the `Host` alias from section 3 (`annex`) |
| `ANNEX_DATA_DIR` | the directory from section 2 (`/srv/annex`) |
| `ANNEX_HOST_PATTERN` | your pattern from section 1, `{name}` first, at least two labels after it: `{name}.annex.example.lan` |
| `ANNEX_COOKIE_DOMAIN` | empty (derived: everything after `{name}.`) |
| `ANNEX_TLS_MODE` | `local`, or `upstream` with `ANNEX_UPSTREAM_IP` (section 1) |
| `ANNEX_PUBLIC_PORT` | empty when reached on 443; `8443` when the browser goes through the tunnel |
| `ANNEX_BIND_HTTP`, `ANNEX_BIND_HTTPS` | empty: 80 and 443 on every interface of the VM |
| `ANNEX_EMAIL_DOMAINS`, `ANNEX_EMAIL_ALLOW`, `ANNEX_EMAIL_FROM` | who apps may email. Mail never leaves the VM (Mailpit catches it), so the defaults are fine; the demo users' domains must stay listed while you use them |
| `ANNEX_ALERT_SLACK_CHANNEL` | only matters once Slack is connected (`docs/setup.md` §7) |
| `ANNEX_APP_SUBNETS` | a /16 that overlaps neither your LAN nor Docker's own pools; the default `10.213.0.0/16` unless your LAN is in it |
| `TZ` | the instance's time zone (schedules, the weekly digest) |

Check it without touching the VM:
```bash
python3 scripts/render.py
```
It prints the mode and the home URL, or says which key is wrong. The rendered bundle is in `.build/vm/platform`;
its `.env` is what the VM will get (non-secret settings only).

## 5. Bootstrap, push, smoke

```bash
scripts/bootstrap.sh
```
Creates the layout under the data directory, generates every secret **on the VM** (`secrets/master.env`,
Authelia's keys, the database passwords, three demo users with random passwords) and, in `local` mode, issues the
wildcard certificate with `mkcert` and copies it over. Re-running it never replaces an existing secret.

```bash
scripts/push.sh --extras
```
Renders, syncs the bundle to `<data dir>/platform`, applies the ops database roles and migrations, builds the
`annex-ops` image on the VM, pulls the pinned images and starts everything (`--extras` adds Metabase and Dozzle;
leave it out on a small VM), connects the platform to every app network, applies the gateway schema, brings up and
links Goat Arcade's database (the demo client, seeded on first start) and configures Metabase. The first push
pulls every pinned image (Metabase is the big one) and waits up to seven minutes for the stack to be healthy;
later pushes take about a minute. It ends with the container table and the home URL. The same command applies every later change to the
platform.

```bash
scripts/backup.sh
```
Once, right after the first push: the smoke test checks that the newest backup is under 26 hours old, and a fresh
instance has none until the nightly run at 02:00.

```bash
scripts/test-smoke.sh > .build/smoke.log 2>&1; tail -5 .build/smoke.log
```
Under a minute: every part once, through the SSH tunnel it opens itself (no hosts entries needed). Write it to a
file rather than through a pipe (the tunnel can hold a pipe open). Every line should be green; a red one names the
part and what to look at. The passwords of the three demo users (`sam`, a builder; `maja`, a user;
`kaldi-admin`, an admin and builder):
```bash
scripts/show-users.sh
```

## 6. The browser

`local` mode, browsers on the LAN: the names from section 1 must resolve to the VM's IP, and the mkcert root CA
(`$(mkcert -CAROOT)/rootCA.pem` on the operator machine) must be trusted on each browser machine (copy the file
there and add it to the system's trust store, or run `mkcert -install` on a machine that has the same CA files).
Then open `https://home.<your domain>/` and sign in as a demo user.

Through the tunnel only (`ANNEX_PUBLIC_PORT=8443`): `scripts/tunnel.sh up`, then the names must resolve to
`127.0.0.1` on the operator machine (`docs/setup.md` §3: `/etc/hosts` lines or a `dnsmasq` wildcard) and the URL
is `https://home.<your domain>:8443/`.

`upstream` mode: the proxy's names and certificate; nothing to trust.

## 7. After it runs

- **Deploy an app:** `./annex init python-web my-tool --owner sam`, `./annex deploy my-tool`; the contract is
  `./annex contract`. Claude Code gets the MCP server and the skill with `docs/setup.md` §4.
- **Accounts:** `./annex users add <login> --name '…' --email … --groups users|builders|admins` emails an
  invitation (into Mailpit, until there is a real relay). The three demo users and Goat Arcade's database are what
  the test suites rely on: keep them while you use the suites, remove them (`./annex users remove`,
  `docs/teardown.md` "Remove only the demo client's database") when the instance is for something else.
- **Slack:** optional, `docs/setup.md` §7. Without it, Slack posts fail with a clear "not connected" and email is
  unaffected.
- **Metabase and Dozzle:** `scripts/extras.sh on|off`.
- **Backups:** nightly at 02:00 into `<data dir>/backups`, 7 days, same disk, unencrypted, secrets included:
  copy them somewhere only if that place may hold the secrets. Restoring one onto another VM:
  `docs/operations.md` "Restore a whole VM" (its first step is this page's section 2).
- **Start over:** `scripts/wipe-vm.sh` (asks first) removes everything Annex from the VM; `bootstrap.sh` and
  `push.sh` bring it back with new secrets. The VM's own preparation stays. `docs/teardown.md` has the rest.
