"""annex-selftest: Annex's own test app. It uses every feature the platform offers, on demand, so that the test
scripts (and anyone diagnosing a problem) can drive them. It is not a pretend business app. Standard library plus
annex_runtime.py (and psycopg for its database).

  python main.py                  the web server
  python main.py job <name>       a job: tick | work | notify  (what annex.yaml schedules)

Web endpoints (all GET unless said; every response has an X-Request-Id, every log line of the request carries it):
  /healthz                200; 503 while /data/unhealthy exists
  /                       a page: the signed-in user, which gateway views are allowed or denied, the browser error script
  /whoami                 identity headers as JSON
  /gateway/<view>         a gateway read: {"view", "rows"} or {"error", "status"}
  /db                     a round trip through the app's Postgres: inserts a row, answers the count
  /files                  a round trip through /data
  /emit?level=&msg=       one structured line with fields that look secret (password, token, nested api_key), the same
                          secret in a plain line and in a printed traceback: for "secrets never reach the stored logs"
  /burst?n=               n log lines as fast as possible (the rate cap)
  /raise?q=boomN|oops|kindN   an unhandled exception (one kind per q: structured error with exc and stack, 500)
  /boom                   a printed Python traceback on stderr, and a 500
  /fail500                a plain 500 (error-rate alerts), nothing logged but the access line
  /notify/email?to=&subject=&text=     an email through Annex, answers {"status", ...} even when refused
  /notify/slack?channel=&text=         a Slack post through Annex, same
  /env                    the names (never the values) of the environment Annex injected
  POST /hooks/trello | gh | plain      a webhook forwarded by Annex: checks the token, records it in /data/events.jsonl
  POST /api/_errors, GET /annex-errors.js    browser error reports (the runtime's)
  Every POST goes through the runtime's csrf_violation first: a cross-site Origin or Sec-Fetch-Site gets 403 (ANNEX_CSRF=off
  under env: turns it off); any other POST path answers 404, which tests use as "passed the check".

Switches (files in /data, so a test can flip them with `docker exec`):
  /data/crashes   a number N > 0: the server writes N-1 and exits 1, once a second until it reaches 0 (crash loop)
  /data/failjob   while it exists, the `tick` job exits 1
  /data/unhealthy while it exists, /healthz answers 503
Environment (annex.yaml `env:`, set by the variants a test builds): SELFTEST_LOG=plain logs with print() (the
"logs aren't structured" warning), SELFTEST_MARKER (in /emit and the started line), SELFTEST_SECRET (the fake secret
/emit logs), SELFTEST_BAD_MIGRATION=1 (drops the run history at start, for the rollback --with-data test),
SELFTEST_GREETING (the page's "Hello", to tell two versions apart), SELFTEST_RECIPIENT and SELFTEST_SLACK_CHANNEL (the
notify job), SELFTEST_SLOW_HEALTH=N (/healthz answers 503 for the first N seconds: a deploy that sits in its health
gate, for the killed-deploy test).
"""
import json
import os
import sys
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import annex_runtime as rt
from annex_runtime import GatewayError, HookError, NotifyError, current_user, gateway, hook_event, log, request_context

DATA = Path(os.environ.get("DATA_DIR", "/data"))
PLAIN = os.environ.get("SELFTEST_LOG") == "plain"
MARKER = os.environ.get("SELFTEST_MARKER", "boot-default")
SECRET = os.environ.get("SELFTEST_SECRET", "not-a-secret-just-a-default")
SLOW_UNTIL = time.monotonic() + float(os.environ.get("SELFTEST_SLOW_HEALTH") or 0)
GRANTED = ["client_summary", "top_clients_by_aum"]
UNGRANTED = "advisor_book"
HOOK_PATHS = {"/hooks/trello", "/hooks/gh", "/hooks/plain"}
SIGNATURE_HEADERS = ("x-hub-signature-256", "stripe-signature", "x-slack-signature", "x-trello-webhook", "x-annex-hook-secret")
INJECTED = ("PORT", "ANNEX_APP_NAME", "ANNEX_BASE_URL", "LOG_LEVEL", "DATA_DIR", "DATABASE_URL", "ANNEX_GATEWAY_URL",
            "ANNEX_GATEWAY_TOKEN", "ANNEX_OPS_URL", "ANNEX_OPS_TOKEN", "ANNEX_HOOKS_URL", "ANNEX_HOOK_TOKEN")


def say(level: str, msg: str, **fields) -> None:
    """One log line in the Annex convention, or (SELFTEST_LOG=plain) the old way with print()."""
    if PLAIN:
        print(msg + ("".join(f" {k}={v}" for k, v in fields.items())), flush=True)
    else:
        getattr(log, level)(msg, **fields)


# --- the database ---------------------------------------------------------------------------------------------------
def db():
    import psycopg
    conn = psycopg.connect(os.environ["DATABASE_URL"], connect_timeout=5)
    conn.execute("CREATE TABLE IF NOT EXISTS selftest_runs (id serial PRIMARY KEY, ran_at timestamptz NOT NULL DEFAULT now(), note text)")
    conn.commit()
    return conn


def init_db() -> None:
    if not os.environ.get("DATABASE_URL"):
        return
    import psycopg
    with psycopg.connect(os.environ["DATABASE_URL"], connect_timeout=5) as conn:
        if os.environ.get("SELFTEST_BAD_MIGRATION") == "1":  # a version with a destructive migration
            conn.execute("DROP TABLE IF EXISTS selftest_runs")
    db().close()


def record_run(note: str) -> int:
    with db() as conn:
        conn.execute("INSERT INTO selftest_runs (note) VALUES (%s)", (note,))
        return conn.execute("SELECT count(*) FROM selftest_runs").fetchone()[0]


# --- jobs --------------------------------------------------------------------------------------------------------------
def job_tick() -> int:
    if (DATA / "failjob").exists():
        print("tick: failing on purpose (/data/failjob exists)", file=sys.stderr, flush=True)
        return 1
    say("info", "tick ok")
    return 0


def job_work() -> int:
    t0 = time.monotonic()
    say("info", "job started", job="work")
    rows = len(gateway("top_clients_by_aum", limit=10))
    runs = record_run("work") if os.environ.get("DATABASE_URL") else 0
    (DATA / "runs").mkdir(parents=True, exist_ok=True)
    f = DATA / "runs" / f"run-{int(time.time())}.txt"
    f.write_text(f"{rows} rows\n")
    say("info", "job finished", job="work", rows=rows, runs=runs, file=f.name, ms=round((time.monotonic() - t0) * 1000))
    return 0


def job_notify() -> int:
    recipients = [r.strip() for r in os.environ.get("SELFTEST_RECIPIENT", "maja").split(",") if r.strip()]
    channel = os.environ.get("SELFTEST_SLACK_CHANNEL", "#annex-selftest")
    say("info", "job started", job="notify")
    try:
        sent = rt.send_email(recipients, "annex-selftest: notification check", template="notice", data={
            "title": "annex-selftest: notification check", "body": "This is the Annex self-test app. Nobody needs to do anything."})
        say("info", "notify emailed", to=recipients, count=len(sent["to"]))  # logins, not addresses
    except NotifyError as e:
        say("error", "email failed", status=e.status, error=str(e))
        return 1
    try:
        rt.post_slack(channel, "annex-selftest: notification check")
        say("info", "notify posted to Slack", channel=channel)
    except NotifyError as e:
        say("warn", "slack failed", status=e.status, error=str(e))
    return 0


# --- the crash switch ----------------------------------------------------------------------------------------------------
def crash_watch() -> None:
    while True:
        try:
            n = int((DATA / "crashes").read_text().strip() or 0)
        except (OSError, ValueError):
            n = 0
        if n > 0:
            (DATA / "crashes").write_text(str(n - 1))
            print(f"selftest: crashing on purpose ({n - 1} more)", file=sys.stderr, flush=True)
            os._exit(1)
        time.sleep(1)


# --- the server ----------------------------------------------------------------------------------------------------------
def page(user, checks: list[str]) -> str:
    items = "".join(f"<li>{c}</li>" for c in checks)
    greeting = os.environ.get("SELFTEST_GREETING", "Hello")
    return (f'<!doctype html><html><head><meta charset="utf-8"><title>annex-selftest</title></head><body>'
            f"<h1>{greeting}, {user.name}</h1><p>Signed in as <b>{user.login}</b></p><ul>{items}</ul>"
            '<script src="/annex-errors.js"></script></body></html>')


def view_state(view: str) -> str:
    try:
        gateway(view, limit=1)
        return f"{view} : allowed"
    except GatewayError as e:
        return f"{view} : denied ({e.status})"


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"

    def reply(self, status: int, body: bytes | str = b"ok", ctype: str = "text/plain; charset=utf-8") -> None:
        data = body.encode() if isinstance(body, str) else body
        self.status = status
        self.send_response(status)
        self.send_header("X-Request-Id", self.rid)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(data)

    def json(self, status: int, obj) -> None:
        self.reply(status, json.dumps(obj), "application/json")

    def handle_request(self) -> None:
        t0 = time.monotonic()
        self.status = 0
        with request_context(self.headers) as rid:
            self.rid = rid
            url = urlparse(self.path)
            self.q = {k: v[0] for k, v in parse_qs(url.query).items()}
            try:
                why = rt.csrf_violation(self.command, self.headers)  # the contract's "Cross-site requests"
                if why:
                    log.warn("cross-site request refused", method=self.command, path=url.path, why=why)
                    self.reply(403, "Cross-site request refused")
                else:
                    self.route(url.path)
            except Exception as e:  # noqa: BLE001: an unhandled exception is one error line with its stack, and a 500
                if PLAIN:
                    traceback.print_exc()
                else:
                    log.exception("unhandled error", method=self.command, path=url.path)
                if not self.status:
                    self.reply(500, "Internal Server Error")
            if PLAIN:
                print(f"got a request for {url.path}", flush=True)
            else:
                (log.debug if url.path == "/healthz" else log.info)("request", method=self.command, path=url.path,
                                                                    status=self.status, ms=round((time.monotonic() - t0) * 1000))

    do_GET = do_POST = handle_request

    def route(self, path: str) -> None:
        if self.command == "POST":
            return self.route_post(path)
        if path == "/healthz":
            return self.reply(503 if (DATA / "unhealthy").exists() or time.monotonic() < SLOW_UNTIL else 200, "ok")
        if path == "/annex-errors.js":
            return self.reply(200, rt.browser_errors_js(), "application/javascript")
        handler = {"/": self.handle_page, "/whoami": self.handle_whoami, "/db": self.handle_db, "/files": self.handle_files,
                   "/emit": self.handle_emit, "/burst": self.handle_burst, "/raise": self.handle_raise, "/boom": self.handle_boom,
                   "/fail500": lambda: self.reply(500, "fail"), "/notify/email": self.handle_email,
                   "/notify/slack": self.handle_slack, "/env": self.handle_env}.get(path)
        if handler:
            return handler()
        if path.startswith("/gateway/"):
            return self.handle_gateway(path[len("/gateway/"):])
        self.reply(404, "not found")

    def route_post(self, path: str) -> None:
        body = self.rfile.read(min(int(self.headers.get("Content-Length") or 0), 1 << 20))
        if path == "/api/_errors":
            return self.reply(rt.report_browser_error(self.headers, body), b"")
        if path not in HOOK_PATHS:
            return self.reply(404, "not found")
        try:
            ev = hook_event(self.headers)
        except HookError as e:
            return self.reply(e.status, str(e))
        with (DATA / "events.jsonl").open("a") as f:
            f.write(json.dumps({"hook": ev.hook, "event_id": ev.event_id, "attempt": ev.attempt, "body": body.decode(errors="replace"),
                                "remote_user": self.headers.get("Remote-User") or "", "github_event": self.headers.get("X-GitHub-Event") or "",
                                "sig_header": any(self.headers.get(h) for h in SIGNATURE_HEADERS)}) + "\n")
        self.reply(200, "ok")

    # --- endpoints ---
    def handle_page(self) -> None:
        user = current_user(self.headers)
        self.reply(200, page(user, [view_state(v) for v in GRANTED] + [view_state(UNGRANTED)]), "text/html; charset=utf-8")

    def handle_whoami(self) -> None:
        u = current_user(self.headers)
        self.json(200, {"login": u.login, "name": u.name, "email": u.email, "groups": u.groups, "request_id": self.rid})

    def handle_gateway(self, view: str) -> None:
        try:
            self.json(200, {"view": view, "rows": len(gateway(view, limit=5))})
        except GatewayError as e:
            self.json(e.status or 502, {"view": view, "error": str(e), "status": e.status})

    def handle_db(self) -> None:
        if not os.environ.get("DATABASE_URL"):
            return self.json(404, {"error": "no database: storage.postgres is off"})
        self.json(200, {"runs": record_run("db round trip")})

    def handle_files(self) -> None:
        DATA.mkdir(parents=True, exist_ok=True)
        f = DATA / "files-marker.txt"
        f.write_text(self.q.get("text", "hello"))
        self.json(200, {"wrote": f.name, "read": f.read_text()})

    def handle_emit(self) -> None:
        level = self.q.get("level", "info")
        msg = self.q.get("msg", "emit")
        user = current_user(self.headers).login
        if PLAIN:
            print(f"{level}: {msg} marker={MARKER} password={SECRET}", flush=True)
        else:
            getattr(log, "warn" if level == "warning" else level if level in ("debug", "info", "warn", "error") else "info")(
                msg, marker=MARKER, count=3, password=SECRET, token=SECRET, nested={"api_key": SECRET, "keep": "yes"},
                headers={"Authorization": f"Bearer {SECRET}"}, who=user)
        print(f"login failed for sam password={SECRET} Authorization: Bearer {SECRET}", flush=True)
        try:
            raise ValueError(f"emit asked for a traceback ({MARKER})")
        except ValueError:
            traceback.print_exc()
        self.reply(200, "done")

    def handle_burst(self) -> None:
        for i in range(int(self.q.get("n", "1000"))):
            say("info", "burst line", i=i)
        self.reply(200, "done")

    def handle_raise(self) -> None:
        q = self.q.get("q", "boom")
        if q.startswith("oops"):
            {}["missing key"]
        if q.startswith("kind"):
            raise type(f"Failure{q[4:]}Error", (Exception,), {})("one of many different kinds")
        raise RuntimeError(f"cannot load client {q[4:]} from view client_summary")

    def handle_boom(self) -> None:
        try:
            1 / 0
        except ZeroDivisionError:
            traceback.print_exc()
        self.reply(500, "boom")

    def handle_email(self) -> None:
        to = [t for t in self.q.get("to", os.environ.get("SELFTEST_RECIPIENT", "maja")).split(",") if t]
        try:
            r = rt.send_email(to, self.q.get("subject", "annex-selftest"), text=self.q.get("text", "hello from annex-selftest"))
            self.json(200, {"status": 200, "to": r["to"]})
        except NotifyError as e:
            self.json(200, {"status": e.status, "message": str(e)})

    def handle_slack(self) -> None:
        try:
            r = rt.post_slack(self.q.get("channel", "#annex-selftest"), self.q.get("text", "hello from annex-selftest"))
            self.json(200, {"status": 200, "channel": r["channel"]})
        except NotifyError as e:
            self.json(200, {"status": e.status, "message": str(e)})

    def handle_env(self) -> None:
        self.json(200, {"injected": [n for n in INJECTED if n in os.environ], "custom": sorted(k for k in os.environ if k.startswith("SELFTEST_"))})

    def log_message(self, *args) -> None:
        pass


if __name__ == "__main__":
    if sys.argv[1:2] == ["job"]:
        sys.exit({"tick": job_tick, "work": job_work, "notify": job_notify}[sys.argv[2]]())
    port = int(os.environ.get("PORT", "8080"))
    init_db()
    threading.Thread(target=crash_watch, daemon=True).start()
    say("info", "started", port=port, marker=MARKER)
    ThreadingHTTPServer(("0.0.0.0", port), Handler).serve_forever()
