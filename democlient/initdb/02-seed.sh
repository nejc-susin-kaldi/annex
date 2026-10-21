#!/bin/sh
# Runs once on first start of arcade-db. The data ends the day before the anchor (default: today), so a reseed
# makes it current; the shape and every planted story are the same for any anchor (sql/seed.sql).
set -eu
anchor=${ARCADE_ANCHOR:-$(date +%F)}
echo "arcade-db: seeding with anchor $anchor"
psql -v ON_ERROR_STOP=1 -U postgres -d arcade -v anchor="$anchor" -f /docker-entrypoint-initdb.d/sql/seed.sql
