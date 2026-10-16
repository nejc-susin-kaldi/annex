-- The annex-ops service's database (apps' email/Slack grants and the send log).
-- Idempotent: scripts/push.sh applies it on every push, as postgres, with
--   psql -v ops_pw=<APPDATA_DB_OPS_PASSWORD> -v usage_pw=<APPDATA_DB_METABASE_USAGE_PASSWORD>
--        -v hooks_pw=<APPDATA_DB_HOOKS_PASSWORD> -v console_pw=<APPDATA_DB_CONSOLE_PASSWORD> -d postgres -f -
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
-- Metabase's read-only usage role (M1.4): what it may read is granted at the end of this file
DO $$ BEGIN
  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'metabase_usage') THEN CREATE ROLE metabase_usage LOGIN; END IF;
END $$;
ALTER ROLE metabase_usage WITH LOGIN PASSWORD :'usage_pw';
GRANT CONNECT ON DATABASE annex_ops TO metabase_usage;
-- The webhook receiver's role (its own container, D55): it may only add events; its grants are at the end
DO $$ BEGIN
  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'annex_hooks') THEN CREATE ROLE annex_hooks LOGIN; END IF;
END $$;
ALTER ROLE annex_hooks WITH LOGIN PASSWORD :'hooks_pw';
GRANT CONNECT ON DATABASE annex_ops TO annex_hooks;

-- The console's role (M2.4, its own container): read-only, and only what its pages show (grants at the end of this file)
DO $$ BEGIN
  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'annex_console') THEN CREATE ROLE annex_console LOGIN; END IF;
END $$;
ALTER ROLE annex_console WITH LOGIN PASSWORD :'console_pw';
GRANT CONNECT ON DATABASE annex_ops TO annex_console;

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

-- --- M1.4: usage stats ---------------------------------------------------------------
-- One row per signed-in page request on an app's router (no health checks or static files), kept 30 days.
-- Never a query string. Logins are here and nowhere else; only `annex usage <app> --by-user` shows them.
CREATE TABLE IF NOT EXISTS annex_ops.requests (
  ts          timestamptz NOT NULL,
  app         text NOT NULL,
  login       text NOT NULL,
  method      text NOT NULL,
  path        text NOT NULL,          -- no query string, at most 200 characters
  status      int NOT NULL,
  duration_ms int NOT NULL
);
CREATE INDEX IF NOT EXISTS requests_app_ts ON annex_ops.requests (app, ts);
CREATE INDEX IF NOT EXISTS requests_ts ON annex_ops.requests (ts);
-- a restart replays the log lines after the saved cursor: identical rows are dropped (INSERT ... ON CONFLICT DO NOTHING)
CREATE UNIQUE INDEX IF NOT EXISTS requests_dedupe ON annex_ops.requests (app, ts, login, method, path);
-- summaries without logins, kept indefinitely (days in Europe/Ljubljana, ISO weeks starting Monday);
-- `synthetic` rows come from scripts/seed-usage.sh and are never overwritten by the rollup
CREATE TABLE IF NOT EXISTS annex_ops.usage_daily (
  day date NOT NULL, app text NOT NULL, users int NOT NULL, requests int NOT NULL,
  errors int NOT NULL, p95_ms int, synthetic boolean NOT NULL DEFAULT false, PRIMARY KEY (day, app));
CREATE TABLE IF NOT EXISTS annex_ops.usage_weekly (
  week date NOT NULL, app text NOT NULL, users int NOT NULL, requests int NOT NULL,
  synthetic boolean NOT NULL DEFAULT false, PRIMARY KEY (week, app));
-- when Annex first saw an app, and when it was last used
CREATE TABLE IF NOT EXISTS annex_ops.app_seen (
  app text PRIMARY KEY, first_seen timestamptz NOT NULL, last_used timestamptz);
-- set by the rollup when the app's container is gone (its history stays; the view hides it until it returns)
ALTER TABLE annex_ops.app_seen ADD COLUMN IF NOT EXISTS removed_at timestamptz;
-- the weekly digest, once per week even across restarts
CREATE TABLE IF NOT EXISTS annex_ops.digests (week date PRIMARY KEY, sent_at timestamptz NOT NULL);
-- the rollup's verdict per app (the rules are Python, in ops/annex_ops/usage.py): graduation or dormant
CREATE TABLE IF NOT EXISTS annex_ops.app_signals (
  app text PRIMARY KEY, signal text NOT NULL CHECK (signal IN ('graduation', 'dormant')), since timestamptz NOT NULL DEFAULT now());

-- One row per app: rolling 7/30-day distinct users from the raw rows, the rest from the summaries.
-- The view runs with its owner's rights (no security_invoker), so Metabase can read it without any access
-- to `requests`.
DROP VIEW IF EXISTS annex_ops.usage_apps;
CREATE VIEW annex_ops.usage_apps AS
WITH cw AS (SELECT date_trunc('week', now() AT TIME ZONE 'Europe/Ljubljana')::date AS week),
r7 AS (SELECT app, count(DISTINCT login) AS users, percentile_cont(0.95) WITHIN GROUP (ORDER BY duration_ms)::int AS p95
         FROM annex_ops.requests WHERE ts > now() - interval '7 days' GROUP BY app),
rprev AS (SELECT app, count(DISTINCT login) AS users FROM annex_ops.requests
            WHERE ts > now() - interval '14 days' AND ts <= now() - interval '7 days' GROUP BY app),
r30 AS (SELECT app, count(DISTINCT login) AS users FROM annex_ops.requests WHERE ts > now() - interval '30 days' GROUP BY app),
d7 AS (SELECT app, sum(requests)::int AS requests, sum(errors)::int AS errors FROM annex_ops.usage_daily
         WHERE day > (now() AT TIME ZONE 'Europe/Ljubljana')::date - 7 GROUP BY app),
wk AS (SELECT w.app,
         max(w.users) FILTER (WHERE w.week = cw.week - 28) AS week1, max(w.users) FILTER (WHERE w.week = cw.week - 21) AS week2,
         max(w.users) FILTER (WHERE w.week = cw.week - 14) AS week3, max(w.users) FILTER (WHERE w.week = cw.week - 7) AS week4
         FROM annex_ops.usage_weekly w, cw WHERE w.week >= cw.week - 28 GROUP BY w.app)
SELECT s.app, s.first_seen, s.last_used,
       coalesce(r7.users, 0) AS users_7d, coalesce(rprev.users, 0) AS users_prev_7d, coalesce(r30.users, 0) AS users_30d,
       coalesce(d7.requests, 0) AS requests_7d, coalesce(d7.errors, 0) AS errors_7d,
       CASE WHEN coalesce(d7.requests, 0) = 0 THEN 0 ELSE round(d7.errors::numeric / d7.requests, 4) END AS error_rate_7d,
       r7.p95 AS p95_7d, wk.week1, wk.week2, wk.week3, wk.week4, g.signal
FROM annex_ops.app_seen s
LEFT JOIN r7 USING (app) LEFT JOIN rprev USING (app) LEFT JOIN r30 USING (app) LEFT JOIN d7 USING (app)
LEFT JOIN wk USING (app) LEFT JOIN annex_ops.app_signals g USING (app)
WHERE s.removed_at IS NULL;

-- Metabase's usage connection: summaries and the view, and nothing else (not requests, sends, incidents or grants)
REVOKE ALL ON ALL TABLES IN SCHEMA annex_ops FROM metabase_usage;
GRANT USAGE ON SCHEMA annex_ops TO metabase_usage;
GRANT SELECT ON annex_ops.usage_daily, annex_ops.usage_weekly, annex_ops.usage_apps TO metabase_usage;

-- --- M1.5: incoming webhooks ----------------------------------------------------------------
-- Every call to https://hooks.<domain>/h/<app>/<hook> that passed (or failed) its signature check. Bodies are kept
-- for 7 days for retries and replay (so they are in the nightly dump for that long); rejected requests have none.
-- Signing secrets are never stored here: they stay in /srv/annex/secrets/hooks/<app>.json.
CREATE TABLE IF NOT EXISTS annex_ops.hook_events (
  id              bigserial PRIMARY KEY,
  app             text NOT NULL,
  hook            text NOT NULL,
  received_at     timestamptz NOT NULL DEFAULT now(),
  status          text NOT NULL CHECK (status IN ('pending', 'delivered', 'failed', 'rejected')),
  content_type    text,
  headers         jsonb NOT NULL DEFAULT '{}',   -- the allowlisted provider headers only
  body            bytea,                         -- null for rejected requests
  attempts        int NOT NULL DEFAULT 0,
  next_attempt_at timestamptz,
  delivered_at    timestamptz,
  last_error      text,
  replay_of       bigint                         -- set on events created by `annex hooks replay`
);
CREATE INDEX IF NOT EXISTS hook_events_due ON annex_ops.hook_events (next_attempt_at) WHERE status = 'pending';
CREATE INDEX IF NOT EXISTS hook_events_app ON annex_ops.hook_events (app, received_at DESC);

-- The webhook receiver (annex_hooks) may insert events and read back the new id (RETURNING id), nothing else:
-- no bodies, no other tables. Delivery, retries and `annex hooks` run in annex-ops as annex_ops.
REVOKE ALL ON ALL TABLES IN SCHEMA annex_ops FROM annex_hooks;
GRANT USAGE ON SCHEMA annex_ops TO annex_hooks;
GRANT INSERT ON annex_ops.hook_events TO annex_hooks;
GRANT SELECT (id) ON annex_ops.hook_events TO annex_hooks;
GRANT USAGE ON SEQUENCE annex_ops.hook_events_id_seq TO annex_hooks;

-- --- M2.2: logs that outlive containers ---------------------------------------------------------
-- Every shipped container's lines (apps, annex-ops, the receiver, Authelia's sign-in lines, the gateway's
-- request lines, scheduled jobs' output), kept log_retention_days (14) in day partitions that annex-ops makes
-- ahead of time and drops whole (never DELETEs). Secret-looking fields are removed before a line gets here.
-- ts is Docker's own timestamp (a line can't choose its own day), `ns` the sub-microsecond remainder, so lines
-- written within the same microsecond (a traceback) keep their order.
CREATE EXTENSION IF NOT EXISTS pg_trgm SCHEMA public;   -- trusted, so the database owner may; its index makes --grep fast
CREATE TABLE IF NOT EXISTS annex_ops.logs (
  ts         timestamptz NOT NULL,
  ns         smallint NOT NULL DEFAULT 0,
  app        text,                 -- null for platform containers
  container  text NOT NULL,        -- annex-app-client-lookup, annex-ops-1, ... or 'gateway' for the gateway's request lines
  release    int,                  -- from the container's com.kaldi.annex.release label
  stream     text NOT NULL,        -- stdout | stderr
  level      text,                 -- debug | info | warn | error; for lines that aren't JSON, guessed (error) or null
  request_id text,
  msg        text NOT NULL,        -- the msg field, or the whole line; at most 8 KB
  fields     jsonb,                -- the other JSON fields, minus anything secret; null = the line wasn't structured
  h          uuid NOT NULL         -- md5 of container, stream, nanosecond timestamp and line: a replay can't duplicate
) PARTITION BY RANGE (ts);
CREATE UNIQUE INDEX IF NOT EXISTS logs_dedupe ON annex_ops.logs (ts, h);
CREATE INDEX IF NOT EXISTS logs_app_ts ON annex_ops.logs (app, ts DESC);
CREATE INDEX IF NOT EXISTS logs_app_release ON annex_ops.logs (app, release, ts DESC);
CREATE INDEX IF NOT EXISTS logs_request ON annex_ops.logs (request_id) WHERE request_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS logs_search ON annex_ops.logs USING gin ((msg || ' ' || coalesce(fields::text, '')) gin_trgm_ops);

-- every release of every app: annex_vm.py release-record writes a row wherever the CLI appends to releases.jsonl
-- (which stays the file of record); release-backfill loads the existing files, idempotently
CREATE TABLE IF NOT EXISTS annex_ops.releases (
  app         text NOT NULL,
  release     int NOT NULL,
  image       text NOT NULL,
  result      text NOT NULL,       -- live | failed
  deployed_at timestamptz NOT NULL,
  deployed_by text,
  via         text,                -- cli | mcp
  git         jsonb,               -- {sha, dirty}
  note        text,                -- why it failed, or "rollback from r5 to r3 [with data]"
  rollback_of int,                 -- the release a rollback switched away from
  manifest    jsonb NOT NULL,
  PRIMARY KEY (app, deployed_at, release)   -- an app removed and deployed again starts at r1 again: its history stays
);

-- --- M2.3: error groups ---------------------------------------------------------------------------------
-- annex-ops reads the stored logs (above) for errors: structured error lines, and Python tracebacks an app printed.
-- One event per error, grouped by a fingerprint (ops/annex_ops/errors_group.py). An event is stored once (its key is the
-- hash of the line that started it), so reading a window twice counts nothing twice. Events are kept 14 days, groups 30
-- days after they were last seen. `annex remove` deletes an app's groups and events (a redeployed name starts clean).
CREATE TABLE IF NOT EXISTS annex_ops.error_events (
  h           uuid PRIMARY KEY,
  app         text NOT NULL,
  fingerprint text NOT NULL,
  ts          timestamptz NOT NULL,
  release     int,
  request_id  text,
  login       text
);
CREATE INDEX IF NOT EXISTS error_events_app_ts ON annex_ops.error_events (app, ts DESC);
CREATE TABLE IF NOT EXISTS annex_ops.error_groups (
  app                 text NOT NULL,
  fingerprint         text NOT NULL,
  kind                text NOT NULL,           -- server | browser | job
  title               text NOT NULL,           -- the normalised summary
  first_seen          timestamptz NOT NULL,
  last_seen           timestamptz NOT NULL,
  count               int NOT NULL DEFAULT 1,
  first_release       int,
  last_release        int,
  sample_msg          text,
  sample_request_id   text,
  last_user           text,
  alert_due           boolean NOT NULL DEFAULT false,   -- an email is owed (sent by the next pass, within the hourly limit)
  alert_again         boolean NOT NULL DEFAULT false,   -- ... and it is "back", not new
  alerted_at          timestamptz,
  PRIMARY KEY (app, fingerprint)
);
-- every error email, for the hourly limit (and, later, the audit trail)
CREATE TABLE IF NOT EXISTS annex_ops.error_alerts (
  ts timestamptz NOT NULL DEFAULT now(), app text NOT NULL, groups int NOT NULL, summary boolean NOT NULL DEFAULT false);
CREATE INDEX IF NOT EXISTS error_alerts_app_ts ON annex_ops.error_alerts (app, ts DESC);

-- --- M2.4: the console's read-only role ------------------------------------------------------------------
-- annex_console runs in its own container (compose `console`) on a network shared only with appdata-db. It may SELECT
-- what its pages show and nothing else: no INSERT/UPDATE/DELETE anywhere, no `app_grants` (token hashes), no
-- `log_cursors`, no webhook bodies or headers (column grants on hook_events), no email addresses (`sends` is not
-- granted until the audit page, and then without bodies there are none). Later steps add their grants below.
REVOKE ALL ON ALL TABLES IN SCHEMA annex_ops FROM annex_console;
GRANT USAGE ON SCHEMA annex_ops TO annex_console;
GRANT SELECT ON annex_ops.logs, annex_ops.releases, annex_ops.error_groups, annex_ops.error_events,
  annex_ops.incidents, annex_ops.job_runs, annex_ops.requests, annex_ops.usage_daily, annex_ops.usage_weekly,
  annex_ops.usage_apps, annex_ops.app_seen, annex_ops.app_signals TO annex_console;
GRANT SELECT (id, app, hook, received_at, status, content_type, attempts, next_attempt_at, delivered_at, last_error, replay_of)
  ON annex_ops.hook_events TO annex_console;
