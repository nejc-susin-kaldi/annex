-- 004 (B5, D90): the alert outbox and the engine state.

-- One row per message the alert engine owes (an incident opened, reminded or resolved). The engine thread adds it; the
-- sender thread (ops/annex_ops/sender.py) composes it once into `message` (title, body, recipients, and per delivery
-- (the email, each Slack channel) whether it went out), sends it and retries what failed: 3 attempts, then `failed`.
-- A delivered part is marked the moment it is sent, so a restart never sends it twice. Kept 30 days after it finished.
CREATE TABLE IF NOT EXISTS annex_ops.outbox (
  id              bigserial PRIMARY KEY,
  created_at      timestamptz NOT NULL DEFAULT now(),
  kind            text NOT NULL CHECK (kind IN ('open', 'remind', 'resolve')),
  subject         text NOT NULL,                 -- app:<a> | job:<a>/<job> | platform:<service>
  action          jsonb NOT NULL,                -- what the engine said, in wall time: signals, opened_at, duration_s
  message         jsonb,                         -- composed by the sender at its first attempt (has addresses)
  status          text NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'sent', 'failed')),
  attempts        int NOT NULL DEFAULT 0,
  next_attempt_at timestamptz,                   -- for people: the sender keeps its own (monotonic) retry times
  finished_at     timestamptz,
  last_error      text
);
CREATE INDEX IF NOT EXISTS outbox_pending ON annex_ops.outbox (id) WHERE status = 'pending';
CREATE INDEX IF NOT EXISTS outbox_created ON annex_ops.outbox (created_at DESC);
-- The engine's counters and timers that Docker can't give back after an ops restart: per container (`ctr:<id>`) when it
-- stopped, its last exit and its recent crashes; `host:mem_low_since`. Written when they change, in wall time; a row
-- disappears when the container is forgotten. Everything else the engine knows is rebuilt at start (D90 lists it).
CREATE TABLE IF NOT EXISTS annex_ops.engine_state (
  key        text PRIMARY KEY,
  value      jsonb NOT NULL,
  updated_at timestamptz NOT NULL DEFAULT now()
);
-- the console's Platform page lists the messages that weren't delivered: no `message` or `action` (addresses, log lines)
GRANT SELECT (id, created_at, kind, subject, status, attempts, next_attempt_at, finished_at, last_error)
  ON annex_ops.outbox TO annex_console;
