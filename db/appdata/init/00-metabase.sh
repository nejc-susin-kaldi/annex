#!/bin/sh
# Runs once on first start of appdata-db: Metabase's own application database.
set -eu
# the password goes in through the environment, not an argument (D80)
psql -v ON_ERROR_STOP=1 -U postgres <<'SQL'
\getenv mb_pw METABASE_DB_PASSWORD
REVOKE CONNECT ON DATABASE postgres FROM PUBLIC;
REVOKE CONNECT ON DATABASE template1 FROM PUBLIC;
CREATE ROLE metabase LOGIN PASSWORD :'mb_pw';
CREATE DATABASE metabase OWNER metabase;
REVOKE CONNECT ON DATABASE metabase FROM PUBLIC;
GRANT CONNECT ON DATABASE metabase TO metabase;
SQL
