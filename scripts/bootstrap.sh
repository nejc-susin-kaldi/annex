#!/usr/bin/env bash
# One-time (idempotent) VM preparation: directory layout, secrets and demo users
# generated on the VM, and in local TLS mode the mkcert wildcard certificate.
# Re-running never replaces existing secrets.
source "$(dirname "$0")/lib.sh"

render
log "Preparing $DATA on $SSH_HOST"
vm "test -d $DATA && test -w $DATA" || { echo "$DATA missing or not writable on $SSH_HOST" >&2; exit 1; }
vm "mkdir -p $DATA/platform/vm"
scp -q "$ROOT/vm/bootstrap-secrets.sh" "$SSH_HOST:$DATA/platform/vm/bootstrap-secrets.sh"
vm "ANNEX_DATA_DIR=$DATA bash $DATA/platform/vm/bootstrap-secrets.sh $(compose_image authelia/authelia)"

if [ "$(setting ANNEX_TLS_MODE)" = local ]; then
  names=$(setting ANNEX_CERT_NAMES)
  if vm "test -s $DATA/certs/wildcard.pem && test -s $DATA/certs/names && [ \"\$(cat $DATA/certs/names)\" = '$names' ]"; then
    log "Certificate for $names already on the VM"
  else
    log "Issuing mkcert certificate for $names"
    tmp=$(mktemp -d)
    trap 'rm -rf "$tmp"' EXIT
    # shellcheck disable=SC2086
    (cd "$tmp" && mkcert -cert-file wildcard.pem -key-file wildcard-key.pem $names)
    echo "$names" > "$tmp/names"
    scp -q "$tmp/wildcard.pem" "$tmp/wildcard-key.pem" "$tmp/names" "$SSH_HOST:$DATA/certs/"
    vm "chmod 600 $DATA/certs/wildcard-key.pem"
  fi
fi
log "Bootstrap done. Next: scripts/push.sh"
