"""Pure helpers for league-desk: no database, network or clock of their own (tested in tests/test_desk.py)."""
import re
from datetime import date, datetime

# Nicknames in the club system are letters, digits and a few separators. Anything else can't be one, and keeping
# to these also keeps PostgREST filter syntax (commas, parentheses, '*') out of the gateway query.
NICK_RE = re.compile(r"^[A-Za-z0-9_.\-]{2,40}$")


def clean_nickname(raw: str) -> str | None:
    """The typed nickname, trimmed (a leading '@' dropped), or None if it can't be a nickname."""
    nick = (raw or "").strip().lstrip("@").strip()
    return nick if NICK_RE.match(nick) else None


def search_term(raw: str) -> str | None:
    """A search box value made safe for an ilike filter: the allowed characters only, or None if nothing is left."""
    term = "".join(ch for ch in (raw or "").strip() if ch.isalnum() or ch in "_.-")[:40]
    return term or None


def pick_exact(rows: list[dict], nick: str) -> dict | None:
    """The profile whose nickname is exactly `nick`, ignoring case (ilike treats '_' as a wildcard, so filter here)."""
    want = nick.lower()
    return next((r for r in rows if (r.get("nickname") or "").lower() == want), None)


def official_nicknames(rows: list[dict]) -> list[str]:
    """The nicknames of the club system's entries, sorted ignoring case (rows from `tournament_entries`)."""
    return sorted({r["nickname"] for r in rows if r.get("nickname")}, key=str.lower)


def is_official(rows: list[dict], nick: str) -> bool:
    """Is `nick` (any case) among the club system's entries?"""
    want = nick.lower()
    return any((r.get("nickname") or "").lower() == want for r in rows)


def as_date(v) -> date:
    if isinstance(v, date) and not isinstance(v, datetime):
        return v
    return date.fromisoformat(str(v)[:10])


def split_tournaments(rows: list[dict]) -> tuple[list[dict], list[dict]]:
    """(upcoming, past): upcoming are open for registration, soonest first; past are completed, newest first."""
    upcoming = sorted((t for t in rows if t.get("status") == "registration"), key=lambda t: str(t["starts_on"]))
    past = sorted((t for t in rows if t.get("status") == "completed"), key=lambda t: str(t["starts_on"]), reverse=True)
    return upcoming, past


def places_left(max_players: int | None, official: int | None, pending: int) -> int:
    """Places still free, counting the club system's entries and the desk's pending sign-ups (never below 0)."""
    return max(0, (max_players or 0) - (official or 0) - pending)


def placing_label(position: int, field: int | None) -> str:
    """'1st of 24', '22nd', ..."""
    suffix = "th" if 10 <= position % 100 <= 20 else {1: "st", 2: "nd", 3: "rd"}.get(position % 10, "th")
    return f"{position}{suffix}" + (f" of {field}" if field else "")


def days_between(a: date, b: date) -> int:
    return (b - a).days


def slack_line(nickname: str, rating: int | None, home: str | None, tournament: dict, by: str, left: int) -> str:
    """The one line posted to #ga-community for a desk sign-up."""
    who = nickname + (f" (rating {rating}" + (f", {home}" if home else "") + ")" if rating else "")
    when = as_date(tournament["starts_on"])
    return (f"Desk sign-up for {tournament['name']} ({when:%a %-d %b}, {tournament['venue_name']}): {who}, "
            f"signed up by {by}. {left} place{'s' if left != 1 else ''} left.")
