"""Follow container logs, keeping a cursor so nothing is read twice or lost across restarts.

`Follower` is generic (a container and a line handler). The Traefik and Ofelia handlers below feed the alert
engine: 5xx per app, and scheduled job results. In `exact` mode (M2.2: log shipping, logship.py) it hands
the handler integer nanoseconds, never skips a line that shares a microsecond with the one before it (the
lines of a traceback do), and replays a little on a restart: the handler's insert is idempotent.
"""
import json
import re
import sys
import threading
import time

from . import db, docker_api
from .errors import OpsError

LOOKBACK_S = 600  # first start without a cursor: begin this far back
SAVE_EVERY_S = 2
RETRY_S = 2
MAX_IDLE_S = 10   # exact mode: a container with nothing new is asked again at most this seldom
EXACT_MARGIN_NS = 10**9  # exact mode, restart: replay this much before the cursor (it is rounded to a microsecond)


class CursorStore:
    """annex_ops.log_cursors: container name -> timestamp of the last line handled."""

    def get(self, container: str) -> float | None:
        with db.connect() as conn:
            row = conn.execute("SELECT extract(epoch FROM ts) FROM annex_ops.log_cursors WHERE container = %s",
                               (container,)).fetchone()
        return float(row[0]) if row else None

    def set(self, container: str, ts: float) -> None:
        with db.connect() as conn:
            conn.execute("INSERT INTO annex_ops.log_cursors (container, ts) VALUES (%s, to_timestamp(%s)) "
                         "ON CONFLICT (container) DO UPDATE SET ts = EXCLUDED.ts", (container, ts))

    def delete(self, container: str) -> None:
        with db.connect() as conn:
            conn.execute("DELETE FROM annex_ops.log_cursors WHERE container = %s", (container,))


def newer(ts: float, cursor: float) -> bool:
    """Is a line after the cursor? Compared at microsecond precision, which is what the database keeps."""
    return round(ts, 6) > round(cursor, 6)


class Follower(threading.Thread):
    """Follows one container's log and calls `handler(ts, stream, line)` for every new line, saving a cursor
    under `key` (default: the container). `container` can be a name or an id. `exact=True` is for log shipping:
    the handler gets integer nanoseconds, equal-microsecond lines are kept, and `before_save()` (if given) runs
    before each cursor save: False means "the lines aren't stored yet, don't move the cursor"."""

    def __init__(self, container: str, handler, store: CursorStore | None = None, lookback_s: float = LOOKBACK_S,
                 *, key: str | None = None, exact: bool = False, before_save=None, start_ns: int | None = None):
        super().__init__(daemon=True, name=f"follow-{key or container}")
        self.container, self.handler, self.store, self.lookback_s = container, handler, store or CursorStore(), lookback_s
        self.key, self.exact, self.before_save, self.start_ns = key or container, exact, before_save, start_ns
        self.stopping = threading.Event()
        self.wake = threading.Event()

    def stop(self) -> None:
        """Ends the thread once the container's stream is over (a destroyed container's stream already is)."""
        self.stopping.set()
        self.wake.set()

    def poke(self) -> None:
        """The container (re)started: ask again now instead of after the idle wait."""
        self.wake.set()

    def run(self) -> None:
        cursor = None
        while cursor is None and not self.stopping.is_set():
            try:
                cursor = self.store.get(self.key)
                if cursor is None:
                    cursor = self.start_ns / 1e9 if self.start_ns else time.time() - self.lookback_s
                    self.store.set(self.key, cursor)  # so a quiet log doesn't replay the lookback at every restart
            except OpsError:
                cursor = None
                time.sleep(RETRY_S)
        if cursor is None:
            return
        if self.exact:
            self._run_exact(cursor)
        else:
            self._run(cursor)

    def _run(self, cursor: float) -> None:
        saved = 0.0  # the first line saves at once; later ones at most every SAVE_EVERY_S
        while not self.stopping.is_set():
            try:
                for ts, stream, line in docker_api.stream_logs(self.container, cursor):
                    if not newer(ts, cursor):
                        continue
                    cursor = ts
                    self._deliver(ts, stream, line)
                    if time.monotonic() - saved >= SAVE_EVERY_S:
                        self.store.set(self.key, cursor)
                        saved = time.monotonic()
            except (docker_api.DockerError, OSError, ValueError):
                pass  # the container isn't there (yet), or was recreated: wait and resolve the name again
            except OpsError:
                pass
            try:
                self.store.set(self.key, cursor)
            except OpsError:
                pass
            saved = time.monotonic()
            time.sleep(RETRY_S)

    def _run_exact(self, cursor: float) -> None:
        last_ns = int(round(cursor * 1e9)) - EXACT_MARGIN_NS
        saved, idle = 0.0, 0
        while not self.stopping.is_set():
            got = False
            try:
                for ns, stream, line in docker_api.stream_logs_ns(self.container, last_ns):
                    if ns < last_ns:
                        continue  # equal is replayed on purpose; the handler's insert is idempotent
                    got = got or ns > last_ns
                    last_ns = ns
                    self._deliver(ns, stream, line)
                    if time.monotonic() - saved >= SAVE_EVERY_S:
                        self._save_exact(last_ns)
                        saved = time.monotonic()
            except (docker_api.DockerError, OSError, ValueError, OpsError):
                pass  # the container isn't there (yet) or the stream broke: ask again
            except Exception as e:  # noqa: BLE001: a truncated chunked stream is an HTTPException, not an OSError
                print(f"follow_logs[{self.key}]: {type(e).__name__}: {e}; asking again", file=sys.stderr, flush=True)
            self._save_exact(last_ns)
            saved = time.monotonic()
            idle = 0 if got else min(idle + 1, MAX_IDLE_S // RETRY_S)
            self.wake.wait(RETRY_S * (1 + idle))
            self.wake.clear()

    def _deliver(self, ts, stream: str, line: str) -> None:
        try:
            self.handler(ts, stream, line)
        except Exception as e:  # noqa: BLE001: one bad line must not stop the follower
            print(f"follow_logs[{self.key}]: handler failed: {type(e).__name__}: {e}", file=sys.stderr, flush=True)

    def _save_exact(self, ns: int) -> None:
        try:
            if self.before_save is None or self.before_save():
                self.store.set(self.key, ns / 1e9)
        except Exception as e:  # noqa: BLE001: the database is busy or down: save at the next round
            if not isinstance(e, OpsError):
                print(f"follow_logs[{self.key}]: couldn't save the cursor: {type(e).__name__}: {e}", file=sys.stderr, flush=True)


# --- Traefik ---------------------------------------------------------------------
def access_entry(line: str) -> dict | None:
    """The parsed access-log entry of an app's router, else None. Traefik's own log lines share stdout; the
    access log is JSON, and `RouterName` is `app-<name>@docker` for apps."""
    if not line.startswith("{"):
        return None
    try:
        e = json.loads(line)
    except ValueError:
        return None
    router = e.get("RouterName") or ""
    if not router.startswith("app-") or not router.endswith("@docker"):
        return None
    return e


def entry_app(entry: dict) -> str:
    return entry["RouterName"][4:-len("@docker")]


def entry_ts(entry: dict) -> float | None:
    return docker_api.parse_ts(str(entry["StartUTC"]).replace("+00:00", "Z")) if entry.get("StartUTC") else None


def parse_traefik(line: str):
    """(app, epoch seconds, status) for an access-log entry of an app's router, else None (the alert engine's view)."""
    e = access_entry(line)
    if e is None:
        return None
    status = e.get("DownstreamStatus", e.get("OriginStatus"))
    ts = entry_ts(e)
    if not isinstance(status, int) or ts is None:
        return None
    return entry_app(e), ts, status


# --- Ofelia ----------------------------------------------------------------------
ANSI = re.compile(r"\x1b\[[0-9;]*m")
OFELIA_FINISHED = re.compile(r'\[Job "(?P<key>[^"]+)" \((?P<id>[0-9a-f]+)\)\] Finished in "(?P<dur>[^"]+)", '
                             r'failed: (?P<failed>true|false), skipped: (?P<skipped>true|false), error: (?P<error>.*)$')
_DUR = re.compile(r"([\d.]+)(ns|µs|us|ms|s|m|h)")
_UNIT_MS = {"ns": 1e-6, "µs": 1e-3, "us": 1e-3, "ms": 1.0, "s": 1000.0, "m": 60000.0, "h": 3600000.0}


def parse_duration_ms(text: str) -> int:
    """A Go duration ("288.2ms", "1m2.5s") in milliseconds."""
    return int(sum(float(n) * _UNIT_MS[u] for n, u in _DUR.findall(text)))


def parse_ofelia(line: str):
    """{key, failed, skipped, error, duration_ms} for Ofelia's "Finished in ..." line, else None."""
    m = OFELIA_FINISHED.search(ANSI.sub("", line))
    if not m:
        return None
    err = m["error"].strip()
    return {"key": m["key"], "failed": m["failed"] == "true", "skipped": m["skipped"] == "true",
            "error": None if err == "none" else err, "duration_ms": parse_duration_ms(m["dur"])}


def job_map(containers: list[dict]) -> dict[str, tuple[str, str]]:
    """Ofelia job key -> (app, job), from the `ofelia.job-exec.<key>.schedule` labels on app containers. App
    and job names both contain '-', so the key is never split: the app comes from the container's own label."""
    out = {}
    for c in containers:
        labels = c.get("Labels") or {}
        app = labels.get("com.kaldi.annex.app")
        if not app:
            continue
        for label in labels:
            m = re.fullmatch(r"ofelia\.job-exec\.(.+)\.schedule", label)
            if m and m[1].startswith(app + "-"):
                out[m[1]] = (app, m[1][len(app) + 1:])
    return out
