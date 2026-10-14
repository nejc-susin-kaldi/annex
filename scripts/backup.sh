#!/usr/bin/env bash
# Run the nightly backup now: dump every app database, archive dumps + app files
# into /srv/annex/backups, apply the 7-day retention. Prints the archive name.
source "$(dirname "$0")/lib.sh"
vm "python3 $DATA/platform/vm/annex_vm.py backup-now" | python3 -c '
import json, sys
r = json.load(sys.stdin)
if not r.get("ok"): sys.exit("backup failed: " + r.get("error", ""))
print(f"{r["archive"]} ({r["size_bytes"] // 1024} KiB); archives kept: {len(r["archives"])}")'
