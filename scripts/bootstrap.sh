#!/usr/bin/env bash
# One-time (idempotent) preparation of the VM, or of the local stack with ANNEX_ENV=local (D99): directory layout,
# secrets and the first users (the demo users, or one admin with ANNEX_DEMO=off, D118) generated there, and in local
# TLS mode the mkcert wildcard certificate.
# Re-running never replaces existing secrets.
source "$(dirname "$0")/lib.sh"

render
log "Preparing $DATA on $WHERE"
if [ "$TARGET" = local ]; then
  # the VM's /srv/annex is made by a human (a system change); the local data dir is ours, outside the repo (D99)
  mkdir -p "$DATA"
  missing=
  for c in docker jq rsync openssl mkcert; do command -v "$c" >/dev/null || missing="$missing $c"; done
  [ -z "$missing" ] || { echo "local stack: missing on this machine:$missing" >&2; exit 1; }
  python3 -c 'import yaml' 2>/dev/null \
    || { echo "local stack: python3 ($(command -v python3)) has no PyYAML, which annex_vm.py users-* needs (as python3-yaml on the VM). Install it for that python3: python3 -m pip install --user pyyaml==6.0.3" >&2; exit 1; }
  docker info >/dev/null 2>&1 \
    || { echo "local stack: Docker (context ${DOCKER_CONTEXT:-default}) isn't answering. Start Docker Desktop, then run this again." >&2; exit 1; }
fi
vm "test -d $DATA && test -w $DATA" || { echo "$DATA missing or not writable on $WHERE" >&2; exit 1; }
vm "mkdir -p $DATA/platform/vm"
put "$ROOT/platform/vm/bootstrap-secrets.sh" "$DATA/platform/vm/"
# the first users: the demo users, or with ANNEX_DEMO=off the one admin from the settings (D118)
first_users="ANNEX_DEMO=$(setting_local ANNEX_DEMO)"
if [ "$(setting_local ANNEX_DEMO)" = off ]; then
  for k in LOGIN NAME EMAIL; do first_users="$first_users ANNEX_ADMIN_$k=$(printf '%q' "$(setting_local ANNEX_ADMIN_$k)")"; done
fi
vm "ANNEX_DATA_DIR=$DATA $first_users bash $DATA/platform/vm/bootstrap-secrets.sh $(compose_image authelia/authelia)"

if [ "$(setting_local ANNEX_TLS_MODE)" = local ]; then   # before the first push there is no deployed .env: the local render
  names=$(setting_local ANNEX_CERT_NAMES)
  if vm "test -s $DATA/certs/wildcard.pem && test -s $DATA/certs/names && [ \"\$(cat $DATA/certs/names)\" = '$names' ]"; then
    log "Certificate for $names already in $DATA/certs"
  else
    log "Issuing mkcert certificate for $names"
    tmp=$(mktemp -d)
    trap 'rm -rf "$tmp"' EXIT
    # shellcheck disable=SC2086
    (cd "$tmp" && mkcert -cert-file wildcard.pem -key-file wildcard-key.pem $names)
    echo "$names" > "$tmp/names"
    put "$tmp/wildcard.pem" "$tmp/wildcard-key.pem" "$tmp/names" "$DATA/certs/"
    vm "chmod 600 $DATA/certs/wildcard-key.pem"
  fi
fi
if [ "$ANNEX_ENV" = vm ]; then log "Bootstrap done. Next: scripts/push.sh"; else log "Bootstrap done. Next: ANNEX_ENV=$ANNEX_ENV scripts/push.sh"; fi
