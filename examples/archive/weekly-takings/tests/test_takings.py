"""Unit tests for the pure helpers: python3 -m unittest discover -s tests (from the app folder)."""
import sys
import unittest
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from takings import build, last_complete_week, to_csv  # noqa: E402


def row(week, code, amount):
    return {"week_start": week, "venue_code": code, "venue_name": f"Goat {code}", "city": "X", "amount_eur": amount,
            "coins_eur": amount / 2, "tokens": 10, "card_plays": 5, "machines": 3}


class LastCompleteWeek(unittest.TestCase):
    def test_sunday_is_complete(self):
        self.assertEqual(last_complete_week(date(2026, 10, 18)), date(2026, 10, 12))  # a Sunday

    def test_midweek_goes_back(self):
        self.assertEqual(last_complete_week(date(2026, 10, 21)), date(2026, 10, 12))  # a Wednesday

    def test_monday_goes_back(self):
        self.assertEqual(last_complete_week(date(2026, 10, 19)), date(2026, 10, 12))


class Build(unittest.TestCase):
    rows = [row("2026-10-05", "LJ1", 1000.0), row("2026-10-05", "NG", 200.0),
            row("2026-10-12", "LJ1", 1100.0), row("2026-10-12", "MB", 500.0),
            row("2026-10-19", "LJ1", 300.0)]

    def test_change_and_totals(self):
        w = build(self.rows, date(2026, 10, 12), date(2026, 10, 21))
        self.assertEqual([v.venue_code for v in w.venues], ["LJ1", "MB"])
        self.assertAlmostEqual(w.venues[0].change_pct, 10.0)
        self.assertIsNone(w.venues[1].prev_amount)            # a new venue
        self.assertEqual(w.prev_total, 1200.0)                 # a venue closed since still counts in the week before
        self.assertEqual(w.in_progress, date(2026, 10, 19))
        self.assertEqual(len(w.venues[0].trend), 8)
        self.assertEqual(w.venues[0].trend[-2:], [1000.0, 1100.0])

    def test_missing_week(self):
        with self.assertRaises(LookupError):
            build(self.rows, date(2026, 9, 28), date(2026, 10, 21))

    def test_csv(self):
        lines = to_csv(build(self.rows, date(2026, 10, 12), date(2026, 10, 21))).splitlines()
        self.assertTrue(lines[0].startswith("week_start,week_end,venue_code"))
        self.assertIn(",LJ1,", lines[1])
        self.assertTrue(lines[1].endswith(",1000.00,100.00,10.0"))
        self.assertIn(",TOTAL,", lines[-1])
        self.assertEqual(len(lines), 4)


if __name__ == "__main__":
    unittest.main()
