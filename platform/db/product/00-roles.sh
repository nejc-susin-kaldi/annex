#!/bin/sh
# Runs once on first start of product-db. Passwords come from product-db.env.
set -eu
psql -v ON_ERROR_STOP=1 -U postgres -d product \
  -v auth_pw="$AUTHENTICATOR_PASSWORD" -v mb_pw="$METABASE_RO_PASSWORD" <<'SQL'
-- PostgREST connects as authenticator and switches to the app's role from the JWT
CREATE ROLE authenticator LOGIN NOINHERIT NOCREATEDB NOCREATEROLE PASSWORD :'auth_pw';
-- Metabase reads annex_api only
CREATE ROLE metabase_ro LOGIN NOCREATEDB NOCREATEROLE PASSWORD :'mb_pw';
REVOKE CONNECT ON DATABASE product FROM PUBLIC;
GRANT CONNECT ON DATABASE product TO authenticator, metabase_ro;
REVOKE ALL ON SCHEMA public FROM PUBLIC;
SQL
