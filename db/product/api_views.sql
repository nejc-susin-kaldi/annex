-- The data gateway allowlist. Apps and Metabase only ever see views in
-- annex_api; adding a view here is a reviewed change. Idempotent: applied on
-- first start and again by scripts/push.sh on every push.
CREATE SCHEMA IF NOT EXISTS annex_api;

CREATE OR REPLACE VIEW annex_api.client_summary AS
WITH latest AS (
  SELECT DISTINCT ON (instrument_id) instrument_id, close
  FROM product.prices ORDER BY instrument_id, price_date DESC)
SELECT c.client_id,
       c.full_name     AS client_name,
       c.segment,
       c.risk_profile,
       c.country,
       ad.full_name    AS advisor_name,
       count(DISTINCT a.account_id)                     AS account_count,
       coalesce(round(sum(h.quantity * l.close), 2), 0) AS aum_eur,
       c.onboarded_on
FROM product.clients c
JOIN product.advisors ad USING (advisor_id)
LEFT JOIN product.accounts a USING (client_id)
LEFT JOIN product.holdings h USING (account_id)
LEFT JOIN latest l USING (instrument_id)
GROUP BY c.client_id, ad.full_name;

CREATE OR REPLACE VIEW annex_api.top_clients_by_aum AS
SELECT rank() OVER (ORDER BY aum_eur DESC) AS rank,
       client_id, client_name, segment, advisor_name, aum_eur
FROM annex_api.client_summary
ORDER BY aum_eur DESC
LIMIT 25;

CREATE OR REPLACE VIEW annex_api.monthly_positions AS
WITH months AS (SELECT DISTINCT price_date AS month_end FROM product.prices),
pos AS (
  SELECT m.month_end, t.account_id, t.instrument_id,
         sum(CASE t.side WHEN 'BUY' THEN t.quantity ELSE -t.quantity END) AS quantity
  FROM months m
  JOIN product.transactions t ON t.trade_date <= m.month_end
  GROUP BY m.month_end, t.account_id, t.instrument_id)
SELECT pos.month_end,
       c.client_id,
       c.full_name       AS client_name,
       pos.account_id,
       i.symbol,
       i.name            AS instrument_name,
       i.asset_class,
       pos.quantity,
       p.close           AS price,
       round(pos.quantity * p.close, 2) AS market_value_eur
FROM pos
JOIN product.accounts a USING (account_id)
JOIN product.clients c USING (client_id)
JOIN product.instruments i USING (instrument_id)
JOIN product.prices p ON p.instrument_id = pos.instrument_id AND p.price_date = pos.month_end
WHERE pos.quantity > 0;

CREATE OR REPLACE VIEW annex_api.advisor_book AS
SELECT advisor_name,
       count(*)              AS clients,
       sum(aum_eur)          AS aum_eur,
       round(avg(aum_eur), 2) AS avg_client_aum_eur
FROM annex_api.client_summary
GROUP BY advisor_name;

-- Metabase gets every curated view, nothing else.
GRANT USAGE ON SCHEMA annex_api TO metabase_ro;
GRANT SELECT ON ALL TABLES IN SCHEMA annex_api TO metabase_ro;

-- PostgREST picks up new or changed views
NOTIFY pgrst, 'reload schema';
