-- The planted stories of the Goat Arcade seed (initdb/sql/seed.sql): one row each, ok must be true.
-- Run on arcade-db with scripts/democlient.sh check. The anchor is the day after the last game.
SET search_path = arcade;
CREATE TEMP TABLE a AS SELECT (max(started_at AT TIME ZONE 'Europe/Ljubljana'))::date + 1 AS d FROM games;
CREATE TEMP VIEW g AS SELECT *, (started_at AT TIME ZONE 'Europe/Ljubljana') AS local_at, (SELECT d FROM a) - (started_at AT TIME ZONE 'Europe/Ljubljana')::date AS ago FROM games;

WITH m AS (SELECT id FROM machines WHERE model_id = 22 AND venue_id = 3),
ev AS (SELECT (SELECT d FROM a) - (occurred_at AT TIME ZONE 'Europe/Ljubljana')::date AS ago FROM machine_events WHERE event_type = 'coil_fault' AND machine_id IN (SELECT id FROM m)),
x AS (SELECT (SELECT count(*) FROM ev WHERE ago BETWEEN 11 AND 24) AS recent,
             (SELECT count(*) FROM ev WHERE ago BETWEEN 43 AND 56) AS early,
             (SELECT count(*) FROM g WHERE machine_id IN (SELECT id FROM m) AND ago <= 7) AS games_7d,
             (SELECT count(*) FROM g WHERE machine_id IN (SELECT id FROM m) AND ago BETWEEN 50 AND 56) AS games_wk_before,
             (SELECT count(*) FROM service_tickets WHERE machine_id IN (SELECT id FROM m) AND closed_at IS NULL AND machine_down) AS open_down)
SELECT '1 dying machine (Haunted Lighthouse, Maribor)' AS story,
       recent >= 10 AND recent > 3 * early AND games_7d = 0 AND games_wk_before > 20 AND open_down = 1 AS ok,
       format('coil faults %s vs %s six weeks ago; games last 7 days %s (a week, 7 weeks ago: %s); open ticket, down: %s',
              recent, early, games_7d, games_wk_before, open_down) AS detail FROM x
UNION ALL
SELECT '2 the champion (goatmother)', count(*) >= 10 AND count(DISTINCT venue_id) >= 3,
       format('top score on %s models at %s venues', count(*), count(DISTINCT venue_id))
FROM (SELECT DISTINCT ON (m.model_id) m.model_id, gg.player_id, gg.venue_id
      FROM games gg JOIN machines m ON m.id = gg.machine_id JOIN players p ON p.id = gg.player_id AND p.deleted_at IS NULL
      WHERE gg.score IS NOT NULL ORDER BY m.model_id, gg.score DESC) top
WHERE player_id = 1
UNION ALL
SELECT '3 Celje lost its stars', recent < 0.8 * before,
       format('weekly revenue %s EUR in the last 4 weeks vs %s EUR in weeks 8-20 before', round(recent), round(before))
FROM (SELECT avg(amt) FILTER (WHERE wk >= (SELECT d FROM a) - 28) AS recent,
             avg(amt) FILTER (WHERE wk BETWEEN (SELECT d FROM a) - 140 AND (SELECT d FROM a) - 56) AS before
      FROM (SELECT week_start AS wk, sum(amount_eur) AS amt FROM coin_collections WHERE venue_id = 4 GROUP BY 1) w) x
UNION ALL
SELECT '4 rhythm (weekends, league night, dead Mondays)', sat > 2 * tue AND thu_lj2 > 1.5 * wed_lj2 AND mon_early = 0,
       format('games per Saturday %s vs Tuesday %s; Bezigrad Thursday %s vs Wednesday %s; Monday games before 18:00: %s',
              round(sat), round(tue), round(thu_lj2), round(wed_lj2), mon_early)
FROM (SELECT count(*) FILTER (WHERE extract(isodow FROM local_at) = 6) / 26.0 AS sat,
             count(*) FILTER (WHERE extract(isodow FROM local_at) = 2) / 26.0 AS tue,
             count(*) FILTER (WHERE venue_id = 2 AND extract(isodow FROM local_at) = 4) / 26.0 AS thu_lj2,
             count(*) FILTER (WHERE venue_id = 2 AND extract(isodow FROM local_at) = 3) / 26.0 AS wed_lj2,
             count(*) FILTER (WHERE extract(isodow FROM local_at) = 1 AND extract(hour FROM local_at) < 18) AS mon_early
      FROM g) x
UNION ALL
SELECT '5 era taste', max(era) FILTER (WHERE rev_rank = 1) = 'LCD' AND max(era) FILTER (WHERE dur_rank = 1) = 'EM',
       string_agg(format('%s: %s EUR/game, %s s', era, round(rev, 2), round(dur)), '; ' ORDER BY era)
FROM (SELECT era, rev, dur, rank() OVER (ORDER BY rev DESC) AS rev_rank, rank() OVER (ORDER BY dur DESC) AS dur_rank
      FROM (SELECT mm.era, avg(gg.credits_used * m.price_per_play_cents) / 100.0 AS rev, avg(gg.duration_s) AS dur
            FROM games gg JOIN machines m ON m.id = gg.machine_id JOIN machine_models mm ON mm.id = m.model_id
            WHERE mm.machine_type = 'pinball' GROUP BY 1) e) x
UNION ALL
SELECT '6 tilt-happy cabinet (Cyber Samurai, Graz)', tilty > 3 * others,
       format('tilt rate %s%% vs %s%% on the other Cyber Samurai', round(100 * tilty, 1), round(100 * others, 1))
FROM (SELECT avg(tilted::int) FILTER (WHERE m.venue_id = 7) AS tilty, avg(tilted::int) FILTER (WHERE m.venue_id <> 7) AS others
      FROM games gg JOIN machines m ON m.id = gg.machine_id WHERE m.model_id = 12) x
UNION ALL
SELECT '7 upcoming tournament', count(*) = 1 AND min(entries) > 0 AND max(matches) = 0,
       string_agg(format('%s on %s: %s entries, %s match rows', name, starts_on, entries, matches), '; ')
FROM (SELECT t.name, t.starts_on, (SELECT count(*) FROM tournament_entries e WHERE e.tournament_id = t.id) AS entries,
             (SELECT count(*) FROM tournament_matches mt WHERE mt.tournament_id = t.id) AS matches
      FROM tournaments t WHERE t.status = 'registration' AND t.starts_on > (SELECT d FROM a)) x;
