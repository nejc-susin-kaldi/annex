"""Tiny server for static Annex apps. Keep this file as is; put your site in public/.

  GET /healthz             200 when ready
  GET /api/me              the signed-in user, from the front door's headers
  GET /api/data/<view>?... a gateway view (only views listed in annex.yaml), PostgREST filters pass through
  GET /anything-else       files from public/ (index.html for /)
"""
import json
import os
import re
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

PUBLIC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "public")
VIEW = re.compile(r"^/api/data/([a-z_][a-z0-9_]*)$")


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=PUBLIC, **kwargs)

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
            return self.send_json(502, json.dumps({"error": str(e.reason)}).encode())

    def log_message(self, fmt, *args):
        print(f"{self.address_string()} {fmt % args}", flush=True)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8080"))
    print(f"serving {PUBLIC} on :{port}", flush=True)
    ThreadingHTTPServer(("0.0.0.0", port), Handler).serve_forever()
