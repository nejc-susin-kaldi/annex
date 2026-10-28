"""The app's own data in its Postgres (DATABASE_URL): "I'm on it" claims and the watch job's last state."""
import os

import psycopg
from psycopg.rows import dict_row

KINDS = ("machine", "ticket")

SCHEMA = """
CREATE TABLE IF NOT EXISTS claims (
  id           serial PRIMARY KEY,
  kind         text NOT NULL CHECK (kind IN ('machine', 'ticket')),
  ref_id       int NOT NULL,
  login        text NOT NULL,
  name         text NOT NULL,
  claimed_at   timestamptz NOT NULL DEFAULT now(),
  released_at  timestamptz,
  released_by  text
);
CREATE UNIQUE INDEX IF NOT EXISTS claims_one_open ON claims (kind, ref_id) WHERE released_at IS NULL;
CREATE TABLE IF NOT EXISTS watch_state (
  machine_id   int PRIMARY KEY,
  title        text NOT NULL,
  venue_code   text NOT NULL,
  seen_at      timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS watch_runs (
  id           serial PRIMARY KEY,
  ran_at       timestamptz NOT NULL DEFAULT now(),
  out_of_order int NOT NULL,
  newly_out    int NOT NULL,
  posted       boolean NOT NULL
);
"""


def connect() -> psycopg.Connection:
    # autocommit: every statement stands alone, and `with conn.transaction()` is a real BEGIN/COMMIT
    global _schema_ready
    conn = psycopg.connect(os.environ["DATABASE_URL"], row_factory=dict_row, connect_timeout=5, autocommit=True)
    if not _schema_ready:
        conn.execute(SCHEMA)
        _schema_ready = True
    return conn


_schema_ready = False  # once per process


def open_claims(conn) -> dict[tuple[str, int], dict]:
    rows = conn.execute("SELECT kind, ref_id, login, name, claimed_at FROM claims WHERE released_at IS NULL").fetchall()
    return {(r["kind"], r["ref_id"]): r for r in rows}


def claim(conn, kind: str, ref_id: int, login: str, name: str) -> dict | None:
    """Claim it for `login`. Returns the open claim: the new one, or someone else's that was there first."""
    with conn.transaction():
        conn.execute("INSERT INTO claims (kind, ref_id, login, name) VALUES (%s, %s, %s, %s) "
                     "ON CONFLICT (kind, ref_id) WHERE released_at IS NULL DO NOTHING", (kind, ref_id, login, name))
        return conn.execute("SELECT login, name, claimed_at FROM claims WHERE kind = %s AND ref_id = %s "
                            "AND released_at IS NULL", (kind, ref_id)).fetchone()


def release(conn, kind: str, ref_id: int, login: str) -> dict | None:
    """Release the open claim (anyone on the team may: someone off sick shouldn't block a machine).
    Returns the released claim, or None if there was none."""
    with conn.transaction():
        return conn.execute("UPDATE claims SET released_at = now(), released_by = %s WHERE kind = %s AND ref_id = %s "
                            "AND released_at IS NULL RETURNING login, name, claimed_at",
                            (login, kind, ref_id)).fetchone()


def watch_previous(conn) -> dict[int, dict] | None:
    """The machines out of order at the last watch run, or None if the watch has never run."""
    if conn.execute("SELECT 1 FROM watch_runs LIMIT 1").fetchone() is None:
        return None
    return {r["machine_id"]: r for r in conn.execute("SELECT machine_id, title, venue_code FROM watch_state")}


def watch_save(conn, current: dict[int, dict], newly_out: int, posted: bool) -> None:
    """Replace the saved state with `current` ({machine_id: {title, venue_code}}) and record the run."""
    with conn.transaction():
        conn.execute("DELETE FROM watch_state WHERE NOT (machine_id = ANY(%s))", (list(current),))
        for mid, m in current.items():
            conn.execute("INSERT INTO watch_state (machine_id, title, venue_code) VALUES (%s, %s, %s) "
                         "ON CONFLICT (machine_id) DO UPDATE SET title = EXCLUDED.title, venue_code = EXCLUDED.venue_code",
                         (mid, m["title"], m["venue_code"]))
        conn.execute("INSERT INTO watch_runs (out_of_order, newly_out, posted) VALUES (%s, %s, %s)",
                     (len(current), newly_out, posted))
