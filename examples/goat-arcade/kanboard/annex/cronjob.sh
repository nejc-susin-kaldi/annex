#!/bin/bash
# Kanboard's daily housekeeping (`cli cronjob`: recurring tasks, daily statistics), with the start and end lines the
# Annex logging convention asks of a job. Exits with Kanboard's exit code, so a failure alerts the owner.
set -uo pipefail
log() { printf '{"ts":"%s","level":"%s","msg":"%s"%s}\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$1" "$2" "${3:+,$3}"; }
start=$(date +%s)
log info "cronjob started"
php /var/www/app/cli cronjob
status=$?
if [ "$status" -eq 0 ]; then
  log info "cronjob finished" "\"s\":$(( $(date +%s) - start ))"
else
  log error "cronjob failed" "\"exit_code\":${status}"
fi
exit "$status"
