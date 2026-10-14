#!/bin/sh
# Backup pre-hook, run inside appdata-db by the backup container before each
# archive. Dumps every database (custom format) plus globals into /dumps/current.
set -eu
rm -rf /dumps/new
mkdir -p /dumps/new
for db in $(psql -U postgres -Atc "SELECT datname FROM pg_database WHERE NOT datistemplate AND datname <> 'postgres'"); do
  pg_dump -U postgres -Fc -f "/dumps/new/$db.dump" "$db"
done
pg_dumpall -U postgres --globals-only > /dumps/new/globals.sql
rm -rf /dumps/current
mv /dumps/new /dumps/current
echo "dump-all: $(ls /dumps/current | wc -l) files"
