# Teardown

When the demo stage is over. Steps 1–3 come from the brief; build-specific items are marked *(build)*.

## 0. Keep what's worth keeping *(build)*
- Push the repo to GitHub, if that hasn't happened. Nejc adds the remote; `.env` and `.build/` are gitignored and stay local.
- Optionally copy anything you want from the VM first, e.g. `scripts/backup.sh` and then `scp annex:/srv/annex/backups/<archive> .`. Everything on the VM is synthetic.

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
