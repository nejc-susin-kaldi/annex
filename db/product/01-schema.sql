-- Synthetic "client product": a small wealth-management back office.
-- Nothing here is real data. Base tables live in schema product and are never
-- granted to anyone; consumers see only the curated views in annex_api.
CREATE SCHEMA product;

CREATE TABLE product.advisors (
  advisor_id   int PRIMARY KEY,
  full_name    text NOT NULL,
  email        text NOT NULL,
  office       text NOT NULL
);

CREATE TABLE product.clients (
  client_id    int PRIMARY KEY,
  full_name    text NOT NULL,
  segment      text NOT NULL CHECK (segment IN ('Retail', 'Affluent', 'Private', 'Institutional')),
  risk_profile text NOT NULL CHECK (risk_profile IN ('Conservative', 'Balanced', 'Growth', 'Aggressive')),
  country      text NOT NULL,
  advisor_id   int NOT NULL REFERENCES product.advisors,
  onboarded_on date NOT NULL
);

CREATE TABLE product.accounts (
  account_id   int PRIMARY KEY,
  client_id    int NOT NULL REFERENCES product.clients,
  account_type text NOT NULL CHECK (account_type IN ('Brokerage', 'Managed', 'Pension', 'Savings')),
  currency     char(3) NOT NULL DEFAULT 'EUR',
  opened_on    date NOT NULL
);

CREATE TABLE product.instruments (
  instrument_id int PRIMARY KEY,
  symbol        text NOT NULL UNIQUE,
  name          text NOT NULL,
  asset_class   text NOT NULL CHECK (asset_class IN ('Equity', 'Bond', 'Fund', 'ETF', 'Money market')),
  currency      char(3) NOT NULL DEFAULT 'EUR'
);

-- month-end closing prices
CREATE TABLE product.prices (
  instrument_id int NOT NULL REFERENCES product.instruments,
  price_date    date NOT NULL,
  close         numeric(14, 4) NOT NULL,
  PRIMARY KEY (instrument_id, price_date)
);

CREATE TABLE product.transactions (
  transaction_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  account_id     int NOT NULL REFERENCES product.accounts,
  instrument_id  int NOT NULL REFERENCES product.instruments,
  trade_date     date NOT NULL,
  side           text NOT NULL CHECK (side IN ('BUY', 'SELL')),
  quantity       numeric(14, 4) NOT NULL CHECK (quantity > 0),
  price          numeric(14, 4) NOT NULL,
  amount         numeric(16, 2) NOT NULL
);
CREATE INDEX ON product.transactions (account_id, instrument_id, trade_date);

-- current positions, derived from transactions by the seed
CREATE TABLE product.holdings (
  account_id    int NOT NULL REFERENCES product.accounts,
  instrument_id int NOT NULL REFERENCES product.instruments,
  quantity      numeric(14, 4) NOT NULL,
  avg_cost      numeric(14, 4) NOT NULL,
  PRIMARY KEY (account_id, instrument_id)
);
