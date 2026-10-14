-- Deterministic synthetic seed: 8 advisors, 300 clients, ~600 accounts,
-- 30 instruments, 24 month-end prices each, ~5k transactions.
SELECT setseed(0.42);

INSERT INTO product.advisors VALUES
  (1, 'Petra Zupan',    'petra.zupan@advisors.example',    'Ljubljana'),
  (2, 'Gregor Kos',     'gregor.kos@advisors.example',     'Ljubljana'),
  (3, 'Lea Vidmar',     'lea.vidmar@advisors.example',     'Maribor'),
  (4, 'Matej Golob',    'matej.golob@advisors.example',    'Maribor'),
  (5, 'Tina Kralj',     'tina.kralj@advisors.example',     'Koper'),
  (6, 'Rok Turk',       'rok.turk@advisors.example',       'Kranj'),
  (7, 'Nika Bizjak',    'nika.bizjak@advisors.example',    'Vienna'),
  (8, 'Andrej Hribar',  'andrej.hribar@advisors.example',  'Zagreb');

WITH first_names AS (
  SELECT array['Ana','Luka','Eva','Marko','Nina','Jan','Sara','Tomaz','Maja','Blaz','Zala','Miha',
               'Katja','Jure','Urska','Ales','Tjasa','Gasper','Spela','Klemen','Hana','David','Lara','Ziga'] AS a),
last_names AS (
  SELECT array['Novak','Horvat','Krajnc','Zupancic','Potocnik','Kovac','Mlakar','Kos','Vidmar','Golob',
               'Turk','Bozic','Kralj','Korosec','Zupan','Bizjak','Hribar','Kavcic','Rozman','Petek'] AS a)
INSERT INTO product.clients
SELECT g.i,
       f.a[1 + g.i % 24] || ' ' || l.a[1 + (g.i / 24 * 3 + g.i) % 20],
       CASE WHEN r < 0.50 THEN 'Retail' WHEN r < 0.80 THEN 'Affluent' WHEN r < 0.96 THEN 'Private' ELSE 'Institutional' END,
       (array['Conservative','Balanced','Growth','Aggressive'])[1 + floor(random() * 4)::int],
       (array['Slovenia','Slovenia','Slovenia','Croatia','Austria','Italy'])[1 + floor(random() * 6)::int],
       1 + floor(random() * 8)::int,
       date '2015-01-01' + floor(random() * 3500)::int
-- random() sits in a subquery so it runs once per row, not once per query
FROM (SELECT i, random() AS r FROM generate_series(1, 300) AS i) g, first_names f, last_names l;

INSERT INTO product.accounts (account_id, client_id, account_type, opened_on)
SELECT row_number() OVER (ORDER BY c.client_id, n), c.client_id,
       (array['Brokerage','Managed','Pension','Savings'])[1 + floor(random() * 4)::int],
       c.onboarded_on + floor(random() * 365)::int
FROM product.clients c,
     LATERAL generate_series(1, CASE WHEN c.segment IN ('Private', 'Institutional') THEN 3 ELSE 1 + floor(random() * 2)::int END) AS n;

INSERT INTO product.instruments VALUES
  ( 1, 'ALPX', 'Alpine Industrials',             'Equity',       'EUR'),
  ( 2, 'ADRN', 'Adriatic Shipping',              'Equity',       'EUR'),
  ( 3, 'KRST', 'Karst Energy',                   'Equity',       'EUR'),
  ( 4, 'LJPH', 'Ljubljana Pharma',               'Equity',       'EUR'),
  ( 5, 'DRVA', 'Drava Utilities',                'Equity',       'EUR'),
  ( 6, 'SAVA', 'Sava Retail Group',              'Equity',       'EUR'),
  ( 7, 'TRGL', 'Triglav Tech',                   'Equity',       'EUR'),
  ( 8, 'PIRN', 'Piran Hospitality',              'Equity',       'EUR'),
  ( 9, 'MURA', 'Mura Agro',                      'Equity',       'EUR'),
  (10, 'SOCA', 'Soca Semiconductors',            'Equity',       'EUR'),
  (11, 'EUGV', 'Euro Govt Bond 2031',            'Bond',         'EUR'),
  (12, 'EUG5', 'Euro Govt Bond 2029',            'Bond',         'EUR'),
  (13, 'SICB', 'Slovene Corporate Bond 2030',    'Bond',         'EUR'),
  (14, 'HYEU', 'European High Yield 2028',       'Bond',         'EUR'),
  (15, 'GRNB', 'Green Infrastructure Bond 2033', 'Bond',         'EUR'),
  (16, 'BALF', 'Balanced Growth Fund',           'Fund',         'EUR'),
  (17, 'CONF', 'Conservative Income Fund',       'Fund',         'EUR'),
  (18, 'EMKF', 'Emerging Markets Fund',          'Fund',         'EUR'),
  (19, 'DIVF', 'Dividend Leaders Fund',          'Fund',         'EUR'),
  (20, 'TECF', 'Global Technology Fund',         'Fund',         'EUR'),
  (21, 'WRLD', 'World Equity ETF',               'ETF',          'EUR'),
  (22, 'EURS', 'Europe 600 ETF',                 'ETF',          'EUR'),
  (23, 'USLC', 'US Large Cap ETF',               'ETF',          'EUR'),
  (24, 'AGGB', 'Aggregate Bond ETF',             'ETF',          'EUR'),
  (25, 'GOLD', 'Physical Gold ETC',              'ETF',          'EUR'),
  (26, 'ESGW', 'ESG World ETF',                  'ETF',          'EUR'),
  (27, 'SMCP', 'Europe Small Cap ETF',           'ETF',          'EUR'),
  (28, 'EMMK', 'Euro Money Market',              'Money market', 'EUR'),
  (29, 'LIQF', 'Liquidity Reserve Fund',         'Money market', 'EUR'),
  (30, 'TBIL', 'Short Treasury ETF',             'Money market', 'EUR');

-- random walk of month-end prices, Oct 2024 to Sep 2026
WITH months AS (
  SELECT (date_trunc('month', d) + interval '1 month - 1 day')::date AS price_date, row_number() OVER (ORDER BY d) AS m
  FROM generate_series(date '2024-10-01', date '2026-09-01', interval '1 month') AS d),
steps AS (
  SELECT i.instrument_id, mo.price_date, mo.m,
         CASE i.asset_class WHEN 'Money market' THEN 0.002 WHEN 'Bond' THEN 0.01 ELSE 0.05 END AS vol,
         CASE i.asset_class WHEN 'Money market' THEN 1 WHEN 'Bond' THEN 95 ELSE 20 + i.instrument_id * 7 END AS base,
         random() - 0.47 AS shock
  FROM product.instruments i CROSS JOIN months mo)
INSERT INTO product.prices
SELECT instrument_id, price_date,
       round((base * exp(sum(shock * vol * 2) OVER (PARTITION BY instrument_id ORDER BY m)))::numeric, 4)
FROM steps;

-- buys: 3 to 10 per account on month-end dates
WITH acc AS (
  SELECT a.account_id, c.segment, 3 + floor(random() * 8)::int AS n_buys
  FROM product.accounts a
  JOIN product.clients c USING (client_id)),
buys AS (
  SELECT acc.account_id,
         1 + floor(random() * 30)::int AS instrument_id,
         (date '2024-10-31' + make_interval(months => floor(random() * 23)::int))::date AS d0,
         CASE acc.segment WHEN 'Retail' THEN 20 WHEN 'Affluent' THEN 120 WHEN 'Private' THEN 600 ELSE 3000 END
           * (0.2 + random()) AS budget_k
  FROM acc
  CROSS JOIN LATERAL generate_series(1, acc.n_buys) AS n)
INSERT INTO product.transactions (account_id, instrument_id, trade_date, side, quantity, price, amount)
SELECT b.account_id, b.instrument_id, p.price_date, 'BUY',
       round((b.budget_k * 1000 / 6 / p.close)::numeric, 4), p.close,
       round((b.budget_k * 1000 / 6)::numeric, 2)
FROM buys b
JOIN product.prices p ON p.instrument_id = b.instrument_id
 AND p.price_date = (date_trunc('month', b.d0) + interval '1 month - 1 day')::date;

-- sells: about one in five buys is partly sold a few months later
INSERT INTO product.transactions (account_id, instrument_id, trade_date, side, quantity, price, amount)
SELECT t.account_id, t.instrument_id, p.price_date, 'SELL', q, p.close, round(q * p.close, 2)
FROM product.transactions t
CROSS JOIN LATERAL (SELECT round(t.quantity * (0.3 + random() * 0.4)::numeric, 4) AS q,
                           1 + floor(random() * 6)::int AS later) s
JOIN product.prices p ON p.instrument_id = t.instrument_id
 AND p.price_date = (date_trunc('month', t.trade_date) + make_interval(months => s.later + 1) - interval '1 day')::date
WHERE t.side = 'BUY' AND random() < 0.2;

INSERT INTO product.holdings
SELECT account_id, instrument_id,
       sum(CASE side WHEN 'BUY' THEN quantity ELSE -quantity END),
       round(sum(amount) FILTER (WHERE side = 'BUY') / sum(quantity) FILTER (WHERE side = 'BUY'), 4)
FROM product.transactions
GROUP BY account_id, instrument_id
HAVING sum(CASE side WHEN 'BUY' THEN quantity ELSE -quantity END) > 0;

ANALYZE;
