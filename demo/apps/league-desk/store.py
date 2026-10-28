"""Desk sign-ups, in the app's own database (DATABASE_URL). The gateway is read-only, so sign-ups the club system
doesn't have yet live here until the club system catches up (then staff withdraw the desk copy)."""
import os

import psycopg
from psycopg.rows import dict_row

SCHEMA = """
CREATE TABLE IF NOT EXISTS signups (
  id            bigserial PRIMARY KEY,
  tournament_id integer     NOT NULL,
  nickname      text        NOT NULL,
  signed_up_by  text        NOT NULL,
  signed_up_at  timestamptz NOT NULL DEFAULT now(),
  withdrawn_at  timestamptz,
  withdrawn_by  text
);
CREATE UNIQUE INDEX IF NOT EXISTS signups_one_pending
  ON signups (tournament_id, lower(nickname)) WHERE withdrawn_at IS NULL;
"""

_ready = False


class Full(Exception):
    """No places left."""


class Duplicate(Exception):
    """Already a pending sign-up for this tournament."""


def _connect() -> psycopg.Connection:
    global _ready
    conn = psycopg.connect(os.environ["DATABASE_URL"], connect_timeout=5, row_factory=dict_row)
    if not _ready:
        conn.execute(SCHEMA)
        conn.commit()
        _ready = True
    return conn


def pending(tournament_id: int) -> list[dict]:
    with _connect() as conn:
        return conn.execute("""SELECT id, nickname, signed_up_by, signed_up_at,
                                      signed_up_at AT TIME ZONE 'Europe/Ljubljana' AS local_at FROM signups
                               WHERE tournament_id = %s AND withdrawn_at IS NULL ORDER BY signed_up_at, id""",
                            (tournament_id,)).fetchall()


def get(signup_id: int) -> dict | None:
    with _connect() as conn:
        return conn.execute("SELECT * FROM signups WHERE id = %s", (signup_id,)).fetchone()


def add(tournament_id: int, nickname: str, by: str, free_places: int) -> tuple[int, int]:
    """Store a pending sign-up; returns (id, pending count after it). `free_places` is max_players minus the club
    system's entries. One transaction under a per-tournament lock, so two desks can't take the last place twice.
    Raises Duplicate or Full."""
    with _connect() as conn, conn.transaction():
        conn.execute("SELECT pg_advisory_xact_lock(%s)", (tournament_id,))
        n = conn.execute("SELECT count(*) AS n FROM signups WHERE tournament_id = %s AND withdrawn_at IS NULL",
                         (tournament_id,)).fetchone()["n"]
        if conn.execute("""SELECT 1 FROM signups WHERE tournament_id = %s AND lower(nickname) = lower(%s)
                           AND withdrawn_at IS NULL""", (tournament_id, nickname)).fetchone():
            raise Duplicate()
        if n >= free_places:
            raise Full()
        row = conn.execute("""INSERT INTO signups (tournament_id, nickname, signed_up_by) VALUES (%s, %s, %s)
                              RETURNING id""", (tournament_id, nickname, by)).fetchone()
        return row["id"], n + 1


def withdraw(signup_id: int, by: str) -> dict | None:
    """Mark a pending sign-up withdrawn (the row stays, with who and when). None if it wasn't pending."""
    with _connect() as conn:
        return conn.execute("""UPDATE signups SET withdrawn_at = now(), withdrawn_by = %s
                               WHERE id = %s AND withdrawn_at IS NULL RETURNING id, tournament_id, nickname""",
                            (by, signup_id)).fetchone()
