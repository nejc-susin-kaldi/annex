"""The cross-site check: csrf_violation (any framework) and CsrfMiddleware (ASGI).
Run: python3 -m unittest discover -s runtime/tests"""
import asyncio
import os
import sys
import unittest
from pathlib import Path

os.environ["LOG_LEVEL"] = "debug"  # as test_logging.py: the runtime reads it once, at import
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import annex_runtime as rt  # noqa: E402

HOST = {"host": "myapp.annex.lan:8443"}


def h(**kw):
    return {**HOST, **{k.replace("_", "-"): v for k, v in kw.items()}}


class Violation(unittest.TestCase):
    def setUp(self):
        os.environ.pop("ANNEX_CSRF", None)

    def test_no_browser_headers_pass(self):
        for m in ("POST", "PUT", "PATCH", "DELETE"):
            self.assertIsNone(rt.csrf_violation(m, HOST))

    def test_safe_methods_are_never_checked(self):
        self.assertIsNone(rt.csrf_violation("GET", h(sec_fetch_site="cross-site", origin="https://evil.example")))
        self.assertIsNone(rt.csrf_violation("HEAD", h(sec_fetch_site="cross-site")))

    def test_sec_fetch_site(self):
        for ok in ("same-origin", "none", "Same-Origin"):
            self.assertIsNone(rt.csrf_violation("POST", h(sec_fetch_site=ok)))
        for bad in ("cross-site", "same-site", "garbage"):
            self.assertIn(bad, rt.csrf_violation("POST", h(sec_fetch_site=bad)))

    def test_origin_must_be_the_apps_own_host(self):
        self.assertIsNone(rt.csrf_violation("POST", h(origin="https://myapp.annex.lan:8443")))
        self.assertIsNone(rt.csrf_violation("POST", h(origin="https://MyApp.annex.lan:8443/")))
        self.assertTrue(rt.csrf_violation("POST", h(origin="https://other.annex.lan:8443")))   # a sibling app
        self.assertTrue(rt.csrf_violation("POST", h(origin="https://myapp.annex.lan")))         # other port
        self.assertTrue(rt.csrf_violation("POST", h(origin="null")))
        self.assertTrue(rt.csrf_violation("POST", h(origin="")))

    def test_forwarded_host_counts_as_own_host(self):
        hdrs = {"host": "app:8080", "x-forwarded-host": "myapp.annex.lan", "origin": "https://myapp.annex.lan"}
        self.assertIsNone(rt.csrf_violation("POST", hdrs))
        self.assertTrue(rt.csrf_violation("POST", {**hdrs, "origin": "https://evil.example"}))

    def test_same_origin_browser_post_passes(self):
        self.assertIsNone(rt.csrf_violation("POST", h(sec_fetch_site="same-origin", origin="https://myapp.annex.lan:8443")))

    def test_webhook_delivery_headers_pass(self):
        # what annex-ops sends (hooks_delivery.delivery_headers): no Origin, no Sec-Fetch-Site
        hdrs = {"host": "myapp:8080", "content-type": "application/json", "user-agent": "annex-ops",
                "x-annex-hook": "trello", "x-annex-hook-event": "7", "x-annex-hook-token": "t", "x-annex-hook-attempt": "1"}
        self.assertIsNone(rt.csrf_violation("POST", hdrs))

    def test_opt_out(self):
        bad = h(sec_fetch_site="cross-site", origin="https://evil.example")
        for off in ("off", "OFF", "false", "0", "no"):
            os.environ["ANNEX_CSRF"] = off
            self.assertIsNone(rt.csrf_violation("POST", bad), off)
        for on in ("on", "true", ""):
            os.environ["ANNEX_CSRF"] = on
            self.assertTrue(rt.csrf_violation("POST", bad), on)
        os.environ.pop("ANNEX_CSRF")


class Middleware(unittest.TestCase):
    def setUp(self):
        os.environ.pop("ANNEX_CSRF", None)

    def call(self, method, headers):
        reached, sent = [], []

        async def inner(scope, receive, send):
            reached.append(1)
            await send({"type": "http.response.start", "status": 204, "headers": []})

        async def send(msg):
            sent.append(msg)

        scope = {"type": "http", "method": method, "path": "/x",
                 "headers": [(k.encode(), v.encode()) for k, v in headers.items()]}
        asyncio.run(rt.CsrfMiddleware(inner)(scope, None, send))
        return bool(reached), sent[0]["status"]

    def test_refuses_cross_site_post_with_403(self):
        self.assertEqual(self.call("POST", h(origin="https://evil.example", sec_fetch_site="cross-site")), (False, 403))

    def test_passes_same_origin_and_headerless(self):
        self.assertEqual(self.call("POST", h(origin="https://myapp.annex.lan:8443", sec_fetch_site="same-origin")), (True, 204))
        self.assertEqual(self.call("POST", HOST), (True, 204))

    def test_get_and_non_http_pass(self):
        self.assertEqual(self.call("GET", h(sec_fetch_site="cross-site")), (True, 204))
        reached = []

        async def inner(scope, receive, send):
            reached.append(scope["type"])
        asyncio.run(rt.CsrfMiddleware(inner)({"type": "lifespan"}, None, None))
        self.assertEqual(reached, ["lifespan"])


if __name__ == "__main__":
    unittest.main()
