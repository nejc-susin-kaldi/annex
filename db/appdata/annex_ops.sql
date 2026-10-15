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

-- --- M1.3: incidents and alerts ------------------------------------------------------
-- one open incident per subject (app:<a> | job:<a>/<job> | platform:<service>); signals attach to it
CREATE TABLE IF NOT EXISTS annex_ops.incidents (
  id               bigserial PRIMARY KEY,
  subject          text NOT NULL,
  app              text,                       -- null for platform
  opened_at        timestamptz NOT NULL DEFAULT now(),
  resolved_at      timestamptz,
  resolved_quietly boolean NOT NULL DEFAULT false,  -- removed apps: no "resolved" message
  last_notified_at timestamptz,
  reminders        int NOT NULL DEFAULT 0,
  signals          jsonb NOT NULL DEFAULT '{}', -- active: {"down": {"since": ..., "detail": ..., "cid": ...}, ...}
  history          jsonb NOT NULL DEFAULT '[]'  -- every signal raised/cleared, for the console (M2)
);
CREATE UNIQUE INDEX IF NOT EXISTS incidents_one_open ON annex_ops.incidents (subject) WHERE resolved_at IS NULL;
-- scheduled job results, read from Ofelia's log (kept 90 days)
CREATE TABLE IF NOT EXISTS annex_ops.job_runs (
  id bigserial PRIMARY KEY, app text NOT NULL, job text NOT NULL, finished_at timestamptz NOT NULL,
  ok boolean NOT NULL, duration_ms int, error text);
CREATE INDEX IF NOT EXISTS job_runs_app_job ON annex_ops.job_runs (app, job, finished_at DESC);
-- where each followed container's log was read up to (M2.2 reuses this for log shipping)
CREATE TABLE IF NOT EXISTS annex_ops.log_cursors (container text PRIMARY KEY, ts timestamptz NOT NULL);
-- alerts held for a subject until a time (restore and rollback --with-data open one)
CREATE TABLE IF NOT EXISTS annex_ops.maintenance (subject text PRIMARY KEY, until timestamptz NOT NULL);
