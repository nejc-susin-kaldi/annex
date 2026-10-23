-- The gateway allowlist for Goat Arcade, the demo client's own database (D77, D78; annex_api.sql holds the schema). Rules:
-- apps and Metabase see only these views, adding one is a reviewed change, and every view and column has a
-- comment (PostgREST puts them in its OpenAPI description, which is the data dictionary builders' agents read;
-- scripts/democlient.sh dictionary writes them to docs/goat-arcade/data-dictionary.md).
--
-- The views read src_arcade, the client's tables as postgres_fdw foreign tables (clientlink/link.sql). They
-- run as their owner (postgres, the only user mapping), so security_invoker must stay off. They leave out
-- personal data: players' and technicians' names, emails, phones, birth dates and pay; erased players.
-- Time filters use (SELECT now() - ...) so postgres_fdw sends them to the client's database as parameters.
-- Applied by scripts/democlient.sh link, and by scripts/push.sh once the link exists. Idempotent.
SET search_path = annex_api;

CREATE OR REPLACE VIEW annex_api.venues AS
SELECT v.code AS venue_code, v.name AS venue_name, v.city, v.country, v.lat, v.lon, v.opened_on,
       v.hours AS opening_hours,
       coalesce(f.machines, 0) AS machines, coalesce(f.pinball_machines, 0) AS pinball_machines
FROM src_arcade.venues v
LEFT JOIN (SELECT m.venue_id, count(*) AS machines, count(*) FILTER (WHERE mm.machine_type = 'pinball') AS pinball_machines
           FROM src_arcade.machines m JOIN src_arcade.machine_models mm ON mm.id = m.model_id
           WHERE NOT m.is_deleted AND m.venue_id IS NOT NULL GROUP BY m.venue_id) f ON f.venue_id = v.id
WHERE v.closed_on IS NULL;

CREATE OR REPLACE VIEW annex_api.machine_models AS
SELECT mm.id AS model_id, mm.title, mf.name AS manufacturer, mf.country AS manufacturer_country, mm.release_year,
       mm.era,
       CASE mm.era WHEN 'EM' THEN 'electro-mechanical' WHEN 'SS' THEN 'solid state' WHEN 'DMD' THEN 'dot-matrix display'
                   WHEN 'LCD' THEN 'LCD screen' END AS era_name,
       mm.machine_type, mm.theme, mm.designer, mm.flippers, mm.max_multiball,
       (mm.features ->> 'ramps')::int AS ramps, (mm.features ->> 'modes')::int AS modes,
       ARRAY(SELECT jsonb_array_elements_text(coalesce(mm.features -> 'toys', mm.features -> 'controls'))) AS toys,
       coalesce(c.cabinets, 0) AS cabinets_on_floor
FROM src_arcade.machine_models mm
JOIN src_arcade.manufacturers mf ON mf.id = mm.manufacturer_id
LEFT JOIN (SELECT model_id, count(*) AS cabinets FROM src_arcade.machines
           WHERE NOT is_deleted AND venue_id IS NOT NULL GROUP BY model_id) c ON c.model_id = mm.id;

CREATE OR REPLACE VIEW annex_api.machine_floor AS
SELECT m.id AS machine_id, m.serial_no, v.code AS venue_code, v.name AS venue_name, v.city,
       mm.id AS model_id, mm.title, mf.name AS manufacturer, mm.release_year, mm.era, mm.machine_type,
       CASE lower(trim(m.condition)) WHEN 'mint' THEN 'excellent' WHEN 'restored' THEN 'good' ELSE lower(trim(m.condition)) END AS condition,
       m.price_per_play_cents / 100.0 AS price_per_play_eur, m.installed_at AS installed_on,
       coalesce(p.games_7d, 0) AS games_7d, coalesce(p.games_30d, 0) AS games_30d, p.last_played_at,
       coalesce(t.open_tickets, 0) > 0 AS open_ticket, coalesce(t.down, false) AS out_of_order, t.last_service_at
FROM src_arcade.machines m
JOIN src_arcade.venues v ON v.id = m.venue_id
JOIN src_arcade.machine_models mm ON mm.id = m.model_id
JOIN src_arcade.manufacturers mf ON mf.id = mm.manufacturer_id
LEFT JOIN (SELECT machine_id, count(*) FILTER (WHERE started_at >= (SELECT now() - interval '7 days')) AS games_7d,
                  count(*) AS games_30d, max(started_at) AS last_played_at
           FROM src_arcade.games WHERE started_at >= (SELECT now() - interval '30 days') GROUP BY machine_id) p ON p.machine_id = m.id
LEFT JOIN (SELECT machine_id, count(*) FILTER (WHERE closed_at IS NULL) AS open_tickets,
                  bool_or(machine_down AND closed_at IS NULL) AS down, max(opened_at) AS last_service_at
           FROM src_arcade.service_tickets GROUP BY machine_id) t ON t.machine_id = m.id
WHERE NOT m.is_deleted;

CREATE OR REPLACE VIEW annex_api.leaderboard AS
WITH best AS (  -- each member's best game on each model
  SELECT DISTINCT ON (m.model_id, g.player_id) m.model_id, g.player_id, g.score, g.started_at, g.venue_id, g.machine_id
  FROM src_arcade.games g JOIN src_arcade.machines m ON m.id = g.machine_id
  WHERE g.player_id IS NOT NULL AND g.score IS NOT NULL
  ORDER BY m.model_id, g.player_id, g.score DESC, g.started_at),
ranked AS (SELECT *, rank() OVER (PARTITION BY model_id ORDER BY score DESC) AS rank FROM best)
SELECT r.model_id, mm.title, r.rank, p.handle, p.nickname, r.score, v.code AS venue_code, r.started_at AS played_at, r.machine_id
FROM ranked r
JOIN src_arcade.players p ON p.id = r.player_id AND p.deleted_at IS NULL
JOIN src_arcade.machine_models mm ON mm.id = r.model_id
JOIN src_arcade.venues v ON v.id = r.venue_id
WHERE r.rank <= 10 AND mm.machine_type IN ('pinball', 'video');

CREATE OR REPLACE VIEW annex_api.player_profiles AS
SELECT p.id AS player_id, p.handle, p.nickname, v.code AS home_venue_code, p.member_since, p.rating,
       coalesce(g.games, 0) AS games_180d, coalesce(g.venues, 0) AS venues_played, g.last_played_at,
       coalesce(l.spots, 0) AS leaderboard_spots, l.best_rank
FROM src_arcade.players p
LEFT JOIN src_arcade.venues v ON v.id = p.home_venue_id
LEFT JOIN (SELECT player_id, count(*) AS games, count(DISTINCT venue_id) AS venues, max(started_at) AS last_played_at
           FROM src_arcade.games WHERE player_id IS NOT NULL GROUP BY player_id) g ON g.player_id = p.id
LEFT JOIN (SELECT nickname, count(*) AS spots, min(rank) AS best_rank FROM annex_api.leaderboard GROUP BY nickname) l ON l.nickname = p.nickname
WHERE p.deleted_at IS NULL;

CREATE OR REPLACE VIEW annex_api.daily_plays AS
SELECT d.day, v.code AS venue_code, d.machine_id, mm.title, mm.era, mm.machine_type,
       d.games, d.credits, round(d.credits * m.price_per_play_cents / 100.0, 2) AS revenue_eur,
       d.avg_duration_s, d.tilts, d.members
FROM (SELECT (started_at AT TIME ZONE 'Europe/Ljubljana')::date AS day, venue_id, machine_id, count(*) AS games,
             sum(credits_used) AS credits, round(avg(duration_s)) AS avg_duration_s, count(*) FILTER (WHERE tilted) AS tilts,
             count(DISTINCT player_id) AS members
      FROM src_arcade.games GROUP BY 1, 2, 3) d
JOIN src_arcade.venues v ON v.id = d.venue_id
JOIN src_arcade.machines m ON m.id = d.machine_id
JOIN src_arcade.machine_models mm ON mm.id = m.model_id;

CREATE OR REPLACE VIEW annex_api.hourly_plays AS
SELECT h.day, extract(isodow FROM h.day)::int AS weekday, h.hour, v.code AS venue_code, h.games, h.credits
FROM (SELECT (started_at AT TIME ZONE 'Europe/Ljubljana')::date AS day,
             extract(hour FROM started_at AT TIME ZONE 'Europe/Ljubljana')::int AS hour, venue_id,
             count(*) AS games, sum(credits_used) AS credits
      FROM src_arcade.games GROUP BY 1, 2, 3) h
JOIN src_arcade.venues v ON v.id = h.venue_id;

CREATE OR REPLACE VIEW annex_api.machine_events_daily AS
SELECT e.day, e.machine_id, mm.title, v.code AS current_venue_code, e.event_type, e.events
FROM (SELECT (occurred_at AT TIME ZONE 'Europe/Ljubljana')::date AS day, machine_id, event_type, count(*) AS events
      FROM src_arcade.machine_events GROUP BY 1, 2, 3) e
JOIN src_arcade.machines m ON m.id = e.machine_id
JOIN src_arcade.machine_models mm ON mm.id = m.model_id
LEFT JOIN src_arcade.venues v ON v.id = m.venue_id;

CREATE OR REPLACE VIEW annex_api.service_log AS
SELECT t.id AS ticket_id, t.machine_id, m.serial_no, mm.title, v.code AS current_venue_code,
       t.opened_at, t.closed_at,
       CASE lower(trim(t.status)) WHEN 'done' THEN 'closed' WHEN 'in progress' THEN 'in_progress' ELSE lower(trim(t.status)) END AS status,
       t.symptom, t.resolution, t.reported_by, t.machine_down AS took_machine_off_floor,
       CASE WHEN t.machine_down THEN round((extract(epoch FROM coalesce(t.closed_at, now()) - t.opened_at) / 3600)::numeric, 1) END AS downtime_hours,
       tech.full_name AS technician, t.labour_hours,
       coalesce(pa.parts_cost_eur, 0) AS parts_cost_eur, pa.parts
FROM src_arcade.service_tickets t
JOIN src_arcade.machines m ON m.id = t.machine_id
JOIN src_arcade.machine_models mm ON mm.id = m.model_id
LEFT JOIN src_arcade.venues v ON v.id = m.venue_id
LEFT JOIN src_arcade.technicians tech ON tech.id = t.technician_id
LEFT JOIN (SELECT tp.ticket_id, sum(tp.qty * p.unit_cost_eur) AS parts_cost_eur,
                  string_agg(p.name || CASE WHEN tp.qty > 1 THEN ' x' || tp.qty ELSE '' END, ', ' ORDER BY p.name) AS parts
           FROM src_arcade.ticket_parts tp JOIN src_arcade.parts p ON p.id = tp.part_id GROUP BY tp.ticket_id) pa ON pa.ticket_id = t.id;

CREATE OR REPLACE VIEW annex_api.revenue_weekly AS
SELECT c.week_start, v.code AS venue_code, v.name AS venue_name, v.city,
       sum(c.amount_eur) AS amount_eur, sum(c.coins_eur) AS coins_eur, sum(c.tokens) AS tokens, sum(c.card_plays) AS card_plays,
       count(DISTINCT c.machine_id) AS machines
FROM src_arcade.coin_collections c
JOIN src_arcade.venues v ON v.id = c.venue_id
GROUP BY c.week_start, v.code, v.name, v.city;

CREATE OR REPLACE VIEW annex_api.tournaments AS
SELECT t.id AS tournament_id, t.name, v.code AS venue_code, v.name AS venue_name, t.starts_on, t.format, t.status,
       t.entry_fee_eur, t.max_players, coalesce(e.entries, 0) AS entries, w.handle AS winner_handle, w.nickname AS winner_nickname
FROM src_arcade.tournaments t
JOIN src_arcade.venues v ON v.id = t.venue_id
LEFT JOIN (SELECT tournament_id, count(*) AS entries FROM src_arcade.tournament_entries GROUP BY tournament_id) e ON e.tournament_id = t.id
LEFT JOIN (SELECT DISTINCT ON (x.tournament_id) x.tournament_id, p.handle, p.nickname
           FROM (SELECT tournament_id, player_id, sum(points) AS points, max(score) AS best FROM src_arcade.tournament_matches GROUP BY 1, 2) x
           JOIN src_arcade.players p ON p.id = x.player_id AND p.deleted_at IS NULL
           ORDER BY x.tournament_id, x.points DESC, x.best DESC) w ON w.tournament_id = t.id;

CREATE OR REPLACE VIEW annex_api.tournament_entries AS
SELECT e.tournament_id, p.nickname, p.handle, e.seed, e.entered_at
FROM src_arcade.tournament_entries e
JOIN src_arcade.players p ON p.id = e.player_id AND p.deleted_at IS NULL;

CREATE OR REPLACE VIEW annex_api.tournament_standings AS
SELECT s.tournament_id, t.name AS tournament_name, t.starts_on,
       rank() OVER (PARTITION BY s.tournament_id ORDER BY s.points DESC, s.best_score DESC) AS position,
       p.handle, p.nickname, s.points, s.games_played, s.best_score
FROM (SELECT tournament_id, player_id, sum(points) AS points, count(*) AS games_played, max(score) AS best_score
      FROM src_arcade.tournament_matches GROUP BY 1, 2) s
JOIN src_arcade.tournaments t ON t.id = s.tournament_id
JOIN src_arcade.players p ON p.id = s.player_id AND p.deleted_at IS NULL;

-- --- the data dictionary ------------------------------------------------------------------------------------------
-- Every view comment starts with "Goat Arcade:" (the dictionary script picks the views by it).
COMMENT ON VIEW annex_api.venues IS 'Goat Arcade: the open arcades (venues), with their opening hours and how many machines stand there now.';
COMMENT ON COLUMN annex_api.venues.venue_code IS 'Short venue code, e.g. LJ1. Other views refer to venues by it.';
COMMENT ON COLUMN annex_api.venues.venue_name IS 'Venue name, e.g. Goat Arcade Metelkova.';
COMMENT ON COLUMN annex_api.venues.city IS 'City.';
COMMENT ON COLUMN annex_api.venues.country IS 'Country, ISO 3166 two-letter code (SI, HR, AT, IT).';
COMMENT ON COLUMN annex_api.venues.lat IS 'Latitude (WGS 84), for maps.';
COMMENT ON COLUMN annex_api.venues.lon IS 'Longitude (WGS 84), for maps.';
COMMENT ON COLUMN annex_api.venues.opened_on IS 'The day the venue opened.';
COMMENT ON COLUMN annex_api.venues.opening_hours IS 'JSON object by weekday (mon..sun): ["16:00", "24:00"] or null when closed. A closing time earlier than the opening time is after midnight.';
COMMENT ON COLUMN annex_api.venues.machines IS 'Machines on the floor now (pinball, video and other).';
COMMENT ON COLUMN annex_api.venues.pinball_machines IS 'Pinball machines on the floor now.';

COMMENT ON VIEW annex_api.machine_models IS 'Goat Arcade: the catalogue of machine models (titles): pinball tables by era, plus video cabinets and other games. All titles and makers are fictional.';
COMMENT ON COLUMN annex_api.machine_models.model_id IS 'Model id.';
COMMENT ON COLUMN annex_api.machine_models.title IS 'Title of the game, e.g. Haunted Lighthouse.';
COMMENT ON COLUMN annex_api.machine_models.manufacturer IS 'Manufacturer.';
COMMENT ON COLUMN annex_api.machine_models.manufacturer_country IS 'Manufacturer''s country, ISO two-letter code.';
COMMENT ON COLUMN annex_api.machine_models.release_year IS 'Year the model came out.';
COMMENT ON COLUMN annex_api.machine_models.era IS 'Pinball era: EM (electro-mechanical, to 1977), SS (solid state, 1978-1990), DMD (dot-matrix display, 1991-2012), LCD (2013 on). NULL for video and other machines.';
COMMENT ON COLUMN annex_api.machine_models.era_name IS 'The era spelled out.';
COMMENT ON COLUMN annex_api.machine_models.machine_type IS 'pinball, video (arcade video cabinet) or other (air hockey, claw, roll-a-ball).';
COMMENT ON COLUMN annex_api.machine_models.theme IS 'What the game is about.';
COMMENT ON COLUMN annex_api.machine_models.designer IS 'Designer (fictional).';
COMMENT ON COLUMN annex_api.machine_models.flippers IS 'Number of flippers (pinball only).';
COMMENT ON COLUMN annex_api.machine_models.max_multiball IS 'Most balls in play at once (pinball only).';
COMMENT ON COLUMN annex_api.machine_models.ramps IS 'Number of ramps (pinball only).';
COMMENT ON COLUMN annex_api.machine_models.modes IS 'Number of game modes in the rules (pinball only).';
COMMENT ON COLUMN annex_api.machine_models.toys IS 'Toys and mechanisms on the playfield; for video and other machines, the controls.';
COMMENT ON COLUMN annex_api.machine_models.cabinets_on_floor IS 'How many machines of this model stand in venues now.';

COMMENT ON VIEW annex_api.machine_floor IS 'Goat Arcade: every machine on a venue floor now, with its recent play and service state. One row per physical machine (cabinet).';
COMMENT ON COLUMN annex_api.machine_floor.machine_id IS 'Machine id (one physical cabinet). Other views refer to machines by it.';
COMMENT ON COLUMN annex_api.machine_floor.serial_no IS 'Manufacturer serial number.';
COMMENT ON COLUMN annex_api.machine_floor.venue_code IS 'Where it stands (venues.venue_code).';
COMMENT ON COLUMN annex_api.machine_floor.venue_name IS 'Venue name.';
COMMENT ON COLUMN annex_api.machine_floor.city IS 'Venue city.';
COMMENT ON COLUMN annex_api.machine_floor.model_id IS 'Model (machine_models.model_id).';
COMMENT ON COLUMN annex_api.machine_floor.title IS 'Title of the game.';
COMMENT ON COLUMN annex_api.machine_floor.manufacturer IS 'Manufacturer.';
COMMENT ON COLUMN annex_api.machine_floor.release_year IS 'Year the model came out.';
COMMENT ON COLUMN annex_api.machine_floor.era IS 'Pinball era (EM, SS, DMD, LCD); NULL for video and other.';
COMMENT ON COLUMN annex_api.machine_floor.machine_type IS 'pinball, video or other.';
COMMENT ON COLUMN annex_api.machine_floor.condition IS 'Condition as last recorded: excellent, good, fair or needs work.';
COMMENT ON COLUMN annex_api.machine_floor.price_per_play_eur IS 'Price of one credit (one game) in EUR.';
COMMENT ON COLUMN annex_api.machine_floor.installed_on IS 'When the machine was installed (at its first venue).';
COMMENT ON COLUMN annex_api.machine_floor.games_7d IS 'Games played on it in the last 7 days.';
COMMENT ON COLUMN annex_api.machine_floor.games_30d IS 'Games played on it in the last 30 days.';
COMMENT ON COLUMN annex_api.machine_floor.last_played_at IS 'Start of the last game in the last 30 days (NULL if none).';
COMMENT ON COLUMN annex_api.machine_floor.open_ticket IS 'True if a service ticket for it is still open.';
COMMENT ON COLUMN annex_api.machine_floor.out_of_order IS 'True if an open ticket took it off the floor: nobody can play it now.';
COMMENT ON COLUMN annex_api.machine_floor.last_service_at IS 'When its last service ticket was opened.';

COMMENT ON VIEW annex_api.leaderboard IS 'Goat Arcade: the top 10 scores of all time on every pinball and video model, by members (anonymous games have no name). One row per member and model: their best game. Ties share a rank.';
COMMENT ON COLUMN annex_api.leaderboard.model_id IS 'Model (machine_models.model_id).';
COMMENT ON COLUMN annex_api.leaderboard.title IS 'Title of the game.';
COMMENT ON COLUMN annex_api.leaderboard.rank IS 'Place, 1 to 10.';
COMMENT ON COLUMN annex_api.leaderboard.handle IS 'Three-letter initials, as shown on a machine''s high-score table. Not unique.';
COMMENT ON COLUMN annex_api.leaderboard.nickname IS 'The member''s public nickname. Unique.';
COMMENT ON COLUMN annex_api.leaderboard.score IS 'Score. Scales differ a lot by era: compare scores only within one model.';
COMMENT ON COLUMN annex_api.leaderboard.venue_code IS 'Venue where the score was set.';
COMMENT ON COLUMN annex_api.leaderboard.played_at IS 'When the game started.';
COMMENT ON COLUMN annex_api.leaderboard.machine_id IS 'The machine it was played on.';

COMMENT ON VIEW annex_api.player_profiles IS 'Goat Arcade: members (players with a member card), public profile only: no names or contact details.';
COMMENT ON COLUMN annex_api.player_profiles.player_id IS 'Member id.';
COMMENT ON COLUMN annex_api.player_profiles.handle IS 'Three-letter initials. Not unique.';
COMMENT ON COLUMN annex_api.player_profiles.nickname IS 'Public nickname. Unique; leaderboard and tournament views use it.';
COMMENT ON COLUMN annex_api.player_profiles.home_venue_code IS 'The venue where they signed up.';
COMMENT ON COLUMN annex_api.player_profiles.member_since IS 'Sign-up day.';
COMMENT ON COLUMN annex_api.player_profiles.rating IS 'Skill rating, about 800 to 2400 (league play; higher is better).';
COMMENT ON COLUMN annex_api.player_profiles.games_180d IS 'Games played with their card in the data (the last 180 days).';
COMMENT ON COLUMN annex_api.player_profiles.venues_played IS 'How many venues they played at.';
COMMENT ON COLUMN annex_api.player_profiles.last_played_at IS 'Start of their last game.';
COMMENT ON COLUMN annex_api.player_profiles.leaderboard_spots IS 'On how many models they are in the top 10.';
COMMENT ON COLUMN annex_api.player_profiles.best_rank IS 'Their best place on any leaderboard (1 = holds a top score).';

COMMENT ON VIEW annex_api.daily_plays IS 'Goat Arcade: play per machine per day: games, credits, estimated revenue, tilts. One row per day, venue and machine with at least one game. Days are local (Central European) dates.';
COMMENT ON COLUMN annex_api.daily_plays.day IS 'Local date.';
COMMENT ON COLUMN annex_api.daily_plays.venue_code IS 'Venue where the machine stood that day.';
COMMENT ON COLUMN annex_api.daily_plays.machine_id IS 'Machine (machine_floor.machine_id).';
COMMENT ON COLUMN annex_api.daily_plays.title IS 'Title of the game.';
COMMENT ON COLUMN annex_api.daily_plays.era IS 'Pinball era; NULL for video and other.';
COMMENT ON COLUMN annex_api.daily_plays.machine_type IS 'pinball, video or other.';
COMMENT ON COLUMN annex_api.daily_plays.games IS 'Games played.';
COMMENT ON COLUMN annex_api.daily_plays.credits IS 'Credits used (a game is one credit, sometimes two).';
COMMENT ON COLUMN annex_api.daily_plays.revenue_eur IS 'Credits times the machine''s current price, in EUR. The counted cash is in revenue_weekly.';
COMMENT ON COLUMN annex_api.daily_plays.avg_duration_s IS 'Average game length in seconds.';
COMMENT ON COLUMN annex_api.daily_plays.tilts IS 'Games that ended in a tilt (the player shook the machine too hard).';
COMMENT ON COLUMN annex_api.daily_plays.members IS 'Distinct members who played (anonymous games not counted).';

COMMENT ON VIEW annex_api.hourly_plays IS 'Goat Arcade: play per venue per day and hour: games and credits. One row per day, hour and venue with at least one game. For busiest-hours and weekday-by-hour heatmaps; days and hours are local (Central European) time.';
COMMENT ON COLUMN annex_api.hourly_plays.day IS 'Local date.';
COMMENT ON COLUMN annex_api.hourly_plays.weekday IS 'Day of the week of day: 1 = Monday ... 7 = Sunday.';
COMMENT ON COLUMN annex_api.hourly_plays.hour IS 'Local hour of the day the games started, 0-23 (14 = 14:00 to 14:59).';
COMMENT ON COLUMN annex_api.hourly_plays.venue_code IS 'Venue where the games were played.';
COMMENT ON COLUMN annex_api.hourly_plays.games IS 'Games started in that hour.';
COMMENT ON COLUMN annex_api.hourly_plays.credits IS 'Credits used (a game is one credit, sometimes two).';

COMMENT ON VIEW annex_api.machine_events_daily IS 'Goat Arcade: what the machines reported, per machine, day and event type. Faults hint at a machine that needs a technician.';
COMMENT ON COLUMN annex_api.machine_events_daily.day IS 'Local date.';
COMMENT ON COLUMN annex_api.machine_events_daily.machine_id IS 'Machine (machine_floor.machine_id).';
COMMENT ON COLUMN annex_api.machine_events_daily.title IS 'Title of the game.';
COMMENT ON COLUMN annex_api.machine_events_daily.current_venue_code IS 'Where the machine stands now (NULL: in storage).';
COMMENT ON COLUMN annex_api.machine_events_daily.event_type IS 'tilt, slam_tilt (a hard tilt), ball_search (the machine lost track of a ball), coil_fault, switch_fault, jackpot, replay (a free game won by score), match (a free game won by lottery at the end).';
COMMENT ON COLUMN annex_api.machine_events_daily.events IS 'How many.';

COMMENT ON VIEW annex_api.service_log IS 'Goat Arcade: service tickets: what broke, who fixed it, how long it took and what parts it needed.';
COMMENT ON COLUMN annex_api.service_log.ticket_id IS 'Ticket id.';
COMMENT ON COLUMN annex_api.service_log.machine_id IS 'Machine (machine_floor.machine_id).';
COMMENT ON COLUMN annex_api.service_log.serial_no IS 'Machine serial number.';
COMMENT ON COLUMN annex_api.service_log.title IS 'Title of the game.';
COMMENT ON COLUMN annex_api.service_log.current_venue_code IS 'Where the machine stands now (NULL: in storage).';
COMMENT ON COLUMN annex_api.service_log.opened_at IS 'When the ticket was opened.';
COMMENT ON COLUMN annex_api.service_log.closed_at IS 'When it was closed; NULL while open.';
COMMENT ON COLUMN annex_api.service_log.status IS 'open, in_progress, waiting_parts or closed.';
COMMENT ON COLUMN annex_api.service_log.symptom IS 'What was reported, as typed.';
COMMENT ON COLUMN annex_api.service_log.resolution IS 'What the technician did; NULL while open.';
COMMENT ON COLUMN annex_api.service_log.reported_by IS 'Who reported it: staff, customer, technician or league player.';
COMMENT ON COLUMN annex_api.service_log.took_machine_off_floor IS 'True if the machine couldn''t be played until it was fixed.';
COMMENT ON COLUMN annex_api.service_log.downtime_hours IS 'Hours off the floor (until now if still open); NULL if it stayed playable.';
COMMENT ON COLUMN annex_api.service_log.technician IS 'The technician''s name.';
COMMENT ON COLUMN annex_api.service_log.labour_hours IS 'Hours of work booked.';
COMMENT ON COLUMN annex_api.service_log.parts_cost_eur IS 'Cost of the parts used, EUR.';
COMMENT ON COLUMN annex_api.service_log.parts IS 'Parts used, e.g. "flipper rebuild kit, coil sleeve x2"; NULL if none.';

COMMENT ON VIEW annex_api.revenue_weekly IS 'Goat Arcade: the cash counted from the machines per venue per week (Monday to Sunday): coins, tokens and member-card plays.';
COMMENT ON COLUMN annex_api.revenue_weekly.week_start IS 'Monday of the week.';
COMMENT ON COLUMN annex_api.revenue_weekly.venue_code IS 'Venue code.';
COMMENT ON COLUMN annex_api.revenue_weekly.venue_name IS 'Venue name.';
COMMENT ON COLUMN annex_api.revenue_weekly.city IS 'City.';
COMMENT ON COLUMN annex_api.revenue_weekly.amount_eur IS 'Total counted, EUR.';
COMMENT ON COLUMN annex_api.revenue_weekly.coins_eur IS 'Of which coins, EUR.';
COMMENT ON COLUMN annex_api.revenue_weekly.tokens IS 'Tokens collected (one token is 0.50 EUR).';
COMMENT ON COLUMN annex_api.revenue_weekly.card_plays IS 'Plays paid with a member card.';
COMMENT ON COLUMN annex_api.revenue_weekly.machines IS 'Machines that earned money that week.';

COMMENT ON VIEW annex_api.tournaments IS 'Goat Arcade: pinball tournaments, past (completed) and upcoming (registration).';
COMMENT ON COLUMN annex_api.tournaments.tournament_id IS 'Tournament id.';
COMMENT ON COLUMN annex_api.tournaments.name IS 'Name.';
COMMENT ON COLUMN annex_api.tournaments.venue_code IS 'Venue code.';
COMMENT ON COLUMN annex_api.tournaments.venue_name IS 'Venue name.';
COMMENT ON COLUMN annex_api.tournaments.starts_on IS 'Day of the tournament.';
COMMENT ON COLUMN annex_api.tournaments.format IS 'match_play (groups of four play one machine, points 7-5-3-1, four rounds) or best_game (the same, on electro-mechanical machines only).';
COMMENT ON COLUMN annex_api.tournaments.status IS 'registration (upcoming) or completed.';
COMMENT ON COLUMN annex_api.tournaments.entry_fee_eur IS 'Entry fee, EUR.';
COMMENT ON COLUMN annex_api.tournaments.max_players IS 'Places.';
COMMENT ON COLUMN annex_api.tournaments.entries IS 'Players entered.';
COMMENT ON COLUMN annex_api.tournaments.winner_handle IS 'Winner''s initials; NULL until completed.';
COMMENT ON COLUMN annex_api.tournaments.winner_nickname IS 'Winner''s nickname; NULL until completed.';

COMMENT ON VIEW annex_api.tournament_entries IS 'Goat Arcade: who is entered in each tournament in the club system (past and upcoming), by nickname; erased players are left out, so a tournament can list fewer rows than tournaments.entries.';
COMMENT ON COLUMN annex_api.tournament_entries.tournament_id IS 'Tournament (tournaments.tournament_id).';
COMMENT ON COLUMN annex_api.tournament_entries.nickname IS 'Nickname of the entered member (unique; players.nickname).';
COMMENT ON COLUMN annex_api.tournament_entries.handle IS 'Three-letter initials (not unique).';
COMMENT ON COLUMN annex_api.tournament_entries.seed IS 'Seed number; NULL until seeding is done.';
COMMENT ON COLUMN annex_api.tournament_entries.entered_at IS 'When the entry was made.';

COMMENT ON VIEW annex_api.tournament_standings IS 'Goat Arcade: final standings of completed tournaments: points over four rounds, by player.';
COMMENT ON COLUMN annex_api.tournament_standings.tournament_id IS 'Tournament (tournaments.tournament_id).';
COMMENT ON COLUMN annex_api.tournament_standings.tournament_name IS 'Tournament name.';
COMMENT ON COLUMN annex_api.tournament_standings.starts_on IS 'Day of the tournament.';
COMMENT ON COLUMN annex_api.tournament_standings.position IS 'Final place (ties on points are split by best single score).';
COMMENT ON COLUMN annex_api.tournament_standings.handle IS 'Three-letter initials.';
COMMENT ON COLUMN annex_api.tournament_standings.nickname IS 'Nickname.';
COMMENT ON COLUMN annex_api.tournament_standings.points IS 'Points (7, 5, 3, 1 per group game).';
COMMENT ON COLUMN annex_api.tournament_standings.games_played IS 'Group games played.';
COMMENT ON COLUMN annex_api.tournament_standings.best_score IS 'Their best single score in the tournament.';

GRANT USAGE ON SCHEMA annex_api TO metabase_ro;
GRANT SELECT ON ALL TABLES IN SCHEMA annex_api TO metabase_ro;

-- PostgREST picks up new or changed views
NOTIFY pgrst, 'reload schema';
