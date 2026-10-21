-- Goat Arcade d.o.o.: synthetic production data. Run by 02-seed.sh with -v anchor=YYYY-MM-DD.
--
-- Every random choice comes from pg_temp.r(key), a hash of a text key, never from random(): the data doesn't
-- depend on row order or query plans, so the same anchor gives the same rows. Dates are days before the
-- anchor (pg_temp.ago(n)); the last day with data is the day before it.
--
-- Planted stories (democlient/checks.sql proves each one):
--   1 dying machine: Haunted Lighthouse in Maribor, coil faults rising for six weeks, plays falling, now down
--   2 the champion: goatmother (GOA) holds the top score on many models at three venues
--   3 Celje lost its stars: Mountain Goat Mayhem and Dragon Heist moved to Zagreb 35 days ago; revenue fell
--   4 rhythm: evenings and weekends are busy, Mondays nearly dead, Thursday league night at Bezigrad
--   5 era taste: LCD tables earn the most per game, electro-mechanical games last the longest
--   6 tilt-happy cabinet: the Cyber Samurai in Graz tilts three times as often as the one in Ljubljana
--   7 upcoming tournament: Goat Arcade Masters, registration open, no matches yet
SET search_path = arcade;
SET client_min_messages = warning;
SET max_parallel_workers_per_gather = 0;

CREATE TEMP TABLE anchor AS SELECT :'anchor'::date AS d;

CREATE FUNCTION pg_temp.r(k text) RETURNS float8 LANGUAGE sql IMMUTABLE
  AS $$ SELECT (hashtextextended(k, 7) & 9007199254740991)::float8 / 9007199254740992.0 $$;
CREATE FUNCTION pg_temp.z(k text) RETURNS float8 LANGUAGE sql IMMUTABLE
  AS $$ SELECT sqrt(-2 * ln(1 - pg_temp.r(k || '~a'))) * cos(2 * pi() * pg_temp.r(k || '~b')) $$;
CREATE FUNCTION pg_temp.pick(k text, a text[]) RETURNS text LANGUAGE sql IMMUTABLE
  AS $$ SELECT a[1 + floor(pg_temp.r(k) * cardinality(a))::int] $$;
CREATE FUNCTION pg_temp.ago(n int) RETURNS date LANGUAGE sql STABLE
  AS $$ SELECT d - n FROM pg_temp.anchor $$;
-- local wall-clock time on a day -> timestamptz (all venues are on Central European time)
CREATE FUNCTION pg_temp.at(day date, minutes float8) RETURNS timestamptz LANGUAGE sql STABLE
  AS $$ SELECT (day::timestamp + make_interval(secs => round(minutes * 60))) AT TIME ZONE 'Europe/Ljubljana' $$;

-- --- venues -------------------------------------------------------------------------------------------------------
INSERT INTO venues (id, code, name, city, country, lat, lon, tz, opened_on, closed_on, monthly_rent_eur, hours)
SELECT id, code, name, city, country, lat, lon, tz, pg_temp.ago(opened),
       CASE WHEN closed IS NOT NULL THEN pg_temp.ago(closed) END, rent,
       CASE WHEN code = 'LJ1' THEN jsonb_set(h, '{mon}', '["18:00", "24:00"]') ELSE h END
FROM (VALUES
  (1, 'LJ1', 'Goat Arcade Metelkova',  'Ljubljana',   'SI', 46.05690, 14.51550, 'Europe/Ljubljana', 4200, NULL::int, 6400.00),
  (2, 'LJ2', 'Goat Arcade Bežigrad',   'Ljubljana',   'SI', 46.07110, 14.51100, 'Europe/Ljubljana', 2300, NULL, 4100.00),
  (3, 'MB',  'Goat Arcade Lent',       'Maribor',     'SI', 46.55600, 15.64400, 'Europe/Ljubljana', 2900, NULL, 3200.00),
  (4, 'CE',  'Goat Arcade Celje',      'Celje',       'SI', 46.23090, 15.26040, 'Europe/Ljubljana', 1800, NULL, 2600.00),
  (5, 'KP',  'Goat Arcade Koper',      'Koper',       'SI', 45.54690, 13.72940, 'Europe/Ljubljana', 1500, NULL, 2900.00),
  (6, 'ZG',  'Goat Arcade Tkalčićeva', 'Zagreb',      'HR', 45.81500, 15.97700, 'Europe/Zagreb',    1300, NULL, 4800.00),
  (7, 'GZ',  'Goat Arcade Lend',       'Graz',        'AT', 47.07070, 15.43000, 'Europe/Vienna',    1000, NULL, 5200.00),
  (8, 'TS',  'Goat Arcade Trieste',    'Trieste',     'IT', 45.64950, 13.77680, 'Europe/Rome',       700, NULL, 4400.00),
  (9, 'NG',  'Goat Arcade Nova Gorica','Nova Gorica', 'SI', 45.95600, 13.64800, 'Europe/Ljubljana', 1600, 120,  2200.00)
) v(id, code, name, city, country, lat, lon, tz, opened, closed, rent)
CROSS JOIN (SELECT '{"mon": null, "tue": ["16:00", "24:00"], "wed": ["16:00", "24:00"], "thu": ["16:00", "24:00"],
                     "fri": ["16:00", "02:00"], "sat": ["12:00", "02:00"], "sun": ["12:00", "22:00"]}'::jsonb AS h) hh;
SELECT setval('venues_id_seq', 9);

CREATE TEMP TABLE vpop (venue_id int PRIMARY KEY, pop float8, cabinets int);
INSERT INTO vpop VALUES (1, 1.4, 24), (2, 1.0, 18), (3, 0.9, 16), (4, 0.85, 15), (5, 0.75, 14),
                        (6, 1.2, 20), (7, 1.05, 18), (8, 0.8, 14), (9, 0.6, 12);

-- --- manufacturers and models ---------------------------------------------------------------------------------------
INSERT INTO manufacturers (id, name, country, founded, defunct) VALUES
  (1, 'Brightwave Amusements',    'US', 1957, false),
  (2, 'Kessler Pin Co.',          'DE', 1948, true),
  (3, 'Silver Ball Works',        'US', 1971, false),
  (4, 'Nordlicht Spielgeräte',    'AT', 1964, false),
  (5, 'Tilt Theory',              'US', 2011, false),
  (6, 'Adriatic Amusements',      'SI', 1990, false),
  (7, 'Lumen Games',              'JP', 1976, false),
  (8, 'Quarterhorse Electronics', 'US', 1979, true);
SELECT setval('manufacturers_id_seq', 8);

INSERT INTO machine_models (id, title, manufacturer_id, release_year, era, machine_type, theme, designer, flippers, max_multiball, features)
SELECT id, title, mfr, yr, era, kind, theme, designer,
       CASE WHEN kind = 'pinball' THEN CASE era WHEN 'EM' THEN 2 WHEN 'SS' THEN 2 + (pg_temp.r('fl' || id) < 0.3)::int
            WHEN 'DMD' THEN 2 + floor(pg_temp.r('fl' || id) * 3)::int ELSE 3 + (pg_temp.r('fl' || id) < 0.4)::int END END,
       CASE WHEN kind = 'pinball' THEN CASE era WHEN 'EM' THEN 1 WHEN 'SS' THEN 1 + floor(pg_temp.r('mb' || id) * 3)::int
            WHEN 'DMD' THEN 3 + floor(pg_temp.r('mb' || id) * 4)::int ELSE 4 + floor(pg_temp.r('mb' || id) * 3)::int END END,
       CASE WHEN kind = 'pinball' THEN jsonb_build_object(
              'ramps', CASE era WHEN 'EM' THEN 0 WHEN 'SS' THEN floor(pg_temp.r('rp' || id) * 2)::int
                                WHEN 'DMD' THEN 1 + floor(pg_temp.r('rp' || id) * 3)::int ELSE 2 + floor(pg_temp.r('rp' || id) * 3)::int END,
              'modes', CASE era WHEN 'EM' THEN 0 WHEN 'SS' THEN 2 + floor(pg_temp.r('md' || id) * 4)::int
                                WHEN 'DMD' THEN 6 + floor(pg_temp.r('md' || id) * 8)::int ELSE 10 + floor(pg_temp.r('md' || id) * 12)::int END,
              'toys', to_jsonb(toys))
            ELSE jsonb_build_object('players', CASE WHEN kind = 'video' THEN 2 ELSE 1 END, 'controls', to_jsonb(toys)) END
FROM (VALUES
  ( 1, 'Moon Rodeo',            1, 1962, 'EM',  'pinball', 'space western',                 'Edna Fairweather', ARRAY['spinning target']),
  ( 2, 'Twin Comets',           1, 1966, 'EM',  'pinball', 'astronomy',                     'Edna Fairweather', ARRAY['pop bumpers']),
  ( 3, 'Fortune Teller',        1, 1964, 'EM',  'pinball', 'carnival',                      'Rafe Calloway',    ARRAY['fortune wheel']),
  ( 4, 'Hula Hoedown',          2, 1968, 'EM',  'pinball', 'tropical dance',                'Bruno Stahl',      ARRAY['drop targets']),
  ( 5, 'Alpine Express',        2, 1971, 'EM',  'pinball', 'mountain railway',              'Bruno Stahl',      ARRAY['toy train']),
  ( 6, 'Bowling Kings',         3, 1973, 'EM',  'pinball', 'bowling',                       'Dale Moravec',     ARRAY['kickout hole']),
  ( 7, 'Jet Set',               3, 1975, 'EM',  'pinball', 'jet-age travel',                'Dale Moravec',     ARRAY['roto-target']),
  ( 8, 'Starlight Rally',       4, 1977, 'EM',  'pinball', 'rally racing',                  'Greta Vogel',      ARRAY['spinner']),
  ( 9, 'Laser Goat',            1, 1980, 'SS',  'pinball', 'sci-fi goats',                  'Ines Halloran',    ARRAY['laser cannon', 'talking goat head']),
  (10, 'Deep Sea Disco',        2, 1979, 'SS',  'pinball', 'underwater disco',              'Bruno Stahl',      ARRAY['mirror ball']),
  (11, 'Neon Highway',          4, 1982, 'SS',  'pinball', 'night driving',                 'Greta Vogel',      ARRAY['spinner']),
  (12, 'Cyber Samurai',         3, 1983, 'SS',  'pinball', 'cyberpunk',                     'Dale Moravec',     ARRAY['sword target']),
  (13, 'Volcano Island',        4, 1984, 'SS',  'pinball', 'volcano adventure',             'Greta Vogel',      ARRAY['eruption kicker']),
  (14, 'Robot Uprising',        1, 1985, 'SS',  'pinball', 'robots',                        'Ines Halloran',    ARRAY['robot arm']),
  (15, 'Iron Wizard',           3, 1986, 'SS',  'pinball', 'heavy-metal fantasy',           'Pat Okonkwo',      ARRAY['speech']),
  (16, 'Galactic Garage',       3, 1987, 'SS',  'pinball', 'space mechanics',               'Pat Okonkwo',      ARRAY['upper playfield']),
  (17, 'Mummy''s Revenge',      1, 1988, 'SS',  'pinball', 'egyptology horror',             'Ines Halloran',    ARRAY['sarcophagus']),
  (18, 'Punk Planet',           4, 1989, 'SS',  'pinball', 'punk rock in space',            'Lars Brenner',     ARRAY['guitar spinner']),
  (19, 'Wizard''s Tower',       3, 1992, 'DMD', 'pinball', 'wizardry',                      'Pat Okonkwo',      ARRAY['spinning tower', 'magnet']),
  (20, 'Dragon Heist',          1, 1993, 'DMD', 'pinball', 'a dragon robs a bank',          'Ines Halloran',    ARRAY['animated dragon', 'vault']),
  (21, 'Time Machine Tavern',   1, 1994, 'DMD', 'pinball', 'time-travelling pub',           'Rafe Calloway',    ARRAY['clock']),
  (22, 'Haunted Lighthouse',    3, 1995, 'DMD', 'pinball', 'maritime ghosts',               'Joe Bianchi',      ARRAY['rotating lamp', 'ghost ship']),
  (23, 'Monster Truck Mayhem',  4, 1996, 'DMD', 'pinball', 'monster trucks',                'Lars Brenner',     ARRAY['crushing truck']),
  (24, 'Lava Lamp Lounge',      6, 1996, 'DMD', 'pinball', '70s lounge',                    'Tomo Vrhovnik',    ARRAY['lava lamp']),
  (25, 'Dungeon Delvers',       3, 1997, 'DMD', 'pinball', 'tabletop role-playing',         'Pat Okonkwo',      ARRAY['d20 die', 'dungeon door']),
  (26, 'Kaiju Kitchen',         6, 1998, 'DMD', 'pinball', 'giant-monster cooking show',    'Tomo Vrhovnik',    ARRAY['kaiju head', 'frying pan']),
  (27, 'Starship Janitor',      1, 1999, 'DMD', 'pinball', 'space cleaning crew',           'Rafe Calloway',    ARRAY['mop spinner']),
  (28, 'Hacker Heist',          1, 2000, 'DMD', 'pinball', '90s hacking',                   'Ines Halloran',    ARRAY['floppy disk lock']),
  (29, 'Cosmic Crusaders',      3, 2001, 'DMD', 'pinball', 'space opera',                   'Joe Bianchi',      ARRAY['mothership']),
  (30, 'Pirate Radio',          4, 2004, 'DMD', 'pinball', 'offshore radio pirates',        'Lars Brenner',     ARRAY['radio dial']),
  (31, 'Zombie Bowling League', 3, 2006, 'DMD', 'pinball', 'zombies go bowling',            'Joe Bianchi',      ARRAY['zombie pins']),
  (32, 'Ghost Train',           4, 2008, 'DMD', 'pinball', 'fairground horror',             'Greta Vogel',      ARRAY['ghost train ride']),
  (33, 'Retro Rocket',          1, 2010, 'DMD', 'pinball', '50s space race',                'Rafe Calloway',    ARRAY['launching rocket']),
  (34, 'Pixel Knights',         1, 2015, 'LCD', 'pinball', '8-bit fantasy',                 'Anja Kos',         ARRAY['castle gate']),
  (35, 'Quantum Quest',         5, 2016, 'LCD', 'pinball', 'physics lab',                   'Joe Bianchi',      ARRAY['quantum spinner', 'magnet']),
  (36, 'Synthwave Racer',       4, 2017, 'LCD', 'pinball', '80s neon racing',               'Lars Brenner',     ARRAY['car']),
  (37, 'Cryptid Hunters',       5, 2018, 'LCD', 'pinball', 'bigfoot and friends',           'Anja Kos',         ARRAY['bigfoot']),
  (38, 'Mountain Goat Mayhem',  5, 2019, 'LCD', 'pinball', 'alpine goats',                  'Anja Kos',         ARRAY['climbing goat', 'cliff ramp']),
  (39, 'Abyssal Expedition',    3, 2019, 'LCD', 'pinball', 'deep sea',                      'Joe Bianchi',      ARRAY['submarine']),
  (40, 'Neon Ninja',            1, 2020, 'LCD', 'pinball', 'ninja',                         'Rafe Calloway',    ARRAY['shuriken spinner']),
  (41, 'Mars Colony',           3, 2021, 'LCD', 'pinball', 'Mars settlers',                 'Pat Okonkwo',      ARRAY['dome', 'rover']),
  (42, 'Cosmic Cat Café',       5, 2022, 'LCD', 'pinball', 'cats in space',                 'Anja Kos',         ARRAY['cat paw']),
  (43, 'Robo Rodeo',            1, 2022, 'LCD', 'pinball', 'robot cowboys',                 'Ines Halloran',    ARRAY['bucking robot']),
  (44, 'Dragon of Ljubljana',   6, 2023, 'LCD', 'pinball', 'the city dragon',               'Tomo Vrhovnik',    ARRAY['dragon bridge']),
  (45, 'Tesla Coil Tower',      4, 2023, 'LCD', 'pinball', 'electricity',                   'Greta Vogel',      ARRAY['spark coil']),
  (46, 'Coffee Quest',          5, 2024, 'LCD', 'pinball', 'Kaldi the goatherd finds coffee','Anja Kos',        ARRAY['dancing goats']),
  (47, 'Deep Space Diner',      6, 2024, 'LCD', 'pinball', 'a truck stop on Pluto',         'Tomo Vrhovnik',    ARRAY['milkshake mixer']),
  (48, 'Wizard Academy',        3, 2025, 'LCD', 'pinball', 'magic school',                  'Joe Bianchi',      ARRAY['floating book']),
  (49, 'Asteroid Miner',        7, 1981, NULL,  'video',   'space mining shooter',          'Mika Sato',        ARRAY['joystick', '2 buttons']),
  (50, 'Pixel Pete',            8, 1982, NULL,  'video',   'platformer',                    'Dot Mercer',       ARRAY['joystick', '1 button']),
  (51, 'Galaxy Goat',           7, 1983, NULL,  'video',   'shoot-em-up with goats',        'Mika Sato',        ARRAY['joystick', '2 buttons']),
  (52, 'Light Cycle Arena',     8, 1984, NULL,  'video',   'racing',                        'Dot Mercer',       ARRAY['wheel']),
  (53, 'Turbo Tunnel',          7, 1985, NULL,  'video',   'racing',                        'Ken Arai',         ARRAY['wheel', 'pedal']),
  (54, 'Bubble Bandits',        7, 1987, NULL,  'video',   'puzzle',                        'Ken Arai',         ARRAY['joystick', '1 button']),
  (55, 'Street Bruisers',       8, 1991, NULL,  'video',   'fighting',                      'Dot Mercer',       ARRAY['joystick', '6 buttons']),
  (56, 'Ninja Kart',            7, 1992, NULL,  'video',   'kart racing',                   'Mika Sato',        ARRAY['wheel']),
  (57, 'Mecha Duel',            7, 1995, NULL,  'video',   'fighting robots',               'Ken Arai',         ARRAY['joystick', '4 buttons']),
  (58, 'Dance Pad Fever',       7, 1999, NULL,  'video',   'rhythm',                        'Mika Sato',        ARRAY['dance pad']),
  (59, 'Puck Rink',             8, 2005, NULL,  'other',   'air hockey',                    NULL,               ARRAY['pucks', 'mallets']),
  (60, 'Plush Claw Deluxe',     8, 2010, NULL,  'other',   'claw crane',                    NULL,               ARRAY['claw']),
  (61, 'Roll-a-Goat Lanes',     6, 2012, NULL,  'other',   'roll-a-ball lanes',             'Tomo Vrhovnik',    ARRAY['balls'])
) m(id, title, mfr, yr, era, kind, theme, designer, toys);
SELECT setval('machine_models_id_seq', 61);

-- how much players like a model (era taste, with a per-model jitter) and how likely it is on a floor
CREATE TEMP TABLE mweight AS
SELECT id AS model_id,
       CASE WHEN machine_type <> 'pinball' THEN CASE machine_type WHEN 'video' THEN 0.6 ELSE 0.5 END
            ELSE CASE era WHEN 'EM' THEN 0.55 WHEN 'SS' THEN 0.75 WHEN 'DMD' THEN 1.0 ELSE 1.35 END END
         * (0.7 + 0.6 * pg_temp.r('pop' || id)) * CASE WHEN id IN (20, 38) THEN 1.6 ELSE 1 END AS pop,
       CASE era WHEN 'EM' THEN 1 WHEN 'SS' THEN 1.5 WHEN 'DMD' THEN 2.5 WHEN 'LCD' THEN 3 ELSE 1 END AS w
FROM machine_models;

-- --- machines -------------------------------------------------------------------------------------------------------
-- (venue, model) pairs the stories need
CREATE TEMP TABLE forced (venue_id int, model_id int);
INSERT INTO forced VALUES (3, 22), (4, 38), (4, 20), (7, 12), (2, 12), (2, 1), (2, 3), (2, 5), (2, 8);

-- a weighted sample without replacement per venue (Efraimidis-Spirakis keys): about 80% pinball
CREATE TEMP TABLE slots AS
WITH cand AS (
  SELECT v.venue_id, v.cabinets, mm.id AS model_id, mm.machine_type = 'pinball' AS pin,
         CASE WHEN f.model_id IS NOT NULL THEN 0
              ELSE -ln(1 - pg_temp.r('slot.' || v.venue_id || '.' || mm.id)) / w.w END AS k
  FROM vpop v CROSS JOIN machine_models mm
  JOIN mweight w ON w.model_id = mm.id
  LEFT JOIN forced f ON f.venue_id = v.venue_id AND f.model_id = mm.id),
ranked AS (SELECT *, row_number() OVER (PARTITION BY venue_id, pin ORDER BY k, model_id) AS rn FROM cand)
SELECT venue_id, model_id FROM ranked
WHERE (pin AND rn <= round(cabinets * 0.8)) OR (NOT pin AND rn <= cabinets - round(cabinets * 0.8));

INSERT INTO machines (serial_no, model_id, venue_id, installed_at, condition, price_per_play_cents, cab_no, notes)
SELECT upper(left(regexp_replace(mf.name, '[^A-Za-z]', '', 'g'), 2)) || '-' || mm.release_year || '-'
         || lpad(((mm.id * 97 + s.venue_id * 1301) % 9000 + 1000)::text, 4, '0'),
       mm.id, s.venue_id,
       least(greatest(make_date(mm.release_year, 1, 1) + floor(pg_temp.r('inst' || k) * 300)::int,
                      v.opened_on + floor(pg_temp.r('inst2' || k) * 0.6 * (pg_temp.ago(15) - v.opened_on))::int),
             coalesce(v.closed_on - 60, pg_temp.ago(15 + floor(pg_temp.r('inst3' || k) * 20)::int))),
       pg_temp.pick('cond' || k, CASE mm.era
         WHEN 'EM'  THEN ARRAY['fair', 'FAIR', 'good', 'needs work', 'Needs Work', 'restored']
         WHEN 'SS'  THEN ARRAY['good', 'Good', 'fair', 'good ', 'needs work']
         WHEN 'DMD' THEN ARRAY['good', 'Good', 'excellent', 'fair', 'good']
         WHEN 'LCD' THEN ARRAY['Excellent', 'excellent', 'good', 'mint']
         ELSE ARRAY['good', 'fair', 'Good'] END),
       CASE WHEN mm.machine_type = 'pinball' THEN CASE mm.era WHEN 'EM' THEN 50 WHEN 'SS' THEN 100
              WHEN 'DMD' THEN CASE WHEN pg_temp.r('price' || k) < 0.3 THEN 150 ELSE 100 END ELSE 200 END
            WHEN mm.machine_type = 'video' THEN CASE WHEN pg_temp.r('price' || k) < 0.3 THEN 100 ELSE 50 END
            WHEN mm.id = 60 THEN 200 ELSE 100 END,
       CASE WHEN pg_temp.r('cab' || k) < 0.3 THEN 'C-' || lpad((floor(pg_temp.r('cabn' || k) * 900) + 100)::text, 3, '0') END,
       CASE WHEN pg_temp.r('note' || k) < 0.12 THEN pg_temp.pick('notetxt' || k,
         ARRAY['bought from a collector in Vienna', 'shop job done', 'translite faded', 'replacement playfield',
               'LED kit installed', 'needs new rubbers soon', 'owner''s favourite, do not move']) END
FROM slots s
JOIN machine_models mm ON mm.id = s.model_id
JOIN manufacturers mf ON mf.id = mm.manufacturer_id
JOIN venues v ON v.id = s.venue_id
CROSS JOIN LATERAL (SELECT s.venue_id || '.' || s.model_id AS k) kk
ORDER BY s.venue_id, s.model_id;

CREATE TEMP TABLE story (name text PRIMARY KEY, machine_id int);
INSERT INTO story SELECT 'dying', id FROM machines WHERE venue_id = 3 AND model_id = 22;
INSERT INTO story SELECT 'tilty', id FROM machines WHERE venue_id = 7 AND model_id = 12;
INSERT INTO story SELECT 'calm_samurai', id FROM machines WHERE venue_id = 2 AND model_id = 12;
INSERT INTO story SELECT 'star_goat', id FROM machines WHERE venue_id = 4 AND model_id = 38;
INSERT INTO story SELECT 'star_dragon', id FROM machines WHERE venue_id = 4 AND model_id = 20;
-- story machines have a long history at their venue
UPDATE machines SET installed_at = least(installed_at, pg_temp.ago(400)) WHERE id IN (SELECT machine_id FROM story);

-- where each machine stood when: stints, changed by moves (each machine moves at most once)
CREATE TEMP TABLE moves (machine_id int PRIMARY KEY, to_venue int, on_d date, reason text, sold boolean DEFAULT false);
INSERT INTO moves (machine_id, to_venue, on_d, reason)
SELECT machine_id, 6, pg_temp.ago(35), 'expected to earn more in Zagreb' FROM story WHERE name IN ('star_goat', 'star_dragon');
-- Nova Gorica closed 120 days ago: five machines went to other venues, two were sold, the rest went to storage
INSERT INTO moves (machine_id, to_venue, on_d, reason, sold)
SELECT id, CASE WHEN rn <= 5 THEN (ARRAY[1, 6, 8, 3, 7])[rn::int] END,
       CASE WHEN rn <= 5 THEN pg_temp.ago(113) ELSE pg_temp.ago(120) END,
       CASE WHEN rn <= 5 THEN 'Nova Gorica closed' WHEN rn <= 7 THEN 'sold after Nova Gorica closed' ELSE 'Nova Gorica closed, to storage' END,
       rn IN (6, 7)
FROM (SELECT id, row_number() OVER (ORDER BY pg_temp.r('ng' || id)) AS rn FROM machines WHERE venue_id = 9) x;
-- six ordinary swaps between open venues
INSERT INTO moves (machine_id, to_venue, on_d, reason)
SELECT id, (ARRAY[1, 2, 3, 4, 5, 6, 7, 8])[1 + (venue_id + floor(pg_temp.r('swapv' || id) * 7)::int) % 8],
       pg_temp.ago(20 + floor(pg_temp.r('swapd' || id) * 140)::int),
       pg_temp.pick('swapr' || id, ARRAY['rotation', 'rotation', 'customer requests', 'space for a new title'])
FROM machines
WHERE venue_id BETWEEN 1 AND 8 AND id NOT IN (SELECT machine_id FROM story)
  AND (venue_id, model_id) NOT IN (SELECT venue_id, model_id FROM forced) AND installed_at < pg_temp.ago(200)
ORDER BY pg_temp.r('swap' || id) LIMIT 6;

CREATE TEMP TABLE stints AS SELECT id AS machine_id, venue_id, installed_at AS from_d, NULL::date AS to_d FROM machines;
UPDATE stints s SET to_d = m.on_d FROM moves m WHERE m.machine_id = s.machine_id;
INSERT INTO stints SELECT machine_id, to_venue, on_d, NULL FROM moves WHERE to_venue IS NOT NULL;

INSERT INTO machine_moves (machine_id, from_venue_id, to_venue_id, moved_on, reason)
SELECT * FROM (
  SELECT m.machine_id, ma.venue_id, m.to_venue, m.on_d, m.reason FROM moves m JOIN machines ma ON ma.id = m.machine_id
  UNION ALL
  SELECT id, NULL, venue_id, installed_at, 'new install' FROM machines WHERE installed_at >= pg_temp.ago(180)
) x ORDER BY 4, 1;
UPDATE machines ma SET venue_id = m.to_venue, is_deleted = m.sold FROM moves m WHERE m.machine_id = ma.id;

-- --- players --------------------------------------------------------------------------------------------------------
CREATE TEMP TABLE vcum AS
SELECT venue_id, sum(pop) OVER (ORDER BY venue_id) / sum(pop) OVER () AS cum FROM vpop;

CREATE TEMP TABLE pskill AS
SELECT i AS player_id, CASE WHEN i = 1 THEN 2.8 ELSE exp(0.45 * pg_temp.z('skill' || i)) END AS skill
FROM generate_series(1, 2500) i;

INSERT INTO players (id, handle, nickname, full_name, email, phone, birth_date, home_venue_id, member_since, rating, marketing_opt_in, deleted_at)
SELECT i, handle, nickname, full_name, email, phone, birth_date, home, member_since, rating, opt_in, deleted_at
FROM (
  SELECT i, home,
         CASE WHEN i = 1 THEN 'GOA' ELSE upper(translate(left(fn, 1) || left(ln, 1), 'ČŠŽĆĐčšžćđ', 'CSZCDcszcd')) || chr(65 + floor(pg_temp.r('h' || i) * 26)::int) END AS handle,
         CASE WHEN i = 1 THEN 'goatmother' WHEN erased THEN 'erased-' || i
              ELSE pg_temp.pick('adj' || i, ARRAY['tilt', 'silver', 'multi', 'magna', 'flipper', 'spinner', 'outlane', 'bumper',
                     'jackpot', 'replay', 'plunger', 'sling', 'nudge', 'wizard', 'lucky', 'turbo', 'pixel', 'neon'])
                || pg_temp.pick('noun' || i, ARRAY['fox', 'goat', 'wolf', 'owl', 'wizard', 'ninja', 'pirate', 'knight', 'raven',
                     'otter', 'yeti', 'kraken', 'comet', 'rocket', 'badger', 'lynx']) || i END AS nickname,
         CASE WHEN NOT erased THEN fn || ' ' || ln END AS full_name,
         CASE WHEN NOT erased THEN translate(lower(fn || '.' || ln), 'čšžćđ', 'csczd') || CASE WHEN i = 1 THEN '' ELSE i::text END
                || '@' || pg_temp.pick('dom' || i, ARRAY['example.com', 'example.net', 'example.org']) END AS email,
         CASE WHEN NOT erased THEN
           CASE v.country WHEN 'HR' THEN '+385 9' || floor(pg_temp.r('ph1' || i) * 10)::int WHEN 'AT' THEN '+43 66' || floor(pg_temp.r('ph1' || i) * 10)::int
                          WHEN 'IT' THEN '+39 34' || floor(pg_temp.r('ph1' || i) * 10)::int
                          ELSE '+386 ' || pg_temp.pick('ph1' || i, ARRAY['30', '31', '40', '41', '51', '64', '70']) END
           || ' ' || lpad(floor(pg_temp.r('ph2' || i) * 1000)::text, 3, '0') || ' ' || lpad(floor(pg_temp.r('ph3' || i) * 1000)::text, 3, '0') END AS phone,
         CASE WHEN NOT erased THEN make_date(1965 + floor(pg_temp.r('by' || i) * 41)::int, 1, 1) + floor(pg_temp.r('bd' || i) * 364)::int END AS birth_date,
         CASE WHEN i = 1 THEN pg_temp.ago(2100)
              ELSE greatest(v.opened_on, pg_temp.ago(1 + floor(pg_temp.r('ms' || i) ^ 0.8 * 2200)::int)) END AS member_since,
         greatest(800, least(2400, round(1500 + ln(ps.skill) / 0.45 * 250 + 60 * pg_temp.z('rt' || i))))::int AS rating,
         NOT erased AND pg_temp.r('opt' || i) < 0.35 AS opt_in,
         CASE WHEN erased THEN pg_temp.at(pg_temp.ago(1 + floor(pg_temp.r('del' || i) * 150)::int), 600 + pg_temp.r('delt' || i) * 600) END AS deleted_at
  FROM generate_series(1, 2500) i
  JOIN pskill ps ON ps.player_id = i
  CROSS JOIN LATERAL (SELECT i > 1 AND pg_temp.r('erase' || i) < 0.015 AS erased,
                             CASE WHEN i = 1 THEN 1 ELSE (SELECT venue_id FROM vcum WHERE cum > pg_temp.r('home' || i) ORDER BY cum LIMIT 1) END AS home,
                             CASE WHEN i = 1 THEN 'Vesna' ELSE pg_temp.pick('fn' || i, ARRAY['Luka', 'Nika', 'Maja', 'Jan', 'Ana', 'Žiga', 'Tina', 'Matej', 'Eva', 'Petra',
                               'Rok', 'Urška', 'Tim', 'Lara', 'Ivan', 'Marko', 'Ivana', 'Josip', 'Lukas', 'Anna', 'Felix', 'Lena', 'Jonas',
                               'Sophie', 'Marco', 'Giulia', 'Luca', 'Sara', 'Matteo', 'Chiara', 'Tomaž', 'Klara', 'Domen', 'Hana', 'Filip',
                               'Katarina', 'Gašper', 'Neža', 'Ema', 'Vid']) END AS fn,
                             CASE WHEN i = 1 THEN 'Kozel' ELSE pg_temp.pick('ln' || i, ARRAY['Novak', 'Horvat', 'Kovačič', 'Krajnc', 'Zupančič', 'Potočnik', 'Kos',
                               'Vidmar', 'Golob', 'Turk', 'Kovač', 'Babić', 'Marić', 'Jurić', 'Knežević', 'Gruber', 'Huber', 'Bauer',
                               'Wagner', 'Pichler', 'Rossi', 'Bianchi', 'Romano', 'Colombo', 'Ferrari', 'Kralj', 'Mlakar', 'Bizjak',
                               'Hribar', 'Oblak', 'Petek', 'Jelen', 'Zorko', 'Lah', 'Vrhovec']) END AS ln) p
  JOIN venues v ON v.id = p.home
) x;
SELECT setval('players_id_seq', 2500);

-- home regulars per venue, in a random order: low idx = plays most
CREATE TEMP TABLE vplayers AS
SELECT home_venue_id AS venue_id, row_number() OVER (PARTITION BY home_venue_id ORDER BY pg_temp.r('reg' || id)) AS idx, id AS player_id
FROM players;
CREATE INDEX ON vplayers (venue_id, idx);
CREATE TEMP TABLE vcount AS SELECT venue_id, count(*) AS n FROM vplayers GROUP BY venue_id;

-- --- technicians and service tickets ---------------------------------------------------------------------------------
INSERT INTO technicians (id, full_name, email, phone, hourly_rate_eur, base_venue_id, active) VALUES
  (1, 'Boris Zajc',     'boris.zajc@goat-arcade.example',     '+386 41 220 118', 32.00, 1, true),
  (2, 'Mojca Rebolj',   'mojca.rebolj@goat-arcade.example',   '+386 40 731 902', 30.00, 2, true),
  (3, 'Andrej Kokol',   'andrej.kokol@goat-arcade.example',   '+386 31 448 260', 29.00, 3, true),
  (4, 'Ivan Perić',     'ivan.peric@goat-arcade.example',     '+385 91 554 017', 27.00, 6, true),
  (5, 'Stefan Lechner', 'stefan.lechner@goat-arcade.example', '+43 664 120 7781', 38.00, 7, true),
  (6, 'Paolo Furlan',   'paolo.furlan@goat-arcade.example',   '+39 347 220 1934', 34.00, 8, true),
  (7, 'Tadej Vovk',     'tadej.vovk@goat-arcade.example',     '+386 51 300 455', 28.00, 9, false);
SELECT setval('technicians_id_seq', 7);
CREATE TEMP TABLE tech_for (venue_id int PRIMARY KEY, tech int, collector text);
INSERT INTO tech_for VALUES (1, 1, 'Boris'), (2, 2, 'Mojca'), (3, 3, 'Andrej'), (4, 3, 'Andrej'), (5, 6, 'Paolo'),
                            (6, 4, 'Ivan'), (7, 5, 'Stefan'), (8, 6, 'Paolo'), (9, 7, 'Tadej');

INSERT INTO parts (id, sku, name, unit_cost_eur) VALUES
  (1, 'RUB-KIT', 'rubber ring kit', 24.00), (2, 'FLP-RBK', 'flipper rebuild kit', 38.50), (3, 'COIL-2750', 'flipper coil 27-1500', 21.90),
  (4, 'COIL-SLV', 'coil sleeve', 2.40), (5, 'EOS-SW', 'end-of-stroke switch', 6.80), (6, 'LED-GI', 'GI LED strip', 14.00),
  (7, 'FUSE-5A', 'slow-blow fuse 5A', 0.90), (8, 'OPTO-BRD', 'opto board', 46.00), (9, 'DISP-DMD', 'DMD display panel', 310.00),
  (10, 'BALL-SET', 'pinball set (4)', 18.00), (11, 'POST-SLV', 'post sleeve', 1.20), (12, 'MECH-COIN', 'coin mech', 89.00),
  (13, 'JOY-8W', '8-way joystick', 29.00), (14, 'BTN-30', 'arcade button 30mm', 3.50), (15, 'CRT-CAP', 'monitor cap kit', 22.00),
  (16, 'BLOWER', 'air hockey blower motor', 140.00), (17, 'CLAW-CL', 'claw coil', 35.00), (18, 'TILT-BOB', 'tilt bob and ring', 9.50),
  (19, 'SCORE-RL', 'score reel', 64.00), (20, 'CHIME', 'chime unit', 52.00);
SELECT setval('parts_id_seq', 20);

-- the days each machine stood on a floor (the 180 days before the anchor)
CREATE TEMP TABLE floor_days AS
SELECT s.machine_id, s.venue_id, g.n, pg_temp.ago(g.n) AS day
FROM stints s JOIN generate_series(1, 180) g(n)
  ON pg_temp.ago(g.n) >= s.from_d AND (s.to_d IS NULL OR pg_temp.ago(g.n) < s.to_d)
WHERE s.venue_id IS NOT NULL;

CREATE TEMP TABLE tk AS
SELECT fd.machine_id, fd.venue_id, fd.n, mm.machine_type, mm.era, mm.id AS model_id, kk.key,
       pg_temp.at(fd.day, 600 + pg_temp.r('tkt' || kk.key) * 600) AS opened_at,
       floor(pg_temp.r('tkd' || kk.key) ^ 2 * 8)::int AS days_to_fix,
       pg_temp.r('tkdown' || kk.key) < 0.3 AS machine_down
FROM floor_days fd
JOIN machines m ON m.id = fd.machine_id
JOIN machine_models mm ON mm.id = m.model_id
CROSS JOIN LATERAL (SELECT fd.machine_id || '.' || fd.n AS key) kk
WHERE fd.machine_id <> (SELECT machine_id FROM story WHERE name = 'dying')
  AND pg_temp.r('tk' || kk.key) < CASE WHEN mm.machine_type = 'video' THEN 0.02 WHEN mm.machine_type = 'other' THEN 0.012
                                       ELSE CASE mm.era WHEN 'EM' THEN 0.045 WHEN 'SS' THEN 0.035 WHEN 'DMD' THEN 0.028 ELSE 0.014 END END;

INSERT INTO service_tickets (machine_id, opened_at, closed_at, reported_by, symptom, resolution, technician_id, labour_hours, machine_down, status, status_history)
SELECT machine_id, opened_at, closed_at, reported_by, symptom,
       CASE WHEN closed_at IS NOT NULL THEN pg_temp.pick('res' || key, ARRAY['replaced coil sleeve', 'rebuilt flipper', 'cleaned and adjusted switch',
         'replaced rubber ring kit', 'reflowed solder on connector', 'replaced LED board', 'adjusted tilt bob', 'replaced fuse',
         'cleaned playfield and waxed', 'no fault found', 'replaced opto board', 'reseated connectors']) END,
       tf.tech, round((0.5 + pg_temp.r('lab' || key) * 3.5)::numeric, 1), machine_down,
       CASE WHEN closed_at IS NULL THEN pg_temp.pick('st' || key, ARRAY['open', 'in progress', 'waiting_parts'])
            WHEN pg_temp.r('stc' || key) < 0.15 THEN 'Closed' WHEN pg_temp.r('stc' || key) < 0.3 THEN 'done' ELSE 'closed' END,
       jsonb_build_array(jsonb_build_object('status', 'open', 'at', opened_at))
         || CASE WHEN closed_at IS NOT NULL OR days_to_fix > 0
                 THEN jsonb_build_array(jsonb_build_object('status', 'in progress', 'at', opened_at + interval '3 hours')) ELSE '[]' END
         || CASE WHEN closed_at IS NOT NULL THEN jsonb_build_array(jsonb_build_object('status', 'closed', 'at', closed_at)) ELSE '[]' END
FROM (
  SELECT tk.*,
         CASE WHEN tk.opened_at + make_interval(days => days_to_fix, hours => 2 + floor(pg_temp.r('tkc' || key) * 6)::int)
                   < pg_temp.ago(0)::timestamptz
              THEN tk.opened_at + make_interval(days => days_to_fix, hours => 2 + floor(pg_temp.r('tkc' || key) * 6)::int) END AS closed_at,
         pg_temp.pick('rep' || key, ARRAY['staff', 'staff', 'customer', 'technician', 'league player']) AS reported_by,
         CASE machine_type
           WHEN 'video' THEN pg_temp.pick('sym' || key, ARRAY['monitor colours off', 'joystick dead (P1)', 'coin mech rejects 1 EUR', 'button 2 sticky', 'screen goes black after 10 min'])
           WHEN 'other' THEN pg_temp.pick('sym' || key, ARRAY['puck blower weak', 'claw grip too weak', 'ticket dispenser empty', 'coin mech jammed'])
           ELSE pg_temp.pick('sym' || key, ARRAY['left flipper weak', 'right flipper sticks up', 'ball stuck in VUK', 'pop bumper not firing',
                  'display flickering', 'drop targets not resetting', 'ramp diverter stuck', 'trough jam, ball search loops',
                  'rubber broken on left sling', 'coin door lock jammed', 'shooter lane feeds two balls', 'magnet too weak',
                  'GI lights out on lower playfield', 'tilt bob too sensitive', 'switch matrix error (row 3)', 'playfield wear near inlanes',
                  'speaker buzzing'] || CASE WHEN era = 'EM' THEN ARRAY['score reel stuck', 'chimes not ringing', 'stepper unit skipping'] ELSE '{}' END)
         END AS symptom
  FROM tk
) t
JOIN tech_for tf ON tf.venue_id = t.venue_id
ORDER BY opened_at;

-- story 1: the dying Haunted Lighthouse. A failed fix four weeks ago, then the machine is taken down 10 days ago.
INSERT INTO service_tickets (machine_id, opened_at, closed_at, reported_by, symptom, resolution, technician_id, labour_hours, machine_down, status, status_history)
SELECT s.machine_id, o, c, rep, sym, res, 3, lab, down, st, hist
FROM story s, LATERAL (VALUES
  (pg_temp.at(pg_temp.ago(30), 1020), pg_temp.at(pg_temp.ago(29), 840), 'league player', 'left flipper weak',
   'adjusted EOS switch', 1.0, false, 'closed',
   jsonb_build_array(jsonb_build_object('status', 'open', 'at', pg_temp.at(pg_temp.ago(30), 1020)),
                     jsonb_build_object('status', 'closed', 'at', pg_temp.at(pg_temp.ago(29), 840)))),
  (pg_temp.at(pg_temp.ago(10), 1110), NULL::timestamptz, 'staff', 'left flipper coil getting hot, flipper fades during play',
   NULL::text, 2.5, true, 'waiting_parts',
   jsonb_build_array(jsonb_build_object('status', 'open', 'at', pg_temp.at(pg_temp.ago(10), 1110)),
                     jsonb_build_object('status', 'in progress', 'at', pg_temp.at(pg_temp.ago(9), 660)),
                     jsonb_build_object('status', 'waiting_parts', 'at', pg_temp.at(pg_temp.ago(8), 720))))
) v(o, c, rep, sym, res, lab, down, st, hist)
WHERE s.name = 'dying';

INSERT INTO ticket_parts (ticket_id, part_id, qty)
SELECT DISTINCT ON (t.id, p.part_id) t.id, p.part_id, 1 + (pg_temp.r('qty' || t.id || '.' || p.part_id) < 0.2)::int
FROM service_tickets t
JOIN machines m ON m.id = t.machine_id JOIN machine_models mm ON mm.id = m.model_id
CROSS JOIN LATERAL generate_series(1, floor(pg_temp.r('np' || t.id) * 3)::int) j
CROSS JOIN LATERAL (SELECT CASE mm.machine_type
    WHEN 'video' THEN (ARRAY[12, 13, 14, 15, 7])[1 + floor(pg_temp.r('pp' || t.id || '.' || j) * 5)::int]
    WHEN 'other' THEN (ARRAY[16, 17, 12, 7])[1 + floor(pg_temp.r('pp' || t.id || '.' || j) * 4)::int]
    ELSE (ARRAY[1, 2, 3, 4, 5, 6, 7, 8, 10, 11, 18] || CASE WHEN mm.era = 'EM' THEN ARRAY[19, 20] WHEN mm.era = 'DMD' THEN ARRAY[9] ELSE '{}' END)
         [1 + floor(pg_temp.r('pp' || t.id || '.' || j) * (11 + CASE WHEN mm.era = 'EM' THEN 2 WHEN mm.era = 'DMD' THEN 1 ELSE 0 END))::int] END AS part_id) p
WHERE t.closed_at IS NOT NULL AND t.resolution NOT IN ('no fault found', 'cleaned and adjusted switch', 'adjusted tilt bob');
INSERT INTO ticket_parts (ticket_id, part_id, qty)
SELECT id, 5, 1 FROM service_tickets WHERE resolution = 'adjusted EOS switch';

-- --- games ----------------------------------------------------------------------------------------------------------
-- one row per machine per playable day: the expected number of games
CREATE TEMP TABLE mday AS
SELECT fd.machine_id, fd.venue_id, fd.n, fd.day, mm.id AS model_id, mm.machine_type, mm.era, m.price_per_play_cents AS price, h,
       floor(10.5 * vp.pop * mw.pop
         * CASE extract(isodow FROM fd.day)::int WHEN 1 THEN 0.55 WHEN 2 THEN 0.6 WHEN 3 THEN 0.7 WHEN 4 THEN 0.9 WHEN 5 THEN 1.4 WHEN 6 THEN 1.8 ELSE 1.1 END
         * CASE WHEN fd.venue_id = 2 AND extract(isodow FROM fd.day) = 4 THEN 2.0 ELSE 1 END     -- Thursday league night
         * CASE WHEN fd.venue_id IN (5, 8) AND extract(month FROM fd.day) IN (6, 7, 8) THEN 1.4 ELSE 1 END  -- summer at the coast
         * CASE WHEN fd.machine_id = st.machine_id THEN CASE WHEN fd.n > 42 THEN 1 WHEN fd.n > 21 THEN 1 - 0.7 * (42 - fd.n) / 21.0 ELSE 0.3 END ELSE 1 END
         * (0.5 + pg_temp.r('n.' || fd.machine_id || '.' || fd.n)))::int AS n_games
FROM floor_days fd
JOIN machines m ON m.id = fd.machine_id
JOIN machine_models mm ON mm.id = m.model_id
JOIN mweight mw ON mw.model_id = mm.id
JOIN vpop vp ON vp.venue_id = fd.venue_id
JOIN venues v ON v.id = fd.venue_id
CROSS JOIN (SELECT machine_id FROM story WHERE name = 'dying') st
CROSS JOIN LATERAL (SELECT v.hours -> to_char(fd.day, 'dy') AS h) hh
WHERE h IS NOT NULL AND h <> 'null'::jsonb
  AND NOT EXISTS (SELECT FROM service_tickets t WHERE t.machine_id = fd.machine_id AND t.machine_down
                  AND fd.day > (t.opened_at AT TIME ZONE 'Europe/Ljubljana')::date
                  AND fd.day < coalesce((t.closed_at AT TIME ZONE 'Europe/Ljubljana')::date, pg_temp.ago(-1)));

INSERT INTO games (machine_id, venue_id, player_id, started_at, duration_s, score, balls_played, extra_balls, multiballs, tilted, credits_used)
SELECT machine_id, venue_id, player_id, started_at,
       greatest(20, round(dur * CASE WHEN tilted THEN 0.6 ELSE 1 END))::int,
       score, balls, extra, multi, tilted, credits
FROM (
  SELECT md.machine_id, md.venue_id, pl.player_id,
         pg_temp.at(md.day, o + (c - o - 20) * ((pg_temp.r('t1' || k) + pg_temp.r('t2' || k) + pg_temp.r('t3' || k)) / 3) ^ 0.75) AS started_at,
         CASE md.machine_type
           WHEN 'pinball' THEN CASE md.era WHEN 'EM' THEN 270 WHEN 'SS' THEN 165 WHEN 'DMD' THEN 200 ELSE 235 END * sk.skill ^ 0.6
           WHEN 'video' THEN 150 * sk.skill ^ 0.5 ELSE 90 END * exp(0.35 * pg_temp.z('d' || k)) AS dur,
         CASE WHEN md.machine_type = 'pinball' OR md.machine_type = 'video' THEN
                (round(CASE md.era WHEN 'EM' THEN 120000 WHEN 'SS' THEN 900000 WHEN 'DMD' THEN 180000000 WHEN 'LCD' THEN 45000000 ELSE 85000 END
                       * exp(0.4 * pg_temp.z('scale' || md.model_id)) * sk.skill ^ 1.3 * exp(0.55 * pg_temp.z('s' || k)) / 10) * 10)::bigint
              WHEN md.model_id = 59 THEN floor(pg_temp.r('s' || k) * 8)::bigint
              WHEN md.model_id = 61 THEN least(900, round(sk.skill * pg_temp.r('s' || k) * 60) * 10)::bigint
         END AS score,
         CASE WHEN md.machine_type = 'pinball' THEN CASE WHEN md.era = 'EM' AND pg_temp.r('b5' || md.machine_id) < 0.5 THEN 5 ELSE 3 END END AS balls,
         CASE WHEN md.machine_type = 'pinball' THEN floor(pg_temp.r('x' || k) ^ 3 * 3 * least(1.5, sk.skill))::int END AS extra,
         CASE WHEN md.machine_type = 'pinball' THEN CASE md.era WHEN 'EM' THEN 0
                ELSE floor(pg_temp.r('mb' || k) * (CASE md.era WHEN 'SS' THEN 2 WHEN 'DMD' THEN 4 ELSE 5 END) * least(1.0, sk.skill / 1.5))::int END END AS multi,
         md.machine_type = 'pinball' AND pg_temp.r('tilt' || k) <
           CASE WHEN md.machine_id = (SELECT machine_id FROM story WHERE name = 'tilty') THEN 0.14 WHEN md.era = 'EM' THEN 0.05 ELSE 0.035 END AS tilted,
         CASE WHEN pg_temp.r('cr' || k) < 0.04 THEN 2 ELSE 1 END AS credits
  FROM mday md
  CROSS JOIN LATERAL generate_series(1, md.n_games) g
  CROSS JOIN LATERAL (SELECT md.machine_id || '.' || md.n || '.' || g AS k,
                             split_part(md.h ->> 0, ':', 1)::int * 60 AS o,
                             split_part(md.h ->> 1, ':', 1)::int * 60
                               + CASE WHEN split_part(md.h ->> 1, ':', 1)::int * 60 <= split_part(md.h ->> 0, ':', 1)::int * 60 THEN 1440 ELSE 0 END AS c) kk
  LEFT JOIN vcount vc ON vc.venue_id = md.venue_id
  CROSS JOIN LATERAL (SELECT CASE
      WHEN md.venue_id IN (1, 6, 7) AND pg_temp.r('pc' || k) < 0.012 THEN 1
      WHEN pg_temp.r('pk' || k) >= 0.45 THEN NULL
      WHEN pg_temp.r('ph' || k) < 0.85 AND vc.n > 0
        THEN (SELECT player_id FROM vplayers vpl WHERE vpl.venue_id = md.venue_id AND vpl.idx = 1 + floor(pg_temp.r('pi' || k) ^ 2.2 * vc.n)::int)
      -- anyone, skewed to a few travelling regulars (7919 is prime: this permutes 0..2499)
      ELSE 1 + (floor(pg_temp.r('pg' || k) ^ 1.6 * 2500)::int * 7919) % 2500 END AS player_id) pl
  LEFT JOIN pskill ps ON ps.player_id = pl.player_id
  CROSS JOIN LATERAL (SELECT coalesce(ps.skill, 0.75 * exp(0.3 * pg_temp.z('anon' || k))) AS skill) sk
) x
ORDER BY started_at, machine_id;

-- members can't play before they joined or after they were erased: those games are anonymous
UPDATE games g SET player_id = NULL FROM players p
WHERE p.id = g.player_id AND (g.started_at < p.member_since OR g.started_at >= p.deleted_at);

-- --- machine events ---------------------------------------------------------------------------------------------------
CREATE TEMP TABLE model_p90 AS
SELECT m.model_id, percentile_disc(0.9) WITHIN GROUP (ORDER BY g.score) AS p90
FROM games g JOIN machines m ON m.id = g.machine_id GROUP BY m.model_id;

INSERT INTO machine_events (machine_id, occurred_at, event_type, detail)
SELECT machine_id, occurred_at, event_type, detail FROM (
  SELECT g.machine_id, g.started_at + make_interval(secs => g.duration_s) AS occurred_at,
         CASE WHEN pg_temp.r('slam' || g.id) < 0.03 THEN 'slam_tilt' ELSE 'tilt' END AS event_type, NULL AS detail
  FROM games g WHERE g.tilted
  UNION ALL
  SELECT g.machine_id, g.started_at + make_interval(secs => g.duration_s * 0.6), 'jackpot', NULL
  FROM games g WHERE g.multiballs > 0 AND pg_temp.r('jp' || g.id) < 0.35
  UNION ALL
  SELECT g.machine_id, g.started_at + make_interval(secs => g.duration_s * 0.9), 'replay', NULL
  FROM games g JOIN machines m ON m.id = g.machine_id JOIN model_p90 p ON p.model_id = m.model_id
  JOIN machine_models mm ON mm.id = m.model_id
  WHERE mm.machine_type = 'pinball' AND g.score > p.p90
  UNION ALL
  SELECT g.machine_id, g.started_at + make_interval(secs => g.duration_s + 2), 'match', NULL
  FROM games g JOIN machines m ON m.id = g.machine_id JOIN machine_models mm ON mm.id = m.model_id
  WHERE mm.machine_type = 'pinball' AND pg_temp.r('match' || g.id) < 0.07
  UNION ALL
  -- per playable day: ball searches, coil and switch faults; the dying machine's coil faults rise for six weeks
  SELECT md.machine_id, pg_temp.at(md.day, 1020 + pg_temp.r('evt' || md.machine_id || '.' || md.n || '.' || e.kind || '.' || j) * 300),
         e.kind,
         CASE e.kind WHEN 'coil_fault' THEN pg_temp.pick('cd' || md.machine_id || '.' || md.n || '.' || j,
                       CASE WHEN md.machine_id = st.machine_id THEN ARRAY['flipper coil L1 overheating', 'flipper coil L1 current high', 'flipper L1 EOS open']
                            ELSE ARRAY['coil 12 current high', 'flipper coil R1 overheating', 'slingshot coil stuck on', 'pop bumper coil 3 open'] END)
                     WHEN 'switch_fault' THEN 'switch ' || (11 + floor(pg_temp.r('sw' || md.machine_id || '.' || md.n) * 60)::int) || ' not seen in 30 balls' END
  FROM mday md
  CROSS JOIN (SELECT machine_id FROM story WHERE name = 'dying') st
  CROSS JOIN LATERAL (VALUES
      ('ball_search', floor(pg_temp.r('bs' || md.machine_id || '.' || md.n) * CASE WHEN md.machine_id = st.machine_id AND md.n <= 42 THEN 4 ELSE 1.4 END)::int),
      ('coil_fault', CASE WHEN md.machine_id = st.machine_id AND md.n <= 42
                          THEN floor((0.2 + 2.5 * (42 - md.n) / 32.0) * (0.5 + pg_temp.r('cf' || md.machine_id || '.' || md.n)))::int
                          ELSE (pg_temp.r('cf' || md.machine_id || '.' || md.n) < CASE WHEN md.era IN ('EM', 'SS') THEN 0.015 ELSE 0.008 END)::int END),
      ('switch_fault', (pg_temp.r('sf' || md.machine_id || '.' || md.n) < 0.012)::int)
    ) e(kind, cnt)
  CROSS JOIN LATERAL generate_series(1, e.cnt) j
  WHERE md.machine_type = 'pinball'
) x
ORDER BY occurred_at, machine_id, event_type;

-- --- coin collections (weekly, per machine, Monday to Sunday) ---------------------------------------------------------
INSERT INTO coin_collections (machine_id, venue_id, week_start, coins_eur, tokens, card_plays, amount_eur, collected_by)
SELECT w.machine_id, w.venue_id, w.week_start, round(amount * 0.65, 2), round(amount * 0.05 / 0.5)::int,
       round(amount * 0.30 * 100 / price)::int, amount, tf.collector
FROM (
  SELECT g.machine_id, g.venue_id, date_trunc('week', g.started_at AT TIME ZONE 'Europe/Ljubljana')::date AS week_start, m.price_per_play_cents AS price,
         round((sum(g.credits_used * m.price_per_play_cents) / 100.0 * (1 + 0.015 * pg_temp.z('cash' || g.machine_id || '.' ||
               date_trunc('week', g.started_at AT TIME ZONE 'Europe/Ljubljana')::date)))::numeric, 2) AS amount
  FROM games g JOIN machines m ON m.id = g.machine_id
  GROUP BY 1, 2, 3, 4
) w
JOIN tech_for tf ON tf.venue_id = w.venue_id
WHERE w.week_start + 7 <= pg_temp.ago(0) AND w.week_start >= pg_temp.ago(180)
ORDER BY w.week_start, w.machine_id;

-- --- tournaments --------------------------------------------------------------------------------------------------------
INSERT INTO tournaments (id, name, venue_id, starts_on, ends_on, format, status, entry_fee_eur, max_players)
SELECT id, name, venue, pg_temp.ago(offs), pg_temp.ago(offs), fmt, CASE WHEN offs < 0 THEN 'registration' ELSE 'completed' END, fee, maxp
FROM (VALUES
  ( 1, 'Spring Flip Open',          1, 172, 'match_play', 15.00, 24),
  ( 2, 'Nova Gorica Farewell Flip', 9, 125, 'match_play', 10.00, 16),
  ( 3, 'Tilt Tuesday Cup',          2, 138, 'match_play', 10.00, 16),
  ( 4, 'Zagreb Silverball Classic', 6, 117, 'match_play', 15.00, 24),
  ( 5, 'Graz Flipper Festival',     7, 104, 'match_play', 20.00, 24),
  ( 6, 'Adriatic Summer Slam',      5,  89, 'match_play', 10.00, 16),
  ( 7, 'Trieste Bora Cup',          8,  75, 'match_play', 15.00, 16),
  ( 8, 'Celje Castle Cup',          4,  61, 'match_play', 10.00, 16),
  ( 9, 'Metelkova Mayhem',          1,  47, 'match_play', 15.00, 24),
  (10, 'EM Classics Night',         2,  32, 'best_game',  10.00, 16),
  (11, 'Goat Arcade Championship',  6,  17, 'match_play', 25.00, 32),
  (12, 'Goat Arcade Masters',       1,  -9, 'match_play', 25.00, 32)
) t(id, name, venue, offs, fmt, fee, maxp);
SELECT setval('tournaments_id_seq', 12);

INSERT INTO tournament_entries (tournament_id, player_id, seed, entered_at)
SELECT tournament_id, player_id, row_number() OVER (PARTITION BY tournament_id ORDER BY rating DESC, player_id),
       pg_temp.at(starts_on - 1 - floor(pg_temp.r('ent' || tournament_id || '.' || player_id) * 20)::int, 600 + pg_temp.r('entt' || player_id) * 600)
FROM (
  SELECT t.id AS tournament_id, t.starts_on, p.id AS player_id, p.rating,
         row_number() OVER (PARTITION BY t.id ORDER BY
           CASE WHEN p.id = 1 AND pg_temp.r('champ' || t.id) < 0.75 THEN -1
                ELSE pg_temp.r('te' || t.id || '.' || p.id) * 0.5 + CASE WHEN p.home_venue_id = t.venue_id THEN 0 ELSE 0.85 END
                     - least(0.3, p.rating / 8000.0) END) AS rn,
         floor(t.max_players * CASE WHEN t.status = 'registration' THEN 0.6 ELSE 0.75 + 0.25 * pg_temp.r('fill' || t.id) END) AS n
  FROM tournaments t JOIN players p ON p.deleted_at IS NULL AND p.member_since < t.starts_on - 30
) x WHERE rn <= n;

-- four rounds in groups of four; each group plays one machine that stood at the venue that day
CREATE TEMP TABLE tgroups AS
SELECT e.tournament_id, r.round, e.player_id,
       (row_number() OVER (PARTITION BY e.tournament_id, r.round ORDER BY pg_temp.r('grp' || e.tournament_id || '.' || r.round || '.' || e.player_id)) - 1) / 4 + 1 AS group_no
FROM tournament_entries e JOIN tournaments t ON t.id = e.tournament_id AND t.status = 'completed'
CROSS JOIN generate_series(1, 4) r(round);

CREATE TEMP TABLE tmachines AS
SELECT t.id AS tournament_id, s.machine_id, row_number() OVER (PARTITION BY t.id ORDER BY s.machine_id) AS idx, count(*) OVER (PARTITION BY t.id) AS n
FROM tournaments t
JOIN stints s ON s.venue_id = t.venue_id AND s.from_d <= t.starts_on AND (s.to_d IS NULL OR s.to_d > t.starts_on)
JOIN machines m ON m.id = s.machine_id JOIN machine_models mm ON mm.id = m.model_id
WHERE mm.machine_type = 'pinball' AND (t.format <> 'best_game' OR mm.era = 'EM');

INSERT INTO tournament_matches (tournament_id, round, group_no, machine_id, player_id, score, points)
SELECT tournament_id, round, group_no, machine_id, player_id, score,
       (ARRAY[7, 5, 3, 1])[(rank() OVER (PARTITION BY tournament_id, round, group_no ORDER BY score DESC, player_id))::int] AS points
FROM (
  SELECT g.tournament_id, g.round, g.group_no, tm.machine_id, g.player_id,
         (round(CASE mm.era WHEN 'EM' THEN 120000 WHEN 'SS' THEN 900000 WHEN 'DMD' THEN 180000000 ELSE 45000000 END
                * exp(0.4 * pg_temp.z('scale' || mm.id)) * ps.skill ^ 1.3
                * exp(0.55 * pg_temp.z('ts' || g.tournament_id || '.' || g.round || '.' || g.player_id)) / 10) * 10)::bigint AS score
  FROM tgroups g
  JOIN tmachines tm ON tm.tournament_id = g.tournament_id
   AND tm.idx = 1 + floor(pg_temp.r('tmach' || g.tournament_id || '.' || g.round || '.' || g.group_no) * tm.n)::int
  JOIN machines m ON m.id = tm.machine_id JOIN machine_models mm ON mm.id = m.model_id
  JOIN pskill ps ON ps.player_id = g.player_id
) x
ORDER BY tournament_id, round, group_no, points;
