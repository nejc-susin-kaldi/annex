# Connecting a client's database to the Annex gateway

This is what a client (their DBA, or the DBA's coding agent) does so that Annex can read their database, and what Annex does on its side. Goat Arcade is the worked example: `demo/client-db/initdb/00-roles.sh` and `03-grants.sql` are the client's part, and `platform/db/product/clientlink/` is Annex's part. In production the client's side points at a **read replica**, not the primary.

## What the client does
1. **Create one read-only login for Annex.** It can read the schemas Annex needs and nothing else:
   ```sql
   CREATE ROLE annex_reader LOGIN NOINHERIT NOCREATEDB NOCREATEROLE CONNECTION LIMIT 10 PASSWORD '<generated>';
   ALTER ROLE annex_reader SET default_transaction_read_only = on;
   ALTER ROLE annex_reader SET statement_timeout = '30s';
   GRANT CONNECT ON DATABASE <db> TO annex_reader;
   GRANT USAGE ON SCHEMA <schema> TO annex_reader;
   GRANT SELECT ON ALL TABLES IN SCHEMA <schema> TO annex_reader;  -- or only the tables Annex needs
   ALTER DEFAULT PRIVILEGES IN SCHEMA <schema> GRANT SELECT ON TABLES TO annex_reader;  -- optional: tables added later
   ```
   Tables with very sensitive data can be left out entirely, or column grants (`GRANT SELECT (col, ...)`) can limit what Annex can read at all.
2. **Open one network path:** Postgres (5432) from the Annex server to the replica, and nothing else. On AWS that's a security group rule, or a VPC peering or PrivateLink between the two accounts. Use TLS (`sslmode=require` on Annex's side).
3. **Hand over** the host, port, database name and the password, through a secrets channel, never by email or chat. Add a short description of the tables: what they hold, which columns are personal data, and how statuses are spelled.

## What Annex does
1. **Stores the password** on the Annex server only (`secrets/`), never in a repo.
2. **Links the database** with `postgres_fdw` from Annex's gateway database (`link.sql`):
   - a foreign server, with a user mapping for the views' owner only;
   - the client's tables imported into a private schema (`src_<client>`), where nobody has grants.
3. **Writes curated views** in `annex_api` (`api_views_<client>.sql`). Each view:
   - renames and cleans up columns;
   - leaves out personal data;
   - pre-aggregates the big tables;
   - carries a comment on the view and on every column.

   The views are the allowlist. Apps request them by name in `annex.yaml`. Each app gets its own database role with `SELECT` on exactly those views, and its own token. Every gateway call is logged with the app's name.
4. **Tests it:**
   - every view answers quickly (filters on time go to the client as parameters);
   - apps can't reach the raw tables or the client's database;
   - no personal columns are in the views;
   - the client's login can't write.

## What the client gets
- One read-only login to monitor and revoke. Revoking it, or closing the network path, cuts Annex off at once.
- A reviewed list of exactly what Annex exposes: the views file and its data dictionary.
- A log of which app read which view, when.
- No Annex objects in their database: no schemas, functions or triggers.
