#!/usr/bin/env bash
# SSH tunnel from the Mac to the VM's Traefik: localhost:<port> -> annex:443
# (or :80 in upstream mode). Port defaults to ANNEX_PUBLIC_PORT, else 8443.
# Usage: scripts/tunnel.sh up|down|status   (no argument: status)
source "$(dirname "$0")/lib.sh"
PORT=${ANNEX_PUBLIC_PORT:-8443}
if [ "$TARGET" = local ]; then   # the local stack (D99) publishes Traefik on loopback itself
  case "${1:-status}" in
    up|down|status) echo "local mode: no tunnel, Traefik is on ${ANNEX_BIND_HTTPS:-127.0.0.1:$PORT}"; exit 0 ;;
    *) echo "usage: $0 up|down|status" >&2; exit 2 ;;
  esac
fi
TARGET=443
[ "${ANNEX_TLS_MODE:-local}" = upstream ] && TARGET=80
mkdir -p "$ROOT/.build"
SOCK=$ROOT/.build/tunnel.sock

case "${1:-status}" in
  up)
    if ssh -S "$SOCK" -O check "$SSH_HOST" 2>/dev/null; then exit 0; fi
    ssh -f -N -M -S "$SOCK" -o ExitOnForwardFailure=yes -L "127.0.0.1:$PORT:localhost:$TARGET" "$SSH_HOST"
    echo "tunnel: 127.0.0.1:$PORT -> $SSH_HOST:$TARGET" ;;
  down)
    ssh -S "$SOCK" -O exit "$SSH_HOST" 2>/dev/null || true ;;
  status)
    if ssh -S "$SOCK" -O check "$SSH_HOST" 2>/dev/null; then
      echo "tunnel: up (127.0.0.1:$PORT -> $SSH_HOST:$TARGET)"
    else
      echo "tunnel: down. Start it with: scripts/tunnel.sh up"
      exit 1
    fi ;;
  *) echo "usage: $0 up|down|status" >&2; exit 2 ;;
esac
