#!/usr/bin/env bash
# Demo beat (docs/demo.md, "the safety net"): deploy a version of venue-pulse whose health check fails, and show that
# Annex rolls it back by itself. The running version stays live the whole time; the failed release is recorded, its
# image removed, and its logs (the error it wrote) stay searchable in the console and with `annex logs`.
#
# The broken version is a copy in .build/demo-bad-deploy: examples/goat-arcade/venue-pulse itself is never changed.
# Needs venue-pulse deployed (scripts/reset-demo.sh does that). Works on both lanes (ANNEX_ENV=local in front).
# Usage: scripts/demo-bad-deploy.sh
source "$(dirname "$0")/lib.sh"
APP=venue-pulse
SRC=$ROOT/examples/goat-arcade/$APP
WORK=$ROOT/.build/demo-bad-deploy

"$ROOT/annex" status "$APP" >/dev/null 2>&1 \
  || { echo "demo-bad-deploy: $APP isn't deployed on $WHERE (scripts/reset-demo.sh, or annex deploy $SRC)" >&2; exit 1; }

rm -rf "$WORK" && mkdir -p "$WORK"
trap 'rm -rf "$WORK"' EXIT
cp -R "$SRC" "$WORK/$APP"
# the health check now fails the way a version that lost its database would
sed -i '' 's/    return {"ok": True}/    raise RuntimeError("the new version cannot reach its database")/' "$WORK/$APP/main.py"
grep -q "cannot reach its database" "$WORK/$APP/main.py" || { echo "demo-bad-deploy: could not break the copy (has $SRC/main.py's healthz changed?)" >&2; exit 1; }

log "Deploying a broken version of $APP"
if "$ROOT/annex" deploy "$WORK/$APP"; then
  echo "demo-bad-deploy: the broken version passed its health check, which it shouldn't" >&2
  exit 1
fi
echo
log "Releases"
out=$("$ROOT/annex" releases "$APP")
head -5 <<<"$out"
echo
echo "The live release is unchanged. In the console: $APP, Releases (the failed one in red), then its logs."
