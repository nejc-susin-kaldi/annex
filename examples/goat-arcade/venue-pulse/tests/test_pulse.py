"""Unit tests for pulse.py: uv run --python 3.13 --with pytest pytest tests -q (from the app folder)."""
import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import pulse  # noqa: E402

L = date(2026, 10, 20)


def row(days_ago, venue, mid, title, games=10, rev=10.0):
    return {"day": (L - timedelta(days=days_ago)).isoformat(), "venue_code": venue, "machine_id": mid,
            "title": title, "games": games, "revenue_eur": rev}


def test_parse_home_venues():
    assert pulse.parse_home_venues(" maja=lj1, boris=MB,bad, =X") == {"maja": "LJ1", "boris": "MB"}


def test_week_summary_splits_weeks_and_venue():
    plays = [row(0, "LJ1", 1, "A", 5, 7.5), row(6, "LJ1", 1, "A", 1, 1), row(7, "LJ1", 1, "A", 2, 3),
             row(13, "LJ1", 2, "B", 4, 4), row(14, "LJ1", 2, "B", 99, 99), row(0, "MB", 3, "C", 50, 50)]
    s = pulse.week_summary(plays, "LJ1", L)
    assert (s["games_this"], s["games_last"]) == (6, 6)
    assert s["rev_this"] == 8.5 and s["rev_last"] == 7
    assert s["machines"][2].rev_change == -4


def test_movers():
    ms = [pulse.MachineWeek(1, "A", rev_this=10, rev_last=0), pulse.MachineWeek(2, "B", rev_this=0, rev_last=5),
          pulse.MachineWeek(3, "C", rev_this=3, rev_last=3)]
    up, down = pulse.movers(ms)
    assert [m.machine_id for m in up] == [1] and [m.machine_id for m in down] == [2]


def test_games_per_day_fills_gaps():
    series = pulse.games_per_day([row(0, "LJ1", 1, "A", 3), row(2, "LJ1", 1, "A", 4)], "LJ1", L, days=3)
    assert [g for _, g in series] == [4, 0, 3]


def test_model_rates_counts_quiet_days_and_moves():
    # machine 1: at ZG the whole window, plays on only 3 days -> 30 cabinet-days
    # machine 2: at LJ1 until 10 days ago, then at ZG (where it stands now)
    # machine 3: on the LJ1 floor, never played in the window -> 30 days at zero
    plays = [row(29, "ZG", 1, "M", rev=30), row(15, "ZG", 1, "M", rev=30), row(1, "ZG", 1, "M", rev=30),
             row(25, "LJ1", 2, "M", rev=20), row(10, "LJ1", 2, "M", rev=20), row(9, "ZG", 2, "M", rev=90)]
    floor = [{"machine_id": 1, "venue_code": "ZG", "title": "M"}, {"machine_id": 2, "venue_code": "ZG", "title": "M"},
             {"machine_id": 3, "venue_code": "LJ1", "title": "M"}]
    r = pulse.model_rates(plays, floor, L)
    assert r["M"]["ZG"]["days"] == 30 + 10 and r["M"]["ZG"]["revenue"] == 180
    assert r["M"]["LJ1"]["days"] == 20 + 30 and r["M"]["LJ1"]["cabinets"] == 2


def test_rotation_candidates_threshold():
    rates = {"M": {"LJ1": {"rate": 10, "days": 30, "cabinets": 1}, "ZG": {"rate": 20, "days": 30, "cabinets": 1}},
             "N": {"LJ1": {"rate": 10, "days": 30, "cabinets": 1}, "ZG": {"rate": 13, "days": 30, "cabinets": 1}}}
    c = pulse.rotation_candidates(rates, "LJ1", {"M", "N"})
    assert [x["title"] for x in c] == ["M"] and c[0]["there_venue"] == "ZG"
