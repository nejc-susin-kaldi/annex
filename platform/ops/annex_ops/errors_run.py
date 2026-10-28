"""Error groups at work (M2.3): read the stored logs, count events per group, email the app's people.

A scanner thread reads annex_ops.logs every few seconds (a window that ends `errors_settle_s` ago, so every line of
a traceback has been stored), turns it into events (errors_group.assemble), stores each once and upserts its group.
A new kind of error, one that returns after a quiet day, and one that appears in a newer release than it was last
seen in, owe an email; the same thread sends what is owed, at most `error_alerts_per_hour` per app, the rest in one
summary. This is a notification, not an incident (D67): there is no "resolved" to wait for, and it doesn't touch the
alert engine.
"""
import sys
import threading
import time
import traceback
from dataclasses import dataclass

from . import config, db, directory, docker_api, errors_group as eg, follow_logs, incidents, messages, threads
from .errors import OpsError

CURSOR = "errors-scan"
HOUSEKEEPING_S = 3600


@dataclass
class ErrorSettings:
    errors_scan_s: float = 10
    errors_settle_s: float = 10          # only lines older than this are read: all of a traceback is stored by then
    error_quiet_s: float = 86400         # a kind of error not seen for this long is quiet, and an email is owed if it returns
    error_alerts_per_hour: int = 5       # emails per app; the rest of what is owed goes out in one summary
    error_event_days: int = 14
    error_group_days: int = 30


def log(msg: str) -> None:
    print(f"errors: {msg}", file=sys.stderr, flush=True)


# --- reading and storing --------------------------------------------------------------------------
SCAN = ("SELECT extract(epoch FROM ts), ns, app, container, release, level, request_id, msg, fields, h::text "
        "FROM annex_ops.logs WHERE ts > to_timestamp(%s) AND ts <= to_timestamp(%s) AND app IS NOT NULL "
        "AND container <> 'gateway' AND (level = 'error' OR fields IS NULL) ORDER BY ts, ns")


def fetch_rows(since: float, to: float) -> list[dict]:
    with db.connect() as conn:
        rows = conn.execute(SCAN, (since, to)).fetchall()
    return [{"ts": r[0], "ns": r[1], "app": r[2], "container": r[3], "release": r[4], "level": r[5], "request_id": r[6],
             "msg": r[7], "fields": r[8], "h": r[9]} for r in rows]


def record(ev: eg.Event, s: ErrorSettings) -> bool:
    """Store an event once and count it in its group. True if it was new."""
    with db.connect() as conn, conn.transaction():
        new = conn.execute("INSERT INTO annex_ops.error_events (h, app, fingerprint, ts, release, request_id, login) "
                           "VALUES (%s::uuid, %s, %s, to_timestamp(%s), %s, %s, %s) ON CONFLICT DO NOTHING RETURNING 1",
                           (ev.h, ev.app, ev.fingerprint, ev.ts, ev.release, ev.request_id, ev.user)).fetchone()
        if not new:
            return False
        prev = conn.execute("SELECT extract(epoch FROM last_seen), last_release FROM annex_ops.error_groups "
                            "WHERE app = %s AND fingerprint = %s FOR UPDATE", (ev.app, ev.fingerprint)).fetchone()
        owed = eg.should_alert(prev and {"last_seen": float(prev[0]), "last_release": prev[1]}, ev.ts, ev.release, s.error_quiet_s)
        if prev is None:
            conn.execute("INSERT INTO annex_ops.error_groups (app, fingerprint, kind, title, first_seen, last_seen, count, "
                         "first_release, last_release, sample_msg, sample_request_id, last_user, alert_due) "
                         "VALUES (%s, %s, %s, %s, to_timestamp(%s), to_timestamp(%s), 1, %s, %s, %s, %s, %s, true)",
                         (ev.app, ev.fingerprint, ev.kind, ev.title, ev.ts, ev.ts, ev.release, ev.release, ev.sample,
                          ev.request_id, ev.user))
        else:
            conn.execute("UPDATE annex_ops.error_groups SET count = count + 1, last_seen = greatest(last_seen, to_timestamp(%s)), "
                         "last_release = greatest(last_release, %s), sample_request_id = coalesce(%s, sample_request_id), "
                         "last_user = coalesce(%s, last_user), alert_due = alert_due OR %s, alert_again = alert_again OR %s "
                         "WHERE app = %s AND fingerprint = %s",
                         (ev.ts, ev.release, ev.request_id, ev.user, owed, owed, ev.app, ev.fingerprint))
        return True


def scan(now: float, s: ErrorSettings, store: follow_logs.CursorStore) -> int:
    """One pass: the lines stored since the last one, up to `errors_settle_s` ago. Returns the events stored."""
    since = store.get(CURSOR)
    if since is None:
        store.set(CURSOR, now - s.errors_settle_s)  # nothing before M2.3 started: no flood of emails about old errors
        return 0
    to = now - s.errors_settle_s
    if to <= since:
        return 0
    events, open_traces = eg.assemble(fetch_rows(since, to), force_before=now - 30, window_end=to)
    stored = sum(record(ev, s) for ev in events)
    store.set(CURSOR, min([to, *(t - 1e-6 for t in open_traces.values())]))
    return stored


# --- emailing -----------------------------------------------------------------------------------------
def app_labels(app: str) -> dict | None:
    try:
        return docker_api.inspect(f"annex-app-{app}").get("Config", {}).get("Labels") or {}
    except (docker_api.DockerError, OSError):
        return None


def due_groups(app: str) -> list[dict]:
    with db.connect() as conn:
        rows = conn.execute("SELECT fingerprint, title, kind, first_seen, count, last_release, sample_msg, last_user, "
                            "sample_request_id, alert_again FROM annex_ops.error_groups WHERE app = %s AND alert_due "
                            "ORDER BY first_seen", (app,)).fetchall()
    keys = ["fingerprint", "title", "kind", "first_seen", "count", "last_release", "sample_msg", "last_user",
            "sample_request_id", "alert_again"]
    return [dict(zip(keys, r)) for r in rows]


def mark_sent(app: str, fingerprints: list[str], summary: bool) -> None:
    with db.connect() as conn:
        conn.execute("UPDATE annex_ops.error_groups SET alert_due = false, alert_again = false, alerted_at = now() "
                     "WHERE app = %s AND fingerprint = ANY(%s)", (app, fingerprints))
        conn.execute("INSERT INTO annex_ops.error_alerts (app, groups, summary) VALUES (%s, %s, %s)",
                     (app, len(fingerprints), summary))


def send_due(s: ErrorSettings) -> int:
    """Email what is owed, app by app. Returns how many emails went out."""
    with db.connect() as conn:
        apps = [r[0] for r in conn.execute("SELECT DISTINCT app FROM annex_ops.error_groups WHERE alert_due").fetchall()]
    sent_total = 0
    for app in apps:
        labels = app_labels(app)
        due = due_groups(app)
        if labels is None:  # the app is gone: nobody to tell
            mark_sent(app, [g["fingerprint"] for g in due], False) if due else None
            continue
        with db.connect() as conn:
            sent = conn.execute("SELECT count(*) FROM annex_ops.error_alerts WHERE app = %s AND ts > now() - interval '1 hour'",
                                (app,)).fetchone()[0]
        singles, rest = eg.plan_alerts(due, sent, s.error_alerts_per_hour)
        try:
            users = directory.users()
        except OpsError:
            users = {}
        emails, channels, notes = messages.recipients("app", labels.get("com.kaldi.annex.owner"),
                                                      labels.get("com.kaldi.annex.alerts-to"), users,
                                                      config.slack_connected(), config.ALERT_SLACK_CHANNEL)
        link = config.app_url(app) or config.STATUS_URL or None

        def send(subject: str, body: str) -> None:
            data = {"title": subject.removeprefix("[Annex] "), "body": "\n".join([body, "", *notes]).strip(),
                    "link": link, "link_text": "Open the app" if config.app_url(app) else "Open the status page"}
            incidents.send_alert(emails, subject, {k: v for k, v in data.items() if v}, channels, subject)

        for g in singles:
            send(eg.alert_subject(app, g, bool(g["alert_again"])), eg.alert_body(app, g))
            mark_sent(app, [g["fingerprint"]], False)
            sent_total += 1
        if rest:
            send(eg.summary_subject(app, len(rest)), eg.summary_body(app, rest))
            mark_sent(app, [g["fingerprint"] for g in rest], True)
            sent_total += 1
    return sent_total


def housekeeping(s: ErrorSettings) -> None:
    with db.connect() as conn:
        conn.execute("DELETE FROM annex_ops.error_events WHERE ts < now() - make_interval(days => %s)", (s.error_event_days,))
        conn.execute("DELETE FROM annex_ops.error_groups WHERE last_seen < now() - make_interval(days => %s) AND NOT alert_due",
                     (s.error_group_days,))
        conn.execute("DELETE FROM annex_ops.error_alerts WHERE ts < now() - interval '7 days'")


# --- queries for ctl and the CLI --------------------------------------------------------------------------
def current_release(app: str) -> int | None:
    with db.connect() as conn:
        row = conn.execute("SELECT release FROM annex_ops.releases WHERE app = %s AND result = 'live' "
                           "ORDER BY deployed_at DESC, release DESC LIMIT 1", (app,)).fetchone()
    return row[0] if row else None


def list_groups(app: str | None = None, state: str | None = None, limit: int = 100) -> list[dict]:
    """Error groups, newest first, each `active` or `quiet` (see errors_group.group_state)."""
    s = config.error_settings()
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT app, fingerprint, kind, title, extract(epoch FROM first_seen), extract(epoch FROM last_seen), count, "
            "first_release, last_release, sample_msg, sample_request_id, last_user FROM annex_ops.error_groups "
            "WHERE (%s::text IS NULL OR app = %s) ORDER BY last_seen DESC LIMIT %s", (app, app, limit)).fetchall()
    now, cache, out = time.time(), {}, []
    for r in rows:
        cache.setdefault(r[0], current_release(r[0]))
        st = eg.group_state(float(r[5]), r[8], cache[r[0]], now, s.error_quiet_s)
        if state and st != state:
            continue
        out.append({"app": r[0], "fingerprint": r[1], "state": st, "kind": r[2], "title": r[3],
                    "first_seen": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(float(r[4]))),
                    "last_seen": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(float(r[5]))), "count": r[6],
                    "first_release": r[7], "last_release": r[8], "sample": r[9], "request_id": r[10], "user": r[11],
                    "current_release": cache[r[0]]})
    return out


# --- the thread ----------------------------------------------------------------------------------------------
class ErrorScanner(threading.Thread):
    def __init__(self) -> None:
        super().__init__(daemon=True, name="error-scanner")
        self.cursors = follow_logs.CursorStore()
        self.last_housekeeping = 0.0

    def run(self) -> None:
        threads.register(self.name, 60)
        while True:
            s = config.error_settings()
            threads.beat(self.name, max(1.0, s.errors_scan_s) + 60)  # sleep plus a slow pass
            try:
                scan(time.time(), s, self.cursors)
                send_due(s)
                if time.time() - self.last_housekeeping > HOUSEKEEPING_S:
                    self.last_housekeeping = time.time()
                    housekeeping(s)
            except Exception:  # noqa: BLE001: the scanner must never die (the database may still be starting)
                log("pass failed:\n" + traceback.format_exc(limit=3))
            time.sleep(max(1.0, s.errors_scan_s))
