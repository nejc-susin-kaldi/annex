#!/usr/bin/env bash
# Turn the "extras" profile (Metabase, Dozzle) on or off.
# Usage: scripts/extras.sh on|off
source "$(dirname "$0")/lib.sh"
case "${1:-}" in
  on)  exec "$ROOT/scripts/push.sh" --extras ;;
  off) vm "cd $DATA/platform && docker compose --profile extras rm -sf metabase dozzle" ;;
  *)   echo "usage: $0 on|off" >&2; exit 2 ;;
esac
