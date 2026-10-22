"""Incidents: the one thread that runs the alert engine and stores incidents; messages go through the outbox.

The Docker watcher, the log followers and the health checker put inputs on a queue; this thread is the only
consumer and the only writer, so there are no races. It also ticks the engine every `tick_s` seconds.

Since hardening B5 (D90) this thread never waits on a mail server, Slack or a log tail: a message that is owed is a row
in `annex_ops.outbox`, which the sender thread (`sender.py`) composes, sends and retries. The engine runs on the
monotonic clock; this thread converts the wall times that come in (a Traefik line's timestamp, a backup's mtime, a
maintenance window, restored incidents and saved timers) and the engine times that go out (an incident's opening, a
signal's start), so a wall-clock step can't freeze or fire a timer. The engine's counters and timers that Docker can't
give back (crashes, when a container stopped, when memory went low) are saved in `annex_ops.engine_state` whenever they
change, in wall time, and loaded at start.
"""
import dataclasses
import queue
import sys
import threading
import time
import traceback

from psycopg.types.json import Jsonb

from . import config, db, docker_api, email, engine as eng, messages, sender, slack, threads
from .errors import OpsError

OFELIA = "annex-ofelia-1"


def log(msg: str) -> None:
    print(f"incidents: {msg}", file=sys.stderr, flush=True)


class Clock:
    """The engine's clock and the wall clock. `now()` -> (monotonic seconds, offset), where wall = monotonic + offset."""

    def now(self) -> tuple[float, float]:
        mono = time.monotonic()
        return mono, time.time() - mono


def to_engine_time(inp, offset: float):
    """An input whose times are wall clock (epoch seconds), on the engine's monotonic clock instead."""
    def m(t):
        return None if t is None else t - offset
    if isinstance(inp, eng.Request):
        return dataclasses.replace(inp, ts=inp.ts - offset)
    if isinstance(inp, eng.HostSample):
        return dataclasses.replace(inp, backup_at=m(inp.backup_at), dump_at=m(inp.dump_at))
    if isinstance(inp, eng.HookFailing):
        return dataclasses.replace(inp, since=inp.since - offset)
    if isinstance(inp, eng.Maintenance):
        return dataclasses.replace(inp, until=m(inp.until))
    return inp


def signals_shifted(signals: dict, offset: float) -> dict:
    """A copy of a signals snapshot with every `since` moved by `offset` (engine -> wall: +offset, wall -> engine: -offset)."""
    out = {}
    for name, s in signals.items():
        s = dict(s)
        if isinstance(s.get("since"), (int, float)):
            s["since"] = s["since"] + offset
        out[name] = s
    return out


class StateStore:
    """annex_ops.engine_state: the engine's saved counters and timers (Engine.export_state), one row per key."""

    def load(self) -> dict:
        with db.connect() as conn:
            return {k: v for k, v in conn.execute("SELECT key, value FROM annex_ops.engine_state").fetchall()}

    def save(self, changed: dict, removed: list[str]) -> None:
        with db.connect() as conn, conn.cursor() as cur:
            if changed:
                cur.executemany("INSERT INTO annex_ops.engine_state (key, value, updated_at) VALUES (%s, %s, now()) "
                                "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = now()",
                                [(k, Jsonb(v)) for k, v in changed.items()])
            if removed:
                cur.execute("DELETE FROM annex_ops.engine_state WHERE key = ANY(%s)", (removed,))


class Manager(threading.Thread):
    def __init__(self, outbox: sender.OutboxStore | None = None, state: StateStore | None = None,
                 clock: Clock | None = None) -> None:
        super().__init__(daemon=True, name="engine")
        t = config.tuning()
        self.q: queue.Queue = queue.Queue(maxsize=t.queue_max)
        self.queue_soft = t.queue_soft
        self.dropped = 0                         # request lines dropped because the queue was past queue_soft (D86)
        self.last_drop_log = 0.0
        self.engine = eng.Engine(t)
        self.outbox = outbox or sender.OutboxStore()
        self.state = state or StateStore()
        self.clock = clock or Clock()
        self.wake_sender = lambda: None          # set by start_background: the sender looks at once
        self.ids: dict[str, int] = {}            # subject -> id of its open incident
        self.first_signal: dict[str, str] = {}   # subject -> the signal that opened it (wording of "resolved")
        self.opened_wall: dict[str, float] = {}  # subject -> when its open incident opened, wall clock (messages)
        self.maint_wall: dict[str, float] = {}   # subject -> the maintenance window's end as the table says (wall)
        self.saved: dict = {}                    # what engine_state holds, on the engine's clock (to write only changes)
        self.offset = 0.0                        # wall - monotonic, taken at the start of each pass
        self.last_tick: float | None = None
        self.last_housekeeping: float | None = None

    def submit(self, inp) -> None:
        """Queue an input for the engine. A request line (a 5xx-rate sample, log-derived) is the one input that may be
        dropped: past `queue_soft` waiting inputs it is counted and discarded, never blocking the follower. Everything
        else (Docker events, verdicts, samples) waits for room: the soft limit leaves it half the queue (D86)."""
        if isinstance(inp, eng.Request):
            if self.q.qsize() >= self.queue_soft:
                self.drop_request()
                return
            try:
                self.q.put_nowait(inp)
            except queue.Full:
                self.drop_request()
            return
        self.q.put(inp)

    def drop_request(self) -> None:
        self.dropped += 1
        if time.monotonic() - self.last_drop_log > 60:
            self.last_drop_log = time.monotonic()
            log(f"the engine's queue is backed up: {self.dropped} request line(s) dropped so far (Docker events are kept)")

    # --- thread ------------------------------------------------------------
    def run(self) -> None:
        threads.register(self.name, 30)  # a pass waits only on the database now (the pool gives up after 10 s)
        while True:
            threads.beat(self.name)
            try:
                self.restore()
                break
            except Exception as e:  # noqa: BLE001: the database may still be starting
                log(f"can't load open incidents yet ({type(e).__name__}: {e}); retrying in 5 s")
                time.sleep(5)
        while True:
            threads.beat(self.name)
            try:
                inp = self.q.get(timeout=1)
            except queue.Empty:
                inp = None
            try:
                self.step(inp)
            except Exception:  # noqa: BLE001: the engine thread must never die
                log("unexpected error:\n" + traceback.format_exc())

    def step(self, inp) -> None:
        """One pass: the input (if any), then the time-based rules when a tick is due, then the saved state."""
        now, self.offset = self.clock.now()
        self.engine.tuning = config.tuning()
        if inp is not None:
            self.apply(self.engine.handle(now, to_engine_time(inp, self.offset)), now)
        if self.last_tick is None or now - self.last_tick >= self.engine.tuning.tick_s:
            self.last_tick = now
            self.sync_maintenance(now)
            self.apply(self.engine.tick(now), now)
            self.housekeeping(now)
        if not isinstance(inp, eng.Request):  # a request line changes nothing that is saved
            self.save_state()

    def restore(self) -> None:
        now, self.offset = self.clock.now()
        try:
            saved = self.state.load()
        except Exception as e:  # noqa: BLE001: no table yet (before the first push of this version): start without
            log(f"no saved engine state ({type(e).__name__}: {e}); counters start over")
            saved = {}
        self.engine.handle(now, eng.LoadState(saved, self.offset))
        # rows this version couldn't read are listed too, so the first write removes them
        self.saved = {**{k: None for k in saved}, **self.engine.export_state()}
        with db.connect() as conn:
            rows = conn.execute("SELECT id, subject, extract(epoch FROM opened_at), extract(epoch FROM last_notified_at), "
                                "reminders, signals, history FROM annex_ops.incidents WHERE resolved_at IS NULL").fetchall()
        incidents = []
        for id_, subject, opened, notified, reminders, signals, history in rows:
            self.ids[subject] = id_
            self.first_signal[subject] = (history[0]["signal"] if history else next(iter(signals), "down"))
            self.opened_wall[subject] = float(opened)
            incidents.append({"subject": subject, "opened_at": float(opened) - self.offset,
                              "last_notified_at": float(notified or opened) - self.offset,
                              "reminders": reminders, "signals": signals_shifted(signals, -self.offset)})
        self.engine.handle(now, eng.Restore(incidents))
        if incidents:
            log(f"restored {len(incidents)} open incident(s)")
        if saved:
            log(f"restored {len(saved)} saved counter(s) and timer(s)")

    def save_state(self) -> None:
        """Write the engine's saved counters and timers that changed since the last write (compared on the engine's
        clock, so the offset's jitter doesn't count as a change), in wall time. A failed write is tried again next pass."""
        current = self.engine.export_state()
        if current == self.saved:
            return
        changed = {k: v for k, v in self.engine.export_state(self.offset).items() if current[k] != self.saved.get(k)}
        removed = [k for k in self.saved if k not in current]
        try:
            self.state.save(changed, removed)
            self.saved = current
        except Exception as e:  # noqa: BLE001: the database may be down; the engine carries on from memory
            log(f"couldn't save the engine's counters ({type(e).__name__}: {e}); will retry")

    def sync_maintenance(self, now: float) -> None:
        """`ctl maintenance` runs in another process and writes the table: pick the changes up."""
        with db.connect() as conn:
            conn.execute("DELETE FROM annex_ops.maintenance WHERE until <= now()")
            rows = conn.execute("SELECT subject, extract(epoch FROM until) FROM annex_ops.maintenance").fetchall()
        wanted = {s: float(u) for s, u in rows}
        for subject, until in wanted.items():
            if self.maint_wall.get(subject) != until:
                self.maint_wall[subject] = until
                self.apply(self.engine.handle(now, to_engine_time(eng.Maintenance(subject, until), self.offset)), now)
        for subject in list(self.maint_wall):
            if subject not in wanted:
                del self.maint_wall[subject]
                self.apply(self.engine.handle(now, eng.Maintenance(subject, None)), now)

    def housekeeping(self, now: float) -> None:
        if self.last_housekeeping is not None and now - self.last_housekeeping < 86400:
            return
        self.last_housekeeping = now
        with db.connect() as conn:
            conn.execute("DELETE FROM annex_ops.job_runs WHERE finished_at < now() - interval '90 days'")

    # --- applying engine actions ---------------------------------------------
    def wall(self, t: float) -> float:
        return t + self.offset

    def apply(self, actions: list[eng.Action], now: float) -> None:
        for a in actions:
            try:
                getattr(self, "do_" + a.kind)(a, now)
            except Exception:  # noqa: BLE001: a failed write must not stop the rest
                log(f"{a.kind} {a.subject} failed:\n" + traceback.format_exc())

    def do_forget(self, a: eng.Action, now: float) -> None:
        pass  # the engine already dropped the container's counters

    def do_open(self, a: eng.Action, now: float) -> None:
        kind, name, _ = messages.parse_subject(a.subject)
        signals = signals_shifted(a.signals, self.offset)
        history = [{"ts": self.wall(now), "signal": n, "event": "raised", "detail": s.get("detail")} for n, s in signals.items()]
        opened = self.wall(a.opened_at)
        with db.connect() as conn:
            self.ids[a.subject] = conn.execute(
                "INSERT INTO annex_ops.incidents (subject, app, opened_at, signals, history, last_notified_at) "
                "VALUES (%s, %s, to_timestamp(%s), %s, %s, now()) "
                "ON CONFLICT (subject) WHERE resolved_at IS NULL DO UPDATE SET signals = EXCLUDED.signals "
                "RETURNING id", (a.subject, name if kind in ("app", "job") else None, opened,
                                 Jsonb(signals), Jsonb(history))).fetchone()[0]
        self.first_signal[a.subject] = next(iter(a.signals), "down")
        self.opened_wall[a.subject] = opened
        self.notify("open", a)

    def do_add_signal(self, a: eng.Action, now: float) -> None:
        self.record(a, now, "raised", signals_update=True)

    def do_clear_signal(self, a: eng.Action, now: float) -> None:
        self.record(a, now, "cleared", signals_update=False)

    def record(self, a: eng.Action, now: float, event: str, signals_update: bool) -> None:
        id_ = self.ids.get(a.subject)
        if id_ is None:
            return
        entry = Jsonb([{"ts": self.wall(now), "signal": a.signal, "event": event, "detail": a.detail}])
        sig = self.engine.signals.get(a.subject, {}).get(a.signal) if signals_update else None
        with db.connect() as conn:
            if signals_update and sig is not None:
                sig = signals_shifted({a.signal: sig}, self.offset)[a.signal]
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
        self.opened_wall.pop(a.subject, None)

    def do_remind(self, a: eng.Action, now: float) -> None:
        id_ = self.ids.get(a.subject)
        if id_ is not None:
            with db.connect() as conn:
                conn.execute("UPDATE annex_ops.incidents SET last_notified_at = now(), reminders = reminders + 1 "
                             "WHERE id = %s", (id_,))
        self.notify("remind", a)

    # --- messages --------------------------------------------------------------
    def notify(self, kind: str, a: eng.Action) -> None:
        """A message is owed: one outbox row with what the engine said, in wall time. The sender thread does the rest
        (recipients, log lines, the sends and their retries), so this thread never waits on SMTP, Slack or Docker."""
        action = {"signals": signals_shifted(a.signals, self.offset), "duration_s": a.duration_s,
                  "opened_at": self.opened_wall.get(a.subject, self.wall(a.opened_at))}
        self.outbox.add(kind, a.subject, action)
        self.wake_sender()


def send_alert(emails: list[str], subject: str, data: dict, channels: list[str], slack_text: str) -> None:
    """One email to everyone; if that is refused (say, one address outside the allowed domains), one each, so the
    others still hear. Failures are already in the send log and never stop the caller. The error scanner's emails
    (errors_run) use this, in their own thread; the alert engine's messages go through the outbox (sender.py)."""
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
    """The health checker's current verdict for an app: up, down, none (no check, e.g. a job-only app) or unknown (the
    checker isn't running). The thread checks once a minute, so right after a deploy there is no result yet: a reader
    that finds none from after the container's start checks once itself (and stores it), so `annex status` straight
    after `annex deploy` says the truth."""
    from . import health

    row = health.latest().get(f"app:{app}")
    try:
        info = docker_api.inspect(f"annex-app-{app}")
    except (docker_api.DockerError, OSError):
        info = None
    labels = ((info or {}).get("Config") or {}).get("Labels") or {}
    if info is not None and labels.get("com.kaldi.annex.healthcheck"):
        started = docker_api.parse_ts(((info.get("State") or {}).get("StartedAt")) or "")
        ts = health.targets([{"Labels": labels}], "")
        if ts and health.stale_for(row, started):
            row = health.check_now(ts[0], config.tuning().check_timeout_s)
    elif row is None:
        return {"state": "none"} if info is not None else {"state": "unknown", "detail": "no result yet"}
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
    out = sender.Sender(mgr.outbox)  # the alert messages, off the engine thread (D90)
    mgr.wake_sender = out.wake
    out.start()
    threads.gauge("alert-sender", out.status)
    mgr.start()
    threads.gauge("engine", lambda: {"waiting": mgr.q.qsize(), "max": mgr.q.maxsize, "dropped": mgr.dropped})
    ingest = _ingest = usage.Ingest()
    threads.gauge("usage", lambda: {"buffered": len(ingest.buf), "bytes": ingest.bytes, "max": ingest.max_rows,
                                    "max_bytes": ingest.max_bytes, "dropped": ingest.dropped})
    usage.Worker(ingest).start()
    digest.Worker().start()
    hooks_delivery.Worker(mgr.submit).start()
    host.Sampler(mgr.submit).start()
    # logs that outlive containers (M2.2): one follower per shipped container, started and stopped by the watcher's events
    sink = _sink = logstore.Sink()
    sink.start()
    threads.gauge("log-sink", sink.status)
    releases = logship.Releases()
    logship.Keeper(mgr.submit, sink).start()
    errors_run.ErrorScanner().start()  # error groups and their emails (M2.3)
    audit.Auditor().start()  # sign-ins, webhook events and gateway reads into the audit trail (M2.5)
    shipper = logship.Shipper(sink, releases)
    shipper.start()
    threads.gauge("log-shipper", shipper.status)

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
