# A new instance: from an empty VM to a running platform

For a developer who has this repo and an empty virtual machine, and has to bring up a second Annex without
Nejc. It ends with the platform running, the smoke test green and a browser signed in. Day-to-day commands are in
`docs/operations.md`; what the system is, in `docs/architecture.md`.

The scripts assume nothing about the homelab: every address, name and mode comes from `.env`, and on the VM they
write only to Docker and `ANNEX_DATA_DIR`. What they do assume is a VM prepared as in section 2 and a machine to
run them from as in section 3. Those two are not scripted; this page is the script.

For an instance for real use on EC2 (Kaldi's own apps, no demo client), read section 8 first: it changes
sections 2, 4 and 5. This is the prototype's access model: one SSH key that is root-equivalent on the VM (the `docker` group), secrets
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
| `acme` | Traefik on the VM, port 443, a Let's Encrypt wildcard certificate it gets and renews itself (DNS-01 in Route53) | an EC2 instance whose IAM role may write the challenge record; public DNS for the names (D118) | for an instance for real use on EC2: section 8 |

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

`platform/vm/prepare-vm.sh` does the preparation: one idempotent script, run once as root on the fresh VM. Make the
operator's key first (section 3), then send the script over SSH from the repo, with an account that has sudo on
the new VM:
```bash
ssh <you>@<vm-ip> "sudo bash -s -- --lan 192.168.1.0/24 --pubkey '$(cat ~/.ssh/annex_ed25519.pub)'" < platform/vm/prepare-vm.sh
```
`--lan` is the network allowed in; `--user` and `--data-dir` change the defaults `annex` and `/srv/annex` (then
set the same in `.env`); `--no-firewall` leaves the host firewall alone where a security group does that job. It
prints one line per step, `already:` for what was in place, and ends with what the VM now has. Running it again
changes nothing. What it does, and the undo of each (the usual undo is to destroy the VM):

| Step | Undo |
|---|---|
| Docker Engine and the Compose plugin from Docker's apt repository, as on the homelab VM; `jq` (the push's JSON) and `python3-yaml` (the user commands of `platform/vm/annex_vm.py`) | `sudo apt-get purge -y docker-ce docker-ce-cli containerd.io docker-compose-plugin jq python3-yaml && sudo rm /etc/apt/sources.list.d/docker.list /etc/apt/keyrings/docker.asc` |
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
one test in `tests/smoke.sh`, skipped without it), `docker` only for the local lane (`docs/setup.md` §8).

**SSH key and config.** One key for this instance, no passphrase (the scripts and the CLI open many connections):
```bash
ssh-keygen -t ed25519 -f ~/.ssh/annex_ed25519 -N '' -C annex
```
`platform/vm/prepare-vm.sh` puts `~/.ssh/annex_ed25519.pub` on the VM (section 2); then add the block to `~/.ssh/config`. The scripts and the
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
tests/smoke.sh > .build/smoke.log 2>&1; tail -5 .build/smoke.log
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

## 8. On EC2: an instance for real use (D118)

An instance for a team's own apps, with no client database: Kaldi's. It is sections 1 to 7 with five settings
changed, run as its own lane from the same checkout (`ANNEX_ENV=kaldi`, settings in `.env.kaldi`), so the homelab
VM and the local lane stay as they are. What it changes:

| | The homelab VM | The EC2 instance |
|---|---|---|
| Settings | `.env`, `scripts/push.sh` | `.env.kaldi`, `ANNEX_ENV=kaldi scripts/push.sh` (every script and `./annex` take it in front) |
| Certificate | mkcert, trusted by hand on each machine | Let's Encrypt wildcard, renewed by Traefik (`ANNEX_TLS_MODE=acme`) |
| Demo | Goat Arcade's database, three demo users | none: one admin from `ANNEX_ADMIN_*` (`ANNEX_DEMO=off`); the gateway runs empty |
| Sign-in | a password | a password and a second factor (`ANNEX_AUTH_FACTORS=two`) |
| Mail | caught by Mailpit | caught by Mailpit and relayed to a real SMTP server (`ANNEX_MAIL_RELAY=on`) |
| Checks | `tests/smoke.sh` and the suites | `tests/instance.sh` (read-only); the suites refuse to run there |

It is on the internet and holds real data, so the brief's LAN-only and synthetic-data rules don't apply to it
(D118). What still applies is the production list at the end of `docs/plans/hardening.md`. In particular, everyone
who can deploy holds an SSH key that is root on the instance, and admins can read every message in Mailpit,
password-reset links included.

### 8.1 AWS (a human, once)

Everything here changes the AWS account, so the operator runs it. The examples use `annex.kaldi.si` and
`eu-central-1`; replace both.

1. **DNS zone in Route53.** The challenge record goes into a Route53 hosted zone for the domain. If `kaldi.si` is
   hosted elsewhere, create a hosted zone for `annex.kaldi.si` only and delegate it: copy its four NS records into
   `kaldi.si`'s DNS as `annex NS ...`. Note the zone id (`Z...`). Undo: delete the NS records, then the zone (empty
   it first).
2. **IAM role for the instance.** It may only write the challenge's TXT record. Save as `annex-acme-policy.json`,
   with your zone id:
   ```json
   {"Version": "2012-10-17", "Statement": [
     {"Effect": "Allow", "Action": "route53:GetChange", "Resource": "arn:aws:route53:::change/*"},
     {"Effect": "Allow", "Action": "route53:ListHostedZonesByName", "Resource": "*"},
     {"Effect": "Allow", "Action": "route53:ListResourceRecordSets", "Resource": "arn:aws:route53:::hostedzone/ZONE_ID"},
     {"Effect": "Allow", "Action": "route53:ChangeResourceRecordSets", "Resource": "arn:aws:route53:::hostedzone/ZONE_ID",
      "Condition": {"ForAllValues:StringEquals": {
        "route53:ChangeResourceRecordSetsNormalizedRecordNames": ["_acme-challenge.annex.kaldi.si"],
        "route53:ChangeResourceRecordSetsRecordTypes": ["TXT"]}}}]}
   ```
   ```bash
   aws iam create-role --role-name annex-kaldi --assume-role-policy-document '{"Version":"2012-10-17","Statement":[{"Effect":"Allow","Principal":{"Service":"ec2.amazonaws.com"},"Action":"sts:AssumeRole"}]}'
   ```
   ```bash
   aws iam put-role-policy --role-name annex-kaldi --policy-name acme-dns --policy-document file://annex-acme-policy.json
   ```
   ```bash
   aws iam create-instance-profile --instance-profile-name annex-kaldi && aws iam add-role-to-instance-profile --instance-profile-name annex-kaldi --role-name annex-kaldi
   ```
   Undo: `aws iam remove-role-from-instance-profile`, `delete-instance-profile`, `delete-role-policy`, `delete-role`,
   with the same names.
3. **Security group.** Inbound TCP 443 and 80 from Kaldi's addresses (office, VPN), TCP 22 from the operators'.
   Nothing else; outbound as AWS's default (Let's Encrypt, Route53, the mail relay, Slack, apps with `egress:`). Open
   443 to everyone only if an app needs webhooks from a SaaS; two factors are then what guards every other route.
   80 only redirects to 443.
4. **The instance.** Ubuntu 24.04 LTS **x86_64** (not Graviton: the images are pinned for amd64), `t3.medium`
   (2 vCPU, 4 GiB: the core platform without Metabase), a 40 GB encrypted gp3 volume, the instance profile
   `annex-kaldi`, the security group, the operator's EC2 key pair for the `ubuntu` user, and **IMDSv2 required with
   hop limit 2**: Traefik reads the role's credentials from inside a container, one hop further than the host. For
   an instance that already runs:
   ```bash
   aws ec2 modify-instance-metadata-options --instance-id i-... --http-tokens required --http-put-response-hop-limit 2
   ```
   Then an Elastic IP, associated with it.
5. **DNS for the names.** In the zone: `*.annex.kaldi.si A <elastic IP>` (TTL 300). Every app gets a name under
   it, so the wildcard is the only record.
6. **Snapshots.** A Data Lifecycle Manager policy for the instance's volume: daily, keep 7. The nightly backups are
   on the same disk (D87); this is the copy that survives losing it.

### 8.2 The instance and the operator machine

As sections 2 and 3, with a key and an alias of its own. `--no-firewall` because the security group does that job:
```bash
ssh-keygen -t ed25519 -f ~/.ssh/annex_kaldi_ed25519 -N '' -C annex-kaldi
```
```bash
ssh ubuntu@<elastic IP> "sudo bash -s -- --no-firewall --pubkey '$(cat ~/.ssh/annex_kaldi_ed25519.pub)'" < platform/vm/prepare-vm.sh
```
```
Host annex-kaldi
  HostName <elastic IP>
  User annex
  IdentityFile ~/.ssh/annex_kaldi_ed25519
  IdentitiesOnly yes
  ServerAliveInterval 30
```
This is the script's first run on a fresh machine (section 2): read its output, then run section 2's check with
`ssh annex-kaldi`.

### 8.3 `.env.kaldi`

```bash
cp .env.example .env.kaldi
```
Then set, besides section 4's keys (quote a value with spaces):
```
ANNEX_SSH_HOST=annex-kaldi
ANNEX_HOST_PATTERN={name}.annex.kaldi.si
ANNEX_TLS_MODE=acme
ANNEX_ACME_EMAIL=ops@kaldi.si
ANNEX_ACME_STAGING=true
ANNEX_DEMO=off
ANNEX_ADMIN_LOGIN=nejc
ANNEX_ADMIN_NAME="Nejc Sušin"
ANNEX_ADMIN_EMAIL=nejc.susin@kaldi.si
ANNEX_AUTH_FACTORS=two
ANNEX_MAIL_RELAY=on
ANNEX_EMAIL_DOMAINS=kaldi.si
ANNEX_EMAIL_FROM=annex@kaldi.si
ANNEX_ALERT_SLACK_CHANNEL=#annex-alerts
```
`ANNEX_ACME_STAGING=true` is for the first push (section 8.5). Check it: `ANNEX_ENV=kaldi python3 scripts/render.py`.

### 8.4 Mail

The relay's address and password go into a file on the instance that only a human writes. A Google Workspace
account's app password through `smtp.gmail.com`, Workspace's SMTP relay (`smtp-relay.gmail.com`) or Amazon SES
SMTP credentials all work; the relay must accept `ANNEX_EMAIL_FROM` as the sender. Make the folder (bootstrap makes
it too), then paste the file, Enter, Ctrl-D:
```bash
ssh annex-kaldi 'mkdir -p -m 700 /srv/annex/secrets/mailpit; umask 077; cat > /srv/annex/secrets/mailpit/relay.yaml'
```
```yaml
host: smtp.gmail.com
port: 587
starttls: true
auth: plain
username: annex@kaldi.si
password: <app password>
```
Mailpit reads it at start. After a change: `ssh annex-kaldi 'cd /srv/annex/platform && docker compose up -d --force-recreate mailpit'`.
Undo: set `ANNEX_MAIL_RELAY=off` (and `ANNEX_AUTH_FACTORS=one`, which needs it), push, then
`ssh annex-kaldi rm /srv/annex/secrets/mailpit/relay.yaml`.

### 8.5 Bootstrap, push, check

```bash
ANNEX_ENV=kaldi scripts/bootstrap.sh
```
```bash
ANNEX_ENV=kaldi scripts/push.sh
```
No `--extras`. Watch the certificate come (a minute or two):
```bash
ssh annex-kaldi 'docker logs annex-traefik-1 2>&1 | grep -i acme | tail -5'
```
With the staging CA the browser warns (an untrusted certificate): that proves the role, the zone and DNS. Then take
the real one: remove the line `ANNEX_ACME_STAGING=true`, delete the staging account and certificate, and push again.
```bash
ssh annex-kaldi rm /srv/annex/state/acme/acme.json
```
```bash
ANNEX_ENV=kaldi scripts/push.sh
```
Then the first backup (the check wants one under 26 hours old) and the check itself, which is read-only and goes to
the public names, so run it from a machine the security group lets in:
```bash
ANNEX_ENV=kaldi scripts/backup.sh
```
```bash
ANNEX_ENV=kaldi tests/instance.sh > .build/instance.log 2>&1; tail -5 .build/instance.log
```

### 8.6 People

- **The first admin:** `ANNEX_ENV=kaldi scripts/show-users.sh` prints the password bootstrap generated. Sign in at
  `https://home.annex.kaldi.si/`; Authelia then asks for a second factor and emails a code to register it. Change
  the password afterwards (Authelia's settings page).
- **Everyone else:** `ANNEX_ENV=kaldi ./annex users add ana --name 'Ana Novak' --email ana@kaldi.si --groups builders`
  emails an invitation. `users` can open the apps; `builders` deploy and see their own apps in the console; `admins`
  see everything, Mailpit included.
- **Builders' machines:** the repo, the SSH block and key of section 8.2 (the key is root on the instance: give it
  only to people you'd give root), `.env.kaldi`, and the MCP server under its own name, with the lane in its
  environment:
  ```bash
  claude mcp add --scope user annex-kaldi -e ANNEX_ENV=kaldi -- uv run --quiet --project "$(pwd)/toolkit/mcp" annex-mcp
  ```
  Undo: `claude mcp remove --scope user annex-kaldi`.
- **Slack:** `docs/setup.md` §7 in Kaldi's workspace, with `ssh annex-kaldi` and `ANNEX_ENV=kaldi scripts/push.sh`.
  `#annex-selftest` isn't needed: no suite runs there.

**Start over or tear down:** `ANNEX_ENV=kaldi scripts/wipe-vm.sh` removes everything Annex from the instance (asks
first); terminating the instance, releasing the Elastic IP and undoing section 8.1 removes the rest.
