"""The Follower thread itself, with a fake Docker: both modes must run (a method named like a Thread internal once
killed every follower), and exact mode keeps lines that share a microsecond and holds the cursor back until the
rows are stored (M2.2)."""
import time

from annex_ops import docker_api, follow_logs as f

T = 1_800_000_000 * 10**9   # an epoch nanosecond, 2027: after any `lookback`


class MemStore:
    def __init__(self):
        self.d = {}

    def get(self, k):
        return self.d.get(k)

    def set(self, k, ts):
        self.d[k] = ts

    def delete(self, k):
        self.d.pop(k, None)


def wait_for(cond, limit=5.0):
    end = time.time() + limit
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.02)
    return False


def test_exact_mode_keeps_equal_microsecond_lines_in_order_and_saves_the_cursor(monkeypatch):
    lines = [(T + 1, "stdout", "a"), (T + 2, "stdout", "b"), (T + 3, "stderr", "c")]   # the same microsecond

    def fake(container, since_ns, follow=True, timeout=120):
        yield from [x for x in lines if x[0] >= since_ns]

    monkeypatch.setattr(docker_api, "stream_logs_ns", fake)
    got, store = [], MemStore()
    fol = f.Follower("c1", lambda ns, s, line: got.append((ns, s, line)), store, key="ship:x:c1", exact=True,
                     before_save=lambda: True, start_ns=T)
    fol.start()
    assert wait_for(lambda: len(got) >= 3)
    fol.stop()
    fol.join(5)
    assert not fol.is_alive()
    assert got[:3] == lines
    assert abs(store.get("ship:x:c1") - (T + 3) / 1e9) < 1e-3


def test_the_cursor_waits_while_the_rows_are_not_stored(monkeypatch):
    monkeypatch.setattr(docker_api, "stream_logs_ns", lambda c, s, follow=True, timeout=120: iter([(T + 5, "stdout", "x")]))
    store = MemStore()
    fol = f.Follower("c1", lambda *a: None, store, key="k", exact=True, before_save=lambda: False, start_ns=T)
    fol.start()
    time.sleep(0.5)
    fol.stop()
    fol.join(5)
    assert abs(store.get("k") - T / 1e9) < 1e-3     # only the starting point: never moved past unsaved rows


def test_a_restart_replays_a_second_before_the_cursor(monkeypatch):
    asked = []

    def fake(container, since_ns, follow=True, timeout=120):
        asked.append(since_ns)
        return iter([])

    monkeypatch.setattr(docker_api, "stream_logs_ns", fake)
    store = MemStore()
    store.set("k", (T + 5 * 10**9) / 1e9)
    fol = f.Follower("c1", lambda *a: None, store, key="k", exact=True, before_save=lambda: True)
    fol.start()
    assert wait_for(lambda: asked)
    fol.stop()
    fol.join(5)
    assert abs(asked[0] - (T + 4 * 10**9)) < 10**6


def test_the_classic_mode_still_runs(monkeypatch):
    now = time.time()

    def fake(container, since, follow=True, timeout=120):
        yield from [(now + 1, "stdout", "one"), (now + 2, "stdout", "two")]
        raise docker_api.DockerError(404, "gone")

    monkeypatch.setattr(docker_api, "stream_logs", fake)
    got, store = [], MemStore()
    fol = f.Follower("annex-traefik-1", lambda ts, s, line: got.append(line), store)
    fol.start()
    assert wait_for(lambda: got == ["one", "two"])
    fol.stop()
    fol.join(5)
    assert not fol.is_alive() and "annex-traefik-1" in store.d


def test_a_follower_survives_an_error_nobody_expected(monkeypatch):
    """B1: the cursor store raising something that isn't an OpsError (a driver error) used to end the thread, and
    nothing restarted it. Now it is logged, waited out and retried: once the store works the follower reads lines."""
    from annex_ops import threads

    class Flaky(MemStore):
        def __init__(self):
            super().__init__()
            self.failures = 2

        def get(self, k):
            if self.failures:
                self.failures -= 1
                raise RuntimeError("a driver error nobody expected")
            return super().get(k)

    monkeypatch.setattr(f, "RETRY_S", 0.05)
    monkeypatch.setattr(docker_api, "stream_logs_ns", lambda c, s, follow=True, timeout=120: iter([(T + 1, "stdout", "a")]))
    got, store = [], Flaky()
    fol = f.Follower("c1", lambda ns, s, line: got.append(line), store, key="k", exact=True, before_save=lambda: True, start_ns=T)
    fol.start()
    assert wait_for(lambda: got)                 # it got past two failures
    assert fol.is_alive()
    assert any(r["name"] == "follow-k" and r["state"] == "ok" for r in threads.snapshot())
    fol.stop()
    fol.join(5)
    assert not fol.is_alive()
    assert not any(r["name"] == "follow-k" for r in threads.snapshot())   # stopped on purpose: no longer listed


def test_a_crash_inside_the_loop_does_not_end_the_follower(monkeypatch):
    calls = []

    def fake(container, since_ns, follow=True, timeout=120):
        calls.append(since_ns)
        if len(calls) == 1:
            raise MemoryError("not an OSError, not a DockerError")
        return iter([(T + 9, "stdout", "later")])

    monkeypatch.setattr(f, "RETRY_S", 0.05)
    monkeypatch.setattr(docker_api, "stream_logs_ns", fake)
    got = []
    fol = f.Follower("c1", lambda ns, s, line: got.append(line), MemStore(), key="k2", exact=True, before_save=lambda: True, start_ns=T)
    fol.start()
    assert wait_for(lambda: got == ["later"])
    assert fol.is_alive()
    fol.stop()
    fol.join(5)
