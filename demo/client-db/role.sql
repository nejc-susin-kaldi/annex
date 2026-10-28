-- The client's settings for the login it gave Annex, annex_reader (D106). Applied by vm.sh on every `up`
-- (idempotent), so a running database gets changes too: initdb/ runs only on the first start.
--
-- Connections: every product-db session that reads a Goat Arcade view keeps one postgres_fdw connection here
-- for as long as it lives. Annex's gateway holds a pool of 6 (PGRST_DB_POOL in compose.yaml) and Metabase up to
-- 15, so the limit covers both and a few psql shells (arcade-db's max_connections is 30). An idle session is
-- closed after 5 minutes; postgres_fdw reconnects at its next transaction (PostgreSQL 14+), so nothing breaks
-- and the slots are free for Metabase's bursts.
ALTER ROLE annex_reader CONNECTION LIMIT 25;
ALTER ROLE annex_reader SET idle_session_timeout = '5min';
ALTER ROLE annex_reader SET statement_timeout = '30s';
ALTER ROLE annex_reader SET default_transaction_read_only = on;
