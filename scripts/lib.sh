# Shared helpers for scripts/*.sh (sourced, not executed).
set -euo pipefail
ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
ENV_FILE=$ROOT/.env
[ -f "$ENV_FILE" ] || ENV_FILE=$ROOT/.env.example
set -a
# shellcheck disable=SC1090
. "$ENV_FILE"
set +a
SSH_HOST=${ANNEX_SSH_HOST:-annex}
DATA=${ANNEX_DATA_DIR:-/srv/annex}

vm() { ssh "$SSH_HOST" "$@"; }
log() { printf '\033[1m==> %s\033[0m\n' "$*"; }
render() { python3 "$ROOT/scripts/render.py"; }
# a value from the locally rendered platform .env (what the next push will send)
setting_local() { sed -n "s/^$1=//p" "$ROOT/.build/platform/.env"; }
# a value from the platform .env deployed on the VM, the same file the annex CLI reads, so scripts and the CLI
# agree even when the local .env has changed since the last push (D85). One ssh per run: cached in a file keyed
# by this shell's pid ($$ is the main shell's inside $(...) too). With no deployed .env yet (before the first
# push) it falls back to the local render and says so.
_settings_cache=$ROOT/.build/settings.$$
rm -f "$_settings_cache"
setting() {
  if [ ! -s "$_settings_cache" ]; then
    mkdir -p "$ROOT/.build"
    find "$ROOT/.build" -maxdepth 1 -name 'settings.*' -mtime +0 -delete 2>/dev/null || true
    if ! ssh "$SSH_HOST" "cat $DATA/platform/.env" > "$_settings_cache" 2>/dev/null || [ ! -s "$_settings_cache" ]; then
      rm -f "$_settings_cache"
      [ -f "$ROOT/.build/platform/.env" ] || render > /dev/null
      echo "settings: no $DATA/platform/.env on $SSH_HOST yet, using the local render" >&2
      cp "$ROOT/.build/platform/.env" "$_settings_cache"
    fi
  fi
  sed -n "s/^$1=//p" "$_settings_cache"
}
compose_image() { sed -n "s|^ *image: \($1:.*\)|\1|p" "$ROOT/compose.yaml" | head -1; }
