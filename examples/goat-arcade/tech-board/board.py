"""The morning list: what's out of order, which tickets are open, which machines are faulting more.

Everything here reads the gateway and does the joins and sums in code (the gateway doesn't). "Now" for the
data is the latest day in machine_events_daily, never the wall clock: the data ends on the seed day.
"""
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from annex_runtime import gateway

TZ = ZoneInfo("Europe/Ljubljana")
FAULTS = ("coil_fault", "switch_fault")
RECENT_DAYS = 7      # the last week of the data ...
BASELINE_DAYS = 21   # ... against the three weeks before it
WINDOW = RECENT_DAYS + BASELINE_DAYS


@dataclass
class Rising:
    machine_id: int
    title: str
    venue_code: str | None
    until: date              # the last day of its window: the data's end, or the day it went off the floor
    recent: int = 0          # coil + switch faults in the 7 days to `until`
    baseline: int = 0        # in the 21 days before
    coil: int = 0            # of recent: coil faults
    switch: int = 0          # of recent: switch faults
    weekly: list[int] = field(default_factory=lambda: [0, 0, 0, 0])  # faults per week, oldest first

    @property
    def baseline_week(self) -> float:
        """The baseline as a weekly rate, comparable with `recent`."""
        return self.baseline * RECENT_DAYS / BASELINE_DAYS

    @property
    def ratio(self) -> float:
        return self.recent / max(self.baseline_week, 1.0)


@dataclass
class MorningList:
    data_end: date
    out_of_order: list[dict]
    tickets: list[dict]
    rising: list[Rising]
    venues: dict[str, str]


def local_day(ts: str) -> date:
    """The local (Ljubljana) date of an ISO timestamp from the gateway."""
    return datetime.fromisoformat(ts).astimezone(TZ).date()


def rising_faults(rows: list[dict], end: date, off_floor: dict[int, date] | None = None,
                  min_recent: int = 4, min_ratio: float = 2.0) -> list[Rising]:
    """Machines whose coil and switch faults in their last 7 days clearly exceed their rate over the 21 days
    before: at least `min_recent` faults that week and `min_ratio` times the baseline's weekly rate (a baseline
    under one a week counts as one, so a stray fault isn't "rising"). Worst first, by the excess over the
    baseline.

    A machine's last 7 days end on `end` (the data's last day), or, for a machine in `off_floor`
    ({machine_id: the day it was taken off}), on the day it went off: a machine nobody can play reports no
    faults, and the week that made it break is the one that matters.
    `rows` are machine_events_daily rows (day, machine_id, title, current_venue_code, event_type, events)."""
    off_floor = off_floor or {}
    acc: dict[int, Rising] = {}
    for r in rows:
        if r["event_type"] not in FAULTS:
            continue
        mid = r["machine_id"]
        until = min(off_floor.get(mid, end), end)
        day = date.fromisoformat(r["day"])
        back = (until - day).days  # 0 = the window's last day
        if back < 0 or back >= WINDOW:
            continue
        m = acc.get(mid)
        if m is None:
            m = acc[mid] = Rising(mid, r["title"], r.get("current_venue_code"), until)
        n = int(r["events"])
        m.weekly[3 - back // 7] += n
        if back < RECENT_DAYS:
            m.recent += n
            if r["event_type"] == "coil_fault":
                m.coil += n
            else:
                m.switch += n
        else:
            m.baseline += n
    hits = [m for m in acc.values() if m.recent >= min_recent and m.ratio >= min_ratio]
    return sorted(hits, key=lambda m: (-(m.recent - m.baseline_week), -m.recent, m.machine_id))


def age_days(opened_at: str, end: date) -> int:
    """Whole days from a ticket's opening to the end of the data."""
    return max(0, (end - local_day(opened_at)).days)


def load() -> MorningList:
    """Read the three views and build the list. Raises GatewayError, or LookupError if there's no data."""
    latest = gateway("machine_events_daily", select="day", order="day.desc", limit=1)
    if not latest:
        raise LookupError("machine_events_daily is empty")
    end = date.fromisoformat(latest[0]["day"])
    floor = gateway("machine_floor", select="machine_id,serial_no,venue_code,venue_name,title,era,machine_type,"
                                            "condition,open_ticket,out_of_order")
    tickets = gateway("service_log", select="ticket_id,machine_id,title,current_venue_code,opened_at,status,"
                                            "symptom,reported_by,took_machine_off_floor",
                      closed_at="is.null", order="opened_at.asc")
    venues = {m["venue_code"]: m["venue_name"] for m in floor}
    by_machine = {m["machine_id"]: m for m in floor}

    # When did each out-of-order machine go off the floor? Its oldest open ticket that took it off.
    off_since: dict[int, str] = {}
    for t in tickets:
        if t["took_machine_off_floor"] and by_machine.get(t["machine_id"], {}).get("out_of_order"):
            off_since.setdefault(t["machine_id"], t["opened_at"])
    off_day = {mid: local_day(ts) for mid, ts in off_since.items()}

    start = min([end, *off_day.values()]) - timedelta(days=WINDOW - 1)
    events = gateway("machine_events_daily", select="day,machine_id,title,current_venue_code,event_type,events",
                     event_type=f"in.({','.join(FAULTS)})", day=f"gte.{start.isoformat()}", order="day")

    out = sorted((m for m in floor if m["out_of_order"]), key=lambda m: (m["venue_code"], m["title"]))
    for m in out:
        m["off_since"] = off_since.get(m["machine_id"])
    for t in tickets:
        t["age_days"] = age_days(t["opened_at"], end)
        t["venue_code"] = t["current_venue_code"] or "store"
        t["out_of_order"] = bool(by_machine.get(t["machine_id"], {}).get("out_of_order"))
    return MorningList(end, out, tickets, rising_faults(events, end, off_day), venues)


def summary(ml: MorningList) -> str:
    """One sentence for the email."""
    worst = (f" Faulting most: {ml.rising[0].title} (#{ml.rising[0].machine_id}, {ml.rising[0].venue_code})."
             if ml.rising else "")
    oldest = f" Oldest open ticket: {ml.tickets[0]['age_days']} days." if ml.tickets else ""
    n = len(ml.rising)
    return (f"{len(ml.out_of_order)} out of order, {len(ml.tickets)} open tickets, "
            f"{n} machine{'' if n == 1 else 's'} with rising coil or switch faults.{oldest}{worst}")
