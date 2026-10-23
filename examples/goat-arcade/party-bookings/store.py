"""Party requests and their history, in the app's own Postgres (DATABASE_URL)."""
from __future__ import annotations

import os
from datetime import date

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from booking import Request

SCHEMA = """
CREATE TABLE IF NOT EXISTS bookings (
  id              serial PRIMARY KEY,
  event_id        text UNIQUE,                 -- Annex's webhook event id: a redelivery carries the same one
  received_at     timestamptz NOT NULL DEFAULT now(),
  status          text NOT NULL,               -- new, contacted, confirmed, declined, rejected
  reject_reason   text NOT NULL DEFAULT '',
  raw             jsonb,                       -- a rejected request's fields as sent, to see what went wrong
  name            text NOT NULL DEFAULT '',
  email           text NOT NULL DEFAULT '',
  phone           text NOT NULL DEFAULT '',
  preferred_venue text NOT NULL DEFAULT '',
  req_date        date,
  req_time        text NOT NULL DEFAULT '',
  kids            integer,
  age             integer,
  note            text NOT NULL DEFAULT '',
  venue           text NOT NULL DEFAULT '',    -- confirmed venue and slot
  slot_date       date,
  slot_time       text NOT NULL DEFAULT '',
  decline_reason  text NOT NULL DEFAULT '',
  updated_at      timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS bookings_status ON bookings (status, received_at DESC);
CREATE TABLE IF NOT EXISTS booking_history (
  id         serial PRIMARY KEY,
  booking_id integer NOT NULL REFERENCES bookings(id) ON DELETE CASCADE,
  at         timestamptz NOT NULL DEFAULT now(),
  login      text NOT NULL,                    -- Remote-User, or 'website' / 'party-bookings' for the app itself
  action     text NOT NULL,
  detail     text NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS booking_history_booking ON booking_history (booking_id, at);
"""

# new first, then what still needs work, then what is settled
STATUS_ORDER = "CASE status WHEN 'new' THEN 0 WHEN 'contacted' THEN 1 WHEN 'confirmed' THEN 2 " \
               "WHEN 'declined' THEN 3 ELSE 4 END"

_ready = False


def _connect() -> psycopg.Connection:
    global _ready
    conn = psycopg.connect(os.environ["DATABASE_URL"], connect_timeout=5, row_factory=dict_row)
    if not _ready:
        conn.execute(SCHEMA)
        conn.commit()
        _ready = True
    return conn


def ping() -> None:
    with _connect() as conn:
        conn.execute("SELECT 1")


def add_request(event_id: str, req: Request | None, problems: list[str], raw: dict) -> tuple[int | None, str]:
    """Store one incoming request. Returns (id, status), or (None, '') when this event_id was already stored
    (a redelivery or a replay: ignore it)."""
    status = "new" if req else "rejected"
    cols = {"event_id": event_id or None, "status": status}
    if req:
        cols.update(name=req.name, email=req.email, phone=req.phone, preferred_venue=req.preferred_venue,
                    req_date=req.date, req_time=req.time, kids=req.kids, age=req.age, note=req.note)
    else:
        cols.update(reject_reason="; ".join(problems), raw=Jsonb(raw), name=raw.get("name", "")[:120],
                    email=raw.get("email", "")[:200], preferred_venue=raw.get("preferred_venue", "")[:10].upper())
    names = ", ".join(cols)
    marks = ", ".join(["%s"] * len(cols))
    with _connect() as conn:
        row = conn.execute(f"INSERT INTO bookings ({names}) VALUES ({marks}) ON CONFLICT (event_id) DO NOTHING "
                           "RETURNING id", list(cols.values())).fetchone()
        if not row:
            return None, ""
        detail = "from the website form" if req else "rejected: " + "; ".join(problems)
        conn.execute("INSERT INTO booking_history (booking_id, login, action, detail) VALUES (%s, 'website', "
                     "'received', %s)", (row["id"], detail))
        return row["id"], status


def note(booking_id: int, login: str, action: str, detail: str = "") -> None:
    with _connect() as conn:
        conn.execute("INSERT INTO booking_history (booking_id, login, action, detail) VALUES (%s, %s, %s, %s)",
                     (booking_id, login, action, detail))


def list_bookings(venue: str = "", status: str = "", limit: int = 200) -> list[dict]:
    where, args = [], []
    if venue:
        where.append("(preferred_venue = %s OR venue = %s)")
        args += [venue, venue]
    if status == "open":
        where.append("status IN ('new', 'contacted')")
    elif status:
        where.append("status = %s")
        args.append(status)
    sql = ("SELECT b.*, (SELECT max(at) FROM booking_history h WHERE h.booking_id = b.id) AS last_at FROM bookings b"
           + (" WHERE " + " AND ".join(where) if where else "")
           + f" ORDER BY {STATUS_ORDER}, received_at DESC LIMIT %s")
    with _connect() as conn:
        return conn.execute(sql, args + [limit]).fetchall()


def counts() -> dict[str, int]:
    with _connect() as conn:
        return {r["status"]: r["n"] for r in
                conn.execute("SELECT status, count(*) AS n FROM bookings GROUP BY status").fetchall()}


def get(booking_id: int) -> dict | None:
    with _connect() as conn:
        return conn.execute("SELECT * FROM bookings WHERE id = %s", (booking_id,)).fetchone()


def history(booking_id: int) -> list[dict]:
    with _connect() as conn:
        return conn.execute("SELECT at, login, action, detail FROM booking_history WHERE booking_id = %s "
                            "ORDER BY at, id", (booking_id,)).fetchall()


def _change(booking_id: int, login: str, action: str, detail: str, sets: dict, allowed: tuple[str, ...]) -> bool:
    """Update a booking and record who did it, in one transaction. False if its status doesn't allow it."""
    assign = ", ".join(f"{k} = %s" for k in sets)
    with _connect() as conn:
        row = conn.execute(f"UPDATE bookings SET {assign}, updated_at = now() WHERE id = %s AND status = ANY(%s) "
                           "RETURNING id", [*sets.values(), booking_id, list(allowed)]).fetchone()
        if not row:
            return False
        conn.execute("INSERT INTO booking_history (booking_id, login, action, detail) VALUES (%s, %s, %s, %s)",
                     (booking_id, login, action, detail))
        return True


def mark_contacted(booking_id: int, login: str) -> bool:
    return _change(booking_id, login, "contacted", "", {"status": "contacted"}, ("new",))


def confirm(booking_id: int, login: str, venue: str, day: date, time: str, detail: str) -> bool:
    return _change(booking_id, login, "confirmed", detail,
                   {"status": "confirmed", "venue": venue, "slot_date": day, "slot_time": time, "decline_reason": ""},
                   ("new", "contacted", "confirmed", "declined"))


def decline(booking_id: int, login: str, reason: str) -> bool:
    return _change(booking_id, login, "declined", reason, {"status": "declined", "decline_reason": reason},
                   ("new", "contacted", "confirmed"))
