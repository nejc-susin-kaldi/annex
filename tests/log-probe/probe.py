"""Writes the logs a test asks for, in the Annex logging convention (one JSON object per line), plus a few
things an app must never do (log a secret) to prove they are removed before storing. Standard library only.

  GET /healthz                 200
  GET /emit?level=warn&msg=hi  one structured line with extra fields, secret-looking ones included, and the same
                               secret in a plain line, a bearer header and a traceback
  GET /burst?n=1000            n lines as fast as possible (the rate cap)
  GET /boom                    an unstructured traceback on stderr and a 500
  anything else                404 (so a changed health check path fails the health gate)
Every response has an X-Request-Id, and every line of the request carries it.
"""
import json
import os
import sys
import time
import traceback
import uuid
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

MARKER = os.environ.get("PROBE_MARKER", "boot-default")
SECRET = os.environ.get("PROBE_SECRET", "not-a-secret-just-a-default")


def log(level: str, msg: str, **fields) -> None:
    line = {"ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds"), "level": level, "msg": msg, **fields}
    print(json.dumps(line), flush=True)


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        url = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(url.query).items()}
        rid = uuid.uuid4().hex[:16]
        user = self.headers.get("Remote-User", "")
        if url.path == "/healthz":
            return self.reply(200, "ok", rid)
        if url.path == "/emit":
            log(q.get("level", "info"), q.get("msg", "emit"), request_id=rid, user=user, marker=MARKER, count=3,
                password=SECRET, token=SECRET, nested={"api_key": SECRET, "keep": "yes"}, headers={"Authorization": f"Bearer {SECRET}"})
            print(f"login failed for sam password={SECRET} Authorization: Bearer {SECRET}", flush=True)
            try:
                raise ValueError(f"emit asked for a traceback ({MARKER})")
            except ValueError:
                traceback.print_exc()
        elif url.path == "/burst":
            for i in range(int(q.get("n", "1000"))):
                log("info", "burst line", request_id=rid, i=i)
        elif url.path == "/boom":
            try:
                1 / 0
            except ZeroDivisionError:
                traceback.print_exc()
            return self.reply(500, "boom", rid)
        if url.path not in ("/emit", "/burst"):
            return self.reply(404, "not found", rid)
        log("info", "request", request_id=rid, user=user, method="GET", path=url.path, status=200)
        self.reply(200, "done", rid)

    def reply(self, status: int, body: str, rid: str) -> None:
        self.send_response(status)
        self.send_header("X-Request-Id", rid)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body.encode())

    def log_message(self, *args):
        pass


port = int(os.environ.get("PORT", "8080"))
log("info", "started", port=port, marker=MARKER)
ThreadingHTTPServer(("0.0.0.0", port), Handler).serve_forever()
