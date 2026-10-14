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
# a value from the rendered platform .env
setting() { sed -n "s/^$1=//p" "$ROOT/.build/platform/.env"; }
compose_image() { sed -n "s|^ *image: \($1:.*\)|\1|p" "$ROOT/compose.yaml" | head -1; }
