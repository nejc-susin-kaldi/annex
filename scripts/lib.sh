# Shared helpers for scripts/*.sh (sourced, not executed).
set -euo pipefail
ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
# Which stack (D99): ANNEX_ENV unset or vm = the VM, settings in .env; local = this machine's Docker, settings in
# .env.local. Per command (ANNEX_ENV=local scripts/push.sh), never a default. Exported, so ./annex and render.py
# started from a script pick the same file.
export ANNEX_ENV=${ANNEX_ENV:-vm}
case "$ANNEX_ENV" in
  vm) ENV_FILE=$ROOT/.env ;;
  local) ENV_FILE=$ROOT/.env.local ;;
  *) echo "ANNEX_ENV=$ANNEX_ENV must be vm (the default) or local" >&2; exit 2 ;;
esac
[ -f "$ENV_FILE" ] || ENV_FILE=$ENV_FILE.example
# the transport comes from the file only: a value left in the shell by another stack must not redirect this one
unset ANNEX_TARGET ANNEX_DOCKER_CONTEXT
set -a
# shellcheck disable=SC1090
. "$ENV_FILE"
set +a
SSH_HOST=${ANNEX_SSH_HOST:-annex}
DATA=${ANNEX_DATA_DIR:-/srv/annex}
TARGET=${ANNEX_TARGET:-ssh}
BUILD=$ROOT/.build/$ANNEX_ENV   # render.py writes the platform bundle to $BUILD/platform

if [ "$TARGET" = local ]; then
  # Docker for every command of this script and its children (docker, docker compose, ./annex, annex_vm.py), without
  # touching the user's current context. Only a local daemon: a context or DOCKER_HOST that reaches the VM is refused.
  if [ -n "${ANNEX_DOCKER_CONTEXT:-}" ]; then
    unset DOCKER_HOST
    export DOCKER_CONTEXT=$ANNEX_DOCKER_CONTEXT
    _endpoint=$(docker context inspect "$DOCKER_CONTEXT" --format '{{.Endpoints.docker.Host}}' 2>/dev/null) \
      || { echo "local stack: no Docker context '$DOCKER_CONTEXT' (ANNEX_DOCKER_CONTEXT in ${ENV_FILE##*/}); 'docker context ls' lists them" >&2; exit 1; }
  else
    _endpoint=${DOCKER_HOST:-unix://}
  fi
  case "$_endpoint" in
    unix://*|npipe://*) ;;
    *) echo "local stack: Docker endpoint $_endpoint is not a local socket; set ANNEX_DOCKER_CONTEXT in ${ENV_FILE##*/} to a local context (desktop-linux)" >&2; exit 1 ;;
  esac
  # the same command string a script would send over ssh runs here, with the same environment
  vm() { bash -c "$*"; }
  vm_tty() { bash -c "$*"; }   # an interactive command (psql): the terminal is already this one
  # put FILE... DIR: copy files into a directory of the stack (scp to the VM)
  put() { local dest=${*: -1}; cp "${@:1:$#-1}" "$dest"; }
  # rsync destination for a directory of the stack
  dest() { printf '%s' "$1"; }
  WHERE="the local stack ($DATA)"
else
  vm() { ssh "$SSH_HOST" "$@"; }
  vm_tty() { ssh -t "$SSH_HOST" "$@"; }
  put() { local dest=${*: -1}; scp -q "${@:1:$#-1}" "$SSH_HOST:$dest"; }
  dest() { printf '%s:%s' "$SSH_HOST" "$1"; }
  WHERE=$SSH_HOST
fi
log() { printf '\033[1m==> %s\033[0m\n' "$*"; }
render() { python3 "$ROOT/scripts/render.py"; }
# a value from the locally rendered platform .env (what the next push will send)
setting_local() { sed -n "s/^$1=//p" "$BUILD/platform/.env"; }
# a value from the platform .env deployed on the VM (or in the local stack, D99), the same file the annex CLI reads, so scripts and the CLI
# agree even when the local .env has changed since the last push (D85). One ssh per run: cached in a file keyed
# by this shell's pid ($$ is the main shell's inside $(...) too). With no deployed .env yet (before the first
# push) it falls back to the local render and says so.
_settings_cache=$ROOT/.build/settings.$$
rm -f "$_settings_cache"
setting() {
  if [ ! -s "$_settings_cache" ]; then
    mkdir -p "$ROOT/.build"
    find "$ROOT/.build" -maxdepth 1 -name 'settings.*' -mtime +0 -delete 2>/dev/null || true
    if ! vm "cat $DATA/platform/.env" > "$_settings_cache" 2>/dev/null || [ ! -s "$_settings_cache" ]; then
      rm -f "$_settings_cache"
      [ -f "$BUILD/platform/.env" ] || render > /dev/null
      echo "settings: no $DATA/platform/.env on $WHERE yet, using the local render" >&2
      cp "$BUILD/platform/.env" "$_settings_cache"
    fi
  fi
  sed -n "s/^$1=//p" "$_settings_cache"
}
compose_image() { sed -n "s|^ *image: \($1:.*\)|\1|p" "$ROOT/platform/compose.yaml" | head -1; }
