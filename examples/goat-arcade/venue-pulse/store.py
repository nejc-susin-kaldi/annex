"""Each login's chosen venue, in the app's own database (DATABASE_URL)."""
import os

import psycopg

SCHEMA = """
CREATE TABLE IF NOT EXISTS venue_choice (
  login      text PRIMARY KEY,
  venue_code text NOT NULL,
  chosen_at  timestamptz NOT NULL DEFAULT now()
)"""

_ready = False


def _connect() -> psycopg.Connection:
    global _ready
    conn = psycopg.connect(os.environ["DATABASE_URL"], connect_timeout=5)
    if not _ready:
        conn.execute(SCHEMA)
        conn.commit()
        _ready = True
    return conn


def get_choice(login: str) -> str | None:
    with _connect() as conn:
        row = conn.execute("SELECT venue_code FROM venue_choice WHERE login = %s", (login,)).fetchone()
        return row[0] if row else None


def set_choice(login: str, venue_code: str) -> None:
    with _connect() as conn:
        conn.execute("""INSERT INTO venue_choice (login, venue_code) VALUES (%s, %s)
                        ON CONFLICT (login) DO UPDATE SET venue_code = EXCLUDED.venue_code, chosen_at = now()""",
                     (login, venue_code))
