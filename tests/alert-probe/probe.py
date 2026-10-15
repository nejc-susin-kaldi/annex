"""Alert test probe: a web server that can be told to misbehave by files in /data.

  python probe.py          serve /healthz (200) and /boom (500)
  python probe.py check    the scheduled job: exits 1 while /data/failjob exists, else 0

Crash loop: if /data/crashes holds a number above 0, the server writes the number minus one and exits with
code 1, on start and then every second. So `echo 4 > /data/crashes` makes it restart 4 times, then recover.
(`kill 1` can't do this: PID 1 ignores signals it has no handler for.)
"""
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

DATA = Path(os.environ.get("DATA_DIR", "/data"))


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        status, body = (500, b"boom") if self.path.startswith("/boom") else (200, b"ok")
        self.send_response(status)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


def crash_watch() -> None:
    while True:
        try:
            n = int((DATA / "crashes").read_text().strip() or 0)
        except (OSError, ValueError):
            n = 0
        if n > 0:
            (DATA / "crashes").write_text(str(n - 1))
            print(f"probe: crashing on purpose ({n - 1} more)", file=sys.stderr, flush=True)
            os._exit(1)
        time.sleep(1)


def check() -> int:
    if (DATA / "failjob").exists():
        print("probe check: failing on purpose (/data/failjob exists)", file=sys.stderr)
        return 1
    print("probe check: ok")
    return 0


if __name__ == "__main__":
    if sys.argv[1:] == ["check"]:
        sys.exit(check())
    threading.Thread(target=crash_watch, daemon=True).start()
    ThreadingHTTPServer(("0.0.0.0", int(os.environ.get("PORT", "8080"))), Handler).serve_forever()
