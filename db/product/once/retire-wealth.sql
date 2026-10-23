-- Retire the wealth-management demo data (D112). For an EXISTING stack only (the VM, or a local stack made before
-- GA.6): a fresh product-db no longer creates any of it. Run by a human (Nejc), once per stack, AFTER the wealth
-- apps are removed (`annex remove client-lookup`, `annex remove monthly-report`): their gateway roles hold grants
-- on these views, and this script refuses while any role other than metabase_ro still holds one.
--
--   VM:    ssh annex 'docker exec -i annex-product-db-1 psql -X -U postgres -d product' < db/product/once/retire-wealth.sql
--   local: docker --context desktop-linux exec -i annex-product-db-1 psql -X -U postgres -d product < db/product/once/retire-wealth.sql
--
-- It drops the four wealth views in annex_api, then the schema product with its seven tables, and nothing else: not
-- annex_api itself or Goat Arcade's views in it, not src_arcade, not the roles, not any app's grants on other views.
-- No CASCADE: if anything else depends on these objects, it stops and says so. One transaction; idempotent (a second
-- run changes nothing). In a subfolder of product-db's init directory on purpose: a first start must not run it.
\set ON_ERROR_STOP on
BEGIN;

DO $$
DECLARE holders text;
BEGIN
  SELECT string_agg(DISTINCT pg_get_userbyid(a.grantee) || ' (' || c.relname || ')', ', ') INTO holders
  FROM pg_class c
  JOIN pg_namespace n ON n.oid = c.relnamespace
  CROSS JOIN LATERAL aclexplode(c.relacl) a
  WHERE n.nspname = 'annex_api'
    AND c.relname IN ('client_summary', 'top_clients_by_aum', 'monthly_positions', 'advisor_book')
    AND a.grantee <> 0  -- PUBLIC
    AND pg_get_userbyid(a.grantee) NOT IN ('postgres', 'metabase_ro');
  IF holders IS NOT NULL THEN
    RAISE EXCEPTION 'retire-wealth: these roles still read the wealth views: %. Remove those apps first (annex remove <app>), or deploy them again without the views, then run this again. Nothing was dropped.', holders;
  END IF;
END
$$;

-- top_clients_by_aum and advisor_book read client_summary, so they go first
DROP VIEW IF EXISTS annex_api.top_clients_by_aum;
DROP VIEW IF EXISTS annex_api.advisor_book;
DROP VIEW IF EXISTS annex_api.monthly_positions;
DROP VIEW IF EXISTS annex_api.client_summary;

-- the base tables in one statement (their foreign keys point at each other), then the schema, which is empty by now
DROP TABLE IF EXISTS product.holdings, product.transactions, product.prices, product.instruments,
                     product.accounts, product.clients, product.advisors;
DROP SCHEMA IF EXISTS product;

COMMIT;

-- PostgREST forgets the dropped views
NOTIFY pgrst, 'reload schema';

-- what is left in annex_api: Goat Arcade's views only
SELECT count(*) AS annex_api_views, string_agg(table_name, ', ' ORDER BY table_name) AS views
FROM information_schema.views WHERE table_schema = 'annex_api';
