"""Webhook test probe: records every event Annex forwards, one JSON line each, in /data/events.jsonl.

  POST /hooks/trello | /hooks/gh | /hooks/plain   checks the token with hook_event(), then appends
        {hook, event_id, attempt, body, remote_user, github_event, sig_header} and answers 200
  GET  /healthz                                    200

`remote_user` is the Remote-User header as the app saw it (empty: the front door's identity header can't be
spoofed on the hooks host); `sig_header` says whether any provider signature header was forwarded (it must not be).
"""
import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from annex_runtime import HookError, hook_event

EVENTS = Path(os.environ.get("DATA_DIR", "/data")) / "events.jsonl"
HOOK_PATHS = {"/hooks/trello", "/hooks/gh", "/hooks/plain"}
SIGNATURE_HEADERS = ("x-hub-signature-256", "stripe-signature", "x-slack-signature", "x-trello-webhook", "x-annex-hook-secret")


class Handler(BaseHTTPRequestHandler):
    def reply(self, status: int, body: bytes = b"ok") -> None:
        self.send_response(status)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        self.reply(200 if self.path == "/healthz" else 404)

    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        if self.path not in HOOK_PATHS:
            return self.reply(404)
        try:
            ev = hook_event(self.headers)
        except HookError as e:
            return self.reply(e.status, str(e).encode())
        with EVENTS.open("a") as f:
            f.write(json.dumps({"hook": ev.hook, "event_id": ev.event_id, "attempt": ev.attempt,
                                "body": body.decode(errors="replace"), "remote_user": self.headers.get("Remote-User") or "",
                                "github_event": self.headers.get("X-GitHub-Event") or "",
                                "sig_header": any(self.headers.get(h) for h in SIGNATURE_HEADERS)}) + "\n")
        self.reply(200)

    def log_message(self, *args):
        pass


if __name__ == "__main__":
    ThreadingHTTPServer(("0.0.0.0", int(os.environ.get("PORT", "8080"))), Handler).serve_forever()
