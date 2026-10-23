-- The gateway allowlist's shared part: the schema apps and Metabase read, Metabase's grants on it and the reload
-- for PostgREST. The views themselves are the client's: Goat Arcade's are in clientlink/api_views_arcade.sql
-- (D77, D112), and adding one is a reviewed change. A fresh product-db starts with this schema empty: the wealth
-- demo data is gone (D112). Idempotent: applied on first start and again by scripts/push.sh on every push.
CREATE SCHEMA IF NOT EXISTS annex_api;

-- Metabase gets every curated view, nothing else (api_views_arcade.sql grants its views again after creating them).
GRANT USAGE ON SCHEMA annex_api TO metabase_ro;
GRANT SELECT ON ALL TABLES IN SCHEMA annex_api TO metabase_ro;

-- PostgREST picks up new or changed views
NOTIFY pgrst, 'reload schema';
