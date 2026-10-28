#!/usr/bin/env bash
# Prepares a fresh Ubuntu VM for Annex: the steps of docs/new-instance.md section 2 as one idempotent script.
# A human runs it ONCE, as root, on the new VM, before scripts/bootstrap.sh. It is the only Annex script that
# changes the VM's system configuration; everything after it writes to Docker and the data directory only.
#
# From the operator machine, with an account that has sudo on the new VM (the script is sent over stdin):
#   ssh <you>@<vm> "sudo bash -s -- --lan 192.168.1.0/24 --pubkey '$(cat ~/.ssh/annex_ed25519.pub)'" < platform/vm/prepare-vm.sh
#
# Options:
#   --pubkey 'ssh-ed25519 AAAA… comment'   the operator's public key, put in the annex user's authorized_keys (required)
#   --lan 192.168.1.0/24                   the network allowed to reach 22, 80 and 443 (required unless --no-firewall)
#   --user annex                           the account the scripts log in as (default annex)
#   --data-dir /srv/annex                  ANNEX_DATA_DIR of .env (default /srv/annex)
#   --no-firewall                          leave the host firewall alone (the cloud's security group does that job)
#
# What it does, each step skipped when already done: Docker Engine and the Compose plugin from Docker's apt
# repository, jq and python3-yaml; container log rotation in /etc/docker/daemon.json (never overwrites an existing
# file); the user in the docker and sudo groups; the data directory, mode 750; the key in authorized_keys; password
# logins off in sshd (a drop-in file); ufw with 22, 80 and 443 TCP from the LAN. It prints one line per step.
# Undo: docs/new-instance.md section 2.
#
# Running as `bash -s` reads this file from stdin, so the body is a function called on the last line with stdin
# closed: by then bash has read the whole file, and no command can swallow the rest of it.
set -euo pipefail

main() {
  local user=annex data_dir=/srv/annex lan='' pubkey='' firewall=1
  while [ $# -gt 0 ]; do
    case "$1" in
      --user) user=${2:?--user needs a name}; shift 2 ;;
      --data-dir) data_dir=${2:?--data-dir needs a path}; shift 2 ;;
      --lan) lan=${2:?--lan needs a network like 192.168.1.0/24}; shift 2 ;;
      --pubkey) pubkey=${2:?--pubkey needs the public key line}; shift 2 ;;
      --no-firewall) firewall=; shift ;;
      -h|--help) usage; exit 0 ;;
      *) usage >&2; die "unknown option $1" ;;
    esac
  done
  [ "$(id -u)" = 0 ] || die "run it as root: ssh <you>@<vm> sudo bash -s -- … < platform/vm/prepare-vm.sh"
  [ -n "$pubkey" ] || die "--pubkey is required: the operator's public key, e.g. --pubkey \"\$(cat ~/.ssh/annex_ed25519.pub)\""
  [[ "$pubkey" =~ ^(ssh-ed25519|ssh-rsa|ecdsa-sha2-nistp256|sk-ssh-ed25519@openssh.com)\ [A-Za-z0-9+/=]+ ]] \
    || die "--pubkey doesn't look like a public key line (ssh-ed25519 AAAA… comment)"
  if [ -n "$firewall" ]; then
    [[ "$lan" =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+/[0-9]+$ ]] || die "--lan must be a network like 192.168.1.0/24 (or pass --no-firewall)"
  fi
  [[ "$user" =~ ^[a-z_][a-z0-9_-]*$ ]] || die "--user $user is not a plain account name"
  [[ "$data_dir" = /* ]] || die "--data-dir must be an absolute path"
  export DEBIAN_FRONTEND=noninteractive

  # --- the machine ---
  # shellcheck disable=SC1091
  . /etc/os-release
  [ "${ID:-}" = ubuntu ] || die "this is ${PRETTY_NAME:-not Ubuntu}; the script knows Ubuntu's packages only"
  [ "${VERSION_ID:-}" = 24.04 ] || note "Ubuntu ${VERSION_ID:-?}: the prototype runs on 24.04; the steps are the same, the versions aren't"
  [ "$(uname -m)" = x86_64 ] || note "$(uname -m): Annex's images are pinned for amd64 (the local lane builds arm64, D98)"

  # --- packages ---
  step "packages: ca-certificates curl jq python3-yaml"
  apt-get update -qq
  apt-get install -y -qq ca-certificates curl jq python3-yaml >/dev/null
  if [ ! -s /etc/apt/sources.list.d/docker.list ]; then
    step "Docker's apt repository"
    install -m 0755 -d /etc/apt/keyrings
    curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
    chmod a+r /etc/apt/keyrings/docker.asc
    echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu ${VERSION_CODENAME} stable" \
      > /etc/apt/sources.list.d/docker.list
    apt-get update -qq
  else
    already "Docker's apt repository"
  fi
  if command -v docker >/dev/null && docker compose version >/dev/null 2>&1; then
    already "Docker Engine and the Compose plugin ($(docker --version | sed 's/,.*//'), $(docker compose version --short))"
  else
    step "Docker Engine and the Compose plugin"
    apt-get install -y -qq docker-ce docker-ce-cli containerd.io docker-compose-plugin >/dev/null
  fi

  # --- container log rotation (Annex never changes daemon.json later: D88) ---
  if [ -s /etc/docker/daemon.json ]; then
    if grep -q '"max-size"' /etc/docker/daemon.json; then
      already "/etc/docker/daemon.json has log rotation"
    else
      note "/etc/docker/daemon.json exists without log-opts: left as it is. Add {\"log-driver\": \"json-file\", \"log-opts\": {\"max-size\": \"10m\", \"max-file\": \"3\"}} yourself"
    fi
  else
    step "/etc/docker/daemon.json: json-file logs, 10m x 3 per container"
    install -d -m 755 /etc/docker
    printf '{\n  "log-driver": "json-file",\n  "log-opts": {"max-size": "10m", "max-file": "3"}\n}\n' > /etc/docker/daemon.json
    systemctl restart docker
  fi
  systemctl enable --now docker >/dev/null 2>&1 || true

  # --- the account the scripts use ---
  if id "$user" >/dev/null 2>&1; then
    already "user $user"
  else
    step "user $user"
    adduser --disabled-password --gecos 'Annex' "$user" >/dev/null
  fi
  for g in docker sudo; do
    if id -nG "$user" | tr ' ' '\n' | grep -qx "$g"; then already "$user is in $g"; else step "$user into group $g"; usermod -aG "$g" "$user"; fi
  done
  local home; home=$(getent passwd "$user" | cut -d: -f6)

  # --- the data directory ---
  if [ -d "$data_dir" ]; then
    already "$data_dir exists ($(stat -c '%U:%G %a' "$data_dir"))"
    [ "$(stat -c %U "$data_dir")" = "$user" ] || note "$data_dir is not owned by $user: chown -R $user:$user $data_dir if it is empty"
  else
    step "$data_dir, owned by $user, mode 750"
    install -d -o "$user" -g "$user" -m 750 "$data_dir"
  fi

  # --- the operator's key ---
  install -d -o "$user" -g "$user" -m 700 "$home/.ssh"
  touch "$home/.ssh/authorized_keys"; chown "$user:$user" "$home/.ssh/authorized_keys"; chmod 600 "$home/.ssh/authorized_keys"
  local keybody; keybody=$(awk '{print $1" "$2}' <<<"$pubkey")
  if grep -qF "$keybody" "$home/.ssh/authorized_keys"; then
    already "the key is in $home/.ssh/authorized_keys"
  else
    step "the key into $home/.ssh/authorized_keys"
    echo "$pubkey" >> "$home/.ssh/authorized_keys"
  fi

  # --- sshd: keys only ---
  if [ -s /etc/ssh/sshd_config.d/60-annex.conf ]; then
    already "sshd: password logins off (/etc/ssh/sshd_config.d/60-annex.conf)"
  else
    step "sshd: password logins off (/etc/ssh/sshd_config.d/60-annex.conf)"
    printf 'PasswordAuthentication no\nKbdInteractiveAuthentication no\n' > /etc/ssh/sshd_config.d/60-annex.conf
    sshd -t
    systemctl reload ssh 2>/dev/null || systemctl reload sshd
  fi

  # --- the host firewall ---
  if [ -n "$firewall" ]; then
    command -v ufw >/dev/null || { step "ufw"; apt-get install -y -qq ufw >/dev/null; }
    step "ufw: deny incoming, allow 22, 80 and 443 TCP from $lan"
    ufw default deny incoming >/dev/null
    ufw default allow outgoing >/dev/null
    for p in 22 80 443; do ufw allow from "$lan" to any port "$p" proto tcp >/dev/null; done
    ufw --force enable >/dev/null
    note "Docker publishes ports through its own iptables chains, past ufw's input rules; only Traefik publishes any (80, 443). To block other published ports too, add rules to the DOCKER-USER chain or use ufw-docker"
  else
    already "firewall left alone (--no-firewall)"
  fi

  # --- the result ---
  echo
  echo "prepare: done. The VM has:"
  echo "  $(docker --version | sed 's/,.*//'), Compose $(docker compose version --short), $(uname -m)"
  echo "  jq $(jq --version | sed 's/jq-//'), python3-yaml $(python3 -c 'import yaml; print(yaml.__version__)')"
  echo "  user $user in groups: $(id -nG "$user")"
  echo "  $data_dir: $(stat -c '%U:%G %a' "$data_dir")"
  echo "  sshd: $(sshd -T 2>/dev/null | grep -i '^passwordauthentication' || echo 'passwordauthentication ?')"
  [ -n "$firewall" ] && ufw status | sed 's/^/  ufw: /' | head -6
  echo "Next, from the operator machine: a Host block in ~/.ssh/config, then 'ssh annex true', .env, scripts/bootstrap.sh (docs/new-instance.md sections 3 to 5)."
}

usage() {
  cat <<'USAGE'
usage (as root on the new VM; from the operator machine the script goes over stdin):
  ssh <you>@<vm> "sudo bash -s -- --lan 192.168.1.0/24 --pubkey '$(cat ~/.ssh/annex_ed25519.pub)'" < platform/vm/prepare-vm.sh
  --pubkey 'ssh-ed25519 AAAA… comment'   the operator's public key (required)
  --lan 192.168.1.0/24                   the network allowed to reach 22, 80 and 443 (required unless --no-firewall)
  --user annex                           the account the scripts log in as (default annex)
  --data-dir /srv/annex                  ANNEX_DATA_DIR of .env (default /srv/annex)
  --no-firewall                          leave the host firewall alone
USAGE
}
step() { printf 'prepare: %s\n' "$*"; }
already() { printf 'prepare: already: %s\n' "$*"; }
note() { printf 'prepare: NOTE: %s\n' "$*" >&2; }
die() { printf 'prepare: %s\n' "$*" >&2; exit 1; }

main "$@" </dev/null
