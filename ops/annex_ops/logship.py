"""Log shipping (M2.2): every shipped container's lines go into annex_ops.logs, so they outlive the container.

What is shipped (Nejc, 16 Oct): every app container, annex-ops, the webhook receiver, the gateway's request
lines (from product-db's log), Authelia's sign-in lines, and scheduled jobs' output (jobs run inside the app's
container through Ofelia, so the output is in Ofelia's log). Not shipped: the two socket proxies, Traefik (its
access log already feeds usage) and the Docker-level health checks.

Everything that decides something is a pure function with a unit test: `parse_app_line` (JSON or plain text),
`scrub_value` / `scrub_text` (secrets never reach the table), `parse_authelia_line`, `parse_gateway_line`,
`RateCap`. The rest follows containers (follow_logs.Follower in exact mode, D42), inserts in batches
(logstore.Sink, like usage's ingest, D46) and keeps the table small (Keeper).
"""
import json
import math
import queue
import re
import sys
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import md5
from typing import Callable

from . import config, db, docker_api, engine, follow_logs, logstore, threads, watch_docker
from .errors import OpsError

LEVELS = ("debug", "info", "warn", "error")
LEVEL_ALIASES = {"debug": "debug", "trace": "debug", "info": "info", "notice": "info", "warn": "warn",
                 "warning": "warn", "error": "error", "err": "error", "critical": "error", "fatal": "error"}
MAX_MSG = 8192
MAX_FIELDS = 8192      # serialised; a line with more keeps only its key names
MAX_STRING = 2000      # one string value in `fields`
MAX_STACK = 6000       # the `stack` field: a Python traceback keeps its end (where it was raised), others their start
RESERVED = {"ts", "level", "msg", "request_id"}  # moved to columns (or dropped: ts is Docker's); `user` stays in fields
REQUEST_ID = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
RECENT = 2000          # hashes remembered per container, so a replay doesn't even reach the database
SHIP_PREFIX = "ship:"


@dataclass
class LogSettings:
    log_retention_days: int = 14
    log_max_mb: int = 2048          # the size guard: over this, the oldest day goes early (and admins hear)
    log_rate_per_s: int = 200       # per container, counted in log time: a busier second is cut and reported
    log_housekeeping_s: float = 3600


def log(msg: str) -> None:
    print(f"logship: {msg}", file=sys.stderr, flush=True)


# --- cleaning and secrets (pure) ----------------------------------------------------------
ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
SECRET_KEY = re.compile(r"passw(?:or)?d|passwd|token|secret|authorization|cookie|api[-_ ]?key|credential|private[-_ ]?key", re.I)
SECRET_TEXT = re.compile(r"""\b(passw(?:or)?d|passwd|token|secret|authorization|api[-_]?key|cookie|credential)s?"""
                         r"""(["']?\s*[:=]\s*["']?)(?:(?:Bearer|Basic)\s+)?[^\s,;&"'}\]]+""", re.I)
BEARER = re.compile(r"\b(Bearer|Basic)\s+[A-Za-z0-9._~+/=-]{8,}", re.I)
JWT = re.compile(r"\beyJ[\w-]{8,}\.[\w-]{8,}\.[\w-]{4,}")
URL_PASSWORD = re.compile(r"(://[^:/\s@]+:)[^@\s/]+@")


def clean(text: str) -> str:
    """No NUL (Postgres refuses it), no terminal colour codes, and no lone surrogates (a JSON string can carry one)."""
    return ANSI.sub("", text.replace("\x00", "")).encode("utf-8", "replace").decode("utf-8")


def scrub_text(text: str) -> str:
    """Best effort for free text: `password=...`, `Authorization: Bearer ...`, JWTs and passwords in URLs. The rule
    stands that apps never log secrets; this is the net under it."""
    text = SECRET_TEXT.sub(lambda m: f"{m[1]}{m[2]}[redacted]", text)
    text = BEARER.sub(lambda m: f"{m[1]} [redacted]", text)
    text = JWT.sub("[redacted-jwt]", text)
    return URL_PASSWORD.sub(lambda m: f"{m[1]}[redacted]@", text)


def truncate(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:limit - 14] + " …[truncated]"


def scrub_value(v, depth: int = 0):
    """A JSON value for `fields`: keys that look secret are dropped at every depth, strings are cleaned and capped,
    and numbers Postgres' jsonb can't hold (NaN, infinity) become text."""
    if isinstance(v, dict):
        if depth >= 6:
            return "…"
        return {clean(str(k))[:100]: scrub_value(x, depth + 1) for k, x in list(v.items())[:100] if not SECRET_KEY.search(str(k))}
    if isinstance(v, list):
        return "…" if depth >= 6 else [scrub_value(x, depth + 1) for x in v[:50]]
    if isinstance(v, str):
        return truncate(scrub_text(clean(v)), MAX_STRING)
    if isinstance(v, float) and not math.isfinite(v):
        return str(v)
    return v


def clip_stack(text: str) -> str:
    """A stack that is too long: Python prints the innermost frame last, Node first, and that is the frame that
    matters for grouping errors, so a Python traceback keeps its end and anything else its start."""
    if len(text) <= MAX_STACK:
        return text
    if text.lstrip().startswith("Traceback"):
        tail = text[-MAX_STACK:]
        return "…\n" + tail[tail.find("\n") + 1:] if "\n" in tail else tail
    return text[:MAX_STACK - 14] + " …[truncated]"


def bound_fields(fields: dict) -> dict:
    if len(json.dumps(fields, default=str)) <= MAX_FIELDS:
        return fields
    return {"_truncated": f"the fields were over {MAX_FIELDS // 1024} KB and were left out", "keys": sorted(fields)[:30]}


def normalise_level(value) -> str | None:
    return LEVEL_ALIASES.get(str(value).strip().lower()) if isinstance(value, str) else None


ERROR_LINE = re.compile(r"^Traceback \(most recent call last\)|^\s+File \".*\", line \d+|^\s+at .+\(.+:\d+:\d+\)$|"
                        r"\bException\b|^[\w.]*(?:Error|Exception)\b|\b(?:ERROR|CRITICAL|FATAL)\b")


def guess_level(text: str) -> str | None:
    """For lines that aren't JSON: error for tracebacks and lines that say ERROR or Exception, else nothing."""
    return "error" if ERROR_LINE.search(text) else None


# --- parsing (pure) ------------------------------------------------------------------------
@dataclass
class Parsed:
    level: str | None
    msg: str
    request_id: str | None = None
    fields: dict | None = None   # None: the line isn't structured (see the Logging section of the contract)
    app: str | None = None       # set by parsers that know it (the gateway's role)


def parse_app_line(raw: str) -> Parsed | None:
    """A line as the convention wants it (a JSON object with `level` and a string `msg`: the rest becomes `fields`,
    with secrets removed), or any other text as it is, with a guessed level. None for an empty line."""
    text = clean(raw).rstrip()   # not lstrip: the indent of a traceback line is part of it
    if not text.strip():
        return None
    if text.lstrip().startswith("{"):
        try:
            obj = json.loads(text)
        except ValueError:
            obj = None
        if isinstance(obj, dict) and obj.get("level") in LEVELS and isinstance(obj.get("msg"), str):
            rid = obj.get("request_id")
            fields = {k: v for k, v in obj.items() if k not in RESERVED}
            stack = fields.pop("stack", None)
            fields = scrub_value(fields)
            if isinstance(stack, str):
                fields["stack"] = clip_stack(scrub_text(clean(stack)))
            return Parsed(obj["level"], truncate(scrub_text(clean(obj["msg"])), MAX_MSG),
                          rid if isinstance(rid, str) and REQUEST_ID.match(rid) else None, bound_fields(fields))
    return Parsed(guess_level(text), truncate(scrub_text(text), MAX_MSG))


HEALTHZ = re.compile(r'"GET /healthz HTTP/1\.[01]" 200')


def parse_platform_line(raw: str) -> Parsed | None:
    """annex-ops and the receiver: their uvicorn access lines for the 15-second health checks are 11,000 lines a
    day of nothing and are dropped; everything else is kept."""
    return None if HEALTHZ.search(raw) else parse_app_line(raw)


LOGFMT = re.compile(r'(\w+)=("(?:[^"\\]|\\.)*"|\S*)')
SIGNIN = re.compile(r"authentication|1FA|2FA|identity verification|password|reset|forbidden to user|details for user|TOTP|"
                    r"WebAuthn|session|sign", re.I)
AUTHELIA_NOISE = re.compile(r"<anonymous>|Reloaded successfully|Watching file for changes")
AUTHELIA_DEBUG_KEPT = re.compile(r"^Successful (?:1FA|2FA) authentication attempt")  # the only debug lines wanted: successful sign-ins


def parse_logfmt(line: str) -> dict[str, str]:
    out = {}
    for k, v in LOGFMT.findall(line):
        out[k] = v[1:-1].replace('\\"', '"').replace("\\\\", "\\") if v.startswith('"') else v
    return out


def parse_authelia_line(raw: str) -> Parsed | None:
    """Authelia's sign-in lines: successful and failed attempts, unknown users, access denied to a known user,
    password resets, and every warning or error. Not the 'not authorized to user <anonymous>' line the health checker's front-door check
    causes every minute, nor the user file reloads."""
    kv = parse_logfmt(clean(raw))
    msg = kv.get("msg")
    if not msg or AUTHELIA_NOISE.search(msg):
        return None
    level = normalise_level(kv.get("level"))
    if level == "debug":  # Authelia runs at debug to log successful sign-ins (M2.5); everything else at that level is noise
        if not AUTHELIA_DEBUG_KEPT.match(msg):
            return None
    elif level not in ("warn", "error") and not SIGNIN.search(msg):
        return None
    fields = {k: v for k, v in kv.items() if k not in ("time", "level", "msg")}
    return Parsed(level, truncate(scrub_text(msg), MAX_MSG), None, bound_fields(scrub_value(fields)))


GATEWAY = re.compile(r"\bLOG:\s+annex-gateway role=(?P<role>\S+) method=(?P<method>\S+) path=(?P<path>\S*)")


def parse_gateway_line(raw: str) -> Parsed | None:
    """product-db's `RAISE LOG` line for one gateway request (D10): the app's role, the method and the path (no
    query string). The CONTEXT and STATEMENT lines Postgres adds are not kept."""
    m = GATEWAY.search(raw)
    if not m or not m["role"].startswith("app_"):
        return None
    path = m["path"][:200]
    view = path.lstrip("/").split("/", 1)[0]
    return Parsed("info", f"{m['method']} {path}", None, {"method": m["method"], "path": path, "view": view},
                  m["role"][4:].replace("_", "-"))


# --- rows, limits (pure) ------------------------------------------------------------------------
def make_row(ns: int, stream: str, raw: str, parsed: Parsed, container: str, app: str | None, release: int | None,
             hash_key: str) -> logstore.Row:
    ts = datetime.fromtimestamp(ns // 10**9, timezone.utc).replace(microsecond=(ns % 10**9) // 1000)
    h = md5(f"{hash_key}\0{stream}\0{ns}\0{raw}".encode("utf-8", "replace")).hexdigest()
    return logstore.Row(ts, ns % 1000, parsed.app or app, container, release, stream, parsed.level, parsed.request_id,
                        parsed.msg, parsed.fields, h)


def usable(ns: int, now_ns: int, retention_days: int) -> bool:
    """A line inside the retention window and not from the future (a partition for it must be one we'd keep)."""
    return now_ns - retention_days * 86400 * 10**9 <= ns <= now_ns + 2 * 86400 * 10**9


class RateCap:
    """At most `limit` lines per second of log time. Lines over it are counted, and `rolled`/`pending` hand back
    (second, how many) once, so one line can say how many were dropped. Log time, not the clock, so reading
    history gives the same answer as reading live."""

    def __init__(self, limit: int):
        self.limit, self.sec, self.n, self.dropped = limit, None, 0, 0

    def admit(self, ns: int) -> tuple[bool, tuple[int, int] | None]:
        sec, report = ns // 10**9, None
        if self.sec is None or sec > self.sec:
            if self.dropped:
                report = (self.sec, self.dropped)
            self.sec, self.n, self.dropped = sec, 0, 0
        self.n += 1
        if self.n > self.limit:
            self.dropped += 1
            return False, report
        return True, report

    def pending(self, now_sec: int) -> tuple[int, int] | None:
        """A finished second's drops that no later line has reported (the flood stopped)."""
        if self.dropped and self.sec is not None and self.sec < now_sec - 1:
            out, self.dropped = (self.sec, self.dropped), 0
            return out
        return None


# --- what to ship, and how -----------------------------------------------------------------------
@dataclass(frozen=True)
class Spec:
    kind: str
    parse: Callable[[str], Parsed | None]
    container: str | None = None   # a fixed value for the `container` column ('gateway'); else the container's own name


SPECS = {"app": Spec("app", parse_app_line), "platform": Spec("platform", parse_platform_line),
         "authelia": Spec("authelia", parse_authelia_line), "gateway": Spec("gateway", parse_gateway_line, "gateway")}
PLATFORM_SPECS = {"platform:ops": "platform", "platform:hooks": "platform", "platform:authelia": "authelia",
                  "platform:product-db": "gateway"}


def spec_for(subject: str) -> Spec | None:
    """The shipping rule of a watcher subject (app:<name>, platform:<service>), or None: not shipped."""
    if subject.startswith("app:"):
        return SPECS["app"]
    kind = PLATFORM_SPECS.get(subject)
    return SPECS[kind] if kind else None


def ship_key(name: str, cid: str) -> str:
    """The cursor's key: per container id, so a recreated container (same name) has its own cursor and the old
    one's last lines are still read to the end."""
    return f"{SHIP_PREFIX}{name}:{cid[:12]}"


class Releases:
    """app -> the release its running container carries (a label), for lines that don't say (gateway reads, job output)."""

    def __init__(self) -> None:
        self.by_app: dict[str, int] = {}
        self.loaded = 0.0

    def set(self, app: str, release: int | None) -> None:
        if release is not None:
            self.by_app[app] = release

    def get(self, app: str) -> int | None:
        if app not in self.by_app and time.time() - self.loaded > 30:
            self.loaded = time.time()
            try:
                for c in docker_api.containers():
                    labels = c.get("Labels") or {}
                    if labels.get("com.kaldi.annex.app") and labels.get("com.kaldi.annex.release", "").isdigit():
                        self.by_app[labels["com.kaldi.annex.app"]] = int(labels["com.kaldi.annex.release"])
            except (docker_api.DockerError, OSError):
                pass
        return self.by_app.get(app)


class Recent:
    """The last hashes seen: a follower replays a little after a restart, and this keeps that out of the database."""

    def __init__(self, size: int = RECENT):
        self.size, self.seen = size, OrderedDict()

    def add(self, h: str) -> bool:
        """True if it's new."""
        if h in self.seen:
            return False
        self.seen[h] = None
        if len(self.seen) > self.size:
            self.seen.popitem(last=False)
        return True


class ContainerHandler:
    """The line handler of one container's follower: parse, drop what isn't wanted, cap, buffer."""

    def __init__(self, spec: Spec, name: str, app: str | None, release: int | None, sink: logstore.Sink,
                 releases: Releases | None = None):
        self.spec, self.name, self.app, self.release, self.sink, self.releases = spec, name, app, release, sink, releases
        self.cap = RateCap(config.log_settings().log_rate_per_s)
        self.recent = Recent()

    def __call__(self, ns: int, stream: str, raw: str) -> None:
        s = config.log_settings()
        if not usable(ns, time.time_ns(), s.log_retention_days):
            return
        parsed = self.spec.parse(raw)
        if parsed is None:
            return
        app = parsed.app or self.app
        release = self.release if self.release is not None else (self.releases.get(app) if self.releases and app else None)
        row = make_row(ns, stream, raw, parsed, self.spec.container or self.name, app, release, self.name)
        if not self.recent.add(row.h):
            return
        self.cap.limit = s.log_rate_per_s
        ok, report = self.cap.admit(ns)
        if report:
            self.sink.add(dropped_row(*report, self.cap.limit, self.spec.container or self.name, app, release, self.name))
        if ok:
            self.sink.add(row)

    def flush_drops(self) -> None:
        report = self.cap.pending(int(time.time()))
        if report:
            self.sink.add(dropped_row(*report, self.cap.limit, self.spec.container or self.name, self.app, self.release, self.name))


def dropped_row(sec: int, n: int, limit: int, container: str, app: str | None, release: int | None, hash_key: str) -> logstore.Row:
    parsed = Parsed("warn", f"annex-ops dropped {n} log line(s) of this container: it wrote more than {limit} lines "
                    f"in one second", None, {"dropped": n, "limit_per_s": limit})
    return make_row(sec * 10**9 + 999_999_999, "stderr", f"dropped {sec} {n}", parsed, container, app, release, hash_key)


# --- scheduled jobs: their output is in Ofelia's log ----------------------------------------------
OFELIA_LINE = re.compile(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d+)?(?:Z|[+-]\d\d:\d\d) ")
JOB_OUT = re.compile(r'\[Job "(?P<key>[^"]+)" \((?P<id>[0-9a-f]+)\)\] (?P<kind>StdOut|StdErr): ?(?P<rest>.*)$')


class JobOutput:
    """Ofelia logs a job's output as `StdOut: <first line>` and then the other lines bare, until its next own line.
    The lines are stored under the app (container `job:<job>`), with the app's current release."""

    def __init__(self, sink: logstore.Sink, releases: Releases):
        self.sink, self.releases = sink, releases
        self.jobs: dict[str, tuple[str, str]] = {}
        self.refreshed = 0.0
        self.current: tuple[str, str, str] | None = None   # (app, job, StdOut|StdErr) of the block we're in
        self.caps: dict[str, RateCap] = {}
        self.recent = Recent()

    def lookup(self, key: str) -> tuple[str, str] | None:
        if key not in self.jobs and time.time() - self.refreshed > 30:
            self.refreshed = time.time()
            try:
                self.jobs = follow_logs.job_map(docker_api.containers())
            except (docker_api.DockerError, OSError):
                pass
        return self.jobs.get(key)

    def __call__(self, ns: int, stream: str, raw: str) -> None:
        line = ANSI.sub("", raw)
        if OFELIA_LINE.match(line):
            m = JOB_OUT.search(line)
            target = self.lookup(m["key"]) if m else None
            self.current = (*target, m["kind"]) if m and target else None
            if not self.current:
                return
            text = m["rest"]
        elif self.current is None:
            return  # output of a block whose first line we haven't seen (a restart in the middle of a job)
        else:
            text = line
        s = config.log_settings()
        if not usable(ns, time.time_ns(), s.log_retention_days):
            return
        app, job, kind = self.current
        parsed = parse_app_line(text)
        if parsed is None:
            return
        row = make_row(ns, "stderr" if kind == "StdErr" else "stdout", raw, parsed, f"job:{job}", app,
                       self.releases.get(app), "ofelia")
        if not self.recent.add(row.h):
            return
        cap = self.caps.setdefault(app, RateCap(s.log_rate_per_s))
        cap.limit = s.log_rate_per_s
        ok, report = cap.admit(ns)
        if report:
            self.sink.add(dropped_row(*report, cap.limit, f"job:{job}", app, self.releases.get(app), "ofelia"))
        if ok:
            self.sink.add(row)


# --- following containers ---------------------------------------------------------------------------
def started_ns(info: dict, retention_days: int) -> int:
    """Where to start reading a container with no cursor: when it started, but not before the retention window."""
    state = info.get("State") or {}
    ns = docker_api.parse_ts_ns(state.get("StartedAt", "")) or docker_api.parse_ts_ns(info.get("Created", "")) or 0
    return max(ns, time.time_ns() - retention_days * 86400 * 10**9)


def release_of(labels: dict) -> int | None:
    value = labels.get("com.kaldi.annex.release", "")
    return int(value) if value.isdigit() else None


class Shipper(threading.Thread):
    """One follower per shipped container, started when the watcher sees or starts it and stopped when it is destroyed."""

    def __init__(self, sink: logstore.Sink, releases: Releases, store: follow_logs.CursorStore | None = None):
        super().__init__(daemon=True, name="log-shipper")
        self.sink, self.releases = sink, releases
        self.store = store or follow_logs.CursorStore()
        self.q: queue.Queue = queue.Queue()
        self.followers: dict[str, follow_logs.Follower] = {}
        self.handlers: dict[str, ContainerHandler] = {}
        self.inputs: dict[str, object] = {}  # cid -> the Seen/Start that started its follower (to start another)
        self.last_revive = 0.0

    def status(self) -> dict:
        """Followers and the inputs waiting for the shipper (/healthz)."""
        return {"followers": len(self.followers), "waiting": self.q.qsize()}

    def revive(self) -> None:
        """A follower that ended without being stopped (it shouldn't: Follower.run catches everything) is started again
        from its saved cursor. Checked every few seconds; a failed start is tried again at the next check."""
        if time.monotonic() - self.last_revive < 5:
            return
        self.last_revive = time.monotonic()
        for cid, f in list(self.followers.items()):
            if not f.is_alive() and not f.stopping.is_set() and cid in self.inputs:
                log(f"follower {f.key} had ended; starting it again from its cursor")
                self.start_for(self.inputs[cid])

    def submit(self, inp) -> None:
        """The watcher's inputs (called from its thread: never blocks). Only containers coming and going matter."""
        if isinstance(inp, (engine.Seen, engine.Start)) and spec_for(inp.subject):
            self.q.put(inp)
        elif isinstance(inp, engine.Destroy) and inp.cid in self.followers:
            self.q.put(inp)

    def run(self) -> None:
        last_drops = 0.0
        threads.register(self.name, 60)  # stop() waits up to 15 s for a follower
        while True:
            threads.beat(self.name)
            try:
                inp = self.q.get(timeout=1)
            except queue.Empty:
                inp = None
            try:
                self.revive()
                if isinstance(inp, engine.Destroy):
                    self.stop(inp.cid)
                elif inp is not None:
                    self.start_for(inp)
                if time.monotonic() - last_drops > 1:
                    last_drops = time.monotonic()
                    for h in list(self.handlers.values()):
                        h.flush_drops()
            except Exception as e:  # noqa: BLE001: the shipper must never die
                log(f"{type(e).__name__}: {e}")

    def start_for(self, inp) -> None:
        if inp.cid in self.followers:
            if self.followers[inp.cid].is_alive():
                self.followers[inp.cid].poke()  # restarted: ask again now
                return
            # it died (a bug, not a Docker hiccup): start another from the cursor, replacing it once that works
        try:
            info = docker_api.inspect(inp.cid)
        except (docker_api.DockerError, OSError):
            return  # gone again already
        labels = info.get("Config", {}).get("Labels") or {}
        app, release = labels.get("com.kaldi.annex.app"), release_of(labels)
        if app:
            self.releases.set(app, release)
        spec = spec_for(inp.subject)
        name = info.get("Name", "").lstrip("/") or inp.name
        handler = ContainerHandler(spec, name, app, release, self.sink, self.releases)
        f = follow_logs.Follower(inp.cid, handler, self.store, key=ship_key(name, inp.cid), exact=True,
                                 before_save=self.sink.flush,
                                 start_ns=started_ns(info, config.log_settings().log_retention_days))
        self.followers[inp.cid], self.handlers[inp.cid], self.inputs[inp.cid] = f, handler, inp
        f.start()

    def stop(self, cid: str) -> None:
        f = self.followers.pop(cid, None)
        self.handlers.pop(cid, None)
        self.inputs.pop(cid, None)
        if f is None:
            return
        f.stop()
        f.join(15)  # it reads to the end of the stream first
        self.sink.flush()
        try:
            self.store.delete(f.key)
        except OpsError:
            pass


# --- keeping the table small -----------------------------------------------------------------------------
class Keeper(threading.Thread):
    """Daily housekeeping: partitions ahead, whole old partitions dropped, and the size guard. Reports the
    table's size to the alert engine, which raises `platform:logs` when the guard had to drop a day early."""

    def __init__(self, submit, sink: logstore.Sink):
        super().__init__(daemon=True, name="log-keeper")
        self.submit, self.sink = submit, sink

    def run(self) -> None:
        last = 0.0
        threads.register(self.name, 120)  # a cycle can drop partitions
        while True:
            threads.beat(self.name)
            try:
                if time.time() - last >= config.log_settings().log_housekeeping_s:
                    last = time.time()
                    self.cycle(config.log_settings(), datetime.now(timezone.utc))
            except Exception as e:  # noqa: BLE001
                last = 0.0  # the database may still be starting: try again
                log(f"housekeeping failed: {type(e).__name__}: {e}")
            time.sleep(5)

    def cycle(self, s: LogSettings, now: datetime) -> dict:
        logstore.ensure_partitions(logstore.partition_days(now), self.sink.known)
        sizes = logstore.partition_sizes()
        dropped, trimmed = [], []
        for day in logstore.expired_days(list(sizes), now, s.log_retention_days):
            logstore.drop_partition(day)
            self.sink.known.discard(day)
            sizes.pop(day, None)
            dropped.append(str(day))
        for day in logstore.trim_plan(sizes, s.log_max_mb * 2**20):
            logstore.drop_partition(day)
            self.sink.known.discard(day)
            sizes.pop(day, None)
            trimmed.append(str(day))
        if trimmed:
            log(f"the log table passed {s.log_max_mb} MB: dropped {', '.join(trimmed)} early")
        with db.connect() as conn:  # cursors of containers that were destroyed while ops was down
            conn.execute("DELETE FROM annex_ops.log_cursors WHERE container LIKE %s AND ts < now() - make_interval(days => %s)",
                         (SHIP_PREFIX + "%", s.log_retention_days))
        total = sum(sizes.values()) / 2**20
        self.submit(engine.LogSample(total, s.log_max_mb, trimmed))
        return {"expired": dropped, "trimmed": trimmed, "total_mb": round(total, 1)}


# --- before a container is replaced -----------------------------------------------------------------------
def drain(container: str, timeout: float = 5.0) -> dict:
    """Read a container's log from the saved cursor to its end and store it now (`ctl logs-drain`, called by
    `annex deploy` before Docker replaces the container). Idempotent with the running follower: the same line
    has the same hash. It reads at most `timeout` seconds; the CLI carries on whatever happens here."""
    try:
        info = docker_api.inspect(container)
    except docker_api.DockerError as e:
        if e.status == 404:
            return {"container": container, "lines": 0, "stored": True, "note": "no such container"}
        raise OpsError(502, f"can't inspect {container}: {e}") from None
    labels = info.get("Config", {}).get("Labels") or {}
    if labels.get("com.kaldi.annex") != "true":
        raise OpsError(400, f"{container} is not an Annex container")
    cls = watch_docker.classify(labels)
    spec = spec_for(cls[0]) if cls else None
    if spec is None:
        return {"container": container, "lines": 0, "stored": True, "note": "its logs aren't shipped"}
    name, cid = info.get("Name", "").lstrip("/"), info["Id"]
    retention = config.log_settings().log_retention_days
    cursor = follow_logs.CursorStore().get(ship_key(name, cid))
    since = int(cursor * 1e9) - follow_logs.EXACT_MARGIN_NS if cursor else started_ns(info, retention)
    sink = logstore.Sink()
    handler = ContainerHandler(spec, name, labels.get("com.kaldi.annex.app"), release_of(labels), sink, Releases())
    deadline, lines, timed_out = time.monotonic() + timeout, 0, False
    try:
        for ns, stream, line in docker_api.stream_logs_ns(cid, since, follow=False, timeout=max(1.0, timeout)):
            handler(ns, stream, line)
            lines += 1
            if time.monotonic() > deadline:
                timed_out = True
                break
    except (docker_api.DockerError, OSError):
        timed_out = True
    handler.flush_drops()
    return {"container": container, "lines": lines, "stored": sink.flush(), "timed_out": timed_out}
