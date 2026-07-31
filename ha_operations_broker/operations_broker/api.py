"""Internal-only authenticated HTTP API for the read-only broker canary."""

from __future__ import annotations

import hmac
import json
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable


def create_server(
    host: str,
    port: int,
    *,
    api_token: str,
    max_request_bytes: int,
    preflight_handler: Callable[[Any], dict[str, Any]],
) -> ThreadingHTTPServer:
    class Handler(BaseHTTPRequestHandler):
        server_version = "HAOperationsBroker/0.1"

        def log_message(self, _format: str, *_args: Any) -> None:
            return None

        def do_GET(self) -> None:  # noqa: N802
            if self.path != "/healthz":
                self._json(HTTPStatus.NOT_FOUND, {"error": {"code": "not_found"}})
                return
            self._json(
                HTTPStatus.OK,
                {"status": "ok", "version": 1, "execution_enabled": False},
            )

        def do_POST(self) -> None:  # noqa: N802
            if self.path != "/v1/preflight":
                self._json(HTTPStatus.NOT_FOUND, {"error": {"code": "not_found"}})
                return
            authorization = self.headers.get("Authorization", "")
            expected = f"Bearer {api_token}"
            if not hmac.compare_digest(authorization, expected):
                self._json(
                    HTTPStatus.UNAUTHORIZED,
                    {"error": {"code": "not_authorized"}},
                )
                return
            content_type = self.headers.get_content_type()
            if content_type != "application/json":
                self._json(
                    HTTPStatus.UNSUPPORTED_MEDIA_TYPE,
                    {"error": {"code": "content_type_required"}},
                )
                return
            raw_length = self.headers.get("Content-Length", "")
            try:
                content_length = int(raw_length)
            except ValueError:
                content_length = -1
            if content_length < 1 or content_length > max_request_bytes:
                self._json(
                    HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
                    {"error": {"code": "request_size_invalid"}},
                )
                return
            raw = self.rfile.read(content_length)
            try:
                payload = json.loads(raw)
            except (UnicodeDecodeError, json.JSONDecodeError):
                self._json(
                    HTTPStatus.BAD_REQUEST,
                    {"error": {"code": "invalid_json"}},
                )
                return
            self._json(HTTPStatus.OK, preflight_handler(payload))

        def _json(self, status: HTTPStatus, payload: dict[str, Any]) -> None:
            body = json.dumps(
                payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            ).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(body)

    return ThreadingHTTPServer((host, port), Handler)
