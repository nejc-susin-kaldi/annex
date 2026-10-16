"""Logs the old way, with print(): plain text lines, no levels. Deploying it must succeed with a warning."""
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        print(f"got a request for {self.path}")
        self.send_response(200)
        self.send_header("Content-Length", "2")
        self.end_headers()
        self.wfile.write(b"ok")

    def log_message(self, *args):
        pass


port = int(os.environ.get("PORT", "8080"))
print(f"listening on port {port}")
ThreadingHTTPServer(("0.0.0.0", port), Handler).serve_forever()
