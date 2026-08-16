"""Minimal internal HTTP API for the manager-domain shadow."""

from __future__ import annotations

import hmac
import json
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable
from urllib.parse import urlsplit

from .service import ShadowError


def create_server(
    host: str,
    port: int,
    *,
    api_token: str,
    max_request_bytes: int,
    restart_shadow_handler: Callable[[Any], dict[str, Any]],
    allowlist_count: int,
) -> ThreadingHTTPServer:
    class Handler(BaseHTTPRequestHandler):
        server_version = "HAManagerExecutor/0.1.1"

        def log_message(self, _format: str, *_args: Any) -> None:
            return None

        def do_GET(self) -> None:  # noqa: N802
            if urlsplit(self.path).path == "/healthz":
                self._json(
                    HTTPStatus.OK,
                    {
                        "status": "ok",
                        "version": 1,
                        "mode": "shadow",
                        "write_enabled": False,
                        "allowlist_count": allowlist_count,
                    },
                )
                return
            self._not_found()

        def do_POST(self) -> None:  # noqa: N802
            if urlsplit(self.path).path != "/internal/v1/shadow/restart-addon":
                self._not_found()
                return
            if not self._bearer_authorized():
                return
            payload = self._read_json()
            if payload is None:
                return
            try:
                self._json(HTTPStatus.OK, restart_shadow_handler(payload))
            except ShadowError as exc:
                status = {
                    "target_not_allowlisted": HTTPStatus.FORBIDDEN,
                    "baseline_drift": HTTPStatus.CONFLICT,
                    "supervisor_http_error": HTTPStatus.BAD_GATEWAY,
                    "supervisor_unavailable": HTTPStatus.SERVICE_UNAVAILABLE,
                    "supervisor_response_too_large": HTTPStatus.BAD_GATEWAY,
                    "supervisor_invalid_json": HTTPStatus.BAD_GATEWAY,
                    "supervisor_rejected": HTTPStatus.BAD_GATEWAY,
                }.get(exc.code, HTTPStatus.BAD_REQUEST)
                self._json(status, {"error": {"code": exc.code}})

        def _bearer_authorized(self) -> bool:
            expected = f"Bearer {api_token}"
            if not hmac.compare_digest(self.headers.get("Authorization", ""), expected):
                self._json(HTTPStatus.UNAUTHORIZED, {"error": {"code": "not_authorized"}})
                return False
            return True

        def _read_json(self) -> Any | None:
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                self._json(HTTPStatus.BAD_REQUEST, {"error": {"code": "invalid_length"}})
                return None
            if length <= 0 or length > max_request_bytes:
                self._json(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, {"error": {"code": "request_size_invalid"}})
                return None
            raw = self.rfile.read(length)
            try:
                return json.loads(raw)
            except (UnicodeDecodeError, json.JSONDecodeError):
                self._json(HTTPStatus.BAD_REQUEST, {"error": {"code": "invalid_json"}})
                return None

        def _not_found(self) -> None:
            self._json(HTTPStatus.NOT_FOUND, {"error": {"code": "not_found"}})

        def _json(self, status: HTTPStatus, payload: Any) -> None:
            body = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

    return ThreadingHTTPServer((host, port), Handler)
