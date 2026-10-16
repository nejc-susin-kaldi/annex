"""Run: PYTHONPATH=cli/src python3 -m unittest discover -s cli/tests"""
import json
import unittest

from annex_cli import logcheck


GOOD = json.dumps({"ts": "2026-10-16T10:00:00.000+02:00", "level": "info", "msg": "started", "port": 8080})


class ParseLine(unittest.TestCase):
    def test_a_conforming_line(self):
        self.assertEqual(logcheck.parse_log_line(GOOD)["msg"], "started")
        self.assertIsNotNone(logcheck.parse_log_line("  " + GOOD + "\n"))

    def test_all_four_levels(self):
        for level in ("debug", "info", "warn", "error"):
            self.assertIsNotNone(logcheck.parse_log_line(json.dumps({"level": level, "msg": "x"})), level)

    def test_not_json_or_not_an_object(self):
        for bad in ("", "listening on :8080", "INFO:     Started server process [1]", "{broken", "[1, 2]", '"text"', "123", "null"):
            self.assertIsNone(logcheck.parse_log_line(bad), bad)

    def test_json_without_level_or_msg_or_with_a_wrong_level(self):
        for bad in ({"msg": "x"}, {"level": "info"}, {"level": "warning", "msg": "x"}, {"level": "INFO", "msg": "x"},
                    {"level": "info", "msg": 5}, {"level": 3, "msg": "x"}, {"message": "x", "severity": "info"}):
            self.assertIsNone(logcheck.parse_log_line(json.dumps(bad)), bad)

    def test_extra_fields_are_fine(self):
        self.assertIsNotNone(logcheck.parse_log_line(json.dumps({"level": "error", "msg": "x", "anything": [1, {"a": 2}]})))


class Structured(unittest.TestCase):
    def test_one_good_line_is_enough(self):
        self.assertTrue(logcheck.structured(["INFO: starting", GOOD, "plain text"]))

    def test_lines_but_none_conform(self):
        self.assertFalse(logcheck.structured(["listening on :8080", "INFO:     Uvicorn running on http://0.0.0.0:8080"]))
        self.assertFalse(logcheck.structured(['{"message": "hello"}']))

    def test_no_lines_means_nothing_to_judge(self):
        self.assertIsNone(logcheck.structured([]))
        self.assertIsNone(logcheck.structured(["", "  ", "\n"]))

    def test_the_warning_only_when_lines_exist_and_none_conform(self):
        self.assertIsNone(logcheck.warning([GOOD]))
        self.assertIsNone(logcheck.warning([]))
        w = logcheck.warning(["hello world"])
        self.assertIn("print()", w)
        self.assertIn("annex contract", w)


if __name__ == "__main__":
    unittest.main()


class StoredRows(unittest.TestCase):
    """Job-only apps are checked on the lines annex-ops stored from their first run (M2.2)."""

    def test_a_job_that_logs_in_the_convention(self):
        self.assertTrue(logcheck.structured_rows([{"structured": False}, {"structured": True}]))
        self.assertIsNone(logcheck.rows_warning([{"structured": True}]))

    def test_a_job_that_prints(self):
        rows = [{"structured": False}, {"structured": False}]
        self.assertFalse(logcheck.structured_rows(rows))
        self.assertIn("logs aren't structured", logcheck.rows_warning(rows))

    def test_a_job_that_has_not_run_yet_is_not_judged(self):
        self.assertIsNone(logcheck.structured_rows([]))
        self.assertIsNone(logcheck.rows_warning([]))
