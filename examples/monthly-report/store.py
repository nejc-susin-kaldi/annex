"""Run history in the app's own database (DATABASE_URL) and report files in /data."""
import os
from pathlib import Path

import psycopg
from psycopg.rows import dict_row

REPORTS = Path(os.environ.get("DATA_DIR", "/tmp")) / "reports"

SCHEMA = """
CREATE TABLE IF NOT EXISTS report_runs (
  id              serial PRIMARY KEY,
  ran_at          timestamptz NOT NULL DEFAULT now(),
  month_end       date NOT NULL,
  clients         int NOT NULL,
  positions       int NOT NULL,
  total_value_eur numeric(16, 2) NOT NULL,
  file            text NOT NULL,
  duration_ms     int NOT NULL
)"""


def connect() -> psycopg.Connection:
    conn = psycopg.connect(os.environ["DATABASE_URL"], row_factory=dict_row, connect_timeout=5)
    conn.execute(SCHEMA)
    conn.commit()
    return conn
