-- Annex's link to the client's database (D77), run by scripts/democlient.sh link with -v reader_pw=...
-- (the read-only password the client gave us). postgres_fdw imports the client's tables into src_arcade as
-- foreign tables; only the view owner (postgres) has a user mapping, and nobody has grants on src_arcade, so
-- apps and Metabase see the data only through the curated views in annex_api (api_views_arcade.sql).
-- Idempotent, and it never drops src_arcade: that would drop the views and every app's grants on them.
-- In a subfolder of the init dir on purpose: product-db's first start must not run it.
CREATE EXTENSION IF NOT EXISTS postgres_fdw;

SELECT 'CREATE SERVER goat_arcade FOREIGN DATA WRAPPER postgres_fdw OPTIONS ('
       || 'host ''arcade-db'', port ''5432'', dbname ''arcade'', application_name ''annex-gateway'', '
       || 'fetch_size ''10000'', use_remote_estimate ''true''' || ')'
WHERE NOT EXISTS (SELECT FROM pg_foreign_server WHERE srvname = 'goat_arcade') \gexec
REVOKE ALL ON FOREIGN SERVER goat_arcade FROM PUBLIC;

SELECT format('CREATE USER MAPPING FOR postgres SERVER goat_arcade OPTIONS (user %L, password %L)', 'annex_reader', :'reader_pw')
WHERE NOT EXISTS (SELECT FROM pg_user_mappings WHERE srvname = 'goat_arcade' AND usename = 'postgres') \gexec
SELECT format('ALTER USER MAPPING FOR postgres SERVER goat_arcade OPTIONS (SET password %L)', :'reader_pw') \gexec

CREATE SCHEMA IF NOT EXISTS src_arcade;
REVOKE ALL ON SCHEMA src_arcade FROM PUBLIC;
-- import the client's tables that aren't here yet (all of them the first time)
SELECT 'IMPORT FOREIGN SCHEMA arcade '
       || coalesce('EXCEPT (' || string_agg(quote_ident(c.relname), ', ') || ') ', '')
       || 'FROM SERVER goat_arcade INTO src_arcade'
FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE n.nspname = 'src_arcade' AND c.relkind = 'f' \gexec
REVOKE ALL ON ALL TABLES IN SCHEMA src_arcade FROM PUBLIC;
