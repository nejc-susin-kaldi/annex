"""Health checks (M2.6): annex-ops checks every web app and the front door itself.

Every `check_interval_s` (60) the checker lists the app containers, builds the URL of each web app's health check from its
labels (`com.kaldi.annex.healthcheck` and the Traefik port label) and requests it over annex-edge; an app counts as up on
a 200. The front door is requested at ANNEX_FRONTDOOR_URL with `Accept: text/html` and counts as up on a 302 (Authelia
answers 302 only to a request with an Accept header, D41; TLS isn't verified in local mode). After 2 failures in a row an
app is confirmed down, after 2 successes up again: the engine hears about the confirmed state as `HealthState`, with the
same subjects as before (`app:<name>`, `platform:front-door`), so incidents carry over. Results are stored in
`annex_ops.health_checks` (7 days), which `annex status`, the console and the discovery tile read.

What decides something is a pure function with a unit test: `targets`, `judge_*`, `Debounce`, `state_of`, `tile`.
"""
import ssl
import sys
import threading
import time
import traceback
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

from . import config, db, docker_api, engine
from .errors import OpsError

FRONT_DOOR = "platform:front-door"
FRONT_DOOR_NAME = "_front-door"  # the `app` column of its results
RETENTION_DAYS = 7
MIN_FRESH_S = 180  # a result older than this (and three intervals) isn't current: the checker isn't running


@dataclass(frozen=True)
class Target:
    subject: str   # app:<name> or platform:front-door: what the engine calls it
    name: str      # the `app` column of its results
    url: str
    kind: str      # app | front-door


def log(msg: str) -> None:
    print(f"health: {msg}", file=sys.stderr, flush=True)


# --- pure ------------------------------------------------------------------------------------------
def targets(containers: list[dict], frontdoor_url: str) -> list[Target]:
    """One target per web app (a container with a health-check label and a port label), plus the front door.
    Job-only apps have no web page and no check: ops watches their jobs instead."""
    out = []
    for c in containers:
        labels = c.get("Labels") or {}
        app, path = labels.get("com.kaldi.annex.app"), labels.get("com.kaldi.annex.healthcheck")
        port = labels.get(f"traefik.http.services.app-{app}.loadbalancer.server.port") if app else None
        if app and path and port and str(port).isdigit() and labels.get("com.docker.compose.project", "").startswith("annex-app-"):
            out.append(Target(f"app:{app}", app, f"http://annex-app-{app}:{port}{path}", "app"))
    out.sort(key=lambda t: t.name)
    if frontdoor_url:
        out.append(Target(FRONT_DOOR, FRONT_DOOR_NAME, frontdoor_url, "front-door"))
    return out


def judge(kind: str, status: int | None) -> bool:
    """An app is up on a 200; the front door is up when an anonymous request is sent to the login: a 302."""
    return status == (302 if kind == "front-door" else 200)


def explain(kind: str, status: int | None, error: str | None) -> str | None:
    """Why a check failed, in words an alert can carry (None if it didn't)."""
    if judge(kind, status):
        return None
    if status is None:
        return (error or "no answer")[:200]
    want = 302 if kind == "front-door" else 200
    return f"got HTTP {status}, want {want}" + (" (the login redirect)" if want == 302 else "")


class Debounce:
    """`update(ok)` returns the confirmed state when it is newly known: True = down after `fails` failures in a row,
    False = up after `oks` successes in a row; None when nothing changed. The first confirmed state is returned too (so
    a restart can clear an incident that was open before it)."""

    def __init__(self) -> None:
        self.fails = self.oks = 0
        self.state: bool | None = None

    def update(self, ok: bool, fails: int, oks: int) -> bool | None:
        self.fails, self.oks = (0, self.oks + 1) if ok else (self.fails + 1, 0)
        if not ok and self.fails >= fails and self.state is not True:
            self.state = True
            return True
        if ok and self.oks >= oks and self.state is not False:
            self.state = False
            return False
        return None


def state_of(row: dict | None, now: float, interval_s: float) -> dict:
    """`{"state": up|down|unknown, ...}` from the newest stored result of a check (None: never checked)."""
    if row is None:
        return {"state": "unknown", "detail": "no result yet"}
    if now - row["ts"] > max(MIN_FRESH_S, 3 * interval_s):
        return {"state": "unknown", "detail": "the health checker isn't answering", "checked_at": row["ts"]}
    out = {"state": "up" if row["ok"] else "down", "checked_at": row["ts"]}
    if not row["ok"]:
        out["detail"] = row["error"] or "the check failed"
    return out


def tile(rows: dict[str, dict], now: float, interval_s: float) -> dict:
    """What the discovery page's Status tile shows: how many checks are up and down (apps and the front door)."""
    states = [state_of(r, now, interval_s)["state"] for r in rows.values()]
    up, down = states.count("up"), states.count("down")
    return {"up": up, "down": down, "status": "everything is up" if not down else f"{down} down"}


# --- one check ---------------------------------------------------------------------------------------
class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):  # a 302 is the answer, not something to follow
        return None


def fetch(t: Target, timeout: float) -> tuple[int | None, str | None, int]:
    """(HTTP status or None, error, milliseconds)."""
    handlers = [_NoRedirect()]
    if t.url.startswith("https"):
        handlers.append(urllib.request.HTTPSHandler(context=ssl._create_unverified_context()))  # local mkcert certificate
    req = urllib.request.Request(t.url, headers={"Accept": "text/html"} if t.kind == "front-door" else {})
    t0 = time.monotonic()
    try:
        with urllib.request.build_opener(*handlers).open(req, timeout=timeout) as r:
            return r.status, None, round((time.monotonic() - t0) * 1000)
    except urllib.error.HTTPError as e:
        return e.code, None, round((time.monotonic() - t0) * 1000)
    except (urllib.error.URLError, OSError, ValueError) as e:
        return None, str(getattr(e, "reason", e)), round((time.monotonic() - t0) * 1000)


# --- storage ----------------------------------------------------------------------------------------------
def store(results: list[tuple[str, bool, int, str | None]]) -> None:
    if not results:
        return
    with db.connect() as conn, conn.cursor() as cur:
        cur.executemany("INSERT INTO annex_ops.health_checks (app, ok, ms, error) VALUES (%s, %s, %s, %s)", results)


def check_now(t: Target, timeout: float) -> dict:
    """One check right now, stored like the thread's: for a reader that finds no result newer than the app's start (a
    fresh deploy has none for up to a minute). Doesn't touch the debounce: the thread catches up at its next round."""
    status, err, ms = fetch(t, timeout)
    ok = judge(t.kind, status)
    store([(t.name, ok, ms, explain(t.kind, status, err))])
    return {"ts": time.time(), "ok": ok, "ms": ms, "error": explain(t.kind, status, err)}


def stale_for(row: dict | None, started_at: float | None) -> bool:
    """No result, or none from after the container (re)started: what is on record is about an earlier container."""
    return row is None or (started_at is not None and row["ts"] < started_at)


def latest(names: list[str] | None = None) -> dict[str, dict]:
    """The newest result of every check, keyed by subject (`app:<name>`, `platform:front-door`)."""
    with db.connect() as conn:
        rows = conn.execute("SELECT DISTINCT ON (app) app, extract(epoch FROM ts), ok, ms, error FROM annex_ops.health_checks "
                            "WHERE ts > now() - interval '1 hour' ORDER BY app, ts DESC").fetchall()
    return {(FRONT_DOOR if a == FRONT_DOOR_NAME else f"app:{a}"): {"ts": float(ts), "ok": ok, "ms": ms, "error": err}
            for a, ts, ok, ms, err in rows if names is None or a in names}


def history(name: str, n: int) -> list[bool]:
    """The last n results (oldest first), to seed the debounce after a restart."""
    with db.connect() as conn:
        rows = conn.execute("SELECT ok FROM annex_ops.health_checks WHERE app = %s AND ts > now() - interval '10 minutes' "
                            "ORDER BY ts DESC LIMIT %s", (name, n)).fetchall()
    return [r[0] for r in reversed(rows)]


def housekeeping() -> None:
    with db.connect() as conn:
        conn.execute("DELETE FROM annex_ops.health_checks WHERE ts < now() - make_interval(days => %s)", (RETENTION_DAYS,))


# --- the thread ----------------------------------------------------------------------------------------------
class Checker(threading.Thread):
    def __init__(self, submit) -> None:
        super().__init__(daemon=True, name="health-checker")
        self.submit = submit
        self.debounce: dict[str, Debounce] = {}
        self.last_housekeeping = 0.0
        self.pool = ThreadPoolExecutor(max_workers=8, thread_name_prefix="health")

    def seed(self, t: Target, tuning) -> None:
        """After a restart, the results stored before it stand in for the rounds not yet made: an incident that was
        open when ops stopped, for an app that is fine now, is cleared at once (what the old reconcile pass did)."""
        d = self.debounce.setdefault(t.subject, Debounce())
        for ok in history(t.name, max(tuning.check_fail_threshold, tuning.check_ok_threshold) + 1):
            d.update(ok, tuning.check_fail_threshold, tuning.check_ok_threshold)
        if d.state is not None:
            self.submit(engine.HealthState(t.subject, d.state, "" if not d.state else "the health check was failing before annex-ops restarted"))

    def round(self, tuning) -> int:
        ts = targets(docker_api.containers(), config.FRONTDOOR_URL)
        known = {t.subject for t in ts}
        for gone in [s for s in self.debounce if s not in known]:
            del self.debounce[gone]
        fresh = [t for t in ts if t.subject not in self.debounce]
        for t in fresh:
            self.seed(t, tuning)
        outcomes = list(self.pool.map(lambda t: (t, *fetch(t, tuning.check_timeout_s)), ts))
        store([(t.name, judge(t.kind, status), ms, explain(t.kind, status, err)) for t, status, err, ms in outcomes])
        for t, status, err, _ in outcomes:
            ok = judge(t.kind, status)
            verdict = self.debounce.setdefault(t.subject, Debounce()).update(ok, tuning.check_fail_threshold, tuning.check_ok_threshold)
            if verdict is not None:
                self.submit(engine.HealthState(t.subject, verdict, explain(t.kind, status, err) or ""))
        return len(ts)

    def run(self) -> None:
        while True:
            tuning = config.tuning()
            try:
                self.round(tuning)
                if time.time() - self.last_housekeeping > 3600:
                    self.last_housekeeping = time.time()
                    housekeeping()
            except (docker_api.DockerError, OSError, OpsError):
                pass  # Docker or the database isn't there yet: the next round tries again
            except Exception:  # noqa: BLE001: the checker must never die
                log("round failed:\n" + traceback.format_exc(limit=3))
            time.sleep(max(1.0, tuning.check_interval_s))
