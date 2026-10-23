import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import booking  # noqa: E402

TODAY = date(2026, 10, 23)  # a Friday
HOURS = {"mon": None, "tue": ["16:00", "24:00"], "wed": ["16:00", "24:00"], "thu": ["16:00", "24:00"],
         "fri": ["16:00", "02:00"], "sat": ["12:00", "02:00"], "sun": ["12:00", "22:00"]}
GOOD = {"name": "Ana Novak", "email": "ana@example.com", "phone": "+386 40 123 456", "preferred_venue": "lj1",
        "date": "2026-11-07", "time": "15:00", "kids": 8, "age": "7", "note": "gluten-free cake"}


def test_valid_request():
    req, problems = booking.parse_request(GOOD, TODAY, {"LJ1", "MB"})
    assert problems == [] and req.preferred_venue == "LJ1" and req.age == 7 and req.date == date(2026, 11, 7)


def test_every_problem_is_listed():
    bad = {**GOOD, "email": "nope", "date": "2026-01-01", "kids": "lots", "preferred_venue": "NG"}
    req, problems = booking.parse_request(bad, TODAY, {"LJ1"})
    assert req is None
    assert problems == ["email is not an email address", "preferred_venue NG is not one of our venues",
                        "date is in the past", "kids must be a whole number from 1 to 40"]


def test_not_an_object():
    assert booking.parse_request([1], TODAY, None)[1] == ["the body is not a JSON object"]
    assert booking.raw_fields([1]) == {}


def test_unknown_venues_check_shape_only():
    assert booking.parse_request({**GOOD, "preferred_venue": "XX"}, TODAY, None)[1] == []


def test_managers():
    m = booking.parse_managers("LJ1=maja, mb=boris,*=maja")
    assert booking.manager_for(m, "MB") == "boris" and booking.manager_for(m, "ZG") == "maja"
    assert booking.manager_for({}, "ZG") is None


def test_slot_inside_and_outside_hours():
    sat = date(2026, 11, 7)
    assert booking.slot_problem(HOURS, sat, "15:00", 2) is None
    assert booking.slot_problem(HOURS, sat, "10:00", 2).startswith("10:00–12:00 is outside")
    assert booking.slot_problem(HOURS, date(2026, 11, 9), "17:00", 2) == "the venue is closed on Mondays"
    # Saturday 00:30 lies in Friday's 16:00-02:00
    assert booking.slot_problem(HOURS, sat, "00:30", 1) is None
    assert booking.slot_problem(HOURS, date(2026, 11, 8), "21:00", 2).endswith("(12:00–22:00)")
    assert booking.slot_problem(None, sat, "03:00", 2) is None
