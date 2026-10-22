"""Which of ops' long-running threads are alive and still making progress (hardening B1, D81).

Every loop thread registers a name and its expected period (how long one pass may take, sleep included), then calls
`beat(name)` on every pass. `/healthz` shows the table and answers 503 when a thread is dead (it exited) or stale (no
beat for more than `STALE_FACTOR` periods). Followers come and go, so they register when they start and `retire` when
they are stopped on purpose; one that is still registered after its thread ended is a dead one.

Gauges are small callables (queue depths, rows waiting, rows lost) that `/healthz` and `ctl logs-stats` print next to
the threads. The verdict is a pure function (`verdict`); the registry is a dict behind a lock.
"""
import json
import threading
import time
import urllib.error
import urllib.request

STALE_FACTOR = 3  # "stale" = no progress for this many expected periods

_lock = threading.Lock()
_entries: dict[str, dict] = {}
_gauges: dict[str, object] = {}


def register(name: str, period_s: float, thread: threading.Thread | None = None) -> None:
    """Start tracking `name` (replaces an entry of the same name: a restarted follower). `thread` defaults to the caller's."""
    now = time.monotonic()
    with _lock:
        _entries[name] = {"period_s": float(period_s), "thread": thread or threading.current_thread(), "last": now}


def beat(name: str, period_s: float | None = None) -> None:
    """The thread made progress. `period_s` updates the expected period (loops whose sleep is a setting)."""
    now = time.monotonic()
    with _lock:
        e = _entries.get(name)
        if e is not None:
            e["last"] = now
            if period_s is not None:
                e["period_s"] = float(period_s)


def retire(name: str) -> None:
    """The thread ended on purpose: stop tracking it."""
    with _lock:
        _entries.pop(name, None)


def gauge(name: str, fn) -> None:
    """`fn()` returns a dict of numbers shown under `queues` in /healthz (called at each request)."""
    with _lock:
        _gauges[name] = fn


def reset() -> None:
    """For tests."""
    with _lock:
        _entries.clear()
        _gauges.clear()


def verdict(alive: bool, age_s: float, period_s: float) -> str:
    """ok, stale (alive but silent for more than STALE_FACTOR periods) or dead (the thread ended)."""
    if not alive:
        return "dead"
    return "stale" if age_s > STALE_FACTOR * period_s else "ok"


def snapshot(now: float | None = None) -> list[dict]:
    """One row per registered thread, sorted by name: name, state, age_s (since its last beat), period_s."""
    now = time.monotonic() if now is None else now
    with _lock:
        items = [(n, dict(e)) for n, e in _entries.items()]
    rows = []
    for name, e in sorted(items):
        age = max(0.0, now - e["last"])
        rows.append({"name": name, "state": verdict(e["thread"].is_alive(), age, e["period_s"]),
                     "age_s": round(age, 1), "period_s": e["period_s"]})
    return rows


def all_ok(rows: list[dict]) -> bool:
    return all(r["state"] == "ok" for r in rows)


def report(now: float | None = None) -> dict:
    """What /healthz adds to its answer: {"threads": [...], "queues": {gauge: {...}}, "threads_ok": bool}."""
    with _lock:
        gauges = dict(_gauges)
    queues = {}
    for name, fn in sorted(gauges.items()):
        try:
            queues[name] = fn()
        except Exception as e:  # noqa: BLE001: a broken gauge must not take /healthz down
            queues[name] = {"error": f"{type(e).__name__}: {e}"}
    rows = snapshot(now)
    return {"threads_ok": all_ok(rows), "threads": rows, "queues": queues}


def fetch(base_url: str, timeout: float = 2.0) -> dict:
    """annex-ops's /healthz as another process sees it (the console, `ctl`): the parsed answer, 503 included, or
    {"unreachable": why}. Only the standard library, and no database."""
    try:
        try:
            with urllib.request.urlopen(base_url.rstrip("/") + "/healthz", timeout=timeout) as r:
                body = r.read()
        except urllib.error.HTTPError as e:  # 503 carries the table too
            body = e.read()
        data = json.loads(body)
        return data if isinstance(data, dict) else {"unreachable": "unexpected answer"}
    except (OSError, ValueError) as e:
        return {"unreachable": str(getattr(e, "reason", e)) or type(e).__name__}
