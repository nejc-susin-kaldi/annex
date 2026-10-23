"""The team list: people a lead can be assigned to. Stored in the app's own Postgres."""
from __future__ import annotations

import os
from dataclasses import dataclass

import psycopg
from psycopg.rows import class_row

SCHEMA = """
CREATE TABLE IF NOT EXISTS team_members (
    id        serial PRIMARY KEY,
    name      text NOT NULL,
    email     text NOT NULL UNIQUE,
    slack_id  text NOT NULL DEFAULT '',
    added_by  text NOT NULL DEFAULT '',
    added_at  timestamptz NOT NULL DEFAULT now()
)
"""

_schema_ready = False


@dataclass
class Member:
    id: int
    name: str
    email: str
    slack_id: str
    added_by: str


def _connect() -> psycopg.Connection:
    global _schema_ready
    conn = psycopg.connect(os.environ["DATABASE_URL"])
    if not _schema_ready:
        conn.execute(SCHEMA)
        conn.commit()
        _schema_ready = True
    return conn


def list_members() -> list[Member]:
    with _connect() as conn, conn.cursor(row_factory=class_row(Member)) as cur:
        cur.execute("SELECT id, name, email, slack_id, added_by FROM team_members ORDER BY name")
        return cur.fetchall()


def get_member(member_id: int) -> Member | None:
    with _connect() as conn, conn.cursor(row_factory=class_row(Member)) as cur:
        cur.execute("SELECT id, name, email, slack_id, added_by FROM team_members WHERE id = %s",
                    (member_id,))
        return cur.fetchone()


class DuplicateEmail(Exception):
    pass


def add_member(name: str, email: str, slack_id: str, added_by: str) -> None:
    try:
        with _connect() as conn:
            conn.execute(
                "INSERT INTO team_members (name, email, slack_id, added_by) VALUES (%s, %s, %s, %s)",
                (name, email, slack_id, added_by),
            )
    except psycopg.errors.UniqueViolation:
        raise DuplicateEmail(email) from None


def remove_member(member_id: int) -> None:
    with _connect() as conn:
        conn.execute("DELETE FROM team_members WHERE id = %s", (member_id,))
