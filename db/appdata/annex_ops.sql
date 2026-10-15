-- The annex-ops service's database (apps' email/Slack grants and the send log).
-- Idempotent: scripts/push.sh applies it on every push, as postgres, with
--   psql -v ops_pw=<APPDATA_DB_OPS_PASSWORD> -d postgres -f -
-- It lives here and not in init/, because init/ only runs on a fresh database.
-- Later milestones append their tables to this file.
DO $$ BEGIN
  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'annex_ops') THEN CREATE ROLE annex_ops LOGIN; END IF;
END $$;
ALTER ROLE annex_ops WITH LOGIN PASSWORD :'ops_pw';
SELECT 'CREATE DATABASE annex_ops OWNER annex_ops' WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname = 'annex_ops')
\gexec
REVOKE CONNECT ON DATABASE annex_ops FROM PUBLIC;
GRANT CONNECT ON DATABASE annex_ops TO annex_ops;

\c annex_ops
SET ROLE annex_ops;

CREATE SCHEMA IF NOT EXISTS annex_ops;

-- what each app's token may do; written by annex_vm.py provision
CREATE TABLE IF NOT EXISTS annex_ops.app_grants (
  app            text PRIMARY KEY,
  token_sha256   text NOT NULL UNIQUE,
  email          boolean NOT NULL DEFAULT false,
  slack_channels text[] NOT NULL DEFAULT '{}',
  updated_at     timestamptz NOT NULL DEFAULT now()
);

-- every send attempt; never the body (M2's audit view reads this)
CREATE TABLE IF NOT EXISTS annex_ops.sends (
  id         bigserial PRIMARY KEY,
  ts         timestamptz NOT NULL DEFAULT now(),
  app        text NOT NULL,              -- '_platform' for platform alerts (M1.3)
  channel    text NOT NULL CHECK (channel IN ('email', 'slack')),
  recipients text[] NOT NULL,            -- resolved addresses, or the Slack channel
  subject    text,                       -- email only
  status     text NOT NULL CHECK (status IN ('sent', 'rejected', 'failed', 'unavailable')),
  error      text
);
CREATE INDEX IF NOT EXISTS sends_app_ts ON annex_ops.sends (app, ts DESC);
