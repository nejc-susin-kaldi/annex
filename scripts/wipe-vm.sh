#!/usr/bin/env bash
# Return the VM to empty: removes every Annex container, network, volume and image, the demo client's
# database (democlient, D77), the build cache, and everything under /srv/annex (secrets included).
# bootstrap.sh + push.sh bring it back with new secrets and passwords.
source "$(dirname "$0")/lib.sh"
# The VM is dedicated to Annex, so this prunes every image and the build cache. On a Mac's Docker that would take the
# user's own images too: the local stack has its own, narrower wipe (docs/setup.md "Local stack", D99).
[ "$TARGET" = ssh ] || { echo "wipe-vm.sh is for the VM only; wipe the local stack as docs/setup.md \"Local stack\" says" >&2; exit 2; }

echo "This deletes ALL Annex containers, networks, volumes, images and everything under $SSH_HOST:$DATA."
if [ "${1:-}" != --yes ]; then
  read -r -p "Type 'wipe' to continue: " answer
  [ "$answer" = wipe ] || { echo "Aborted."; exit 1; }
fi

HELPER=$(compose_image alpine || true)
HELPER=${HELPER:-alpine:3.24.2}
vm bash -s -- "$DATA" "$HELPER" <<'REMOTE'
set -euo pipefail
DATA=$1 HELPER=$2
platform_images=$(sed -n 's/^ *image: //p' "$DATA"/platform/compose*.yaml 2>/dev/null | sort -u || true)
# the demo client's database isn't labelled (it isn't Annex's), but it holds annex-clientlink and lives in /srv/annex
echo "-- demo client"; docker ps -aq --filter label=com.docker.compose.project=democlient | xargs -r docker rm -f
echo "-- containers"; docker ps -aq --filter label=com.kaldi.annex=true | xargs -r docker rm -f
echo "-- networks";   docker network ls -q --filter label=com.kaldi.annex=true | xargs -r docker network rm
echo "-- volumes";    docker volume ls -q --filter label=com.kaldi.annex=true | xargs -r docker volume rm
echo "-- files";      docker run --rm -v "$DATA:/target" "$HELPER" sh -c 'rm -rf /target/* /target/.[!.]* 2>/dev/null; ls -A /target | wc -l'
echo "-- images"
docker image ls -q --filter label=com.kaldi.annex=true | xargs -r docker rmi -f >/dev/null
# shellcheck disable=SC2086
[ -n "$platform_images" ] && docker rmi $platform_images >/dev/null 2>&1 || true
docker rmi "$HELPER" >/dev/null 2>&1 || true
# the VM is dedicated to Annex: drop any remaining unused images and the build cache
docker image prune -af >/dev/null
docker builder prune -af >/dev/null
echo "-- left over"
docker ps -a --format '{{.Names}}'; docker image ls --format '{{.Repository}}:{{.Tag}}'
docker network ls --format '{{.Name}}' | grep -v -x -e bridge -e host -e none || true
ls -A "$DATA"
REMOTE
log "VM wiped. Restore with: scripts/bootstrap.sh && scripts/push.sh"
