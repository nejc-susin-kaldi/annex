"""The Python logging convention: one JSON object per line with ts, level, msg; request_id and user inside a request.
Run: python3 -m unittest discover -s runtime/tests"""
import io
import json
import logging
import os
import sys
import unittest
from datetime import datetime
from pathlib import Path

os.environ["LOG_LEVEL"] = "debug"
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import annex_runtime as rt  # noqa: E402


def capture(fn) -> list[dict]:
    """Run fn with the runtime's handler writing to a buffer; return the parsed lines."""
    buf = io.StringIO()
    handler = logging.getLogger().handlers[0]
    old, handler.stream = handler.stream, buf
    try:
        fn()
    finally:
        handler.stream = old
    return [json.loads(line) for line in buf.getvalue().splitlines()]


class Convention(unittest.TestCase):
    def test_every_line_is_json_with_ts_level_msg(self):
        (line,) = capture(lambda: rt.log.info("report written", rows=12, ms=340))
        self.assertEqual((line["level"], line["msg"], line["rows"], line["ms"]), ("info", "report written", 12, 340))
        self.assertIsNotNone(datetime.fromisoformat(line["ts"]).tzinfo, "ts has a time zone")

    def test_levels_are_debug_info_warn_error(self):
        lines = capture(lambda: (rt.log.debug("a"), rt.log.info("b"), rt.log.warn("c"), rt.log.warning("d"), rt.log.error("e")))
        self.assertEqual([x["level"] for x in lines], ["debug", "info", "warn", "warn", "error"])

    def test_the_threshold_comes_from_log_level(self):
        root = logging.getLogger()
        old = root.level
        root.setLevel(logging.WARNING)
        try:
            lines = capture(lambda: (rt.log.debug("no"), rt.log.info("no"), rt.log.warn("yes"), rt.log.error("yes")))
        finally:
            root.setLevel(old)
        self.assertEqual([x["msg"] for x in lines], ["yes", "yes"])

    def test_request_context_adds_request_id_and_user_and_only_inside(self):
        def go():
            with rt.request_context({"remote-user": "cm", "x-request-id": "abcd1234efgh"}) as rid:
                rt.log.info("inside")
                self.assertEqual(rid, "abcd1234efgh")
            rt.log.info("outside")
        inside, outside = capture(go)
        self.assertEqual((inside["request_id"], inside["user"]), ("abcd1234efgh", "cm"))
        self.assertNotIn("request_id", outside)
        self.assertNotIn("user", outside)

    def test_an_unsafe_or_missing_request_id_is_replaced(self):
        for bad in ("", "short", "has spaces in it", "inject\nnewline1234", "x" * 65, "ünïcode-id-12345"):
            with rt.request_context({"x-request-id": bad}) as rid:
                self.assertRegex(rid, r"^[0-9a-f]{16}$", bad)
        with rt.request_context({}) as rid:
            self.assertRegex(rid, r"^[0-9a-f]{16}$")

    def test_header_names_are_case_insensitive_enough(self):
        with rt.request_context({"X-Request-Id": "req-12345678", "Remote-User": "sam"}) as rid:
            self.assertEqual(rid, "req-12345678")
            (line,) = capture(lambda: rt.log.info("x"))
            self.assertEqual(line["user"], "sam")

    def test_new_request_id_is_16_hex_chars_and_different_each_time(self):
        a, b = rt.new_request_id(), rt.new_request_id()
        self.assertRegex(a, r"^[0-9a-f]{16}$")
        self.assertNotEqual(a, b)

    def test_a_field_cannot_overwrite_the_required_ones(self):
        (line,) = capture(lambda: rt.log.info("real", level="fake", ts="fake", user="fake"))
        self.assertEqual((line["level"], line["msg"]), ("info", "real"))
        self.assertNotEqual(line["ts"], "fake")
        self.assertEqual((line["level_"], line["ts_"], line["user_"]), ("fake", "fake", "fake"))

    def test_exception_logs_the_type_message_and_stack(self):
        def go():
            try:
                int("x")
            except ValueError:
                rt.log.exception("bad input", field="age")
        (line,) = capture(go)
        self.assertEqual((line["level"], line["field"]), ("error", "age"))
        self.assertTrue(line["exc"].startswith("ValueError:"))
        self.assertIn("Traceback", line["stack"])

    def test_values_that_are_not_json_are_stringified_not_fatal(self):
        (line,) = capture(lambda: rt.log.info("odd", when=datetime(2026, 1, 2), thing=object))
        self.assertTrue(line["when"].startswith("2026-01-02"))

    def test_library_logging_comes_out_in_the_same_format(self):
        (line,) = capture(lambda: logging.getLogger("uvicorn.error").warning("from a library"))
        self.assertEqual((line["level"], line["msg"]), ("warn", "from a library"))

    def test_a_multiline_message_stays_one_line(self):
        buf = io.StringIO()
        handler = logging.getLogger().handlers[0]
        old, handler.stream = handler.stream, buf
        try:
            rt.log.info("line one\nline two")
        finally:
            handler.stream = old
        self.assertEqual(len(buf.getvalue().splitlines()), 1)


if __name__ == "__main__":
    unittest.main()


class BrowserErrors(unittest.TestCase):
    """The server side of the browser error reports (M2.3)."""

    def setUp(self):
        rt._browser_hits.clear()

    def test_a_report_becomes_one_error_line_with_the_user_and_request_id(self):
        body = json.dumps({"msg": "x is not defined", "stack": "ReferenceError: x\n at a.js:1", "url": "/page"}).encode()

        def go():
            with rt.request_context({"remote-user": "sam", "x-request-id": "abcdef0123456789"}):
                self.assertEqual(rt.report_browser_error({"remote-user": "sam"}, body), 204)
        [line] = capture(go)
        self.assertEqual((line["level"], line["msg"], line["source"], line["user"], line["request_id"]),
                         ("error", "browser error: x is not defined", "browser", "sam", "abcdef0123456789"))
        self.assertEqual(line["url"], "/page")

    def test_bad_big_and_too_many(self):
        h = {"remote-user": "sam"}
        self.assertEqual(rt.report_browser_error(h, b"not json"), 400)
        self.assertEqual(rt.report_browser_error(h, b"[1]"), 400)
        self.assertEqual(rt.report_browser_error(h, b"x" * 9000), 413)
        body = b'{"msg": "e"}'
        results = []
        capture(lambda: results.extend(rt.report_browser_error(h, body) for _ in range(12)))
        self.assertEqual(results, [204] * 10 + [429] * 2)
        self.assertEqual(rt.report_browser_error({"remote-user": "cm"}, body), 204)  # another user has their own allowance

    def test_the_script_is_served_from_the_file_next_to_the_runtime(self):
        self.assertIn("unhandledrejection", rt.browser_errors_js())
