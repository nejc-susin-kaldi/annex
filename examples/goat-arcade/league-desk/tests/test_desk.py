"""Unit tests for desk.py: uv run --python 3.13 --with pytest pytest tests -q (from the app folder)."""
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import desk  # noqa: E402


def test_clean_nickname():
    assert desk.clean_nickname("  @otter12 ") == "otter12"
    assert desk.clean_nickname("a") is None
    assert desk.clean_nickname("otter,12") is None
    assert desk.clean_nickname("*") is None
    assert desk.clean_nickname("") is None


def test_search_term_strips_filter_syntax():
    assert desk.search_term(" ot*ter(1) ") == "otter1"
    assert desk.search_term("***") is None


def test_pick_exact_ignores_case_and_wildcard_matches():
    rows = [{"nickname": "otter_1"}, {"nickname": "otterX1"}]
    assert desk.pick_exact(rows, "OTTER_1") == {"nickname": "otter_1"}
    assert desk.pick_exact(rows, "otter.1") is None


def test_split_tournaments():
    ts = [{"status": "completed", "starts_on": "2026-05-01"}, {"status": "registration", "starts_on": "2026-11-01"},
          {"status": "completed", "starts_on": "2026-09-01"}]
    up, past = desk.split_tournaments(ts)
    assert [t["starts_on"] for t in up] == ["2026-11-01"]
    assert [t["starts_on"] for t in past] == ["2026-09-01", "2026-05-01"]


def test_places_left():
    assert desk.places_left(32, 19, 2) == 11
    assert desk.places_left(16, 16, 1) == 0
    assert desk.places_left(None, None, 0) == 0


def test_placing_label():
    assert desk.placing_label(1, 24) == "1st of 24"
    assert desk.placing_label(12, None) == "12th"
    assert desk.placing_label(22, 32) == "22nd of 32"
    assert desk.placing_label(13, 16) == "13th of 16"


def test_slack_line():
    t = {"name": "Goat Arcade Masters", "starts_on": "2026-11-01", "venue_name": "Goat Arcade Metelkova"}
    line = desk.slack_line("otter12", 1840, "LJ1", t, "lena", 1)
    assert line == ("Desk sign-up for Goat Arcade Masters (Sun 1 Nov, Goat Arcade Metelkova): otter12 (rating 1840, "
                    "LJ1), signed up by lena. 1 place left.")
    assert desk.as_date("2026-11-01T10:00:00+01:00") == date(2026, 11, 1)
