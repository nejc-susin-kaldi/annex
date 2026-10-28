"""Unit tests for the pure parts of board.py: python3 -m unittest test_board (in this folder)."""
import unittest
from datetime import date, timedelta

import board

END = date(2026, 10, 21)


def rows(machine_id, per_day, kind="coil_fault", until=END, days=28):
    """machine_events_daily rows: per_day(i) faults on day i (0 = oldest) of the `days` days to `until`."""
    out = []
    for i in range(days):
        n = per_day(i)
        if n:
            out.append({"day": (until - timedelta(days=days - 1 - i)).isoformat(), "machine_id": machine_id,
                        "title": f"M{machine_id}", "current_venue_code": "MB", "event_type": kind, "events": n})
    return out


class RisingFaults(unittest.TestCase):
    def test_steady_machine_is_not_rising(self):
        self.assertEqual(board.rising_faults(rows(1, lambda i: 1), END), [])

    def test_a_stray_fault_is_not_rising(self):
        self.assertEqual(board.rising_faults(rows(1, lambda i: i == 27), END), [])

    def test_a_sicker_machine_rises(self):
        [r] = board.rising_faults(rows(1, lambda i: i // 5), END)
        self.assertEqual((r.machine_id, r.until), (1, END))
        self.assertEqual(sum(r.weekly), r.recent + r.baseline)
        self.assertGreater(r.recent, 2 * r.baseline_week)

    def test_off_the_floor_counts_to_the_day_it_went_off(self):
        off = END - timedelta(days=10)
        data = rows(48, lambda i: 1 + i // 4, until=off)
        self.assertEqual(board.rising_faults(data, END), [])  # nothing in the data's last week
        [r] = board.rising_faults(data, END, {48: off})
        self.assertEqual(r.until, off)

    def test_switch_and_coil_add_up_and_worst_is_first(self):
        data = rows(1, lambda i: 3 * (i >= 21)) + rows(1, lambda i: 1 * (i >= 21), "switch_fault") \
            + rows(2, lambda i: 1 * (i >= 21)) + rows(3, lambda i: 1, "tilt")
        got = board.rising_faults(data, END)
        self.assertEqual([r.machine_id for r in got], [1, 2])
        self.assertEqual((got[0].coil, got[0].switch), (21, 7))


class Dates(unittest.TestCase):
    def test_age_uses_the_local_day(self):
        self.assertEqual(board.local_day("2026-10-11T22:30:00+00:00"), date(2026, 10, 12))
        self.assertEqual(board.age_days("2026-10-12T18:30:00+02:00", END), 9)
        self.assertEqual(board.age_days("2026-10-22T08:00:00+02:00", END), 0)


if __name__ == "__main__":
    unittest.main()
