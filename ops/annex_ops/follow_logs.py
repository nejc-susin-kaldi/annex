"""Follow container logs, keeping a cursor so nothing is read twice or lost across restarts.

`Follower` is generic (a container name and a line handler): M2.2 reuses it to ship every container's logs.
The Traefik and Ofelia handlers below feed the alert engine: 5xx per app, and scheduled job results.
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


def newer(ts: float, cursor: float) -> bool:
    """Is a line after the cursor? Compared at microsecond precision, which is what the database keeps."""
    return round(ts, 6) > round(cursor, 6)


class Follower(threading.Thread):
    def __init__(self, container: str, handler, store: CursorStore | None = None, lookback_s: float = LOOKBACK_S):
        super().__init__(daemon=True, name=f"follow-{container}")
        self.container, self.handler, self.store, self.lookback_s = container, handler, store or CursorStore(), lookback_s

    def run(self) -> None:
        cursor = None
        while cursor is None:
            try:
                cursor = self.store.get(self.container)
                if cursor is None:
                    cursor = time.time() - self.lookback_s
                    self.store.set(self.container, cursor)  # so a quiet log doesn't replay the lookback at every restart
            except OpsError:
                cursor = None
                time.sleep(RETRY_S)
        saved = 0.0  # the first line saves at once; later ones at most every SAVE_EVERY_S
        while True:
            try:
                for ts, stream, line in docker_api.stream_logs(self.container, cursor):
                    if not newer(ts, cursor):
                        continue
                    cursor = ts
                    try:
                        self.handler(ts, stream, line)
                    except Exception as e:  # noqa: BLE001: one bad line must not stop the follower
                        print(f"follow_logs[{self.container}]: handler failed: {type(e).__name__}: {e}", file=sys.stderr, flush=True)
                    if time.monotonic() - saved >= SAVE_EVERY_S:
                        self.store.set(self.container, cursor)
                        saved = time.monotonic()
            except (docker_api.DockerError, OSError, ValueError):
                pass  # the container isn't there (yet), or was recreated: wait and resolve the name again
            except OpsError:
                pass
            try:
                self.store.set(self.container, cursor)
            except OpsError:
                pass
            saved = time.monotonic()
            time.sleep(RETRY_S)


# --- Traefik ---------------------------------------------------------------------
def parse_traefik(line: str):
    """(app, epoch seconds, status) for an access-log entry of an app's router, else None. Traefik's own log
    lines share stdout; the access log is JSON, and `RouterName` is `app-<name>@docker` for apps."""
    if not line.startswith("{"):
        return None
    try:
        e = json.loads(line)
    except ValueError:
        return None
    router = e.get("RouterName") or ""
    if not router.startswith("app-") or not router.endswith("@docker"):
        return None
    status = e.get("DownstreamStatus", e.get("OriginStatus"))
    ts = docker_api.parse_ts(str(e.get("StartUTC", "")).replace("+00:00", "Z")) if e.get("StartUTC") else None
    if not isinstance(status, int) or ts is None:
        return None
    return router[4:-len("@docker")], ts, status


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
