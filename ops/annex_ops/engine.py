"""The alert engine: signals -> incidents. Pure logic: no database, network or clock of its own.

Everything that can go wrong with an app or the platform is a *signal* (down, stopped, crash-loop, unhealthy,
oom, errors, job-failed) attached to a *subject* (`app:<name>`, `job:<app>/<job>`, `platform:<service>`).
A subject has at most one open incident: the first signal opens it (and the caller sends one alert), later
signals are added to it silently, and it resolves once every signal has cleared. That is what makes
"stop an app", which Docker and Gatus both notice, produce exactly one alert and one "resolved".

`handle(now, input)` takes one input and `tick(now)` evaluates the time-based rules; both return actions for
the caller (incidents.py) to persist and send. Containers are tracked by their Docker id, so a Compose recreate
(new container, old one destroyed a moment later) doesn't look like a crash.
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field


@dataclass
class Tuning:
    stopped_grace_s: float = 60       # a container must be down this long before it counts as stopped
    crash_count: int = 3              # non-zero exits ...
    crash_window_s: float = 600       # ... within this window make a crash loop
    crash_stable_s: float = 600       # a crash loop clears after this long without an exit
    error_window_s: float = 300       # 5xx rate is measured over this window ...
    error_min: int = 10               # ... needs at least this many 5xx ...
    error_ratio: float = 0.05         # ... and at least this share of requests
    reminder_first_s: float = 3600    # first reminder after an hour open, then
    reminder_repeat_s: float = 86400  # every 24 hours
    tick_s: float = 15


# --- inputs ------------------------------------------------------------------
@dataclass
class Seen:  # reconcile pass at startup: a container that exists right now
    cid: str
    name: str
    subject: str
    running: bool
    health: str | None = None  # healthy | unhealthy | starting | None (no healthcheck)
    ignore_stopped: bool = False


@dataclass
class ReconcileDone:  # every container that exists now has been reported as Seen
    cids: set[str]


@dataclass
class Die:
    cid: str
    name: str
    subject: str
    exit_code: int
    ignore_stopped: bool = False


@dataclass
class Stop:
    cid: str
    name: str
    subject: str
    ignore_stopped: bool = False


@dataclass
class Start:
    cid: str
    name: str
    subject: str
    ignore_stopped: bool = False


@dataclass
class Health:
    cid: str
    name: str
    subject: str
    healthy: bool
    ignore_stopped: bool = False


@dataclass
class Oom:
    cid: str
    name: str
    subject: str
    ignore_stopped: bool = False


@dataclass
class Destroy:
    cid: str
    subject: str


@dataclass
class GatusState:
    subject: str  # app:<name> or platform:front-door
    down: bool
    detail: str = ""


@dataclass
class Request:  # one request through an app's Traefik router
    app: str
    ts: float
    status: int


@dataclass
class JobResult:
    app: str
    job: str
    ok: bool
    detail: str = ""


@dataclass
class Maintenance:  # until=None ends it
    subject: str
    until: float | None


@dataclass
class Restore:  # open incidents loaded from the database at startup
    incidents: list[dict]  # {subject, opened_at, last_notified_at, reminders, signals: {name: {since, detail, cid}}}


# --- actions -----------------------------------------------------------------
@dataclass
class Action:
    kind: str  # open | add_signal | clear_signal | resolve | remind | forget
    subject: str
    signal: str | None = None
    detail: str | None = None
    quiet: bool = False
    duration_s: float = 0
    signals: dict = field(default_factory=dict)  # snapshot of the active signals (open, remind, resolve)
    opened_at: float = 0


@dataclass
class _Ctr:
    cid: str
    name: str
    subject: str
    ignore_stopped: bool = False
    running: bool = True
    stopped_since: float | None = None
    health: str | None = None
    dies: list = field(default_factory=list)  # (time, exit code) of non-zero exits
    last_die: float | None = None


@dataclass
class _Open:
    opened_at: float
    last_notified: float
    reminders: int = 0


CONTAINER_SIGNALS = ("stopped", "crash-loop", "oom", "unhealthy")


def app_of(subject: str) -> str | None:
    """The app a subject belongs to (app:x -> x, job:x/j -> x), else None."""
    if subject.startswith("app:"):
        return subject[4:]
    if subject.startswith("job:"):
        return subject[4:].split("/", 1)[0]
    return None


class Engine:
    def __init__(self, tuning: Tuning | None = None):
        self.tuning = tuning or Tuning()
        self.ctrs: dict[str, _Ctr] = {}
        self.signals: dict[str, dict[str, dict]] = {}   # subject -> signal -> {since, detail, cid}
        self.open: dict[str, _Open] = {}
        self.requests: dict[str, deque] = {}            # app -> deque of (ts, is_5xx)
        self.err_below: dict[str, float | None] = {}    # app -> since when the 5xx rate is under the threshold
        self.maint: dict[str, float] = {}

    # --- plumbing ----------------------------------------------------------
    def _in_maintenance(self, subject: str, now: float) -> bool:
        app = app_of(subject)
        keys = [subject] + ([f"app:{app}"] if app and not subject.startswith("app:") else [])
        return any(self.maint.get(k, 0) > now for k in keys)

    def _raise(self, now: float, subject: str, name: str, detail: str, out: list[Action], cid: str | None = None) -> None:
        sigs = self.signals.setdefault(subject, {})
        if name in sigs:
            return
        sigs[name] = {"since": now, "detail": detail, "cid": cid}
        if subject in self.open:
            out.append(Action("add_signal", subject, name, detail))
        elif not self._in_maintenance(subject, now):
            self._open(now, subject, out)

    def _open(self, now: float, subject: str, out: list[Action]) -> None:
        self.open[subject] = _Open(opened_at=now, last_notified=now)
        out.append(Action("open", subject, signals=_snapshot(self.signals[subject]), opened_at=now))

    def _clear(self, now: float, subject: str, name: str, out: list[Action], quiet: bool = False) -> None:
        sigs = self.signals.get(subject)
        if not sigs or name not in sigs:
            return
        del sigs[name]
        inc = self.open.get(subject)
        if inc:
            out.append(Action("clear_signal", subject, name))
            if not sigs:
                out.append(Action("resolve", subject, quiet=quiet, duration_s=now - inc.opened_at, opened_at=inc.opened_at))
                del self.open[subject]
        if not sigs:
            del self.signals[subject]

    def _clear_container_signals(self, now: float, cid: str, out: list[Action], names=CONTAINER_SIGNALS, quiet=False) -> None:
        for subject, sigs in list(self.signals.items()):
            for name in list(sigs):
                if name in names and sigs[name].get("cid") == cid:
                    self._clear(now, subject, name, out, quiet)

    def _ctr(self, ev, running: bool | None = None) -> _Ctr:
        c = self.ctrs.get(ev.cid)
        if c is None:
            c = self.ctrs[ev.cid] = _Ctr(ev.cid, ev.name, ev.subject, getattr(ev, "ignore_stopped", False))
        c.name, c.subject = ev.name, ev.subject
        return c

    def _app_degraded(self, app: str) -> bool:
        """Is the app already known to be in trouble: an open incident, or a container that isn't running?"""
        subject = f"app:{app}"
        return subject in self.open or any(c.subject == subject and not c.running for c in self.ctrs.values())

    def _subject_has_container(self, subject: str) -> bool:
        return any(c.subject == subject for c in self.ctrs.values())

    # --- inputs ------------------------------------------------------------
    def handle(self, now: float, inp) -> list[Action]:
        out: list[Action] = []
        t = self.tuning
        if isinstance(inp, Die):
            c = self._ctr(inp)
            c.running = False
            c.stopped_since = c.stopped_since if c.stopped_since is not None else now
            c.last_die = now
            if inp.exit_code != 0:
                c.dies.append((now, inp.exit_code))
                c.dies = [d for d in c.dies if now - d[0] <= t.crash_window_s]
                if len(c.dies) >= t.crash_count:
                    self._raise(now, c.subject, "crash-loop",
                                f"{len(c.dies)} crashes in {_mins(t.crash_window_s)}, last exit code {inp.exit_code}",
                                out, c.cid)
        elif isinstance(inp, Stop):
            c = self._ctr(inp)
            c.running = False
            c.stopped_since = c.stopped_since if c.stopped_since is not None else now
        elif isinstance(inp, Start):
            c = self._ctr(inp)
            c.running, c.stopped_since = True, None
            self._clear_container_signals(now, c.cid, out, ("stopped",))
        elif isinstance(inp, Health):
            c = self._ctr(inp)
            c.health = "healthy" if inp.healthy else "unhealthy"
            if inp.healthy:
                self._clear_container_signals(now, c.cid, out, ("unhealthy",))
            else:
                self._raise(now, c.subject, "unhealthy", "the container's own health check is failing", out, c.cid)
        elif isinstance(inp, Oom):
            c = self._ctr(inp)
            c.last_die = now
            self._raise(now, c.subject, "oom", "the container ran out of memory and was killed", out, c.cid)
        elif isinstance(inp, Destroy):
            self._destroy(now, inp, out)
        elif isinstance(inp, Seen):
            self._seen(now, inp, out)
        elif isinstance(inp, ReconcileDone):
            for cid in [cid for cid in self.ctrs if cid not in inp.cids]:
                self._destroy(now, Destroy(cid, self.ctrs[cid].subject), out)
        elif isinstance(inp, GatusState):
            if inp.subject.startswith("app:") and not self._subject_has_container(inp.subject):
                return out  # a check for an app that no longer exists
            if inp.down:
                self._raise(now, inp.subject, "down", inp.detail or "the health check is failing", out)
            else:
                self._clear(now, inp.subject, "down", out)
        elif isinstance(inp, Request):
            dq = self.requests.setdefault(inp.app, deque())
            dq.append((inp.ts, inp.status >= 500))
        elif isinstance(inp, JobResult):
            subject = f"job:{inp.app}/{inp.job}"
            if inp.ok:
                self._clear(now, subject, "job-failed", out)
            elif self._app_degraded(inp.app):
                pass  # the app is down: its jobs fail because of that (one problem, one alert); a real job bug shows next run
            else:
                self._raise(now, subject, "job-failed", inp.detail or "the job exited with an error", out)
        elif isinstance(inp, Maintenance):
            if inp.until is None:
                self.maint.pop(inp.subject, None)
                self._release_maintenance(now, out)
            else:
                self.maint[inp.subject] = inp.until
        elif isinstance(inp, Restore):
            for i in inp.incidents:
                self.signals[i["subject"]] = {k: dict(v) for k, v in i["signals"].items()}
                self.open[i["subject"]] = _Open(i["opened_at"], i.get("last_notified_at") or i["opened_at"],
                                                i.get("reminders", 0))
        return out

    def _seen(self, now: float, ev: Seen, out: list[Action]) -> None:
        c = self._ctr(ev)
        c.running = ev.running
        if ev.running:
            c.stopped_since = None
            self._clear_signals_of_subject(now, c, ("stopped",), out)
        else:
            c.stopped_since = c.stopped_since if c.stopped_since is not None else now
        c.health = ev.health
        if ev.health == "healthy":
            self._clear_signals_of_subject(now, c, ("unhealthy",), out)
        elif ev.health == "unhealthy":
            self._raise(now, c.subject, "unhealthy", "the container's own health check is failing", out, c.cid)

    def _clear_signals_of_subject(self, now: float, c: _Ctr, names, out: list[Action]) -> None:
        """Reconcile after ops was down: the signal's container id may be unknown (loaded from the database),
        so clear by subject when no other container of the subject still has the problem."""
        for name in names:
            sig = self.signals.get(c.subject, {}).get(name)
            if sig and (sig.get("cid") in (None, c.cid) or sig["cid"] not in self.ctrs):
                self._clear(now, c.subject, name, out)

    def _destroy(self, now: float, ev: Destroy, out: list[Action]) -> None:
        c = self.ctrs.pop(ev.cid, None)
        subject = c.subject if c else ev.subject
        out.append(Action("forget", subject))
        sibling = self._subject_has_container(subject)
        # the container's own problems go with it; with a sibling (a Compose recreate) the app carries on and
        # its owner hears "resolved", otherwise (annex remove, extras off) the incident closes without a message
        self._clear_container_signals(now, ev.cid, out, quiet=not sibling)
        if sibling:
            return
        # nothing of this subject is left (annex remove, extras off): close its incidents without a message
        app = app_of(subject)
        gone = [s for s in list(self.signals) if s == subject or (app and subject.startswith("app:") and app_of(s) == app)]
        for s in gone:
            for name in list(self.signals.get(s, {})):
                self._clear(now, s, name, out, quiet=True)
        if app and subject.startswith("app:"):
            self.requests.pop(app, None)
            self.err_below.pop(app, None)

    def _release_maintenance(self, now: float, out: list[Action]) -> None:
        for subject in list(self.signals):
            if subject not in self.open and self.signals[subject] and not self._in_maintenance(subject, now):
                self._open(now, subject, out)

    # --- time --------------------------------------------------------------
    def tick(self, now: float) -> list[Action]:
        out: list[Action] = []
        t = self.tuning
        for c in list(self.ctrs.values()):
            if not c.running and not c.ignore_stopped and c.stopped_since is not None \
                    and now - c.stopped_since >= t.stopped_grace_s:
                self._raise(now, c.subject, "stopped", f"{c.name} has been stopped for {_mins(now - c.stopped_since)}",
                            out, c.cid)
            sigs = self.signals.get(c.subject, {})
            if c.running and any(s in sigs and sigs[s].get("cid") in (c.cid, None) for s in ("crash-loop", "oom")):
                if c.last_die is None:
                    c.last_die = now  # loaded from the database: start the quiet period now
                if now - c.last_die >= t.crash_stable_s:
                    self._clear_container_signals(now, c.cid, out, ("crash-loop", "oom"))
                    for name in ("crash-loop", "oom"):
                        if sigs.get(name, {}).get("cid") is None:
                            self._clear(now, c.subject, name, out)
        # also apps whose `errors` signal was restored from the database and have no requests in memory yet
        restored = {s[4:] for s, sig in self.signals.items() if s.startswith("app:") and "errors" in sig}
        for app in list(set(self.requests) | restored):
            self._error_rate(now, app, out)
        for subject, until in list(self.maint.items()):
            if until <= now:
                del self.maint[subject]
        self._release_maintenance(now, out)
        for subject, inc in self.open.items():
            if self._in_maintenance(subject, now):
                continue
            due = (now - inc.opened_at >= t.reminder_first_s) if inc.reminders == 0 \
                else (now - inc.last_notified >= t.reminder_repeat_s)
            if due:
                inc.reminders += 1
                inc.last_notified = now
                out.append(Action("remind", subject, signals=_snapshot(self.signals.get(subject, {})),
                                  duration_s=now - inc.opened_at, opened_at=inc.opened_at))
        return out

    def _error_rate(self, now: float, app: str, out: list[Action]) -> None:
        t = self.tuning
        dq = self.requests.setdefault(app, deque())
        while dq and now - dq[0][0] > t.error_window_s:
            dq.popleft()
        total, errors = len(dq), sum(1 for _, e in dq if e)
        subject = f"app:{app}"
        over = errors >= t.error_min and total > 0 and errors / total >= t.error_ratio
        if over:
            self.err_below[app] = None
            self._raise(now, subject, "errors", f"{errors} of {total} requests in the last {_mins(t.error_window_s)} "
                        f"were server errors (5xx)", out)
        elif "errors" in self.signals.get(subject, {}):
            since = self.err_below.get(app)
            if since is None:
                self.err_below[app] = now
            elif now - since >= t.error_window_s:
                self.err_below[app] = None
                self._clear(now, subject, "errors", out)
        if not dq and "errors" not in self.signals.get(subject, {}):
            del self.requests[app]
            self.err_below.pop(app, None)


def _snapshot(sigs: dict) -> dict:
    return {k: dict(v) for k, v in sigs.items()}


def _mins(seconds: float) -> str:
    return f"{seconds / 60:.0f} min" if seconds >= 90 else f"{seconds:.0f} s"
