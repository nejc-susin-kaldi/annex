#!/bin/sh
# Runs once on first start of appdata-db: Metabase's own application database.
set -eu
psql -v ON_ERROR_STOP=1 -U postgres -v mb_pw="$METABASE_DB_PASSWORD" <<'SQL'
REVOKE CONNECT ON DATABASE postgres FROM PUBLIC;
CREATE ROLE metabase LOGIN PASSWORD :'mb_pw';
CREATE DATABASE metabase OWNER metabase;
REVOKE CONNECT ON DATABASE metabase FROM PUBLIC;
GRANT CONNECT ON DATABASE metabase TO metabase;
SQL
