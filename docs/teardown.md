# Teardown

When the demo stage is over. Steps 1–3 come from the brief; build-specific items are marked *(build)*.

## 0. Keep what's worth keeping *(build)*
- Push the repo to GitHub, if that hasn't happened. Nejc adds the remote; `.env` and `.build/` are gitignored and stay local.
- Optionally copy anything you want from the VM first, e.g. `scripts/backup.sh` and then `scp annex:/srv/annex/backups/<archive> .`. Everything on the VM is synthetic.

*(build)* What the hardening steps added on the VM, all under `/srv/annex` or in Docker, so `scripts/wipe-vm.sh` (or destroying the VM) removes it:
- per-app networks and egress networks (below), with their /24s from `ANNEX_APP_SUBNETS` (nothing outside Docker);
- `backups/` (mode 700) holds archives that contain `secrets/` and `state/directory/` (D87): delete any copy of an archive you took off the VM, and any `restore-scratch/` folder left from a restore check;
- `deploy/<app>/.lock*` and the pending lines in `apps/<app>/releases.jsonl` (D92);
- the `annex_ops.schema_version` table and the tables the migrations create (in the `annex_ops` database, which goes with appdata-db's data);
- `platform/VERSION` (the push stamp, D85).

On the Mac: `.build/settings.<pid>` (cached settings) goes with `.build`.

*(build)* App networks (`annex-app-<app>`, `annex-egress-<app>`, D88) carry the label `com.kaldi.annex=true`, so `scripts/wipe-vm.sh` removes them with the other networks (it removes the containers first); `annex remove` removes an app's networks on its own. Nothing outside Docker was changed for them (no `daemon.json`).

## 1. Destroy the VM
On `dronky`, as `neon`. This deletes the disk and its snapshot.
```bash
virsh -c qemu:///system destroy annex
```
```bash
virsh -c qemu:///system undefine annex --snapshots-metadata --remove-all-storage
```

## 2. Clean up the Mac
- Delete the `Host annex` block from `~/.ssh/config`. A backup is at `~/.ssh/config.bak-20261014-annex`.
- Remove the throwaway key and the host key:
  ```bash
  rm ~/.ssh/annex_ed25519 ~/.ssh/annex_ed25519.pub
  ```
  ```bash
  ssh-keygen -R annex
  ```
- *(build)* Stop the tunnel, if it's running. Do this **before** destroying the VM, or the SSH master process lingers until it times out:
  ```bash
  scripts/tunnel.sh down
  ```
- *(build)* Folders Claude Code built demo apps in, if you no longer want them:
  ```bash
  rm -rf ~/annex-demo
  ```
- *(build)* If you added Annex to the Claude chat app (not the Code tab): remove the `annex` entry from `claude_desktop_config.json` (Settings → Developer → Edit Config) and the uploaded `annex` skill (Settings → Capabilities → Skills).
- Undo `docs/setup.md`:
  - Docker context:
    ```bash
    docker context rm annex-vm
    ```
  - `/etc/hosts` entries:
    ```bash
    sudo sed -i '' '/# annex$/d' /etc/hosts
    ```
  - The wildcard resolver, if you set it up (setup.md §3): see the undo commands there (`/etc/resolver/annex.home.arpa`, the `dnsmasq` service and its config line).
  - MCP server and skill:
    ```bash
    claude mcp remove --scope user annex
    ```
    ```bash
    rm ~/.claude/skills/annex
    ```
  - *(build)* `annex` on PATH, if installed (setup.md §6):
    ```bash
    uv tool uninstall annex-cli
    ```
  - mkcert: **don't uninstall** the CA. It was trusted before Annex (see setup.md §2). The Annex certificate exists only on the VM.
- The local stack on Docker Desktop, if it was used (D99): the "Wipe it" commands in setup.md §8 (containers, networks, volumes, Annex's images and `~/.annex/local`), then the `/etc/hosts` lines tagged `# annex-local` and `/etc/resolver/annex-local.home.arpa` if they were added (§8's undo). The local stack's certificate lives in `~/.annex/local/certs` and goes with it.
- *(build)* The repo's local build output (rendered config, SSH control sockets):
  ```bash
  rm -rf .build
  ```

## 3. Clean up the homelab
- Remove the router's DHCP reservation for `52:54:00:a7:13:30`.
- Remove the Annex route from the Traefik on `dronky`, if one was added.
- If you connected Slack for M1.2 (setup.md §7): delete the Slack app you created in the test workspace (*api.slack.com/apps* → the app → *Basic Information* → *Delete App*), or the whole test workspace. The bot token lives only on the VM (`/srv/annex/secrets/slack.env`) and goes with it. Nothing else outside the VM and the Mac was created for mail: Mailpit only catches mail on the VM, and the `mail.annex.home.arpa` line in `/etc/hosts` is covered by the `# annex` undo above.

## Reset without destroying
- **Roll back to `clean-base`.** On `dronky`, as `neon`. This loses everything since the snapshot, including `/srv/annex`.
  ```bash
  virsh -c qemu:///system destroy annex
  ```
  ```bash
  virsh -c qemu:///system snapshot-revert annex clean-base
  ```
  ```bash
  virsh -c qemu:///system start annex
  ```
- **Wipe Annex from the VM**, then bring it back with new secrets:
  ```bash
  scripts/wipe-vm.sh
  ```
  ```bash
  scripts/bootstrap.sh && scripts/push.sh
  ```
- **Remove only the demo client's database** (Goat Arcade, D77), keeping the platform. The views stay in `annex_api`, but they fail until it's back. To drop them too: `DROP SCHEMA src_arcade CASCADE; DROP SERVER goat_arcade CASCADE;` in product-db, then redeploy any app that used them without those views.
  ```bash
  scripts/democlient.sh down
  ```
  ```bash
  ssh annex 'rm -rf /srv/annex/democlient /srv/annex/state/democlient-db /srv/annex/state/democlient.env /srv/annex/secrets/democlient.env'
  ```
  The state directory belongs to the container's postgres user. If `rm` is refused for it, remove it as root from the same image:
  ```bash
  ssh annex 'docker run --rm --network none -v /srv/annex/state:/state --entrypoint rm postgres:16.15-alpine3.24 -rf /state/democlient-db'
  ```
  `wipe-vm.sh` removes all of it too.
- **Reset only the demo content** (apps, report history), keeping the platform and passwords:
  ```bash
  scripts/reset-demo.sh
  ```

## Checklist
- [ ] Repo pushed
- [ ] Tunnel down
- [ ] VM destroyed (disk and `clean-base` snapshot gone)
- [ ] `~/.ssh/config` block, `annex_ed25519` key and host key removed
- [ ] Docker context, `/etc/hosts` line, MCP server, skill (and any Claude chat app config) removed
- [ ] `uv tool uninstall annex-cli`, if it was installed
- [ ] Router DHCP reservation and any `dronky` Traefik route removed
- [ ] Slack test app deleted, if one was created
