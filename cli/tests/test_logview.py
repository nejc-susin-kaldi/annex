"""Run: PYTHONPATH=cli/src python3 -m unittest discover -s cli/tests"""
import unittest

from annex_cli import logview


ROW = {"ts": "2026-10-16T10:50:00.952516Z", "container": "annex-app-x", "release": 44, "stream": "stdout", "level": "info",
       "request_id": None, "msg": "job started", "fields": {"month": "latest", "notify": False, "note": "two words", "n": 3}}


class Render(unittest.TestCase):
    def test_a_structured_line(self):
        self.assertEqual(logview.render(ROW),
                         '2026-10-16T10:50:00.952Z r44 info  job started  month=latest notify=false note="two words" n=3')

    def test_a_plain_line_is_shown_as_written(self):
        row = {"ts": "2026-10-16T10:50:00.100000Z", "container": "annex-app-x", "release": 2, "level": None, "request_id": None,
               "msg": "got a request for /x", "fields": None}
        self.assertEqual(logview.render(row), "2026-10-16T10:50:00.100Z r2 got a request for /x")

    def test_request_id_job_and_gateway_tags(self):
        row = {**ROW, "container": "job:refresh", "request_id": "abc", "fields": {}}
        self.assertEqual(logview.render(row), "2026-10-16T10:50:00.952Z r44 [job:refresh] info  job started  request_id=abc")
        self.assertIn("[gateway]", logview.render({**ROW, "container": "gateway"}))

    def test_a_line_without_a_release(self):
        self.assertTrue(logview.render({**ROW, "release": None}).startswith("2026-10-16T10:50:00.952Z info "))


class Release(unittest.TestCase):
    def test_forms(self):
        self.assertEqual((logview.release_number("r4"), logview.release_number("4"), logview.release_number(None)), (4, 4, None))
        with self.assertRaises(ValueError):
            logview.release_number("four")


if __name__ == "__main__":
    unittest.main()
