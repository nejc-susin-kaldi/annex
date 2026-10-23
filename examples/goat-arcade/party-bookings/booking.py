"""The pure part of party-bookings: validating a website request, who manages which venue, opening hours.
No database, network or clock of its own (today is passed in), so it is easy to test."""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
PHONE_RE = re.compile(r"^\+?[0-9 ()/.-]{6,25}$")
TIME_RE = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")
VENUE_RE = re.compile(r"^[A-Z0-9]{2,4}$")

MAX_KIDS = 40
MAX_DAYS_AHEAD = 366
WEEKDAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
STATUSES = ["new", "contacted", "confirmed", "declined", "rejected"]


@dataclass
class Request:
    name: str
    email: str
    phone: str
    preferred_venue: str
    date: date
    time: str  # HH:MM
    kids: int
    age: int
    note: str


def _text(body: dict, key: str, limit: int) -> str:
    v = body.get(key)
    if v is None:
        return ""
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        v = str(v)
    return v.strip()[:limit] if isinstance(v, str) else ""


def _int(v) -> int | None:
    if isinstance(v, bool):
        return None
    if isinstance(v, int):
        return v
    if isinstance(v, str) and v.strip().isdigit():
        return int(v.strip())
    return None


def raw_fields(body) -> dict[str, str]:
    """What we keep of any body, valid or not: the known fields as short strings (for a rejected request's page)."""
    if not isinstance(body, dict):
        return {}
    out = {}
    for k in ("name", "email", "phone", "preferred_venue", "date", "time", "kids", "age", "note"):
        v = body.get(k)
        if v is not None:
            out[k] = (v if isinstance(v, str) else str(v)).strip()[:300]
    return out


def parse_request(body, today: date, venue_codes: set[str] | None) -> tuple[Request | None, list[str]]:
    """Validate the website form's JSON. Returns the request, or None and every problem found (in plain words).
    venue_codes: the open venues; None when they couldn't be read (then only the code's shape is checked)."""
    if not isinstance(body, dict):
        return None, ["the body is not a JSON object"]
    problems: list[str] = []

    name = _text(body, "name", 120)
    if not name:
        problems.append("name is missing")
    email, phone = _text(body, "email", 200), _text(body, "phone", 40)
    if not email and not phone:
        problems.append("give an email or a phone number")
    if email and not EMAIL_RE.match(email):
        problems.append("email is not an email address")
    if phone and not PHONE_RE.match(phone):
        problems.append("phone is not a phone number")

    venue = _text(body, "preferred_venue", 10).upper()
    if not venue:
        problems.append("preferred_venue is missing")
    elif venue_codes is not None and venue not in venue_codes:
        problems.append(f"preferred_venue {venue} is not one of our venues")
    elif venue_codes is None and not VENUE_RE.match(venue):
        problems.append("preferred_venue is not a venue code")

    day = None
    raw_date = _text(body, "date", 20)
    if not raw_date:
        problems.append("date is missing")
    else:
        try:
            day = date.fromisoformat(raw_date)
        except ValueError:
            problems.append("date is not a date (YYYY-MM-DD)")
        else:
            if day < today:
                problems.append("date is in the past")
            elif day > today + timedelta(days=MAX_DAYS_AHEAD):
                problems.append("date is more than a year ahead")

    raw_time = _text(body, "time", 10)
    m = TIME_RE.match(raw_time)
    if not raw_time:
        problems.append("time is missing")
    elif not m:
        problems.append("time is not a time (HH:MM)")

    kids = _int(body.get("kids"))
    if kids is None or not 1 <= kids <= MAX_KIDS:
        problems.append(f"kids must be a whole number from 1 to {MAX_KIDS}")
    age = _int(body.get("age"))
    if age is None or not 1 <= age <= 18:
        problems.append("age must be a whole number from 1 to 18")

    if problems:
        return None, problems
    return Request(name=name, email=email, phone=phone, preferred_venue=venue, date=day,
                   time=f"{int(m.group(1)):02d}:{m.group(2)}", kids=kids, age=age,
                   note=_text(body, "note", 1000)), []


# --- who hears about which venue ------------------------------------------------------------------------------------
def parse_managers(spec: str) -> dict[str, str]:
    """VENUE_MANAGERS: "LJ1=maja,MB=boris,*=maja" -> {"LJ1": "maja", "MB": "boris", "*": "maja"}."""
    out = {}
    for part in spec.split(","):
        code, _, login = part.partition("=")
        if code.strip() and login.strip():
            out[code.strip().upper()] = login.strip()
    return out


def manager_for(managers: dict[str, str], venue: str) -> str | None:
    return managers.get(venue.upper()) or managers.get("*")


# --- opening hours --------------------------------------------------------------------------------------------------
def _minutes(hhmm: str) -> int:
    h, m = hhmm.split(":")
    return int(h) * 60 + int(m)


def hours_text(opening_hours: dict | None, day: date) -> str:
    """'16:00–24:00' or 'closed' for that weekday."""
    span = (opening_hours or {}).get(WEEKDAYS[day.weekday()])
    return f"{span[0]}–{span[1]}" if span else "closed"


def slot_problem(opening_hours: dict | None, day: date, start: str, hours: float) -> str | None:
    """Why a party from `start` for `hours` on `day` doesn't fit the venue's opening hours, or None if it fits.
    opening_hours is the venues view's JSON: {"mon": ["16:00", "24:00"] | null, ...}; a closing time earlier than
    the opening time is after midnight. A slot fits when it lies inside one opening span (that day's, or the
    previous day's running past midnight). Unknown hours (None) count as fitting: there is nothing to warn about."""
    if opening_hours is None:
        return None
    s = _minutes(start)
    e = s + round(hours * 60)
    spans = []
    for offset, d in ((0, day), (-1440, day - timedelta(days=1))):
        span = opening_hours.get(WEEKDAYS[d.weekday()])
        if span:
            o, c = _minutes(span[0]), _minutes(span[1])
            if c <= o:
                c += 1440
            spans.append((o + offset, c + offset))
    if any(o <= s and e <= c for o, c in spans):
        return None
    wd = day.strftime("%A")
    if not opening_hours.get(WEEKDAYS[day.weekday()]):
        return f"the venue is closed on {wd}s"
    end = f"{(e // 60) % 24:02d}:{e % 60:02d}"
    return f"{start}–{end} is outside the opening hours on {wd}s ({hours_text(opening_hours, day)})"


def fmt_day(d: date) -> str:
    return f"{d:%a} {d.day} {d:%b %Y}"


def fmt_when(ts: datetime) -> str:
    return f"{ts:%a} {ts.day} {ts:%b}, {ts:%H:%M}"
