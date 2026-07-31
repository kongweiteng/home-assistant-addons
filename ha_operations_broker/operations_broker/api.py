"""Authenticated internal API and HA Ingress Passkey approval UI."""

from __future__ import annotations

import hmac
import json
import re
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable
from urllib.parse import parse_qs, unquote, urlsplit

from .authorization import AuthorizationError, AuthorizationManager
from .ui import APP_CSS, APP_JS, INDEX_HTML


APPROVAL_PATH_RE = re.compile(r"^/api/approvals/([^/]+)/(begin|complete)$")
INTERNAL_STATUS_RE = re.compile(r"^/v1/authorization/requests/([^/]+)$")


def create_server(
    host: str,
    port: int,
    *,
    api_token: str,
    max_request_bytes: int,
    preflight_handler: Callable[[Any], dict[str, Any]],
    authorization_manager: AuthorizationManager | None = None,
    allowed_ingress_origins: frozenset[str] = frozenset(),
) -> ThreadingHTTPServer:
    class Handler(BaseHTTPRequestHandler):
        server_version = "HAOperationsBroker/0.2"

        def log_message(self, _format: str, *_args: Any) -> None:
            return None

        def do_GET(self) -> None:  # noqa: N802
            parsed = urlsplit(self.path)
            path = parsed.path
            if path == "/healthz":
                self._json(
                    HTTPStatus.OK,
                    {"status": "ok", "version": 2, "execution_enabled": False},
                )
                return
            if path in {"", "/", "/index.html"}:
                self._asset(HTTPStatus.OK, "text/html; charset=utf-8", INDEX_HTML)
                return
            if path == "/static/app.css":
                self._asset(HTTPStatus.OK, "text/css; charset=utf-8", APP_CSS)
                return
            if path == "/static/app.js":
                self._asset(
                    HTTPStatus.OK, "application/javascript; charset=utf-8", APP_JS
                )
                return
            if path == "/api/context":
                manager = self._manager()
                if manager is None:
                    return
                user_id = self._ingress_user()
                if user_id is None:
                    return
                approval_id = parse_qs(parsed.query).get("approval_id", [None])[0]
                self._authorization_call(
                    lambda: manager.ingress_context(
                        approval_id=approval_id, remote_user_id=user_id
                    )
                )
                return
            status_match = INTERNAL_STATUS_RE.fullmatch(path)
            if status_match:
                if not self._bearer_authorized():
                    return
                manager = self._manager()
                if manager is None:
                    return
                approval_id = unquote(status_match.group(1))
                self._authorization_call(lambda: manager.internal_status(approval_id))
                return
            self._not_found()

        def do_POST(self) -> None:  # noqa: N802
            path = urlsplit(self.path).path
            if path == "/v1/preflight":
                if not self._bearer_authorized():
                    return
                payload = self._read_json()
                if payload is None:
                    return
                self._json(HTTPStatus.OK, preflight_handler(payload))
                return
            if path == "/v1/authorization/requests":
                if not self._bearer_authorized():
                    return
                manager = self._manager()
                if manager is None:
                    return
                payload = self._read_json()
                if payload is None:
                    return
                self._authorization_call(lambda: manager.create_request(payload))
                return
            manager = self._manager()
            if manager is None:
                return
            user_id = self._ingress_user(require_origin=True)
            if user_id is None:
                return
            payload = self._read_json()
            if payload is None:
                return
            if path == "/api/passkeys/register/begin":
                self._authorization_call(
                    lambda: manager.begin_registration(
                        remote_user_id=user_id,
                        enrollment_token=payload.get("enrollment_token"),
                    )
                )
                return
            if path == "/api/passkeys/register/complete":
                self._authorization_call(
                    lambda: manager.complete_registration(
                        remote_user_id=user_id,
                        enrollment_token=payload.get("enrollment_token"),
                        flow_id=payload.get("flow_id"),
                        response=payload.get("response"),
                    )
                )
                return
            approval_match = APPROVAL_PATH_RE.fullmatch(path)
            if approval_match:
                approval_id = unquote(approval_match.group(1))
                operation = approval_match.group(2)
                if operation == "begin":
                    self._authorization_call(
                        lambda: manager.begin_authorization(
                            approval_id=approval_id, remote_user_id=user_id
                        )
                    )
                else:
                    self._authorization_call(
                        lambda: manager.complete_authorization(
                            approval_id=approval_id,
                            remote_user_id=user_id,
                            flow_id=payload.get("flow_id"),
                            response=payload.get("response"),
                        )
                    )
                return
            self._not_found()

        def _manager(self) -> AuthorizationManager | None:
            if authorization_manager is None:
                self._json(
                    HTTPStatus.NOT_FOUND,
                    {"error": {"code": "authorization_unavailable"}},
                )
                return None
            return authorization_manager

        def _bearer_authorized(self) -> bool:
            authorization = self.headers.get("Authorization", "")
            expected = f"Bearer {api_token}"
            if not hmac.compare_digest(authorization, expected):
                self._json(
                    HTTPStatus.UNAUTHORIZED,
                    {"error": {"code": "not_authorized"}},
                )
                return False
            return True

        def _ingress_user(self, *, require_origin: bool = False) -> str | None:
            user_id = self.headers.get("X-Remote-User-Id", "")
            if not user_id:
                self._json(
                    HTTPStatus.UNAUTHORIZED,
                    {"error": {"code": "ingress_user_required"}},
                )
                return None
            if require_origin:
                origin = self.headers.get("Origin", "")
                if origin not in allowed_ingress_origins:
                    self._json(
                        HTTPStatus.FORBIDDEN,
                        {"error": {"code": "ingress_origin_rejected"}},
                    )
                    return None
            return user_id

        def _read_json(self) -> dict[str, Any] | None:
            if self.headers.get_content_type() != "application/json":
                self._json(
                    HTTPStatus.UNSUPPORTED_MEDIA_TYPE,
                    {"error": {"code": "content_type_required"}},
                )
                return None
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
                return None
            raw = self.rfile.read(content_length)
            try:
                payload = json.loads(raw)
            except (UnicodeDecodeError, json.JSONDecodeError):
                self._json(
                    HTTPStatus.BAD_REQUEST,
                    {"error": {"code": "invalid_json"}},
                )
                return None
            if not isinstance(payload, dict):
                self._json(
                    HTTPStatus.BAD_REQUEST,
                    {"error": {"code": "json_object_required"}},
                )
                return None
            return payload

        def _authorization_call(self, callback: Callable[[], dict[str, Any]]) -> None:
            try:
                payload = callback()
            except AuthorizationError as exc:
                status = {
                    "approval_not_found": HTTPStatus.NOT_FOUND,
                    "approval_expired": HTTPStatus.GONE,
                    "challenge_expired": HTTPStatus.GONE,
                    "action_conflict": HTTPStatus.CONFLICT,
                    "approval_not_pending": HTTPStatus.CONFLICT,
                    "passkey_already_enrolled": HTTPStatus.CONFLICT,
                    "challenge_limit": HTTPStatus.TOO_MANY_REQUESTS,
                    "passkey_limit": HTTPStatus.TOO_MANY_REQUESTS,
                    "enrollment_denied": HTTPStatus.FORBIDDEN,
                    "enrollment_disabled": HTTPStatus.FORBIDDEN,
                    "ingress_user_required": HTTPStatus.UNAUTHORIZED,
                }.get(exc.code, HTTPStatus.BAD_REQUEST)
                self._json(
                    status,
                    {"error": {"code": exc.code, "message": str(exc)}},
                )
            except Exception:
                self._json(
                    HTTPStatus.INTERNAL_SERVER_ERROR,
                    {
                        "error": {
                            "code": "internal_error",
                            "message": "Authorization failed without executing an operation.",
                        }
                    },
                )
            else:
                self._json(HTTPStatus.OK, payload)

        def _not_found(self) -> None:
            self._json(HTTPStatus.NOT_FOUND, {"error": {"code": "not_found"}})

        def _asset(self, status: HTTPStatus, content_type: str, content: str) -> None:
            body = content.encode("utf-8")
            self.send_response(status)
            self._security_headers(content_type, len(body))
            self.end_headers()
            self.wfile.write(body)

        def _json(self, status: HTTPStatus, payload: dict[str, Any]) -> None:
            body = json.dumps(
                payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            ).encode("utf-8")
            self.send_response(status)
            self._security_headers("application/json; charset=utf-8", len(body))
            self.end_headers()
            self.wfile.write(body)

        def _security_headers(self, content_type: str, content_length: int) -> None:
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(content_length))
            self.send_header("Cache-Control", "no-store")
            self.send_header("Pragma", "no-cache")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header(
                "Content-Security-Policy",
                "default-src 'self'; script-src 'self'; style-src 'self'; "
                "connect-src 'self'; img-src 'self' data:; object-src 'none'; "
                "base-uri 'none'; frame-ancestors 'self'",
            )

    return ThreadingHTTPServer((host, port), Handler)
