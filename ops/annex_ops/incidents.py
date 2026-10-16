"""Incidents: the one thread that runs the alert engine, stores incidents and sends the messages.

The Docker watcher, the log followers and the health checker put inputs on a queue; this thread is the only
consumer and the only writer, so there are no races. It also ticks the engine every `tick_s` seconds.
"""
import queue
import re
import sys
import threading
import time
import traceback

from psycopg.types.json import Jsonb

from . import config, db, directory, docker_api, email, engine as eng, messages, slack
from .errors import OpsError

OFELIA = "annex-ofelia-1"
ANSI = re.compile(r"\x1b\[[0-9;]*m")


def log(msg: str) -> None:
    print(f"incidents: {msg}", file=sys.stderr, flush=True)


class Manager(threading.Thread):
    def __init__(self) -> None:
        super().__init__(daemon=True, name="engine")
        self.q: queue.Queue = queue.Queue()
        self.engine = eng.Engine(config.tuning())
        self.ids: dict[str, int] = {}            # subject -> id of its open incident
        self.first_signal: dict[str, str] = {}   # subject -> the signal that opened it (wording of "resolved")
        self.apps: dict[str, dict] = {}          # app -> owner, alerts-to, last seen (the container may be gone later)
        self.last_tick = 0.0
        self.last_housekeeping = 0.0

    def submit(self, inp) -> None:
        self.q.put(inp)

    # --- thread ------------------------------------------------------------
    def run(self) -> None:
        while True:
            try:
                self.restore()
                break
            except Exception as e:  # noqa: BLE001: the database may still be starting
                log(f"can't load open incidents yet ({type(e).__name__}: {e}); retrying in 5 s")
                time.sleep(5)
        while True:
            try:
                inp = self.q.get(timeout=1)
            except queue.Empty:
                inp = None
            now = time.time()
            try:
                self.engine.tuning = config.tuning()
                if inp is not None:
                    self.apply(self.engine.handle(now, inp), now)
                if now - self.last_tick >= self.engine.tuning.tick_s:
                    self.last_tick = now
                    self.sync_maintenance(now)
                    self.apply(self.engine.tick(now), now)
                    self.housekeeping(now)
            except Exception:  # noqa: BLE001: the engine thread must never die
                log("unexpected error:\n" + traceback.format_exc())

    def restore(self) -> None:
        with db.connect() as conn:
            rows = conn.execute("SELECT id, subject, extract(epoch FROM opened_at), extract(epoch FROM last_notified_at), "
                                "reminders, signals, history FROM annex_ops.incidents WHERE resolved_at IS NULL").fetchall()
        incidents = []
        for id_, subject, opened, notified, reminders, signals, history in rows:
            self.ids[subject] = id_
            self.first_signal[subject] = (history[0]["signal"] if history else next(iter(signals), "down"))
            incidents.append({"subject": subject, "opened_at": float(opened), "last_notified_at": float(notified or opened),
                              "reminders": reminders, "signals": signals})
        self.engine.handle(time.time(), eng.Restore(incidents))
        if incidents:
            log(f"restored {len(incidents)} open incident(s)")

    def sync_maintenance(self, now: float) -> None:
        """`ctl maintenance` runs in another process and writes the table: pick the changes up."""
        with db.connect() as conn:
            conn.execute("DELETE FROM annex_ops.maintenance WHERE until <= now()")
            rows = conn.execute("SELECT subject, extract(epoch FROM until) FROM annex_ops.maintenance").fetchall()
        wanted = {s: float(u) for s, u in rows}
        for subject, until in wanted.items():
            if self.engine.maint.get(subject) != until:
                self.apply(self.engine.handle(now, eng.Maintenance(subject, until)), now)
        for subject in list(self.engine.maint):
            if subject not in wanted:
                self.apply(self.engine.handle(now, eng.Maintenance(subject, None)), now)

    def housekeeping(self, now: float) -> None:
        if now - self.last_housekeeping < 86400:
            return
        self.last_housekeeping = now
        with db.connect() as conn:
            conn.execute("DELETE FROM annex_ops.job_runs WHERE finished_at < now() - interval '90 days'")

    # --- applying engine actions ---------------------------------------------
    def apply(self, actions: list[eng.Action], now: float) -> None:
        for a in actions:
            try:
                getattr(self, "do_" + a.kind)(a, now)
            except Exception:  # noqa: BLE001: a failed write or send must not stop the rest
                log(f"{a.kind} {a.subject} failed:\n" + traceback.format_exc())

    def do_forget(self, a: eng.Action, now: float) -> None:
        pass  # the engine already dropped the container's counters

    def do_open(self, a: eng.Action, now: float) -> None:
        kind, name, _ = messages.parse_subject(a.subject)
        history = [{"ts": now, "signal": n, "event": "raised", "detail": s.get("detail")} for n, s in a.signals.items()]
        with db.connect() as conn:
            self.ids[a.subject] = conn.execute(
                "INSERT INTO annex_ops.incidents (subject, app, opened_at, signals, history, last_notified_at) "
                "VALUES (%s, %s, to_timestamp(%s), %s, %s, now()) "
                "ON CONFLICT (subject) WHERE resolved_at IS NULL DO UPDATE SET signals = EXCLUDED.signals "
                "RETURNING id", (a.subject, name if kind in ("app", "job") else None, a.opened_at,
                                 Jsonb(a.signals), Jsonb(history))).fetchone()[0]
        self.first_signal[a.subject] = next(iter(a.signals), "down")
        self.notify("open", a)

    def do_add_signal(self, a: eng.Action, now: float) -> None:
        self.record(a, now, "raised", signals_update=True)

    def do_clear_signal(self, a: eng.Action, now: float) -> None:
        self.record(a, now, "cleared", signals_update=False)

    def record(self, a: eng.Action, now: float, event: str, signals_update: bool) -> None:
        id_ = self.ids.get(a.subject)
        if id_ is None:
            return
        entry = Jsonb([{"ts": now, "signal": a.signal, "event": event, "detail": a.detail}])
        sig = self.engine.signals.get(a.subject, {}).get(a.signal) if signals_update else None
        with db.connect() as conn:
            if signals_update and sig is not None:
                conn.execute("UPDATE annex_ops.incidents SET signals = signals || jsonb_build_object(%s::text, %s::jsonb), "
                             "history = history || %s WHERE id = %s", (a.signal, Jsonb(sig), entry, id_))
            else:
                conn.execute("UPDATE annex_ops.incidents SET signals = signals - %s, history = history || %s "
                             "WHERE id = %s", (a.signal, entry, id_))

    def do_resolve(self, a: eng.Action, now: float) -> None:
        id_ = self.ids.pop(a.subject, None)
        first = self.first_signal.pop(a.subject, "down")
        if id_ is not None:
            with db.connect() as conn:
                conn.execute("UPDATE annex_ops.incidents SET resolved_at = now(), resolved_quietly = %s WHERE id = %s",
                             (a.quiet, id_))
        if not a.quiet:
            a.signals = {first: {}}
            self.notify("resolve", a)

    def do_remind(self, a: eng.Action, now: float) -> None:
        id_ = self.ids.get(a.subject)
        if id_ is not None:
            with db.connect() as conn:
                conn.execute("UPDATE annex_ops.incidents SET last_notified_at = now(), reminders = reminders + 1 "
                             "WHERE id = %s", (id_,))
        self.notify("remind", a)

    # --- messages --------------------------------------------------------------
    def app_info(self, app: str) -> dict:
        try:
            labels = docker_api.inspect(f"annex-app-{app}").get("Config", {}).get("Labels") or {}
            self.apps[app] = {"owner": labels.get("com.kaldi.annex.owner"),
                              "alerts_to": labels.get("com.kaldi.annex.alerts-to")}
        except (docker_api.DockerError, OSError):
            pass  # the container is gone or the proxy is down: use what we last knew
        return self.apps.get(app, {})

    def log_lines(self, subject: str) -> list[str]:
        kind, name, job = messages.parse_subject(subject)
        if kind == "app":
            return docker_api.tail_logs(f"annex-app-{name}", 20)
        if kind == "job":
            key = f"{name}-{job}"
            return [ANSI.sub("", ln) for ln in docker_api.tail_logs(OFELIA, 400) if f'"{key}"' in ln][-20:]
        if name in ("disk", "memory", "logs", "front-door"):  # no container of their own
            return []
        return docker_api.tail_logs(f"annex-{name}-1", 20)

    def notify(self, kind: str, a: eng.Action) -> None:
        k, name, _ = messages.parse_subject(a.subject)
        info = self.app_info(name) if k in ("app", "job") else {}
        try:
            users = directory.users()
        except OpsError:
            users = {}
        emails, channels, notes = messages.recipients(k, info.get("owner"), info.get("alerts_to"), users,
                                                      config.slack_connected(), config.ALERT_SLACK_CHANNEL)
        subject = messages.subject_line(kind, a.subject, a.signals, a.duration_s)
        app_url = config.app_url(name) if k in ("app", "job") else ""
        text = messages.body("resolve" if kind == "resolve" else kind, a.subject, a.signals, a.opened_at, a.duration_s,
                             notes, config.STATUS_URL, app_url)
        data = {"title": subject.removeprefix("[Annex] "), "body": text,
                "link": app_url or config.STATUS_URL or None, "link_text": "Open the app" if app_url else "Open the status page"}
        if kind != "resolve":
            lines = self.log_lines(a.subject)
            if lines:
                data["details"] = "Last log lines:\n" + "\n".join(lines)
        data = {k2: v for k2, v in data.items() if v}
        send_alert(emails, subject, data, channels, f"{subject}" + (f" {config.STATUS_URL}" if config.STATUS_URL else ""))


def send_alert(emails: list[str], subject: str, data: dict, channels: list[str], slack_text: str) -> None:
    """One email to everyone; if that is refused (say, one address outside the allowed domains), one each, so the
    others still hear. Failures are already in the send log and never stop the engine."""
    if emails:
        try:
            email.send_email("_platform", emails, subject, template="notice", data=data)
        except OpsError as e:
            log(f"email to {len(emails)} recipient(s) failed ({e.status}: {e.message}); trying one by one")
            for addr in emails:
                try:
                    email.send_email("_platform", [addr], subject, template="notice", data=data)
                except OpsError:
                    pass
    for channel in channels:
        try:
            slack.post_slack("_platform", channel, slack_text)
        except OpsError:
            pass


# --- queries for ctl and the CLI ---------------------------------------------------
def list_incidents(open_only: bool = False, app: str | None = None, limit: int = 50) -> list[dict]:
    where = ["TRUE"] + (["resolved_at IS NULL"] if open_only else []) + (["app = %s"] if app else [])
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT id, subject, app, opened_at, resolved_at, resolved_quietly, reminders, signals "
            f"FROM annex_ops.incidents WHERE {' AND '.join(where)} ORDER BY opened_at DESC LIMIT %s",
            ((app,) if app else ()) + (limit,)).fetchall()
    return [{"id": r[0], "subject": r[1], "app": r[2], "opened_at": r[3].isoformat(timespec="seconds"),
             "resolved_at": r[4] and r[4].isoformat(timespec="seconds"), "resolved_quietly": r[5],
             "reminders": r[6], "signals": {k: v.get("detail") for k, v in r[7].items()}} for r in rows]


def last_job_runs(app: str) -> list[dict]:
    with db.connect() as conn:
        rows = conn.execute("SELECT DISTINCT ON (job) job, finished_at, ok, duration_ms, error FROM annex_ops.job_runs "
                            "WHERE app = %s ORDER BY job, finished_at DESC", (app,)).fetchall()
    return [{"job": r[0], "last_run": r[1].isoformat(timespec="seconds"), "ok": r[2], "duration_ms": r[3], "error": r[4]}
            for r in rows]


def record_job_run(app: str, job: str, finished_at: float, ok: bool, duration_ms: int, error: str | None) -> None:
    with db.connect() as conn:
        # a restart replays the lines after the saved cursor: don't record a run twice
        conn.execute("INSERT INTO annex_ops.job_runs (app, job, finished_at, ok, duration_ms, error) "
                     "SELECT %s, %s, to_timestamp(%s), %s, %s, %s WHERE NOT EXISTS (SELECT 1 FROM annex_ops.job_runs "
                     "WHERE app = %s AND job = %s AND finished_at = to_timestamp(%s))",
                     (app, job, finished_at, ok, duration_ms, error, app, job, finished_at))


def health_check(app: str) -> dict:
    """The health checker's current verdict for an app: up, down, none (no check, e.g. a job-only app) or unknown (no result
    yet, or the checker isn't running)."""
    from . import health

    row = health.latest().get(f"app:{app}")
    if row is None:
        try:
            labels = docker_api.inspect(f"annex-app-{app}").get("Config", {}).get("Labels") or {}
        except (docker_api.DockerError, OSError):
            return {"state": "unknown", "detail": "no result yet"}
        return {"state": "unknown", "detail": "no result yet"} if labels.get("com.kaldi.annex.healthcheck") else {"state": "none"}
    out = health.state_of(row, time.time(), config.tuning().check_interval_s)
    if "checked_at" in out:
        out["checked_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(out["checked_at"]))
    return out


def app_status(app: str) -> dict:
    open_ = list_incidents(open_only=True, app=app)
    app_inc = next((i for i in open_ if i["subject"] == f"app:{app}"), None)
    failing = {i["subject"].split("/", 1)[1] for i in open_ if i["subject"].startswith("job:")}
    jobs = [{**j, "failing": j["job"] in failing} for j in last_job_runs(app)]
    from . import usage

    row = next((r for r in usage.usage_rows() if r["app"] == app), None)
    totals = row and {"users_7d": row["users_7d"], "trend": usage.trend(row["users_7d"], row["users_prev_7d"]),
                      "requests_7d": row["requests_7d"]}
    return {"app": app, "health_check": health_check(app), "incident": app_inc, "jobs": jobs, "usage": totals}


def set_maintenance(subject: str, minutes: int) -> dict:
    with db.connect() as conn:
        until = conn.execute("INSERT INTO annex_ops.maintenance (subject, until) VALUES (%s, now() + make_interval(mins => %s)) "
                             "ON CONFLICT (subject) DO UPDATE SET until = EXCLUDED.until RETURNING until",
                             (subject, minutes)).fetchone()[0]
    return {"subject": subject, "until": until.isoformat(timespec="seconds")}


def end_maintenance(subject: str) -> dict:
    with db.connect() as conn:
        conn.execute("DELETE FROM annex_ops.maintenance WHERE subject = %s", (subject,))
    return {"subject": subject, "until": None}


_manager: Manager | None = None


_ingest = None
_sink = None


def start_background() -> Manager:
    """Start the engine thread, the Docker watcher, the log followers and the usage and digest threads (called
    once, at app startup)."""
    global _manager, _ingest, _sink
    from . import audit, digest, errors_run, follow_logs, health, hooks_delivery, host, logship, logstore, usage, watch_docker

    mgr = _manager = Manager()
    mgr.start()
    ingest = _ingest = usage.Ingest()
    usage.Worker(ingest).start()
    digest.Worker().start()
    hooks_delivery.Worker(mgr.submit).start()
    host.Sampler(mgr.submit).start()
    # logs that outlive containers (M2.2): one follower per shipped container, started and stopped by the watcher's events
    sink = _sink = logstore.Sink()
    sink.start()
    releases = logship.Releases()
    logship.Keeper(mgr.submit, sink).start()
    errors_run.ErrorScanner().start()  # error groups and their emails (M2.3)
    audit.Auditor().start()  # sign-ins, webhook events and gateway reads into the audit trail (M2.5)
    shipper = logship.Shipper(sink, releases)
    shipper.start()

    def submit(inp) -> None:
        mgr.submit(inp)
        shipper.submit(inp)
        if isinstance(inp, (eng.Seen, eng.Start)) and inp.subject.startswith("app:"):
            try:
                ingest.touch_app(inp.subject[4:])  # an app that exists but has no traffic still has a first_seen
            except Exception as e:  # noqa: BLE001
                log(f"usage: couldn't record {inp.subject}: {type(e).__name__}: {e}")

    cursors = follow_logs.CursorStore()
    # the health checker starts after the watcher's first reconcile pass, so the engine has seen every container
    # before the first verdict reaches it (a verdict for an app it hasn't seen is ignored)
    checker: list = []

    def start_checker() -> None:
        if not checker:
            checker.append(health.Checker(mgr.submit))
            checker[0].start()

    watch_docker.Watcher(submit, start_checker).start()
    follow_logs.Follower("annex-traefik-1", traefik_handler(mgr, ingest), cursors).start()
    follow_logs.Follower(OFELIA, ofelia_handler(mgr), cursors).start()
    # scheduled jobs run inside the app's container through Ofelia, so their output is in Ofelia's log: a second
    # reader, in exact mode (the lines of one run share a microsecond), with its own cursor
    follow_logs.Follower(OFELIA, logship.JobOutput(sink, releases), cursors, key=logship.SHIP_PREFIX + OFELIA, exact=True,
                         before_save=sink.flush).start()
    return mgr


def stop_background() -> None:
    """At shutdown: write the buffered usage rows and log lines, so a restart neither loses nor repeats them."""
    if _ingest is not None:
        _ingest.flush()
    if _sink is not None:
        _sink.flush()


def traefik_handler(mgr: Manager, ingest=None):
    """One Traefik follower feeds both the alert engine (5xx rate) and usage (who used what)."""
    from . import follow_logs

    def handle(ts: float, stream: str, line: str) -> None:
        parsed = follow_logs.parse_traefik(line)
        if parsed:
            mgr.submit(eng.Request(parsed[0], parsed[1], parsed[2]))
        if ingest is not None:
            ingest.handle(ts, stream, line)

    return handle


def ofelia_handler(mgr: Manager):
    from . import follow_logs

    jobs: dict = {}
    refreshed = [0.0]

    def refresh() -> None:
        if time.time() - refreshed[0] > 30:
            refreshed[0] = time.time()
            jobs.clear()
            jobs.update(follow_logs.job_map(docker_api.containers()))

    def handle(ts: float, stream: str, line: str) -> None:
        r = follow_logs.parse_ofelia(line)
        if not r or r["skipped"]:
            return
        if r["key"] not in jobs:
            refresh()
        if r["key"] not in jobs:
            return  # e.g. the heartbeat job
        app, job = jobs[r["key"]]
        record_job_run(app, job, ts, not r["failed"], r["duration_ms"], r["error"])
        mgr.submit(eng.JobResult(app, job, not r["failed"], r["error"] or ""))

    return handle
