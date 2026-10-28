-- D116: the role metabase_usage is gone. It was Metabase's read-only connection to the usage summaries (M1.4);
-- usage moved to the console (D76), the connection was removed in Metabase, and bootstrap.sql no longer creates it.
-- Dropping a role needs the superuser, so this file steps out of annex_ops for its one statement and back in
-- (vm/migrate.py runs every migration as annex_ops; the version row is still written as annex_ops).
-- On a fresh instance the role never existed and this does nothing. 001_base.sql's three grants to it are gone
-- (the one edit to an applied migration, D116): a new instance must not grant to a role that doesn't exist.
RESET ROLE;
DO $$ BEGIN
  IF EXISTS (SELECT FROM pg_roles WHERE rolname = 'metabase_usage') THEN
    EXECUTE 'DROP OWNED BY metabase_usage';  -- its grants here and CONNECT on this database
    EXECUTE 'DROP ROLE metabase_usage';
  END IF;
END $$;
SET ROLE annex_ops;
