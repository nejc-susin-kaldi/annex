"""Alert messages, sent off the engine thread from a table that survives a restart (hardening B5, D90).

The engine thread decides that a message is owed (an incident opened, reminded or resolved) and only adds a row to
`annex_ops.outbox`: what the engine said, in wall time. This thread does the slow parts: it reads the app's owner from
its container's labels, the user directory and the last log lines, composes the message once (stored in the row, so a
retry sends the same text to the same people), and sends it: one email to everyone, one post per Slack channel.

Each delivery (the email, each channel) has its own state in the row, saved the moment it is sent, so a retry or an
ops restart never sends a delivered part twice. A delivery that fails for a reason that can pass (the mail server or
Slack unreachable, a rate limit) is tried again after `send_backoff_s` (10 s, then 60 s); after `send_attempts` (3)
attempts the row is given up: logged, and shown on the console's Platform page. A refusal (an address outside the
allowed domains) is final for that delivery; a refused group email is split into one per address first, so the others
still hear (D39). Messages of one subject go out in order: a "resolved" waits while its "opened" is being retried.

What decides is pure (`due`, `retryable`, `compose`, `attempt`, `outcome`) and unit-tested with fakes; `OutboxStore` is
the table, `Transport` the real email and Slack calls, `Sender` the thread.
"""
import re
import sys
import threading
import time
import traceback

from psycopg.types.json import Jsonb

from . import config, db, directory, docker_api, email, messages, slack, threads
from .errors import OpsError

OFELIA = "annex-ofelia-1"
ANSI = re.compile(r"\x1b\[[0-9;]*m")
POLL_S = 5            # the engine wakes the thread when it adds a row; this is for retries
KEEP_DAYS = 30        # finished rows are kept this long (the console shows the failed ones of the last 7 days)
DEFAULT_BACKOFF = (10.0, 60.0)
RETRY_STATUSES = (429, 502, 503)  # rate limited, the mail server or Slack unreachable, not configured right now


def log(msg: str) -> None:
    print(f"sender: {msg}", file=sys.stderr, flush=True)


# --- pure --------------------------------------------------------------------------------------------------
def parse_backoff(text: str) -> list[float]:
    """"10,60" -> [10.0, 60.0]; anything unusable falls back to the defaults."""
    try:
        out = [float(x) for x in str(text).split(",") if x.strip()]
    except ValueError:
        out = []
    return out if out and all(x > 0 for x in out) else list(DEFAULT_BACKOFF)


def retry_delay(attempts: int, backoff: list[float]) -> float:
    """Seconds to wait after the `attempts`-th failed attempt (1-based); the last step repeats."""
    return backoff[min(max(attempts, 1) - 1, len(backoff) - 1)]


def due(pending: list[dict], now: float, retry_at: dict) -> list[dict]:
    """The rows to work on now: the oldest pending row of each subject, if its retry time (`retry_at`: id -> monotonic
    time, kept by the thread) has come. A row with no retry time (new, or pending from before a restart) is due."""
    oldest: dict[str, dict] = {}
    for r in sorted(pending, key=lambda r: r["id"]):
        oldest.setdefault(r["subject"], r)
    return [r for r in oldest.values() if retry_at.get(r["id"], 0) <= now]


def retryable(e: Exception) -> bool:
    """Can a later attempt succeed? Yes for a delivery problem or a limit; no for a refusal (a bad address)."""
    return not isinstance(e, OpsError) or e.status in RETRY_STATUSES


def why(e: Exception) -> str:
    return (e.message if isinstance(e, OpsError) else f"{type(e).__name__}: {e}")[:300]


def compose(kind: str, subject: str, action: dict, info: dict, users: dict, slack_connected: bool, alert_channel: str,
            status_url: str, app_url: str, log_lines: list[str]) -> dict:
    """The message for one outbox row: {title, data (the email template's), slack_text, deliveries}. `action` is what the
    engine said, in wall time: {signals, opened_at, duration_s}. `info` is the app's owner and alerts-to labels."""
    k, name, _ = messages.parse_subject(subject)
    signals = action.get("signals") or {}
    emails, channels, notes = messages.recipients(k, info.get("owner"), info.get("alerts_to"), users, slack_connected,
                                                  alert_channel)
    title = messages.subject_line(kind, subject, signals, action.get("duration_s", 0))
    text = messages.body(kind, subject, signals, action.get("opened_at", 0), action.get("duration_s", 0), notes,
                         status_url, app_url)
    data = {"title": title.removeprefix("[Annex] "), "body": text, "link": app_url or status_url or None,
            "link_text": "Open the app" if app_url else "Open the status page"}
    if kind != "resolve" and log_lines:
        data["details"] = "Last log lines:\n" + "\n".join(log_lines)
    deliveries = ([{"via": "email", "to": emails, "status": "pending"}] if emails else []) + \
                 [{"via": "slack", "to": c, "status": "pending"} for c in channels]
    return {"title": title, "data": {k2: v for k2, v in data.items() if v},
            "slack_text": title + (f" {status_url}" if status_url else ""), "deliveries": deliveries}


def attempt(msg: dict, transport, save) -> None:
    """Send every delivery of `msg` that is still pending, saving the message after each one (`save()`), so a crash
    between two deliveries can't repeat the first. A delivery ends `sent`, stays `pending` with its error (try again),
    ends `refused`, or (a refused email to several people) `split` into one pending delivery per address, which are
    tried in the same pass."""
    ds = msg["deliveries"]
    i = 0
    while i < len(ds):
        d = ds[i]
        i += 1
        if d["status"] != "pending":
            continue
        try:
            if d["via"] == "email":
                transport.email(d["to"], msg["title"], msg["data"])
            else:
                transport.slack(d["to"], msg["slack_text"])
            d["status"] = "sent"
            d.pop("error", None)
        except Exception as e:  # noqa: BLE001: every failure is recorded on the delivery
            d["error"] = why(e)
            if retryable(e):
                pass
            elif d["via"] == "email" and len(d["to"]) > 1:
                d["status"] = "split"
                ds.extend({"via": "email", "to": [addr], "status": "pending"} for addr in d["to"])
            else:
                d["status"] = "refused"
        save()


def outcome(msg: dict, attempts: int, max_attempts: int) -> tuple[str, str | None]:
    """(status, last error) of a row after its `attempts`-th attempt: sent (every delivery went out), pending (try
    again), or failed (given up after max_attempts, a delivery was refused, or there was nobody to send it to)."""
    ds = msg["deliveries"]
    if not ds:
        return "failed", "nobody to send it to: no admin in the user directory has an email address, and Slack isn't connected"
    errors = [f'{d["via"]} {d["to"] if isinstance(d["to"], str) else ", ".join(d["to"])}: {d["error"]}'
              for d in ds if d["status"] in ("pending", "refused") and d.get("error")]
    if any(d["status"] == "pending" for d in ds):
        if attempts >= max_attempts:
            return "failed", f"gave up after {attempts} attempts; " + "; ".join(errors)
        return "pending", "; ".join(errors) or None
    if any(d["status"] == "refused" for d in ds):
        return "failed", "; ".join(errors)
    return "sent", None


# --- the table ---------------------------------------------------------------------------------------------
class OutboxStore:
    def add(self, kind: str, subject: str, action: dict) -> int:
        with db.connect() as conn:
            return conn.execute("INSERT INTO annex_ops.outbox (kind, subject, action) VALUES (%s, %s, %s) RETURNING id",
                                (kind, subject, Jsonb(action))).fetchone()[0]

    def pending(self) -> list[dict]:
        with db.connect() as conn:
            rows = conn.execute("SELECT id, kind, subject, action, message, attempts FROM annex_ops.outbox "
                                "WHERE status = 'pending' ORDER BY id LIMIT 500").fetchall()
        return [{"id": r[0], "kind": r[1], "subject": r[2], "action": r[3], "message": r[4], "attempts": r[5]} for r in rows]

    def save_message(self, id_: int, message: dict) -> None:
        with db.connect() as conn:
            conn.execute("UPDATE annex_ops.outbox SET message = %s WHERE id = %s", (Jsonb(message), id_))

    def finish(self, id_: int, status: str, attempts: int, retry_in: float | None, error: str | None) -> None:
        with db.connect() as conn:
            conn.execute("UPDATE annex_ops.outbox SET status = %s, attempts = %s, last_error = %s, "
                         "next_attempt_at = CASE WHEN %s::float8 IS NULL THEN NULL ELSE now() + make_interval(secs => %s::float8) END, "
                         "finished_at = CASE WHEN %s = 'pending' THEN NULL ELSE now() END WHERE id = %s",
                         (status, attempts, error and error[:1000], retry_in, retry_in, status, id_))

    def housekeeping(self) -> None:
        with db.connect() as conn:
            conn.execute("DELETE FROM annex_ops.outbox WHERE status <> 'pending' AND created_at < now() - make_interval(days => %s)",
                         (KEEP_DAYS,))


class Transport:
    """The real sends, as M1.3 made them: through ops's own email and Slack code (validated, rate-limited, logged in the
    send log as app `_platform`). Raise OpsError on failure."""

    def email(self, to: list[str], title: str, data: dict) -> None:
        email.send_email("_platform", to, title, template="notice", data=data)

    def slack(self, channel: str, text: str) -> None:
        slack.post_slack("_platform", channel, text)


# --- the parts that read Docker and the directory ------------------------------------------------------------
def tail(subject: str) -> list[str]:
    """The last log lines of the subject's container (none for subjects without one)."""
    kind, name, job = messages.parse_subject(subject)
    if kind == "app":
        return docker_api.tail_logs(f"annex-app-{name}", 20)
    if kind == "job":
        key = f"{name}-{job}"
        return [ANSI.sub("", ln) for ln in docker_api.tail_logs(OFELIA, 400) if f'"{key}"' in ln][-20:]
    if name in ("disk", "memory", "logs", "front-door"):  # no container of their own
        return []
    return docker_api.tail_logs(f"annex-{name}-1", 20)


class Sender(threading.Thread):
    def __init__(self, store: OutboxStore | None = None, transport=None) -> None:
        super().__init__(daemon=True, name="alert-sender")
        self.store = store or OutboxStore()
        self.transport = transport or Transport()
        self.wakeup = threading.Event()
        self.retry_at: dict[int, float] = {}   # row id -> monotonic time of its next attempt (a restart retries at once)
        self.apps: dict[str, dict] = {}        # app -> owner, alerts-to, as last read (the container may be gone later)
        self.sent = self.failed = self.waiting = 0
        self.last_housekeeping = 0.0
        self.last_error_log = -60.0

    def wake(self) -> None:
        """A row was added: look now instead of at the next poll."""
        self.wakeup.set()

    def status(self) -> dict:
        return {"waiting": self.waiting, "sent": self.sent, "failed": self.failed}

    def run(self) -> None:
        threads.register(self.name, 90)  # one message: a log tail, an email and Slack posts (10 s timeouts each)
        while True:
            self.wakeup.wait(POLL_S)
            self.wakeup.clear()
            threads.beat(self.name)
            try:
                self.cycle(time.monotonic())
            except OpsError as e:  # the database is down: the rows wait in it
                if time.monotonic() - self.last_error_log > 60:
                    self.last_error_log = time.monotonic()
                    log(f"can't read the outbox ({e.message}); retrying")
            except Exception:  # noqa: BLE001: the sender must never die
                log("error:\n" + traceback.format_exc(limit=5))

    def cycle(self, now: float) -> None:
        t = config.tuning()
        backoff, max_attempts = parse_backoff(t.send_backoff_s), max(1, t.send_attempts)
        pending = self.store.pending()
        self.waiting = len(pending)
        self.retry_at = {i: at for i, at in self.retry_at.items() if any(r["id"] == i for r in pending)}
        for row in due(pending, now, self.retry_at):
            threads.beat(self.name)
            try:
                self.process(row, now, backoff, max_attempts)
            except Exception as e:  # noqa: BLE001: e.g. the database went away mid-row; its sent parts are saved
                self.retry_at[row["id"]] = now + backoff[0]
                log(f"{row['kind']} message for {row['subject']}: {type(e).__name__}: {e}; trying again in {backoff[0]:.0f} s")
        if now - self.last_housekeeping >= 86400 or not self.last_housekeeping:
            self.last_housekeeping = now
            self.store.housekeeping()

    def process(self, row: dict, now: float, backoff: list[float], max_attempts: int) -> str:
        msg = row["message"]
        if msg is None:
            msg = self.compose(row)
            self.store.save_message(row["id"], msg)
        attempt(msg, self.transport, lambda: self.store.save_message(row["id"], msg))
        attempts = row["attempts"] + 1
        status, error = outcome(msg, attempts, max_attempts)
        retry_in = retry_delay(attempts, backoff) if status == "pending" else None
        self.store.finish(row["id"], status, attempts, retry_in, error)
        if status == "pending":
            self.retry_at[row["id"]] = now + retry_in
            log(f"{row['kind']} message for {row['subject']}: attempt {attempts} failed, next in {retry_in:.0f} s: {error}")
        else:
            self.retry_at.pop(row["id"], None)
            if status == "sent":
                self.sent += 1
            else:
                self.failed += 1
                log(f"{row['kind']} message for {row['subject']} was not delivered: {error}")
        return status

    def compose(self, row: dict) -> dict:
        kind, subject, action = row["kind"], row["subject"], row["action"] or {}
        k, name, _ = messages.parse_subject(subject)
        info = self.app_info(name) if k in ("app", "job") else {}
        try:
            users = directory.users()
        except OpsError:
            users = {}
        lines: list[str] = []
        if kind != "resolve":
            try:
                lines = tail(subject)
            except (docker_api.DockerError, OSError):
                lines = []  # the message goes out without them
        return compose(kind, subject, action, info, users, config.slack_connected(), config.ALERT_SLACK_CHANNEL,
                       config.STATUS_URL, config.app_url(name) if k in ("app", "job") else "", lines)

    def app_info(self, app: str) -> dict:
        try:
            labels = docker_api.inspect(f"annex-app-{app}").get("Config", {}).get("Labels") or {}
            self.apps[app] = {"owner": labels.get("com.kaldi.annex.owner"), "alerts_to": labels.get("com.kaldi.annex.alerts-to")}
        except (docker_api.DockerError, OSError):
            pass  # the container is gone or the proxy is down: use what we last knew
        return self.apps.get(app, {})
