-- Goat Arcade d.o.o.: the client's production schema (synthetic). Written the way a real one grows: some
-- legacy columns, status kept as free text, soft deletes, JSON where someone was in a hurry. Annex never
-- changes it; the gateway's curated views (platform/db/product/clientlink/api_views_arcade.sql) clean it up.
CREATE SCHEMA arcade;
SET search_path = arcade;

CREATE TABLE venues (
  id               serial PRIMARY KEY,
  code             text NOT NULL UNIQUE,          -- LJ1, MB, ...
  name             text NOT NULL,
  city             text NOT NULL,
  country          char(2) NOT NULL,
  lat              numeric(8,5),
  lon              numeric(8,5),
  tz               text NOT NULL DEFAULT 'Europe/Ljubljana',
  opened_on        date NOT NULL,
  closed_on        date,
  monthly_rent_eur numeric(10,2),
  hours            jsonb,                         -- {"mon": null, "tue": ["16:00", "24:00"], ...}; null = closed
  created_at       timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE manufacturers (
  id      serial PRIMARY KEY,
  name    text NOT NULL UNIQUE,
  country char(2),
  founded int,
  defunct boolean NOT NULL DEFAULT false
);

CREATE TABLE machine_models (
  id              serial PRIMARY KEY,
  title           text NOT NULL,
  manufacturer_id int NOT NULL REFERENCES manufacturers,
  release_year    int,
  era             text,                           -- pinball only: EM, SS, DMD, LCD
  machine_type    text NOT NULL DEFAULT 'pinball', -- pinball, video, other
  theme           text,
  designer        text,
  flippers        smallint,
  max_multiball   smallint,
  features        jsonb
);

CREATE TABLE machines (
  id                   serial PRIMARY KEY,
  serial_no            text NOT NULL UNIQUE,
  model_id             int NOT NULL REFERENCES machine_models,
  venue_id             int REFERENCES venues,     -- NULL: in storage
  installed_at         date,
  condition            text,                      -- free text, typed by whoever was there
  price_per_play_cents int NOT NULL DEFAULT 100,
  cab_no               text,                      -- legacy cabinet number from the old system, mostly NULL
  notes                text,
  is_deleted           boolean NOT NULL DEFAULT false, -- sold or scrapped
  updated_at           timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE machine_moves (
  id            serial PRIMARY KEY,
  machine_id    int NOT NULL REFERENCES machines,
  from_venue_id int REFERENCES venues,            -- NULL: from storage or new
  to_venue_id   int REFERENCES venues,            -- NULL: to storage
  moved_on      date NOT NULL,
  reason        text
);

CREATE TABLE players (
  id               serial PRIMARY KEY,
  handle           char(3) NOT NULL,              -- initials as on a high-score table; not unique
  nickname         text NOT NULL UNIQUE,
  full_name        text,
  email            text,
  phone            text,
  birth_date       date,
  home_venue_id    int REFERENCES venues,
  member_since     date,
  rating           int,
  marketing_opt_in boolean NOT NULL DEFAULT false,
  deleted_at       timestamptz                    -- GDPR erasure: personal fields are NULL after it
);

CREATE TABLE games (
  id           bigserial PRIMARY KEY,
  machine_id   int NOT NULL REFERENCES machines,
  venue_id     int NOT NULL REFERENCES venues,    -- where the machine stood when the game was played
  player_id    int REFERENCES players,            -- NULL: anonymous (no member card tapped)
  started_at   timestamptz NOT NULL,
  duration_s   int,
  score        bigint,
  balls_played smallint,
  extra_balls  smallint,
  multiballs   smallint,
  tilted       boolean NOT NULL DEFAULT false,
  credits_used smallint NOT NULL DEFAULT 1
);
CREATE INDEX games_machine_started ON games (machine_id, started_at);
CREATE INDEX games_started ON games (started_at);
CREATE INDEX games_player ON games (player_id) WHERE player_id IS NOT NULL;

CREATE TABLE machine_events (
  id          bigserial PRIMARY KEY,
  machine_id  int NOT NULL REFERENCES machines,
  occurred_at timestamptz NOT NULL,
  event_type  text NOT NULL,                      -- tilt, slam_tilt, ball_search, coil_fault, switch_fault, jackpot, replay, match
  detail      text
);
CREATE INDEX machine_events_machine_at ON machine_events (machine_id, occurred_at);

CREATE TABLE technicians (
  id              serial PRIMARY KEY,
  full_name       text NOT NULL,
  email           text,
  phone           text,
  hourly_rate_eur numeric(6,2),
  base_venue_id   int REFERENCES venues,
  active          boolean NOT NULL DEFAULT true
);

CREATE TABLE service_tickets (
  id             serial PRIMARY KEY,
  machine_id     int NOT NULL REFERENCES machines,
  opened_at      timestamptz NOT NULL,
  closed_at      timestamptz,
  reported_by    text,
  symptom        text NOT NULL,
  resolution     text,
  technician_id  int REFERENCES technicians,
  labour_hours   numeric(4,1),
  machine_down   boolean NOT NULL DEFAULT false,  -- taken off the floor until fixed
  status         text NOT NULL,                   -- open, in progress, waiting_parts, closed (also Closed, done: legacy)
  status_history jsonb
);
CREATE INDEX service_tickets_machine ON service_tickets (machine_id);

CREATE TABLE parts (
  id            serial PRIMARY KEY,
  sku           text NOT NULL UNIQUE,
  name          text NOT NULL,
  unit_cost_eur numeric(8,2) NOT NULL
);

CREATE TABLE ticket_parts (
  ticket_id int NOT NULL REFERENCES service_tickets,
  part_id   int NOT NULL REFERENCES parts,
  qty       int NOT NULL DEFAULT 1,
  PRIMARY KEY (ticket_id, part_id)
);

CREATE TABLE coin_collections (
  id           serial PRIMARY KEY,
  machine_id   int NOT NULL REFERENCES machines,
  venue_id     int NOT NULL REFERENCES venues,
  week_start   date NOT NULL,                     -- Monday
  coins_eur    numeric(10,2),
  tokens       int,                               -- 1 token = 0.50 EUR
  card_plays   int,                               -- plays paid by member card
  amount_eur   numeric(10,2) NOT NULL,            -- counted total; can differ a little from the plays
  collected_by text
);

CREATE TABLE tournaments (
  id            serial PRIMARY KEY,
  name          text NOT NULL,
  venue_id      int NOT NULL REFERENCES venues,
  starts_on     date NOT NULL,
  ends_on       date,
  format        text NOT NULL,                    -- match_play, best_game
  status        text NOT NULL,                    -- registration, completed
  entry_fee_eur numeric(6,2),
  max_players   int
);

CREATE TABLE tournament_entries (
  tournament_id int NOT NULL REFERENCES tournaments,
  player_id     int NOT NULL REFERENCES players,
  seed          int,
  entered_at    timestamptz,
  PRIMARY KEY (tournament_id, player_id)
);

CREATE TABLE tournament_matches (
  id            serial PRIMARY KEY,
  tournament_id int NOT NULL REFERENCES tournaments,
  round         int NOT NULL,
  group_no      int NOT NULL,
  machine_id    int NOT NULL REFERENCES machines,
  player_id     int NOT NULL REFERENCES players,
  score         bigint,
  points        int                               -- 7, 5, 3, 1 by place in the group of four
);
