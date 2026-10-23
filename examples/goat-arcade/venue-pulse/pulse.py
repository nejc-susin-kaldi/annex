"""The numbers behind venue-pulse, as pure functions: no gateway, database or clock of their own.

Every "this week" or "last 30 days" is relative to `last_day`, the latest day in the data, never today.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import date, timedelta


def parse_home_venues(spec: str) -> dict[str, str]:
    """'maja=LJ1, boris=MB' -> {'maja': 'LJ1', 'boris': 'MB'}. Malformed parts are skipped."""
    out = {}
    for part in (spec or "").split(","):
        login, sep, venue = part.partition("=")
        if sep and login.strip() and venue.strip():
            out[login.strip()] = venue.strip().upper()
    return out


def as_date(value) -> date:
    return value if isinstance(value, date) else date.fromisoformat(str(value)[:10])


@dataclass
class Week:
    start: date
    end: date

    def __contains__(self, d: date) -> bool:
        return self.start <= d <= self.end


def weeks(last_day: date) -> tuple[Week, Week]:
    """The 7 days ending on last_day, and the 7 before them."""
    this = Week(last_day - timedelta(days=6), last_day)
    prev = Week(last_day - timedelta(days=13), last_day - timedelta(days=7))
    return this, prev


def pct_change(now: float, before: float) -> float | None:
    return None if not before else (now - before) / before * 100


@dataclass
class MachineWeek:
    machine_id: int
    title: str
    games_this: int = 0
    games_last: int = 0
    rev_this: float = 0.0
    rev_last: float = 0.0

    @property
    def rev_change(self) -> float:
        return self.rev_this - self.rev_last


def week_summary(plays: list[dict], venue: str, last_day: date) -> dict:
    """Games and revenue at `venue` this week and last, in total and per machine.

    `plays` are daily_plays rows (day, venue_code, machine_id, title, games, revenue_eur). Only rows at the venue
    count: a machine that moved in mid-week counts from the day it arrived."""
    this, prev = weeks(last_day)
    machines: dict[int, MachineWeek] = {}
    tot = {"games_this": 0, "games_last": 0, "rev_this": 0.0, "rev_last": 0.0}
    for r in plays:
        if r["venue_code"] != venue:
            continue
        d = as_date(r["day"])
        which = "this" if d in this else "last" if d in prev else None
        if which is None:
            continue
        m = machines.setdefault(r["machine_id"], MachineWeek(r["machine_id"], r["title"]))
        games, rev = int(r["games"] or 0), float(r["revenue_eur"] or 0)
        setattr(m, f"games_{which}", getattr(m, f"games_{which}") + games)
        setattr(m, f"rev_{which}", getattr(m, f"rev_{which}") + rev)
        tot[f"games_{which}"] += games
        tot[f"rev_{which}"] += rev
    return {"this": this, "prev": prev, **tot, "machines": machines}


def movers(machines: list[MachineWeek], n: int = 3) -> tuple[list[MachineWeek], list[MachineWeek]]:
    """The n biggest risers and the n biggest fallers by revenue change (each list only holds real moves)."""
    ranked = sorted(machines, key=lambda m: m.rev_change)
    up = [m for m in reversed(ranked) if m.rev_change > 0][:n]
    down = [m for m in ranked if m.rev_change < 0][:n]
    return up, down


def games_per_day(plays: list[dict], venue: str, last_day: date, days: int = 14) -> list[tuple[date, int]]:
    """Games per day at the venue for the `days` days ending on last_day, with zero for days without play."""
    by_day: dict[date, int] = defaultdict(int)
    for r in plays:
        if r["venue_code"] == venue:
            by_day[as_date(r["day"])] += int(r["games"] or 0)
    first = last_day - timedelta(days=days - 1)
    return [(first + timedelta(days=i), by_day.get(first + timedelta(days=i), 0)) for i in range(days)]


def model_rates(plays: list[dict], floor: list[dict], last_day: date, days: int = 30) -> dict[str, dict[str, dict]]:
    """Average daily revenue per cabinet, by model (title) and venue, over the `days` days ending on last_day.

    Machines move, so each cabinet's days are counted where it stood: its rows are split into runs at one venue, and
    each run lasts from its first day to its last. The first run is stretched back to the window's start, and the run
    at the venue where the machine stands now (machine_floor) is stretched to last_day, so quiet days count too.
    A cabinet on a floor now with no play at all in the window counts the whole window at zero.

    Returns {title: {venue_code: {"rate": eur_per_day, "cabinets": n, "days": cabinet_days, "revenue": eur}}}.
    """
    start = last_day - timedelta(days=days - 1)
    current = {m["machine_id"]: (m["venue_code"], m["title"]) for m in floor}
    by_machine: dict[int, list[tuple[date, str, float, str]]] = defaultdict(list)
    for r in plays:
        d = as_date(r["day"])
        if start <= d <= last_day:
            by_machine[r["machine_id"]].append((d, r["venue_code"], float(r["revenue_eur"] or 0), r["title"]))

    acc: dict[tuple[str, str], dict] = {}

    def add(title, venue, machine_id, n_days, revenue):
        a = acc.setdefault((title, venue), {"revenue": 0.0, "days": 0, "cabinets": set()})
        a["revenue"] += revenue
        a["days"] += n_days
        a["cabinets"].add(machine_id)

    for machine_id, rows in by_machine.items():
        rows.sort()
        runs: list[list] = []  # [venue, first_day, last_day, revenue, title]
        for d, venue, rev, title in rows:
            if runs and runs[-1][0] == venue:
                runs[-1][2] = d
                runs[-1][3] += rev
            else:
                runs.append([venue, d, d, rev, title])
        runs[0][1] = start
        here = current.get(machine_id)
        if here and runs[-1][0] == here[0]:
            runs[-1][2] = last_day
        elif here:  # moved to its current floor after its last game in the window
            first_free = runs[-1][2] + timedelta(days=1)
            if first_free <= last_day:
                runs.append([here[0], first_free, last_day, 0.0, here[1]])
        for venue, d0, d1, rev, title in runs:
            add(title, venue, machine_id, (d1 - d0).days + 1, rev)

    for machine_id, (venue, title) in current.items():
        if machine_id not in by_machine:
            add(title, venue, machine_id, days, 0.0)

    out: dict[str, dict[str, dict]] = defaultdict(dict)
    for (title, venue), a in acc.items():
        out[title][venue] = {"rate": a["revenue"] / a["days"] if a["days"] else 0.0,
                             "cabinets": len(a["cabinets"]), "days": a["days"], "revenue": a["revenue"]}
    return dict(out)


def rotation_candidates(rates: dict[str, dict[str, dict]], venue: str, titles_here: set[str],
                        min_ratio: float = 1.5, min_gap_eur: float = 5.0) -> list[dict]:
    """Models on this floor that earn clearly more per cabinet per day at another venue: at least `min_ratio` times
    as much and at least `min_gap_eur` more. Best other venue per model, biggest gap first."""
    out = []
    for title in titles_here:
        per_venue = rates.get(title, {})
        here = per_venue.get(venue)
        if not here:
            continue
        others = [(v, r) for v, r in per_venue.items() if v != venue and r["days"] >= 7]
        if not others:
            continue
        best_venue, best = max(others, key=lambda vr: vr[1]["rate"])
        gap = best["rate"] - here["rate"]
        if gap >= min_gap_eur and best["rate"] >= min_ratio * here["rate"]:
            out.append({"title": title, "here": here, "there_venue": best_venue, "there": best, "gap": gap})
    return sorted(out, key=lambda c: -c["gap"])
