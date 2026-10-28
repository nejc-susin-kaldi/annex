-- 003 (B4, D84): connections per database.

-- The console's Platform page shows how many connections each database holds against max_connections. pg_stat_database
-- is readable by every role and carries `numbackends` per database, so no pg_stat_activity (other roles' rows are hidden
-- from non-superusers), no SECURITY DEFINER and no pg_monitor grant: the view exposes counts and database names only,
-- never a query, a role or an address. A new view; nothing existing changes.
CREATE OR REPLACE VIEW annex_ops.db_connections AS
  SELECT datname AS database, numbackends AS connections, current_setting('max_connections')::int AS max_connections
  FROM pg_catalog.pg_stat_database
  WHERE datname IS NOT NULL AND datname NOT LIKE 'template%';
GRANT SELECT ON annex_ops.db_connections TO annex_console;
