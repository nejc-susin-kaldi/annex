"""Run history in the app's own database (DATABASE_URL) and the CSV files in /data/takings.

One row per week: running the job again for a week replaces that week's row (and its CSV) and counts the run.
"""
import os
from pathlib import Path

import psycopg
from psycopg.rows import dict_row

FILES = Path(os.environ.get("DATA_DIR", "/tmp")) / "takings"

SCHEMA = """
CREATE TABLE IF NOT EXISTS takings_runs (
  week_start     date PRIMARY KEY,
  ran_at         timestamptz NOT NULL DEFAULT now(),
  first_ran_at   timestamptz NOT NULL DEFAULT now(),
  runs           int NOT NULL DEFAULT 1,
  data_through   date NOT NULL,
  venues         int NOT NULL,
  total_eur      numeric(12, 2) NOT NULL,
  prev_total_eur numeric(12, 2),
  file           text NOT NULL,
  emailed_to     text NOT NULL DEFAULT '',
  duration_ms    int NOT NULL
)"""

UPSERT = """
INSERT INTO takings_runs (week_start, data_through, venues, total_eur, prev_total_eur, file, duration_ms)
VALUES (%(week_start)s, %(data_through)s, %(venues)s, %(total_eur)s, %(prev_total_eur)s, %(file)s, %(duration_ms)s)
ON CONFLICT (week_start) DO UPDATE SET
  ran_at = now(), runs = takings_runs.runs + 1, data_through = EXCLUDED.data_through, venues = EXCLUDED.venues,
  total_eur = EXCLUDED.total_eur, prev_total_eur = EXCLUDED.prev_total_eur, file = EXCLUDED.file,
  emailed_to = '', duration_ms = EXCLUDED.duration_ms
RETURNING runs"""

# Times are shown in Ljubljana time; the database converts, so the image needs no time zone data.
SELECT = "SELECT *, ran_at AT TIME ZONE 'Europe/Ljubljana' AS ran_local FROM takings_runs"


def connect() -> psycopg.Connection:
    conn = psycopg.connect(os.environ["DATABASE_URL"], row_factory=dict_row, connect_timeout=5)
    conn.execute(SCHEMA)
    conn.commit()
    return conn


def record(run: dict) -> int:
    """Insert or replace the week's run; returns how many times the week has now been run."""
    with connect() as conn:
        return conn.execute(UPSERT, run).fetchone()["runs"]


def mark_emailed(week_start, to: list[str]) -> None:
    with connect() as conn:
        conn.execute("UPDATE takings_runs SET emailed_to = %s WHERE week_start = %s", (", ".join(to), week_start))


def history(limit: int = 26) -> list[dict]:
    with connect() as conn:
        return conn.execute(SELECT + " ORDER BY week_start DESC LIMIT %s", (limit,)).fetchall()


def run_for(week_start) -> dict | None:
    with connect() as conn:
        return conn.execute(SELECT + " WHERE week_start = %s", (week_start,)).fetchone()
