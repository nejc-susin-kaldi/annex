"""The health checker's decisions (M2.6): what is checked, what counts as up, when the engine is told."""
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

from annex_ops import engine as eng, health as h

WEB = {"com.docker.compose.project": "annex-app-x", "com.kaldi.annex.app": "x", "com.kaldi.annex.healthcheck": "/healthz",
       "traefik.http.services.app-x.loadbalancer.server.port": "8080"}


def test_one_target_per_web_app_plus_the_front_door():
    job = {"com.docker.compose.project": "annex-app-j", "com.kaldi.annex.app": "j"}  # no health path, no port: not a web app
    platform = {"com.docker.compose.project": "annex", "com.docker.compose.service": "traefik"}
    ts = h.targets([{"Labels": WEB}, {"Labels": job}, {"Labels": platform}, {"Labels": None}], "https://home.annex.home.arpa/")
    assert [(t.subject, t.url, t.kind) for t in ts] == [("app:x", "http://annex-app-x:8080/healthz", "app"),
                                                         ("platform:front-door", "https://home.annex.home.arpa/", "front-door")]
    assert ts[0].name == "x" and ts[1].name == "_front-door"


def test_a_custom_health_path_and_port_are_used_and_a_bad_port_label_is_ignored():
    labels = {**WEB, "com.kaldi.annex.healthcheck": "/api/ready", "traefik.http.services.app-x.loadbalancer.server.port": "3000"}
    assert h.targets([{"Labels": labels}], "")[0].url == "http://annex-app-x:3000/api/ready"
    assert h.targets([{"Labels": {**WEB, "traefik.http.services.app-x.loadbalancer.server.port": "x;rm"}}], "") == []


def test_no_front_door_url_means_no_front_door_check():
    assert h.targets([], "") == []


def test_what_counts_as_up():
    assert h.judge("app", 200) and not h.judge("app", 302) and not h.judge("app", 500) and not h.judge("app", None)
    assert h.judge("front-door", 302) and not h.judge("front-door", 401) and not h.judge("front-door", 200) and not h.judge("front-door", None)


def test_the_failure_is_explained_in_words():
    assert h.explain("app", 200, None) is None and h.explain("front-door", 302, None) is None
    assert h.explain("app", 503, None) == "got HTTP 503, want 200"
    assert h.explain("front-door", 401, None) == "got HTTP 401, want 302 (the login redirect)"
    assert h.explain("app", None, "[Errno 111] Connection refused") == "[Errno 111] Connection refused"
    assert h.explain("app", None, None) == "no answer"


def test_down_after_two_failures_up_after_two_successes():
    d = h.Debounce()
    assert [d.update(ok, 2, 2) for ok in (True, True)] == [None, False]      # the first confirmed state is told too
    assert d.update(False, 2, 2) is None                                    # one failure is not down
    assert d.update(True, 2, 2) is None and d.update(False, 2, 2) is None   # a blip resets the count
    assert d.update(False, 2, 2) is True                                    # the second in a row
    assert d.update(False, 2, 2) is None                                    # told once
    assert d.update(True, 2, 2) is None and d.update(True, 2, 2) is False   # two good ones clear it
    assert d.update(True, 2, 2) is None


def test_thresholds_come_from_tuning():
    d = h.Debounce()
    assert d.update(False, 1, 1) is True and d.update(True, 1, 1) is False
    d = h.Debounce()
    assert [d.update(False, 3, 2) for _ in range(3)] == [None, None, True]


def test_a_result_is_up_down_or_stale():
    now = 10_000.0
    assert h.state_of(None, now, 60) == {"state": "unknown", "detail": "no result yet"}
    assert h.state_of({"ts": now - 30, "ok": True, "error": None}, now, 60)["state"] == "up"
    down = h.state_of({"ts": now - 30, "ok": False, "error": "got HTTP 500, want 200"}, now, 60)
    assert down["state"] == "down" and down["detail"] == "got HTTP 500, want 200"
    assert h.state_of({"ts": now - 400, "ok": True, "error": None}, now, 60)["state"] == "unknown"      # the checker isn't running
    assert h.state_of({"ts": now - 400, "ok": True, "error": None}, now, 200)["state"] == "up"          # three intervals of 200 s


def test_the_tile_counts():
    now = 100.0
    rows = {"app:a": {"ts": now, "ok": True, "error": None}, "app:b": {"ts": now, "ok": False, "error": "x"},
            "platform:front-door": {"ts": now, "ok": True, "error": None}, "app:old": {"ts": 0, "ok": True, "error": None}}
    assert h.tile(rows, now + 400, 60) == {"up": 0, "down": 0, "status": "everything is up"}   # all stale: nothing counted
    t = h.tile(rows, now, 60)
    assert (t["up"], t["down"], t["status"]) == (3, 1, "1 down")


def test_the_engine_hears_the_same_input_as_before_under_a_new_name():
    e = eng.Engine()
    e.handle(0, eng.Seen("c1", "annex-app-x", "app:x", True, "healthy"))
    acts = e.handle(1, eng.HealthState("app:x", True, "got HTTP 500, want 200"))
    assert [(a.kind, a.subject) for a in acts] == [("open", "app:x")]
    assert [a.kind for a in e.handle(2, eng.HealthState("app:x", False))] == ["clear_signal", "resolve"]


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/redirect":
            self.send_response(302); self.send_header("Location", "/elsewhere"); self.end_headers()
        elif self.path == "/needs-accept":
            code = 302 if "text/html" in (self.headers.get("Accept") or "") else 401
            self.send_response(code); self.send_header("Location", "/login"); self.end_headers()
        elif self.path == "/broken":
            self.send_response(500); self.end_headers()
        else:
            self.send_response(200); self.send_header("Content-Length", "2"); self.end_headers(); self.wfile.write(b"ok")

    def log_message(self, *a):
        pass


def _serve():
    srv = HTTPServer(("127.0.0.1", 0), _Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def test_fetch_follows_nothing_sends_accept_for_the_front_door_and_reports_errors():
    srv = _serve()
    base = f"http://127.0.0.1:{srv.server_port}"
    try:
        assert h.fetch(h.Target("app:x", "x", base + "/ok", "app"), 3)[:2] == (200, None)
        assert h.fetch(h.Target("app:x", "x", base + "/redirect", "app"), 3)[0] == 302          # not followed: not an up app
        assert h.fetch(h.Target("app:x", "x", base + "/broken", "app"), 3)[0] == 500
        assert h.fetch(h.Target("platform:front-door", "_front-door", base + "/needs-accept", "front-door"), 3)[0] == 302
        assert h.fetch(h.Target("app:x", "x", base + "/needs-accept", "app"), 3)[0] == 401      # an app request has no Accept
    finally:
        srv.shutdown()
    status, err, ms = h.fetch(h.Target("app:x", "x", "http://127.0.0.1:1/", "app"), 2)
    assert status is None and err and ms >= 0


def test_a_result_from_before_the_container_started_is_not_current():
    assert h.stale_for(None, 100.0) and h.stale_for(None, None)
    assert h.stale_for({"ts": 50.0, "ok": True, "error": None}, 100.0)          # about the container that was replaced
    assert not h.stale_for({"ts": 150.0, "ok": True, "error": None}, 100.0)
    assert not h.stale_for({"ts": 50.0, "ok": True, "error": None}, None)       # start time unknown: use what there is


def test_ops_is_a_platform_check_that_is_up_on_a_200():
    ts = h.targets([], "https://home.annex.home.arpa/", "http://ops:8000/healthz")
    ops = ts[-1]
    assert (ops.subject, ops.name, ops.url, ops.kind) == ("platform:ops", "_ops", "http://ops:8000/healthz", "platform")
    assert [t.subject for t in h.targets([], "", "")] == []          # no URL, no check
    assert h.judge("platform", 200) and not h.judge("platform", 503) and not h.judge("platform", 302) and not h.judge("platform", None)
    assert h.explain("platform", 503, None).startswith("got HTTP 503, want 200") and "thread" in h.explain("platform", 503, None)
    assert h.PLATFORM_CHECKS == {"_front-door": "platform:front-door", "_ops": "platform:ops"}
