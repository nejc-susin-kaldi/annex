"""Tiny server for static Annex apps. Keep this file as is; put your site in public/.

  GET /healthz             200 when ready
  GET /api/me              the signed-in user, from the front door's headers
  GET /api/data/<view>?... a gateway view (only views listed in annex.yaml), PostgREST filters pass through
  POST /api/_errors        browser error reports from annex-errors.js (logged as one error line)
  GET /anything-else       files from public/ (index.html for /)
"""
import json
import os
import re
import secrets
import time
from datetime import datetime
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

PUBLIC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "public")
VIEW = re.compile(r"^/api/data/([a-z_][a-z0-9_]*)$")
REQUEST_ID = re.compile(r"^[A-Za-z0-9._-]{8,64}$")
LEVELS = {"debug": 10, "info": 20, "warn": 30, "error": 40}
THRESHOLD = LEVELS.get(os.environ.get("LOG_LEVEL", "info").strip().lower().replace("warning", "warn"), 20)


def log(level: str, msg: str, **fields) -> None:
    """One JSON object per line on stdout, as annex_runtime.log does (`annex contract logging`)."""
    if LEVELS[level] >= THRESHOLD:
        line = {"ts": datetime.now().astimezone().isoformat(timespec="milliseconds"), "level": level, "msg": msg}
        line.update({(k + "_" if k in ("ts", "level", "msg") else k): v for k, v in fields.items()})
        print(json.dumps(line, default=str), flush=True)


ERRORS_PER_MINUTE = 10
error_hits: dict[str, list[float]] = {}


def browser_error(login: str, request_id: str, body: bytes) -> int:
    """Log one error line for a browser error report (as annex_runtime.report_browser_error does); returns the status:
    204 logged, 400 not a report, 413 too big, 429 too many from this user."""
    if len(body) > 8192:
        return 413
    try:
        data = json.loads(body)
    except ValueError:
        return 400
    if not isinstance(data, dict):
        return 400
    now = time.monotonic()
    hits = [t for t in error_hits.get(login or "anonymous", []) if now - t < 60]
    if len(hits) >= ERRORS_PER_MINUTE:
        error_hits[login or "anonymous"] = hits
        return 429
    error_hits[login or "anonymous"] = hits + [now]
    if len(error_hits) > 1000:
        error_hits.clear()
    log("error", f"browser error: {str(data.get('msg') or 'unknown error')[:500]}", request_id=request_id, user=login,
        source="browser", url=str(data.get("url") or "")[:200], stack=str(data.get("stack") or "")[:1500])
    return 204


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=PUBLIC, **kwargs)

    def handle_one_request(self):
        self.t0 = time.monotonic()
        self.request_id = ""
        super().handle_one_request()

    def parse_request(self):
        ok = super().parse_request()
        incoming = self.headers.get("X-Request-Id", "") if ok else ""
        self.request_id = incoming if REQUEST_ID.match(incoming) else secrets.token_hex(8)
        self.user = self.headers.get("Remote-User", "") if ok else ""
        return ok

    def end_headers(self):
        self.send_header("X-Request-Id", self.request_id)  # one id on the response, the access log and the lines
        super().end_headers()

    def log_request(self, code="-", size="-"):
        path = self.path.partition("?")[0]
        log("debug" if path == "/healthz" else "info", "request", request_id=self.request_id, user=self.user,
            method=self.command, path=path, status=int(code) if str(code).isdigit() else 0,
            ms=round((time.monotonic() - self.t0) * 1000))

    def send_json(self, status: int, body: bytes) -> None:
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path, _, query = self.path.partition("?")
        if path == "/healthz":
            return self.send_json(200, b'{"ok": true}')
        if path == "/api/me":
            login = self.headers.get("Remote-User", "")
            me = {"login": login, "name": self.headers.get("Remote-Name") or login,
                  "email": self.headers.get("Remote-Email", ""),
                  "groups": [g for g in self.headers.get("Remote-Groups", "").split(",") if g]}
            return self.send_json(200, json.dumps(me).encode())
        m = VIEW.match(path)
        if m:
            return self.proxy(m.group(1), query)
        if path.startswith("/api/"):
            return self.send_json(404, b'{"error": "unknown API path"}')
        return super().do_GET()

    def do_POST(self):
        if self.path.partition("?")[0] != "/api/_errors":
            return self.send_json(404, b'{"error": "unknown API path"}')
        n = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(min(n, 9000))
        status = browser_error(self.user, self.request_id, body)
        self.send_response(status)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def proxy(self, view: str, query: str) -> None:
        base = os.environ.get("ANNEX_GATEWAY_URL")
        if not base:
            return self.send_json(403, b'{"error": "no gateway access: add views under data.views in annex.yaml"}')
        url = f"{base.rstrip('/')}/{view}" + (f"?{query}" if query else "")
        req = Request(url, headers={"Authorization": f"Bearer {os.environ['ANNEX_GATEWAY_TOKEN']}",
                                    "Accept": "application/json"})
        try:
            with urlopen(req, timeout=15) as r:
                return self.send_json(r.status, r.read())
        except HTTPError as e:
            return self.send_json(e.code, e.read())
        except URLError as e:
            log("warn", "gateway read failed", request_id=self.request_id, user=self.user, view=view, error=str(e.reason))
            return self.send_json(502, json.dumps({"error": str(e.reason)}).encode())

    def log_error(self, fmt, *args):
        log("warn", fmt % args, request_id=getattr(self, "request_id", ""))

    def log_message(self, fmt, *args):
        pass  # request lines come from log_request, errors from log_error


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8080"))
    log("info", "started", port=port, public=PUBLIC)
    ThreadingHTTPServer(("0.0.0.0", port), Handler).serve_forever()
