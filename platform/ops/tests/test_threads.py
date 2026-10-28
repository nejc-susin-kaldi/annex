"""The thread registry (B1, D81): the verdict, the table, and /healthz's answer."""
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from annex_ops import threads


@pytest.fixture(autouse=True)
def clean():
    threads.reset()
    yield
    threads.reset()


class Ended:
    def is_alive(self):
        return False


class Running:
    def is_alive(self):
        return True


def test_the_verdict_is_dead_before_stale_before_ok():
    assert threads.verdict(True, 0, 10) == "ok"
    assert threads.verdict(True, 30, 10) == "ok"          # exactly three periods is still fine
    assert threads.verdict(True, 30.1, 10) == "stale"
    assert threads.verdict(False, 0, 10) == "dead"
    assert threads.verdict(False, 999, 10) == "dead"


def test_a_thread_that_ended_is_dead_and_one_that_stopped_beating_is_stale():
    threads.register("gone", 10, Ended())
    threads.register("fine", 10, Running())
    now = time.monotonic()
    rows = {r["name"]: r["state"] for r in threads.snapshot(now + 1)}
    assert rows == {"fine": "ok", "gone": "dead"}
    assert not threads.all_ok(threads.snapshot(now + 1))
    rows = {r["name"]: r["state"] for r in threads.snapshot(now + 31)}
    assert rows == {"fine": "stale", "gone": "dead"}     # 31 s on, nobody beat


def test_beat_resets_the_age_and_can_change_the_period():
    threads.register("t", 10, Running())
    threads.beat("t", period_s=100)
    row = threads.snapshot(time.monotonic() + 200)[0]
    assert row["period_s"] == 100 and row["state"] == "ok" and row["age_s"] >= 199
    assert threads.snapshot(time.monotonic() + 301)[0]["state"] == "stale"
    threads.beat("unknown")   # not registered: ignored, no error


def test_retire_removes_a_thread_and_a_real_thread_registers_itself():
    done = threading.Event()
    t = threading.Thread(target=lambda: (threads.register("worker", 5), done.wait(5)), daemon=True)
    t.start()
    time.sleep(0.1)
    assert [r["name"] for r in threads.snapshot()] == ["worker"] and threads.all_ok(threads.snapshot())
    done.set()
    t.join(2)
    assert threads.snapshot()[0]["state"] == "dead"      # ended without retiring
    threads.retire("worker")
    assert threads.snapshot() == [] and threads.all_ok([])


def test_the_report_has_gauges_and_survives_a_broken_one():
    threads.register("t", 10, Running())
    threads.gauge("sink", lambda: {"buffered": 3, "lost": 7})
    threads.gauge("bad", lambda: 1 / 0)
    r = threads.report()
    assert r["threads_ok"] and r["queues"]["sink"] == {"buffered": 3, "lost": 7}
    assert "ZeroDivisionError" in r["queues"]["bad"]["error"]


def test_healthz_answers_503_for_a_dead_thread(monkeypatch):
    from annex_ops import app as app_module, db

    class Conn:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def execute(self, *a):
            return None

    monkeypatch.setattr(db, "connect", lambda: Conn())
    monkeypatch.setattr(app_module.config, "WATCHERS", True)
    threads.register("fine", 10, Running())
    ok = app_module.healthz()
    assert ok["ok"] and ok["threads"][0]["name"] == "fine"
    threads.register("engine", 10, Ended())
    bad = app_module.healthz()
    body = json.loads(bad.body)
    assert bad.status_code == 503 and not body["ok"] and "engine (dead)" in body["error"]
    assert {t["name"]: t["state"] for t in body["threads"]} == {"fine": "ok", "engine": "dead"}


def test_fetch_reads_a_503_and_reports_an_unreachable_server():
    class H(BaseHTTPRequestHandler):
        def do_GET(self):
            body = json.dumps({"ok": False, "threads": [{"name": "x", "state": "dead", "age_s": 1, "period_s": 2}]}).encode()
            self.send_response(503)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass

    srv = HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        got = threads.fetch(f"http://127.0.0.1:{srv.server_port}")
    finally:
        srv.shutdown()
    assert got["threads"][0]["state"] == "dead"
    assert "unreachable" in threads.fetch("http://127.0.0.1:1", timeout=0.5)
