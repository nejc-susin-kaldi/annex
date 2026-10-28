#!/bin/sh
# Runs once on first start of arcade-db (the client's database). Passwords come from secrets/democlient.env.
# annex_reader is the one login the client gives Annex: read-only, on schema arcade only (grants: 03-grants.sql).
set -eu
psql -v ON_ERROR_STOP=1 -U postgres -d arcade -v reader_pw="$ANNEX_READER_PASSWORD" <<'SQL'
CREATE ROLE annex_reader LOGIN NOINHERIT NOCREATEDB NOCREATEROLE PASSWORD :'reader_pw';
-- its limits and settings: ../role.sql, applied by vm.sh on every start (D106)
REVOKE CONNECT ON DATABASE arcade FROM PUBLIC;
GRANT CONNECT ON DATABASE arcade TO annex_reader;
REVOKE ALL ON SCHEMA public FROM PUBLIC;
SQL
