-- The annex_ops database, its roles and their passwords (D93: the schema is in migrations/, applied by vm/migrate.py).
-- Idempotent, applied by scripts/push.sh on every push, as postgres in the `postgres` database:
--   psql -d postgres -f -
-- with APPDATA_DB_OPS_PASSWORD, APPDATA_DB_METABASE_USAGE_PASSWORD, APPDATA_DB_HOOKS_PASSWORD and
-- APPDATA_DB_CONSOLE_PASSWORD in psql's environment (read by \getenv below; D80: no password on a command line).
-- This is not a migration: CREATE DATABASE can't run in a transaction, and a password change must reach the roles.
-- It lives here and not in init/, because init/ only runs on a fresh database.
\getenv ops_pw APPDATA_DB_OPS_PASSWORD
\getenv usage_pw APPDATA_DB_METABASE_USAGE_PASSWORD
\getenv hooks_pw APPDATA_DB_HOOKS_PASSWORD
\getenv console_pw APPDATA_DB_CONSOLE_PASSWORD
-- template1 is where every new database starts: nobody but superusers needs to connect to it (D80). init/ does the
-- same on a fresh database; this line covers one that already exists.
REVOKE CONNECT ON DATABASE template1 FROM PUBLIC;
DO $$ BEGIN
  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'annex_ops') THEN CREATE ROLE annex_ops LOGIN; END IF;
END $$;
ALTER ROLE annex_ops WITH LOGIN PASSWORD :'ops_pw';
SELECT 'CREATE DATABASE annex_ops OWNER annex_ops' WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname = 'annex_ops')
\gexec
REVOKE CONNECT ON DATABASE annex_ops FROM PUBLIC;
GRANT CONNECT ON DATABASE annex_ops TO annex_ops;
-- Metabase's read-only usage role (M1.4; retired in M2.9: the console shows usage and nothing connects as this role any more.
-- Kept until a human has removed Metabase's "Annex usage" connection and dropped it: docs/setup.md, step 8)
DO $$ BEGIN
  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'metabase_usage') THEN CREATE ROLE metabase_usage LOGIN; END IF;
END $$;
ALTER ROLE metabase_usage WITH LOGIN PASSWORD :'usage_pw';
GRANT CONNECT ON DATABASE annex_ops TO metabase_usage;
-- The webhook receiver's role (its own container, D55): it may only add events; its grants are at the end
DO $$ BEGIN
  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'annex_hooks') THEN CREATE ROLE annex_hooks LOGIN; END IF;
END $$;
ALTER ROLE annex_hooks WITH LOGIN PASSWORD :'hooks_pw';
GRANT CONNECT ON DATABASE annex_ops TO annex_hooks;

-- The console's role (M2.4, its own container): read-only, and only what its pages show (grants at the end of this file)
DO $$ BEGIN
  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'annex_console') THEN CREATE ROLE annex_console LOGIN; END IF;
END $$;
ALTER ROLE annex_console WITH LOGIN PASSWORD :'console_pw';
GRANT CONNECT ON DATABASE annex_ops TO annex_console;
