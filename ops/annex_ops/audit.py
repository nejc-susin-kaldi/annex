"""The audit trail's annex-ops side (M2.5): what annex-ops itself records, and how the timeline is read.

annex_vm.py writes the audit rows for what an admin or builder does (deploys, rollbacks, secrets, removals, accounts).
Here: successful and failed sign-ins (from Authelia's stored log lines, which are deleted after 14 days while the audit
trail keeps a year), webhook events received (metadata only, never a body), and the gateway reads as aggregates (one row
per app, view and hour from the stored gateway lines, D63: which data an app read, without a row per request). Email
and Slack sends are not copied: the `audit_timeline` view reads them from `sends`.

The decisions are pure functions with unit tests (`signin_event`, `bucket_start`, `gateway_rows`); the rest is plumbing.
Nothing here stores a password: a failed sign-in's username is kept only if it looks like a login.
"""
import re
import sys
import threading
import time
import traceback
from dataclasses import dataclass
from datetime import datetime, timezone

from psycopg.types.json import Jsonb

from . import config, db, follow_logs
from .errors import OpsError

LOGIN = re.compile(r"^[a-z][a-z0-9._-]{1,31}$")  # the same rule as annex users
SIGNED_IN = re.compile(r"^Successful (1FA|2FA) authentication attempt made by user '(?P<user>[^']*)'")
SIGNIN_FAILED = re.compile(r"^Unsuccessful (1FA|2FA) authentication attempt by user '(?P<user>[^']*)'")
CONTAINER = "annex-authelia-1"


@dataclass
class AuditSettings:
    audit_scan_s: float = 60            # how often sign-ins, webhook events and gateway aggregates are read
    audit_bucket_s: float = 3600        # gateway reads are summed per app, view and this many seconds (an hour)
    audit_retention_days: int = 365


def log(msg: str) -> None:
    print(f"audit: {msg}", file=sys.stderr, flush=True)


# --- pure ------------------------------------------------------------------------------------------------
def signin_event(msg: str) -> dict | None:
    """(action, actor, details) of an Authelia sign-in line, or None. A failed attempt's username can be a password typed
    into the wrong box: it is kept only if it looks like a login, and the actor is then NULL."""
    m = SIGNED_IN.match(msg)
    if m:
        return {"action": "signin", "actor": m["user"], "details": {}}
    m = SIGNIN_FAILED.match(msg)
    if m:
        user = m["user"]
        if LOGIN.match(user):
            return {"action": "signin.failed", "actor": user, "details": {}}
        return {"action": "signin.failed", "actor": None, "details": {"username": "not a login"}}
    return None


def bucket_start(ts: float, bucket_s: float) -> float:
    """The start (epoch seconds) of the bucket that `ts` falls in."""
    return (ts // bucket_s) * bucket_s


def gateway_event(app: str, view: str, start: float, bucket_s: float, requests: int) -> dict:
    iso = datetime.fromtimestamp(start, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return {"ts": start + bucket_s, "actor": f"app:{app}", "app": app, "action": "gateway.reads",
            "details": {"view": view, "requests": requests, "from": iso, "bucket_s": int(bucket_s)},
            "ref": f"gateway:{app}:{view}:{iso}:{int(bucket_s)}"}


# --- reading the sources and writing audit rows --------------------------------------------------------------
UPSERT = ("INSERT INTO annex_ops.audit_events (ts, actor, app, action, details, ref) VALUES (to_timestamp(%s), %s, %s, %s, %s, %s) "
          "ON CONFLICT (ref) DO UPDATE SET details = EXCLUDED.details WHERE annex_ops.audit_events.action = 'gateway.reads'")


def write(events: list[dict]) -> None:
    with db.connect() as conn, conn.transaction():
        for e in events:
            conn.execute(UPSERT, (e["ts"], e["actor"], e.get("app"), e["action"], Jsonb(e["details"]), e["ref"]))


def scan_signins(store: follow_logs.CursorStore, now: float) -> int:
    key = "audit-signins"
    since = store.get(key)
    to = now - 5
    if since is None:
        store.set(key, to)  # nothing earlier than this feature
        return 0
    with db.connect() as conn:
        rows = conn.execute("SELECT extract(epoch FROM ts), msg, h::text FROM annex_ops.logs WHERE container = %s AND ts > to_timestamp(%s) "
                            "AND ts <= to_timestamp(%s) AND (msg LIKE 'Successful %% authentication attempt%%' "
                            "OR msg LIKE 'Unsuccessful %% authentication attempt%%') ORDER BY ts", (CONTAINER, since - 30, to)).fetchall()
    events = []
    for ts, msg, h in rows:
        ev = signin_event(msg)
        if ev:
            events.append({**ev, "ts": float(ts), "app": None, "ref": f"signin:{h}"})
    write(events)
    store.set(key, to)
    return len(events)


def scan_webhooks(store: follow_logs.CursorStore, now: float) -> int:
    """Webhook events received: who (the hook), which app, accepted or rejected. Never the body or the headers."""
    key = "audit-hooks"
    since = store.get(key)
    to = now - 5
    if since is None:
        store.set(key, to)
        return 0
    with db.connect() as conn:
        rows = conn.execute("SELECT id, extract(epoch FROM received_at), app, hook, status FROM annex_ops.hook_events "
                            "WHERE received_at > to_timestamp(%s) AND received_at <= to_timestamp(%s) AND replay_of IS NULL ORDER BY id",
                            (since - 30, to)).fetchall()
    events = [{"ts": float(ts), "actor": f"webhook:{hook}", "app": app, "action": "webhook.rejected" if status == "rejected" else "webhook.received",
               "details": {"hook": hook}, "ref": f"hook:{i}"} for i, ts, app, hook, status in rows]
    write(events)
    store.set(key, to)
    return len(events)


GATEWAY_KEY = "audit:gateway"


def gateway_window(now: float, bucket_s: float, cursor: float | None, window_s: float = 172800) -> tuple[float, float] | None:
    """(from, end) of the buckets to aggregate now, or None if no bucket has closed since the cursor. `end` is the start of
    the current (open) bucket, which is also the new cursor. The scan replays one bucket before the cursor (a late line of
    the last bucket counted still lands), and never reaches back further than `window_s`. No cursor: nothing before `end - window_s`."""
    end = bucket_start(now, bucket_s)
    if cursor is not None and end <= cursor:
        return None
    start = end - window_s if cursor is None else bucket_start(cursor, bucket_s) - bucket_s
    return max(start, end - window_s), end


def scan_gateway(store: follow_logs.CursorStore, now: float, bucket_s: float, window_s: float = 172800) -> int:
    """Reads of product data per app, view and bucket, from the stored gateway lines: each closed bucket once (plus one
    bucket replayed), from a cursor. The first run after an upgrade starts at the newest gateway aggregate already stored."""
    cursor = store.get(GATEWAY_KEY)
    if cursor is None:
        with db.connect() as conn:
            row = conn.execute("SELECT extract(epoch FROM max(ts)) FROM annex_ops.audit_events WHERE action = 'gateway.reads'").fetchone()
        cursor = float(row[0]) if row and row[0] is not None else None
    window = gateway_window(now, bucket_s, cursor, window_s)
    if window is None:
        return 0
    start, end = window
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT app, fields->>'view', floor(extract(epoch FROM ts) / %(b)s) * %(b)s AS start, count(*) FROM annex_ops.logs "
            "WHERE container = 'gateway' AND app IS NOT NULL AND ts >= to_timestamp(%(from)s) AND ts < to_timestamp(%(end)s) "
            "GROUP BY 1, 2, 3 ORDER BY 3", {"b": bucket_s, "from": start, "end": end}).fetchall()
    events = [gateway_event(app, view or "?", float(start), bucket_s, n) for app, view, start, n in rows]
    write(events)
    store.set(GATEWAY_KEY, end)
    return len(events)


def housekeeping(s: AuditSettings) -> None:
    with db.connect() as conn:
        conn.execute("DELETE FROM annex_ops.audit_events WHERE ts < now() - make_interval(days => %s)", (s.audit_retention_days,))


# --- queries for ctl and the console ------------------------------------------------------------------------------
def timeline(app: str | None = None, since_s: float | None = None, action: str | list[str] | None = None, actor: str | None = None,
             limit: int = 100, before: datetime | None = None, failures: bool = False) -> list[dict]:
    """The audit trail, newest first: the audit rows and every email and Slack send (`audit_timeline`). `action` is a
    prefix or several (the console's chips); `failures` keeps only what failed or was rejected; `before` pages backwards."""
    where, args = ["TRUE"], []
    if app:
        where.append("app = %s")
        args.append(app)
    if since_s is not None:
        where.append("ts >= now() - make_interval(secs => %s)")
        args.append(since_s)
    if action:
        acts = [action] if isinstance(action, str) else list(action)
        where.append("(" + " OR ".join(["action = %s OR action LIKE %s"] * len(acts)) + ")")
        for a in acts:
            args += [a, a + ".%"]
    if actor:
        where.append("actor = %s")
        args.append(actor)
    if failures:
        where.append("action ~ '(failed|rejected)$'")
    if before is not None:
        where.append("ts < %s")
        args.append(before)
    with db.connect() as conn:
        rows = conn.execute("SELECT ts, actor, app, action, details FROM annex_ops.audit_timeline WHERE " + " AND ".join(where) +
                            " ORDER BY ts DESC, action LIMIT %s", (*args, max(1, min(int(limit), 2000)))).fetchall()
    return [{"ts": r[0].astimezone(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z"), "actor": r[1], "app": r[2],
             "action": r[3], "details": r[4]} for r in rows]


class Auditor(threading.Thread):
    def __init__(self) -> None:
        super().__init__(daemon=True, name="auditor")
        self.cursors = follow_logs.CursorStore()
        self.last_housekeeping = 0.0

    def run(self) -> None:
        while True:
            s = config.audit_settings()
            try:
                now = time.time()
                scan_signins(self.cursors, now)
                scan_webhooks(self.cursors, now)
                scan_gateway(self.cursors, now, s.audit_bucket_s)
                if now - self.last_housekeeping > 86400:
                    self.last_housekeeping = now
                    housekeeping(s)
            except OpsError:
                pass  # the database isn't ready yet
            except Exception:  # noqa: BLE001: the auditor must never die
                log("pass failed:\n" + traceback.format_exc(limit=3))
            time.sleep(max(2.0, s.audit_scan_s))
