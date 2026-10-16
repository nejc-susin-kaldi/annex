#!/usr/bin/env bash
# Demo beat (docs/demo.md, 7:00): deploy a version of client-lookup whose health
# check fails, and show that Annex rolls it back by itself. The running version
# stays live; the failed release is recorded and its image removed.
# Needs client-lookup deployed (scripts/reset-demo.sh does that).
source "$(dirname "$0")/lib.sh"
WORK=$ROOT/.build/demo-bad-deploy
rm -rf "$WORK" && mkdir -p "$WORK"
cp -R "$ROOT/examples/client-lookup" "$WORK/client-lookup"
sed -i '' 's/    return {"ok": True}/    raise RuntimeError("the new version cannot reach its database")/' "$WORK/client-lookup/main.py"
grep -q "cannot reach its database" "$WORK/client-lookup/main.py" || { echo "demo-bad-deploy: could not break the copy" >&2; exit 1; }

log "Deploying a broken version of client-lookup"
if "$ROOT/annex" deploy "$WORK/client-lookup"; then
  echo "demo-bad-deploy: the broken version passed its health check, which it shouldn't" >&2
  exit 1
fi
echo
log "Releases"
"$ROOT/annex" releases client-lookup | head -4
rm -rf "$WORK"
