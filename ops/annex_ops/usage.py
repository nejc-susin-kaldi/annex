"""Usage stats from Traefik's access log: what counts, ingest, rollup and the signals.

Every decision is a pure function (parse_access_entry, trend, graduation, dormant, signal_for, tile) with a unit
test. The rest is plumbing: a buffer that inserts rows in batches, and a rollup that writes summaries.
Logins are stored in `requests` only and never leave it except through `annex usage <app> --by-user`.
"""
import sys
import threading
import time
import traceback
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from . import config, db, docker_api, follow_logs
from .errors import OpsError

STATIC_EXT = (".js", ".mjs", ".css", ".map", ".png", ".jpg", ".jpeg", ".gif", ".svg", ".ico", ".webp",
              ".woff", ".woff2", ".ttf")
MAX_PATH = 200


@dataclass
class UsageSettings:
    usage_retention_days: int = 30    # raw request rows are deleted after this many days
    graduation_min_users: int = 5     # the latest complete week needs at least this many users
    graduation_weeks: int = 4         # ... and the last this-many complete weeks must not fall (2 to 4)
    dormant_days: int = 30            # no signed-in requests for this long (and the app is at least this old)
    rollup_s: float = 600
    digest_weekday: int = 0           # Monday
    digest_hour: int = 8


@dataclass
class Row:
    ts: float
    app: str
    login: str
    method: str
    path: str
    status: int
    duration_ms: int


# --- what counts -------------------------------------------------------------------
def counts_as_use(path: str, health_path: str | None) -> bool:
    """A page request, not the app's health check and not a static asset. `path` has no query string."""
    if health_path and path == health_path:
        return False
    return not path.lower().endswith(STATIC_EXT)


def parse_access_entry(entry: dict, health_paths: dict[str, str]) -> Row | None:
    """A usage row for a parsed access-log entry (see follow_logs.access_entry), or None if it doesn't count:
    anonymous (no signed-in user), the app's health path, a static asset, or an unusable entry. The query
    string is dropped (it can hold client names) and the path capped; Duration is nanoseconds."""
    login = (entry.get("request_Remote-User") or "").strip()
    ts = follow_logs.entry_ts(entry)
    status = entry.get("DownstreamStatus", entry.get("OriginStatus"))
    if not login or ts is None or not isinstance(status, int):
        return None
    app = follow_logs.entry_app(entry)
    path = str(entry.get("RequestPath") or "/").split("?", 1)[0].split("#", 1)[0] or "/"
    if not counts_as_use(path, health_paths.get(app)):
        return None
    duration = entry.get("Duration")
    return Row(ts, app, login, str(entry.get("RequestMethod") or "GET")[:10], path[:MAX_PATH], status,
               int(duration / 1e6) if isinstance(duration, (int, float)) else 0)


def parse_access_line(line: str, health_paths: dict[str, str]) -> Row | None:
    entry = follow_logs.access_entry(line)
    return parse_access_entry(entry, health_paths) if entry else None


# --- signals ---------------------------------------------------------------------------
def trend(users_7d: int, users_prev_7d: int) -> str:
    """The change in users against the 7 days before: "↑2", "↓1" or "=" ."""
    delta = users_7d - users_prev_7d
    return f"↑{delta}" if delta > 0 else f"↓{-delta}" if delta < 0 else "="


def graduation(weeks: list[int | None], min_users: int) -> bool:
    """Graduation candidate: `weeks` is the users of the last complete weeks, oldest first. Every week must be
    known (the app existed), none may be lower than the one before, the latest needs at least `min_users`, and
    the latest must be higher than the first (not just flat)."""
    if len(weeks) < 2 or any(w is None for w in weeks):
        return False
    rising = all(b >= a for a, b in zip(weeks, weeks[1:]))
    return rising and weeks[-1] >= min_users and weeks[-1] > weeks[0]


def dormant(last_used: datetime | None, first_seen: datetime, now: datetime, days: int,
            min_age_days: int | None = None) -> bool:
    """Dormant: no signed-in request for `days` days (or never), and Annex first saw the app at least
    `min_age_days` (default: `days`) ago, so a new app isn't called dormant."""
    min_age = days if min_age_days is None else min_age_days
    if now - first_seen < timedelta(days=min_age):
        return False
    return last_used is None or now - last_used >= timedelta(days=days)


def signal_for(row: dict, now: datetime, s: UsageSettings, dormant_days: int | None = None,
               min_age_days: int | None = None) -> str | None:
    """`graduation`, `dormant` or None for a usage_apps row (a dict)."""
    if dormant(row["last_used"], row["first_seen"], now, dormant_days or s.dormant_days, min_age_days):
        return "dormant"
    weeks = [row.get(f"week{i}") for i in range(1, 5)][4 - max(2, min(4, s.graduation_weeks)):]
    return "graduation" if graduation(weeks, s.graduation_min_users) else None


# --- the discovery tile ---------------------------------------------------------------
STATUS_WORD = {"down": "down", "stopped": "down", "crash-loop": "crash-looping", "unhealthy": "unhealthy",
               "oom": "out of memory", "errors": "errors", "job-failed": "job failing", "webhook": "webhooks failing"}


def tile(row: dict, app_signals: list[str], job_failing: bool, last_run: dict | None, tz=None) -> dict:
    """What a discovery tile shows: totals and status, never logins."""
    status = next((STATUS_WORD[s] for s in app_signals if s in STATUS_WORD), "job failing" if job_failing else "ok")
    out = {"users_7d": row["users_7d"], "trend": trend(row["users_7d"], row["users_prev_7d"]),
           "requests_7d": row["requests_7d"], "status": status, "last_run": "none yet"}
    if last_run:
        when = datetime.fromtimestamp(last_run["finished_at"], tz) if tz else datetime.fromtimestamp(last_run["finished_at"])
        out["last_run"] = f"{'ok' if last_run['ok'] else 'failed'} {when:%H:%M}"
    return out


# --- ingest ------------------------------------------------------------------------------
def log(msg: str) -> None:
    print(f"usage: {msg}", file=sys.stderr, flush=True)


class HealthPaths:
    """app -> health path, from the `com.kaldi.annex.healthcheck` container label (refreshed when an app starts,
    when one is unknown, and every few minutes)."""

    def __init__(self) -> None:
        self.paths: dict[str, str] = {}
        self.loaded = 0.0
        self.lock = threading.Lock()

    def invalidate(self) -> None:
        self.loaded = 0.0

    def get(self) -> dict[str, str]:
        if time.time() - self.loaded > 300:
            with self.lock:
                try:
                    found = {}
                    for c in docker_api.containers():
                        labels = c.get("Labels") or {}
                        if labels.get("com.kaldi.annex.app") and labels.get("com.kaldi.annex.healthcheck"):
                            found[labels["com.kaldi.annex.app"]] = labels["com.kaldi.annex.healthcheck"]
                    self.paths, self.loaded = found, time.time()
                except (docker_api.DockerError, OSError):
                    self.loaded = time.time() - 270  # try again in 30 s; keep what we have
        return self.paths


class Ingest:
    """Buffers usage rows and inserts them in batches (every 5 s or 200 rows)."""

    def __init__(self) -> None:
        self.buf: list[Row] = []
        self.lock = threading.Lock()
        self.last_flush = time.monotonic()
        self.health = HealthPaths()

    def handle(self, ts: float, stream: str, line: str) -> None:
        """The Traefik handler's usage half: one parsed line in, a row buffered if it counts."""
        entry = follow_logs.access_entry(line)
        if entry is None:
            return
        row = parse_access_entry(entry, self.health.get())
        if row is None:
            return
        with self.lock:
            self.buf.append(row)
            full = len(self.buf) >= 200
        if full:
            self.flush()

    def flush(self) -> int:
        with self.lock:
            rows, self.buf = self.buf, []
        self.last_flush = time.monotonic()
        if not rows:
            return 0
        try:
            insert_rows(rows)
        except Exception:  # noqa: BLE001: keep the rows for the next try
            with self.lock:
                self.buf = rows + self.buf
            log("insert failed, will retry:\n" + traceback.format_exc())
            return 0
        return len(rows)

    def touch_app(self, app: str) -> None:
        """An app container exists (started, or seen at startup): record it even if nobody uses it yet, so
        'dormant' can tell a never-used tool from an unknown one."""
        self.health.invalidate()
        with db.connect() as conn:
            conn.execute("INSERT INTO annex_ops.app_seen (app, first_seen) VALUES (%s, now()) "
                         "ON CONFLICT (app) DO UPDATE SET removed_at = NULL", (app,))


def insert_rows(rows: list[Row]) -> None:
    with db.connect() as conn:
        with conn.cursor() as cur:
            # a restart replays the lines after the saved log cursor: identical rows are dropped by the unique index
            cur.executemany("INSERT INTO annex_ops.requests (ts, app, login, method, path, status, duration_ms) "
                            "VALUES (to_timestamp(%s), %s, %s, %s, %s, %s, %s) ON CONFLICT DO NOTHING",
                            [(r.ts, r.app, r.login, r.method, r.path, r.status, r.duration_ms) for r in rows])
            first: dict[str, float] = {}
            for r in rows:
                first[r.app] = min(first.get(r.app, r.ts), r.ts)
            cur.executemany("INSERT INTO annex_ops.app_seen (app, first_seen) VALUES (%s, to_timestamp(%s)) "
                            "ON CONFLICT (app) DO UPDATE SET first_seen = least(annex_ops.app_seen.first_seen, EXCLUDED.first_seen)",
                            list(first.items()))


# --- rollup ---------------------------------------------------------------------------------
def rollup(now: datetime | None = None) -> dict:
    """Summaries for today, yesterday, this week and last week from the raw rows (never over synthetic rows),
    last_used, the signals, and retention. Returns what it did."""
    s = config.usage_settings()
    tz = config.USAGE_TZ
    with db.connect() as conn:
        daily = conn.execute(
            "INSERT INTO annex_ops.usage_daily (day, app, users, requests, errors, p95_ms) "
            "SELECT (ts AT TIME ZONE %(tz)s)::date, app, count(DISTINCT login), count(*), count(*) FILTER (WHERE status >= 500), "
            "       percentile_cont(0.95) WITHIN GROUP (ORDER BY duration_ms)::int "
            "FROM annex_ops.requests WHERE ts >= (date_trunc('day', now() AT TIME ZONE %(tz)s) - interval '1 day') AT TIME ZONE %(tz)s "
            "GROUP BY 1, 2 "
            "ON CONFLICT (day, app) DO UPDATE SET users = EXCLUDED.users, requests = EXCLUDED.requests, "
            "  errors = EXCLUDED.errors, p95_ms = EXCLUDED.p95_ms WHERE NOT annex_ops.usage_daily.synthetic", {"tz": tz}).rowcount
        weekly = conn.execute(
            "INSERT INTO annex_ops.usage_weekly (week, app, users, requests) "
            "SELECT date_trunc('week', ts AT TIME ZONE %(tz)s)::date, app, count(DISTINCT login), count(*) "
            "FROM annex_ops.requests WHERE ts >= (date_trunc('week', now() AT TIME ZONE %(tz)s) - interval '7 days') AT TIME ZONE %(tz)s "
            "GROUP BY 1, 2 "
            "ON CONFLICT (week, app) DO UPDATE SET users = EXCLUDED.users, requests = EXCLUDED.requests "
            "WHERE NOT annex_ops.usage_weekly.synthetic", {"tz": tz}).rowcount
        conn.execute("UPDATE annex_ops.app_seen s SET last_used = r.m FROM (SELECT app, max(ts) AS m FROM annex_ops.requests GROUP BY app) r "
                     "WHERE s.app = r.app AND (s.last_used IS NULL OR r.m > s.last_used)")
        deleted = conn.execute("DELETE FROM annex_ops.requests WHERE ts < now() - make_interval(days => %s)",
                               (s.usage_retention_days,)).rowcount
    mark_removed()
    signals = write_signals(now or datetime.now(timezone.utc), s)
    return {"daily_rows": daily, "weekly_rows": weekly, "deleted_requests": deleted, "signals": signals}


def mark_removed() -> None:
    """Apps whose container is gone are hidden from usage_apps (their history stays); a returning app is shown
    again. Skipped if Docker can't be asked, so a failed call never hides everything."""
    try:
        found = docker_api.containers()
    except (docker_api.DockerError, OSError):
        return
    if not any((c.get("Labels") or {}).get("com.docker.compose.project") == "annex" for c in found):
        return  # not even the platform is visible: don't trust it
    present = sorted({(c.get("Labels") or {}).get("com.kaldi.annex.app") for c in found} - {None})
    with db.connect() as conn:
        conn.execute("UPDATE annex_ops.app_seen SET removed_at = now() WHERE removed_at IS NULL AND app <> ALL(%s)", (present,))
        conn.execute("UPDATE annex_ops.app_seen SET removed_at = NULL WHERE removed_at IS NOT NULL AND app = ANY(%s)", (present,))


def usage_rows() -> list[dict]:
    with db.connect() as conn:
        cur = conn.execute("SELECT * FROM annex_ops.usage_apps ORDER BY app")
        cols = [d.name for d in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]


def write_signals(now: datetime, s: UsageSettings) -> dict:
    """Store the verdict per app (graduation or dormant) where the usage_apps view reads it."""
    wanted = {r["app"]: sig for r in usage_rows() if (sig := signal_for(r, now, s))}
    with db.connect() as conn:
        conn.execute("DELETE FROM annex_ops.app_signals WHERE app <> ALL(%s)", (list(wanted),))
        for app, sig in wanted.items():
            conn.execute("INSERT INTO annex_ops.app_signals (app, signal) VALUES (%s, %s) "
                         "ON CONFLICT (app) DO UPDATE SET signal = EXCLUDED.signal, since = CASE "
                         "WHEN annex_ops.app_signals.signal = EXCLUDED.signal THEN annex_ops.app_signals.since ELSE now() END",
                         (app, sig))
    return wanted


class Worker(threading.Thread):
    """Flushes buffered rows every few seconds and runs the rollup every `rollup_s`."""

    def __init__(self, ingest: Ingest) -> None:
        super().__init__(daemon=True, name="usage")
        self.ingest = ingest

    def run(self) -> None:
        last_rollup = 0.0
        while True:
            time.sleep(1)
            try:
                if time.monotonic() - self.ingest.last_flush >= 5:
                    self.ingest.flush()
                if time.monotonic() - last_rollup >= config.usage_settings().rollup_s:
                    last_rollup = time.monotonic()
                    rollup()
            except Exception:  # noqa: BLE001: the worker must never die
                log("worker error:\n" + traceback.format_exc())


# --- queries for ctl and the tile endpoint -----------------------------------------------------------
PUBLIC_COLS = ["app", "users_7d", "users_prev_7d", "users_30d", "requests_7d", "errors_7d", "error_rate_7d", "p95_7d",
               "week1", "week2", "week3", "week4", "first_seen", "last_used", "signal"]


def usage_table() -> list[dict]:
    """One row per app, totals only (no logins)."""
    out = []
    for r in usage_rows():
        d = {k: r[k] for k in PUBLIC_COLS}
        d["trend"] = trend(r["users_7d"], r["users_prev_7d"])
        d["error_rate_7d"] = float(d["error_rate_7d"])
        for k in ("first_seen", "last_used"):
            d[k] = d[k].isoformat(timespec="seconds") if d[k] else None
        out.append(d)
    return out


def usage_app(app: str, by_user: bool = False) -> dict:
    rows = [r for r in usage_table() if r["app"] == app]
    with db.connect() as conn:
        known = conn.execute("SELECT 1 FROM annex_ops.app_seen WHERE app = %s", (app,)).fetchone()
        if not known:
            raise OpsError(404, f"no usage recorded for {app!r}: it has never been seen. See the apps with: annex usage")
        days = conn.execute("SELECT day, users, requests, errors, p95_ms, synthetic FROM annex_ops.usage_daily "
                            "WHERE app = %s AND day > (now() AT TIME ZONE %s)::date - 30 ORDER BY day DESC",
                            (app, config.USAGE_TZ)).fetchall()
        out = {"app": rows[0] if rows else None, "removed": not rows, "days": [{"day": d[0].isoformat(), "users": d[1], "requests": d[2], "errors": d[3],
                                         "p95_ms": d[4], "synthetic": d[5]} for d in days]}
        if by_user:
            users = conn.execute("SELECT login, count(*) FROM annex_ops.requests WHERE app = %s "
                                 "AND ts > now() - interval '30 days' GROUP BY login ORDER BY 2 DESC, 1", (app,)).fetchall()
            out["by_user"] = [{"login": u[0], "requests": u[1]} for u in users]
    return out


def tile_for(app: str) -> dict:
    rows = [r for r in usage_rows() if r["app"] == app]
    if not rows:
        raise OpsError(404, f"no app named {app!r}")
    with db.connect() as conn:
        open_ = conn.execute("SELECT subject, signals FROM annex_ops.incidents WHERE resolved_at IS NULL AND app = %s", (app,)).fetchall()
        run = conn.execute("SELECT extract(epoch FROM finished_at), ok FROM annex_ops.job_runs WHERE app = %s "
                           "ORDER BY finished_at DESC LIMIT 1", (app,)).fetchone()
    app_signals = [k for subject, sigs in open_ if subject == f"app:{app}" for k in sigs]
    job_failing = any(subject.startswith("job:") for subject, _ in open_)
    from zoneinfo import ZoneInfo

    return tile(rows[0], app_signals, job_failing, {"finished_at": float(run[0]), "ok": run[1]} if run else None,
                ZoneInfo(config.USAGE_TZ))
