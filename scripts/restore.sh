#!/usr/bin/env bash
# Restore one app's database and /data from a backup (default: the newest).
# The app must be deployed. Its current data is replaced.
# Usage: scripts/restore.sh <app> [archive-name] [--yes]
source "$(dirname "$0")/lib.sh"
app=${1:?usage: scripts/restore.sh <app> [archive-name] [--yes]}
archive=; yes=
for arg in "${@:2}"; do
  case "$arg" in --yes) yes=1 ;; *) archive=$arg ;; esac
done
if [ -z "$yes" ]; then
  read -r -p "Replace $app's database and files with ${archive:-the newest backup}? Type the app name: " answer
  [ "$answer" = "$app" ] || { echo "Aborted."; exit 1; }
fi
vm "python3 $DATA/platform/vm/annex_vm.py restore $app --actor \"${ANNEX_USER:-$(whoami)@$(hostname -s)}\" ${archive:+--archive $archive}" | python3 -c '
import json, sys
r = json.load(sys.stdin)
if not r.get("ok"): sys.exit("restore failed: " + r.get("error", ""))
print(f"restored {r["app"]} from {r["archive"]}: database={r["database"]}, files={r["files"]}")'
