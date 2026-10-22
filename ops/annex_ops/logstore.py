"""annex_ops.logs: batched idempotent inserts, day partitions, retention, the size guard and queries (M2.2).

The decisions are pure functions (`partition_days`, `expired_days`, `trim_plan`, `levels_from`, `like_pattern`) with a
unit test each; the rest is plumbing. A partition is made for the day of a line before the line is inserted, and
ahead of time by the keeper (logship.Keeper), so a line can never fail for want of a partition.
"""
import re
import sys
import threading
import time
import traceback
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone

import psycopg
from psycopg.types.json import Jsonb

from . import db, threads
from .errors import OpsError

FLUSH_S = 1.0          # buffered lines are inserted at least this often ...
BATCH = 500            # ... or as soon as this many are waiting
MAX_BUFFER = 20000     # the database is down: keep this many lines in memory, then drop the oldest
PARTITIONS_AHEAD = 2   # the keeper makes today's partition and this many days more
LEVEL_RANK = {"debug": 0, "info": 1, "warn": 2, "error": 3}
PARTITION = re.compile(r"^logs_(\d{4})(\d\d)(\d\d)$")


@dataclass
class Row:
    ts: datetime               # Docker's timestamp, microsecond precision, UTC
    ns: int                    # the 0-999 nanoseconds below that, so lines of one microsecond keep their order
    app: str | None
    container: str
    release: int | None
    stream: str
    level: str | None
    request_id: str | None
    msg: str
    fields: dict | None
    h: str                     # md5 hex of container, stream, nanosecond timestamp and the raw line


def log(msg: str) -> None:
    print(f"logstore: {msg}", file=sys.stderr, flush=True)


# --- partitions (pure) ---------------------------------------------------------------
def day_of(ts: datetime) -> date:
    return ts.astimezone(timezone.utc).date()


def partition_name(day: date) -> str:
    return f"logs_{day:%Y%m%d}"


def partition_days(now: datetime, ahead: int = PARTITIONS_AHEAD) -> list[date]:
    """Today (UTC) and `ahead` days more: the partitions that should exist."""
    today = now.astimezone(timezone.utc).date()
    return [today + timedelta(days=i) for i in range(ahead + 1)]


def expired_days(existing: list[date], now: datetime, retention_days: int) -> list[date]:
    """Partitions whose every line is older than the retention: the day ends at or before now minus the retention
    (so lines live between `retention_days` and one day more; whole partitions are dropped, never deleted from)."""
    cutoff = now.astimezone(timezone.utc) - timedelta(days=retention_days)
    return sorted(d for d in existing
                  if datetime(d.year, d.month, d.day, tzinfo=timezone.utc) + timedelta(days=1) <= cutoff)


def trim_plan(sizes: dict[date, int], max_bytes: int) -> list[date]:
    """The size guard: oldest days to drop early until the table fits, never the newest partition (it's being written)."""
    plan, total = [], sum(sizes.values())
    for day in sorted(sizes)[:-1]:
        if total <= max_bytes:
            break
        plan.append(day)
        total -= sizes[day]
    return plan


def partition_from_name(name: str) -> date | None:
    m = PARTITION.match(name)
    return date(int(m[1]), int(m[2]), int(m[3])) if m else None


def create_partition_sql(day: date) -> str:
    nxt = day + timedelta(days=1)
    return (f"CREATE TABLE IF NOT EXISTS annex_ops.{partition_name(day)} PARTITION OF annex_ops.logs "
            f"FOR VALUES FROM ('{day} 00:00:00+00') TO ('{nxt} 00:00:00+00')")


# --- queries (pure parts) ---------------------------------------------------------------
def levels_from(level: str) -> list[str]:
    """--level warn means warn and error."""
    if level not in LEVEL_RANK:
        raise OpsError(400, f"--level {level!r} is not a level. Use debug, info, warn or error")
    return [x for x, r in LEVEL_RANK.items() if r >= LEVEL_RANK[level]]


def like_pattern(text: str) -> str:
    """A case-insensitive 'contains' pattern for ILIKE: the user's % _ and \\ are literal."""
    return "%" + re.sub(r"([\\%_])", r"\\\1", text) + "%"


# --- inserting ------------------------------------------------------------------------------
INSERT = ("INSERT INTO annex_ops.logs (ts, ns, app, container, release, stream, level, request_id, msg, fields, h) "
          "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::uuid) ON CONFLICT DO NOTHING")


def _params(r: Row) -> tuple:
    return (r.ts, r.ns, r.app, r.container, r.release, r.stream, r.level, r.request_id, r.msg,
            Jsonb(r.fields) if r.fields is not None else None, r.h)


def ensure_partitions(days, known: set[date]) -> None:
    missing = sorted(d for d in set(days) if d not in known)
    if not missing:
        return
    with db.connect() as conn:
        for day in missing:
            try:
                conn.execute(create_partition_sql(day))
            except (psycopg.errors.DuplicateTable, psycopg.errors.UniqueViolation, psycopg.errors.DeadlockDetected):
                pass  # annex-ops and `ctl logs-drain` made it at the same moment
            known.add(day)


def insert_rows(rows: list[Row], known: set[date]) -> int:
    """Insert a batch (duplicates of an earlier insert are ignored). A line the database refuses (it never should:
    text is cleaned first) is skipped and counted, so one bad line can't block the rest. Returns how many were skipped."""
    ensure_partitions({day_of(r.ts) for r in rows}, known)
    try:
        with db.connect() as conn, conn.transaction(), conn.cursor() as cur:
            cur.executemany(INSERT, [_params(r) for r in rows])
        return 0
    except (psycopg.errors.DataError, psycopg.errors.IntegrityError):
        skipped = 0
        with db.connect() as conn:
            for r in rows:
                try:
                    conn.execute(INSERT, _params(r))
                except (psycopg.errors.DataError, psycopg.errors.IntegrityError):
                    skipped += 1
        log(f"{skipped} of {len(rows)} line(s) were refused by the database and skipped")
        return skipped


class Sink:
    """Buffers rows and inserts them in batches. Thread-safe. `flush()` returns True once everything added so far
    is in the database: followers call it before they save a cursor, so a cursor never gets ahead of the rows."""

    def __init__(self) -> None:
        self.buf: list[Row] = []
        self.lock = threading.Lock()
        self.flush_lock = threading.Lock()
        self.known: set[date] = set()
        self.lost = 0
        self.last_error = 0.0

    def add(self, row: Row) -> None:
        with self.lock:
            self.buf.append(row)
            if len(self.buf) > MAX_BUFFER:
                over = len(self.buf) - MAX_BUFFER
                del self.buf[:over]
                self.lost += over
            full = len(self.buf) >= BATCH
        if full:
            self.flush()

    def flush(self) -> bool:
        with self.flush_lock:
            with self.lock:
                rows, self.buf = self.buf, []
            if not rows:
                return True
            try:
                insert_rows(rows, self.known)
                return True
            except Exception as e:  # noqa: BLE001: keep the rows for the next try
                with self.lock:
                    merged = rows + self.buf
                    self.lost += max(0, len(merged) - MAX_BUFFER)  # the oldest are dropped; count them like add() does
                    self.buf = merged[-MAX_BUFFER:]
                if time.monotonic() - self.last_error > 60:  # the shipper's own errors must not flood the log it ships
                    self.last_error = time.monotonic()
                    log(f"insert failed ({type(e).__name__}); {len(rows)} line(s) wait for the next try\n" + traceback.format_exc(limit=2))
                return False

    def status(self) -> dict:
        """Rows waiting for the database and rows dropped since ops started because the buffer was full (/healthz)."""
        with self.lock:
            return {"buffered": len(self.buf), "max": MAX_BUFFER, "lost": self.lost}

    def run(self) -> None:
        threads.register("log-sink", 30)
        while True:
            time.sleep(FLUSH_S)
            threads.beat("log-sink")
            try:
                self.flush()
            except Exception:  # noqa: BLE001
                pass

    def start(self) -> None:
        threading.Thread(target=self.run, daemon=True, name="log-sink").start()


# --- partitions in the database --------------------------------------------------------------
def partition_sizes() -> dict[date, int]:
    """day -> bytes (table and indexes) of every logs partition."""
    with db.connect() as conn:
        rows = conn.execute("SELECT c.relname, pg_total_relation_size(c.oid) FROM pg_inherits i "
                            "JOIN pg_class c ON c.oid = i.inhrelid WHERE i.inhparent = 'annex_ops.logs'::regclass").fetchall()
    return {d: int(size) for name, size in rows if (d := partition_from_name(name))}


def drop_partition(day: date) -> None:
    with db.connect() as conn:
        conn.execute(f"DROP TABLE IF EXISTS annex_ops.{partition_name(day)}")


def stats() -> dict:
    sizes = partition_sizes()
    with db.connect() as conn:
        n = conn.execute("SELECT count(*) FROM annex_ops.logs").fetchone()[0]
    return {"partitions": [{"day": str(d), "mb": round(sizes[d] / 2**20, 2)} for d in sorted(sizes)],
            "total_mb": round(sum(sizes.values()) / 2**20, 2), "lines": n}


# --- reading ----------------------------------------------------------------------------------
def query_logs(app: str, n: int = 100, since_s: float | None = None, release: int | None = None,
               level: str | None = None, grep: str | None = None, request: str | None = None,
               source: str = "app", oldest: bool = False, before: datetime | None = None) -> list[dict]:
    """An app's stored lines, oldest first. The last `n` that match (or the first `n` with `oldest`); `before` (a
    timestamp) pages backwards: only lines older than it (the console's "older" link).
    source: app = the app's own lines and its jobs' output, gateway = its gateway reads, all = both."""
    if source not in ("app", "gateway", "all"):
        raise OpsError(400, f"--source {source!r} is not a source. Use app, gateway or all")
    where, args = ["app = %s"], [app]
    if since_s is not None:
        where.append("ts >= now() - make_interval(secs => %s)")
        args.append(since_s)
    if release is not None:
        where.append("release = %s")
        args.append(release)
    if level:
        where.append("level = ANY(%s)")
        args.append(levels_from(level))
    if grep:
        where.append("(msg || ' ' || coalesce(fields::text, '')) ILIKE %s")
        args.append(like_pattern(grep))
    if request:
        where.append("request_id = %s")
        args.append(request)
    if before is not None:
        where.append("ts < %s")
        args.append(before)
    if source == "app":
        where.append("container <> 'gateway'")
    elif source == "gateway":
        where.append("container = 'gateway'")
    order = "ASC" if oldest else "DESC"
    n = max(1, min(int(n), 5000))
    with db.connect() as conn:
        rows = conn.execute("SELECT ts, ns, container, release, stream, level, request_id, msg, fields FROM annex_ops.logs "
                            f"WHERE {' AND '.join(where)} ORDER BY ts {order}, ns {order} LIMIT %s", (*args, n)).fetchall()
    if not oldest:
        rows.reverse()
    return [{"ts": r[0].astimezone(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z"),
             "container": r[2], "release": r[3], "stream": r[4], "level": r[5], "request_id": r[6], "msg": r[7],
             "fields": r[8], "structured": r[8] is not None} for r in rows]
