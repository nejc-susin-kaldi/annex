#!/usr/bin/env bash
# Host and backup monitoring (D56): disk, memory and the newest backup's age each give admins exactly one
# alert and one "resolved", and nothing to app owners. Thresholds are shortened through ops's tuning file
# (state/ops/tuning.json, removed at the end). Runs scripts/backup.sh once. About 4 minutes.
source "$(dirname "$0")/../scripts/lib.sh"
source "$(dirname "$0")/lib.sh"
use_tunnel
SAM=sam@annex.example
ADMIN=admin@annex.example
TUNING=$DATA/state/ops/tuning.json
on_exit "vm 'rm -f $TUNING' >/dev/null 2>&1"
tune() { vm "printf '%s' '$1' > $TUNING && chmod 644 $TUNING"; ops_state_seen; }
ctl() { vm "docker exec annex-ops-1 python -m annex_ops.ctl $* --json"; }
login kaldi-admin >/dev/null
mail_clear

pair() { # pair DESC ALERT_PREFIX RESOLVED_PREFIX ACTION_TO_RESOLVE...
  local desc=$1 alert=$2 resolved=$3; shift 3
  if mail_wait $ADMIN "$alert" 1 120; then ok "$desc: admins are alerted"; else bad "$desc: admins are alerted" "no '$alert'"; fi
  "$@"
  if mail_wait $ADMIN "$resolved" 1 120; then ok "$desc: and told it's resolved"; else bad "$desc: resolved" "no '$resolved'"; fi
  sleep 15
  expect "$desc: exactly one alert" 1 "$(mail_count $ADMIN "$alert")"
  expect "$desc: exactly one 'resolved'" 1 "$(mail_count $ADMIN "$resolved")"
  expect "$desc: nothing to an app owner" 0 "$(mail_count $SAM "$alert")"
}
BASE='"host_sample_s": 5, "tick_s": 5'

echo "What ops sees"
h=$(ctl host)
field() { python3 -c 'import json,sys; print(json.loads(sys.argv[1])[sys.argv[2]])' "$h" "$1"; }
# the filesystem ops measures (the one under the data dir: the VM's root, or the Mac's disk for the local stack), with
# POSIX df; memory as the Docker host has it (the VM, or Docker Desktop's VM), read in another platform container
df_pct=$(vm "df -P $DATA/state/ops | awk 'NR == 2 {print \$5}' | tr -dc 0-9")
diff=$(python3 -c "print(abs(float('$(field disk_used_pct)') - $df_pct) <= 2)")
expect "disk use matches df on $WHERE ($df_pct %)" True "$diff"
avail=$(vm "docker exec annex-appdata-db-1 awk '/MemAvailable/ {print int(\$2/1024)}' /proc/meminfo")
diff=$(python3 -c "print(abs(float('$(field mem_available_mb)') - $avail) <= 200)")
expect "available memory matches the Docker host's (about $avail MB)" True "$diff"
age=$(field backup_age_h)
python3 -c "import sys; sys.exit(0 if 0 <= float('$age') < 26 else 1)" && ok "the newest backup is $age h old" || bad "backup age" "$age"

echo "Disk"
tune "{$BASE, \"disk_alert_pct\": 1, \"disk_clear_pct\": 0.5}"
pair "disk" "[Annex] platform: disk is nearly full" "[Annex] resolved: platform disk" tune "{$BASE}"
has "the alert says what to check" "docker system df" "$(mail_find $ADMIN "[Annex] platform: disk")"

echo "Memory"
tune "{$BASE, \"mem_alert_mb\": 1000000, \"mem_alert_s\": 0}"
pair "memory" "[Annex] platform: memory is running low" "[Annex] resolved: platform memory" tune "{$BASE}"

echo "Backup"
tune "{$BASE, \"backup_max_age_s\": 60}"
pair "backup" "[Annex] platform: backup is overdue" "[Annex] resolved: platform backup" "$ROOT/scripts/backup.sh"
has "the alert says how to run one" "scripts/backup.sh" "$(mail_find $ADMIN "[Annex] platform: backup")"

vm "rm -f $TUNING"
expect "no incident is left open" "" "$(ctl incidents --open | python3 -c 'import json,sys; print(" ".join(i["subject"] for i in json.load(sys.stdin) if i["subject"].startswith("platform:")))')"
finish
