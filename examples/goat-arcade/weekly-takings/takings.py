"""The numbers: which week is the last complete one, and each venue's takings against the week before.

`load()` reads the gateway; everything else is pure, so the page and the job compute the same thing.
"""
from __future__ import annotations

import csv
import io
from dataclasses import dataclass, field
from datetime import date, timedelta

from annex_runtime import gateway

TREND_WEEKS = 8
CSV_COLUMNS = ["week_start", "week_end", "venue_code", "venue_name", "city", "amount_eur", "coins_eur", "tokens",
               "card_plays", "machines", "prev_amount_eur", "change_eur", "change_pct"]


@dataclass
class VenueWeek:
    venue_code: str
    venue_name: str
    city: str
    amount: float
    coins: float
    tokens: int
    card_plays: int
    machines: int
    prev_amount: float | None          # None: the venue has no takings the week before
    trend: list[float | None] = field(default_factory=list)  # oldest first, TREND_WEEKS long, ends with this week

    @property
    def change(self) -> float | None:
        return None if self.prev_amount is None else self.amount - self.prev_amount

    @property
    def change_pct(self) -> float | None:
        return None if not self.prev_amount else (self.amount - self.prev_amount) / self.prev_amount * 100


@dataclass
class Week:
    week_start: date
    data_through: date                 # the latest day in the data
    first_week: date                   # the oldest week read
    in_progress: date | None           # a later week that isn't complete yet (its Monday), if any
    venues: list[VenueWeek]
    chain_trend: list[float]           # chain total per week, oldest first

    @property
    def week_end(self) -> date:
        return self.week_start + timedelta(days=6)

    @property
    def total(self) -> float:
        return sum(v.amount for v in self.venues)

    @property
    def prev_total(self) -> float | None:
        """The chain's total the week before, all venues that had takings then (also one that has since closed)."""
        return self.chain_trend[-2] or None if len(self.chain_trend) > 1 else None

    @property
    def change_pct(self) -> float | None:
        p = self.prev_total
        return None if not p else (self.total - p) / p * 100


def load(week: date | None = None) -> Week:
    """Read the latest day in the data and enough weeks of revenue_weekly for the trend. `week` (a Monday) picks a
    past week instead of the last complete one. Raises GatewayError, or LookupError when there is no such week."""
    latest = gateway("daily_plays", select="day", order="day.desc", limit=1)
    if not latest:
        raise LookupError("daily_plays is empty")
    data_through = date.fromisoformat(latest[0]["day"])
    target = week or last_complete_week(data_through)
    since = target - timedelta(weeks=TREND_WEEKS - 1)
    rows = gateway("revenue_weekly", week_start=f"gte.{since.isoformat()}",
                   select="week_start,venue_code,venue_name,city,amount_eur,coins_eur,tokens,card_plays,machines",
                   order="week_start,venue_code")
    return build(rows, target, data_through)


def last_complete_week(data_through: date) -> date:
    """The Monday of the last Monday-to-Sunday week that ends on or before the latest day in the data."""
    sunday = data_through - timedelta(days=(data_through.weekday() + 1) % 7)
    return sunday - timedelta(days=6)


def build(rows: list[dict], target: date, data_through: date) -> Week:
    by_week: dict[date, dict[str, dict]] = {}
    for r in rows:
        by_week.setdefault(date.fromisoformat(r["week_start"]), {})[r["venue_code"]] = r
    this = by_week.get(target)
    if not this:
        raise LookupError(f"no takings for the week of {target.isoformat()}")
    weeks = [target - timedelta(weeks=i) for i in range(TREND_WEEKS - 1, -1, -1)]
    prev = by_week.get(target - timedelta(weeks=1), {})
    venues = []
    for code, r in this.items():
        p = prev.get(code)
        venues.append(VenueWeek(
            venue_code=code, venue_name=r["venue_name"], city=r["city"], amount=float(r["amount_eur"]),
            coins=float(r["coins_eur"]), tokens=int(r["tokens"]), card_plays=int(r["card_plays"]),
            machines=int(r["machines"]), prev_amount=float(p["amount_eur"]) if p else None,
            trend=[float(by_week[w][code]["amount_eur"]) if code in by_week.get(w, {}) else None for w in weeks]))
    venues.sort(key=lambda v: -v.amount)
    later = [w for w in by_week if w > target]
    return Week(week_start=target, data_through=data_through, first_week=min(by_week),
                in_progress=max(later) if later and max(later) + timedelta(days=6) > data_through else None,
                venues=venues,
                chain_trend=[sum(float(r["amount_eur"]) for r in by_week.get(w, {}).values()) for w in weeks])


def to_csv(week: Week) -> str:
    """One row per venue, then a TOTAL row. Amounts in EUR with two decimals; change_pct with one."""
    out = io.StringIO()
    w = csv.writer(out, lineterminator="\n")
    w.writerow(CSV_COLUMNS)
    span = [week.week_start.isoformat(), week.week_end.isoformat()]

    def num(x, fmt="{:.2f}"):
        return "" if x is None else fmt.format(x)

    for v in week.venues:
        w.writerow([*span, v.venue_code, v.venue_name, v.city, num(v.amount), num(v.coins), v.tokens, v.card_plays,
                    v.machines, num(v.prev_amount), num(v.change), num(v.change_pct, "{:.1f}")])
    prev = week.prev_total
    w.writerow([*span, "TOTAL", "All venues", "", num(week.total), num(sum(v.coins for v in week.venues)),
                sum(v.tokens for v in week.venues), sum(v.card_plays for v in week.venues),
                sum(v.machines for v in week.venues), num(prev),
                num(None if prev is None else week.total - prev), num(week.change_pct, "{:.1f}")])
    return out.getvalue()


def csv_name(week_start: date) -> str:
    return f"weekly-takings-{week_start.isoformat()}.csv"
