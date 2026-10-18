"""Authenticated HTTP JSON extension endpoint."""
from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import hmac
import json
import os
from pathlib import Path

from . import __version__
from .config import MailConfig
from .service import MailService, validate_context
from .store import MailStore


MAX_BODY = 14 * 1024 * 1024


class Handler(BaseHTTPRequestHandler):
    service: MailService

    def _json(self, status: int, value: object) -> None:
        body = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        if self.path != "/healthz":
            self.send_error(404)
            return
        self._json(200, {"status": "ok", "version": __version__})

    def do_POST(self) -> None:
        if self.path != "/internal/v1/tools/call" or not hmac.compare_digest(
            self.headers.get("Authorization", ""), "Bearer " + self.service.config.extension_bearer
        ):
            self.send_error(403)
            return
        try:
            size = int(self.headers.get("Content-Length", "0"))
            if not 1 <= size <= MAX_BODY:
                raise ValueError("body_invalid")
            payload = json.loads(self.rfile.read(size))
            required = {"version", "name", "arguments", "context"}
            if not isinstance(payload, dict) or not required <= set(payload) or set(payload) - required - {"attachments"} or payload["version"] != 1:
                raise ValueError("request_invalid")
            validate_context(payload["context"])
            if payload["name"] == "send":
                result = self.service.send(payload["arguments"], payload["context"], payload.get("attachments"))
            elif payload["name"] == "recent_status" and payload["arguments"] == {} and "attachments" not in payload:
                result = self.service.recent_status()
            else:
                raise ValueError("tool_invalid")
        except (ValueError, TypeError, KeyError, json.JSONDecodeError):
            self.send_error(400)
            return
        except Exception:
            self.send_error(503)
            return
        self._json(200, result)

    def log_message(self, *_args: object) -> None:
        pass


def main() -> None:
    config = MailConfig.load(Path(os.environ.get("CODEX_MAIL_OPTIONS_FILE", "/data/options.json")))
    store = MailStore(Path(os.environ.get("CODEX_MAIL_DATABASE", "/data/mail.sqlite3")))
    Handler.service = MailService(config, store)
    ThreadingHTTPServer(("0.0.0.0", 8129), Handler).serve_forever()


if __name__ == "__main__":
    main()
