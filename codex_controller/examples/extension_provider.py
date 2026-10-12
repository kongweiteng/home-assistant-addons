"""Independent read-only provider example; bearer injected into this process only."""
import hmac
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import os


class Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        token = os.environ["EXTENSION_BEARER"]
        if self.path != "/internal/v1/tools/call" or not hmac.compare_digest(self.headers.get("Authorization", ""), "Bearer " + token):
            self.send_error(403); return
        try:
            size = int(self.headers.get("Content-Length", "0"))
            if not 1 <= size <= 131072:
                raise ValueError()
            payload = json.loads(self.rfile.read(size))
            if set(payload) != {"version", "name", "arguments", "context"} or payload["version"] != 1 or payload["name"] != "status" or payload["arguments"] != {}:
                raise ValueError()
        except (ValueError, TypeError):
            self.send_error(400); return
        body = b'{"status":"available"}'
        self.send_response(200); self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)

    def log_message(self, *args):
        pass  # Do not log authentication headers or request/response bodies.


if __name__ == "__main__":
    if len(os.environ.get("EXTENSION_BEARER", "")) < 32:
        raise SystemExit("missing provider credential")
    HTTPServer(("0.0.0.0", 8129), Handler).serve_forever()
