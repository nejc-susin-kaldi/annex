-- 002 (B3, D82): the usage rollup table and the logs container index.

-- B3: the view's result, stored by the 10-minute rollup, so a page or a status call reads one row per app instead of
-- aggregating the raw requests (which D76's console does on every load). Same columns as `usage_apps`; `signal` is
-- refreshed by the rollup after the verdicts are written. The view stays: it is the rollup's source and the fallback for apps it has not stored yet (D93).
CREATE TABLE IF NOT EXISTS annex_ops.usage_rollup_apps (
  app text PRIMARY KEY, first_seen timestamptz NOT NULL, last_used timestamptz,
  users_7d bigint NOT NULL, users_prev_7d bigint NOT NULL, users_30d bigint NOT NULL,
  requests_7d int NOT NULL, errors_7d int NOT NULL, error_rate_7d numeric NOT NULL, p95_7d int,
  week1 int, week2 int, week3 int, week4 int, signal text, refreshed_at timestamptz NOT NULL DEFAULT now());
-- B3: the gateway scan and the per-container readers (a parent index: existing partitions get it now, new ones are born with it)
CREATE INDEX IF NOT EXISTS logs_container_ts ON annex_ops.logs (container, ts DESC);
GRANT SELECT ON annex_ops.usage_rollup_apps TO annex_console;
