# Teardown

When the demo stage is over. Steps 1–3 come from the brief; build-specific items are marked *(build)*.

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
- *(build)* Stop the tunnel, if it's running:
  ```bash
  scripts/tunnel.sh down
  ```
- Undo `docs/setup.md`:
  - Docker context:
    ```bash
    docker context rm annex-vm
    ```
  - `/etc/hosts` entries:
    ```bash
    sudo sed -i '' '/# annex$/d' /etc/hosts
    ```
  - MCP server: unregister it from Claude Code (command added in M0.4).
  - mkcert: **don't uninstall** the CA. It was trusted before Annex (see setup.md §2). The Annex certificate exists only on the VM.

## 3. Clean up the homelab
- Remove the router's DHCP reservation for `52:54:00:a7:13:30`.
- Remove the Annex route from the Traefik on `dronky`, if one was added.

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
