"""Delivering stored webhook events to their app, with retries.

The decisions (backoff, which event is due, which hooks are failing long enough to alert) are pure functions with
unit tests. The worker thread wraps them: every 2 s it POSTs the due events (oldest first, and a failing event holds
back the newer ones of the same hook, so the app never sees a hook's events out of order), and reports hooks that
have been failing for a while to the alert engine through the manager's queue. Delivery is at least once: apps
dedupe by X-Annex-Hook-Event.
"""
import http.client
import sys
import threading
import time
import traceback

from . import config, engine, hooks_config, hooks_store

DEFAULT_BACKOFF = (10, 30, 60, 300, 900, 3600)  # then hourly
HOUSEKEEPING_S = 60
DELIVER_TIMEOUT_S = 10


def parse_backoff(text: str) -> list[float]:
    """"10,30,60" -> [10.0, 30.0, 60.0]; anything unusable falls back to the defaults."""
    try:
        out = [float(x) for x in str(text).split(",") if x.strip()]
    except ValueError:
        out = []
    return out if out and all(x > 0 for x in out) else [float(x) for x in DEFAULT_BACKOFF]


def retry_delay(attempts: int, backoff: list[float]) -> float:
    """Seconds to wait after the `attempts`-th failed attempt (1-based); the last step repeats."""
    return backoff[min(max(attempts, 1) - 1, len(backoff) - 1)]


def due_events(pending: list[dict], now: float) -> list[dict]:
    """The events to deliver now: the oldest pending event of each (app, hook), if its time has come. A hook
    whose oldest event is still waiting for its next attempt delivers nothing, so order is kept."""
    oldest: dict[tuple, dict] = {}
    for e in pending:  # oldest first
        oldest.setdefault((e["app"], e["hook"]), e)
    return [e for e in oldest.values() if e["next_attempt_at"] is None or e["next_attempt_at"] <= now]


def failing_hooks(pending: list[dict], now: float, after_s: float) -> dict[tuple, float]:
    """(app, hook) -> since when, for hooks whose oldest pending event has been failing for over `after_s`."""
    oldest: dict[tuple, dict] = {}
    for e in pending:
        oldest.setdefault((e["app"], e["hook"]), e)
    return {k: e["received_at"] for k, e in oldest.items() if e["attempts"] > 0 and now - e["received_at"] > after_s}


def alert_changes(previous: set, current: dict) -> tuple[list, list]:
    """(hooks to report as failing now, hooks that stopped failing)."""
    return list(current), [k for k in previous if k not in current]


def delivery_headers(event: dict, token: str, hook_name: str) -> dict[str, str]:
    """Content-Type and the allowlisted provider headers as stored, plus Annex's own. X-Annex-Hook-Event is the
    original event's id for a replay, so the app can tell it has seen it."""
    headers = {k: str(v) for k, v in (event.get("headers") or {}).items()}
    if event.get("content_type"):
        headers["content-type"] = event["content_type"]
    headers.update({"X-Annex-Hook": hook_name, "X-Annex-Hook-Event": str(event.get("replay_of") or event["id"]),
                    "X-Annex-Hook-Attempt": str(event["attempts"] + 1), "X-Annex-Hook-Token": token})
    return headers


def deliver(event: dict, cfg: dict) -> tuple[bool, str | None]:
    """POST the event to the app. (True, None) on a 2xx, else (False, what went wrong). Redirects aren't followed."""
    hook = cfg["hooks"].get(event["hook"])
    if hook is None:
        return False, "the app no longer declares this webhook"
    try:
        conn = http.client.HTTPConnection(f"annex-app-{event['app']}", int(cfg["port"]), timeout=DELIVER_TIMEOUT_S)
        conn.request("POST", hook["path"], body=event["body"], headers=delivery_headers(event, cfg["token"], event["hook"]))
        resp = conn.getresponse()
        text = resp.read(200).decode(errors="replace").strip().replace("\n", " ")
        conn.close()
    except (OSError, http.client.HTTPException) as e:
        return False, f"{type(e).__name__}: {e}"[:200]
    return (True, None) if 200 <= resp.status < 300 else (False, f"HTTP {resp.status}: {text}"[:250])


def log(msg: str) -> None:
    print(f"hooks_delivery: {msg}", file=sys.stderr, flush=True)


class Worker(threading.Thread):
    def __init__(self, submit) -> None:
        super().__init__(daemon=True, name="hooks-delivery")
        self.submit = submit  # the alert manager's queue
        self.alerting: set = set()
        self.last_housekeeping = 0.0
        self.last_purge = 0.0

    def run(self) -> None:
        while True:
            time.sleep(2)
            try:
                self.cycle(time.time())
            except Exception:  # noqa: BLE001: the worker must never die
                log("error:\n" + traceback.format_exc())

    def cycle(self, now: float) -> None:
        t = config.tuning()
        if now - self.last_housekeeping >= HOUSEKEEPING_S:
            self.last_housekeeping = now
            self.housekeeping(t, now)
        rows = hooks_store.pending()
        backoff = parse_backoff(t.hook_backoff_s)
        for row in due_events(rows, now):
            event = hooks_store.full_event(row["id"])
            cfg = hooks_config.load(row["app"])
            if event is None or cfg is None:
                continue  # delivered meanwhile, or the app's hooks file is gone: housekeeping fails the event
            ok, error = deliver(event, cfg)
            attempts = event["attempts"] + 1
            if ok:
                hooks_store.mark_delivered(event["id"], attempts)
            else:
                hooks_store.mark_retry(event["id"], attempts, error or "unknown error", retry_delay(attempts, backoff))
                log(f"event {event['id']} ({event['app']}/{event['hook']}) attempt {attempts} failed: {error}")
        self.alerts(hooks_store.pending(), t, now)

    def alerts(self, rows: list[dict], t, now: float) -> None:
        failing = failing_hooks(rows, now, t.hook_alert_after_s)
        raise_, clear = alert_changes(self.alerting, failing)
        for app, hook in raise_:
            self.submit(engine.HookFailing(app, hook, failing[(app, hook)]))  # repeated each cycle: the engine expires stale ones
        for app, hook in clear:
            self.submit(engine.HookOk(app, hook))
        self.alerting = set(failing)

    def housekeeping(self, t, now: float) -> None:
        known = {}
        for app in {r["app"] for r in hooks_store.pending()}:
            cfg = hooks_config.load(app)
            if cfg:
                known[app] = set(cfg["hooks"])
        hooks_store.housekeeping(t.hook_max_age_s, known)
        if now - self.last_purge >= 86400:
            self.last_purge = now
            hooks_store.purge(7)
