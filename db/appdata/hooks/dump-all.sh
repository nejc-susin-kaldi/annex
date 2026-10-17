#!/bin/sh
# Backup pre-hook, run inside appdata-db by the backup container before each
# archive. Dumps every database (custom format) plus globals into /dumps/current.
set -eu
rm -rf /dumps/new
mkdir -p /dumps/new
for db in $(psql -U postgres -Atc "SELECT datname FROM pg_database WHERE NOT datistemplate AND datname <> 'postgres'"); do
  # annex-ops's stored logs (14 days, every day's partition) and health-check results (7 days) are left out:
  # short-lived by design, the bulk of the database, and a backup must not keep log lines past their retention.
  # Their tables are still created on restore, empty (D74).
  skip=""
  [ "$db" = annex_ops ] && skip="--exclude-table-data=annex_ops.logs* --exclude-table-data=annex_ops.health_checks"
  # shellcheck disable=SC2086
  pg_dump -U postgres -Fc $skip -f "/dumps/new/$db.dump" "$db"
done
pg_dumpall -U postgres --globals-only > /dumps/new/globals.sql
rm -rf /dumps/current
mv /dumps/new /dumps/current
echo "dump-all: $(ls /dumps/current | wc -l) files"
