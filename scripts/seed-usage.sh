#!/usr/bin/env bash
# Optional demo prep (M1.4): 8 weeks of SYNTHETIC usage history, so the "Annex usage" dashboard has lines to show.
# Usage: scripts/seed-usage.sh [--clear]
#
# Writes weeks 3-10 back into usage_weekly and usage_daily with synthetic = true, for the apps Annex knows. It never
# writes `requests` and never touches the last two weeks, so the rollup, the tiles, `annex usage` totals and the
# tests are unaffected (the rollup never overwrites synthetic rows either). --clear deletes every synthetic row.
# Note: a graduation candidate needs the last four complete weeks (1-4 weeks back) to rise; this seeds only the
# older two of those, so the signal still depends on real use in the last two weeks.
source "$(dirname "$0")/lib.sh"

case "${1:-}" in
  --clear)
    vm "docker exec -i annex-appdata-db-1 psql -U postgres -d annex_ops -qAt" <<'SQL'
DELETE FROM annex_ops.usage_daily WHERE synthetic;
DELETE FROM annex_ops.usage_weekly WHERE synthetic;
SQL
    log "Synthetic usage history cleared" ;;
  "")
    vm "docker exec -i annex-appdata-db-1 psql -U postgres -d annex_ops -v ON_ERROR_STOP=1 -qAt" <<'SQL'
-- a gentle rise for the first app (alphabetically), flat for the others; never over a real row
CREATE TEMP TABLE seed AS
SELECT a.app, b.back,
       (date_trunc('week', now() AT TIME ZONE 'Europe/Ljubljana')::date - b.back * 7) AS week,
       CASE WHEN a.n = 1 THEN 1 + (10 - b.back) ELSE 2 + (a.n % 3) END AS users
FROM (SELECT app, row_number() OVER (ORDER BY app) AS n FROM annex_ops.app_seen WHERE removed_at IS NULL) a,
     generate_series(3, 10) AS b(back);
INSERT INTO annex_ops.usage_weekly (week, app, users, requests, synthetic)
SELECT week, app, users, users * 12 + back, true FROM seed ON CONFLICT (week, app) DO NOTHING;
INSERT INTO annex_ops.usage_daily (day, app, users, requests, errors, p95_ms, synthetic)
SELECT s.week + d, s.app, greatest(1, s.users - (d % 3)), (s.users * 12 + s.back) / 7, 0, 80 + s.back * 3, true
FROM seed s, generate_series(0, 6) AS d ON CONFLICT (day, app) DO NOTHING;
SELECT 'seeded ' || count(*) || ' weekly rows for ' || count(DISTINCT app) || ' app(s)' FROM seed;
SQL
    log "Synthetic usage history written (weeks 3-10 back). Undo: scripts/seed-usage.sh --clear" ;;
  *) echo "usage: $0 [--clear]" >&2; exit 2 ;;
esac
