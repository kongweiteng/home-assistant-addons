"""Chinese Ingress UI and authenticated internal job API."""

from __future__ import annotations

from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import hmac
import json
import re
import secrets
import threading
import time
from typing import Any
from urllib.parse import urlsplit

from .app_server import AppServerError
from .desktop_api import get_desktop_api, post_desktop_api
from .desktop_dashboard import DESKTOP_DASHBOARD_HTML, DESKTOP_DASHBOARD_JS
from .runner_api import (
    RUNNER_ACTION_RE,
    RUNNER_PATH_RE,
    delete_runner_api,
    get_runner_api,
    patch_runner_api,
    post_runner_api,
)
from .runner_dashboard import DASHBOARD_JS
from .service import ControllerService
from .store import StoreError


JOB_PATH_RE = re.compile(r"^/internal/v1/jobs/([0-9a-f-]{36})$")
ARTIFACT_PATH_RE = re.compile(r"^/internal/v1/jobs/([0-9a-f-]{36})/artifacts/(AR-[A-Z2-7]{26})$")
DOWNLOAD_PATH_RE = re.compile(r"^/downloads/artifacts/([A-Za-z0-9_-]{43})$")
RECOVERY_PATH_RE = re.compile(r"^/internal/v1/jobs/([0-9a-f-]{36})/recovery-resolution$")
TOOL_PATH_RE = re.compile(r"^/api/tools/([a-z0-9_]{1,96})$")
RUNNER_RELAY_EVENT_RE = re.compile(
    r"^/internal/v2/runner-relay/events/"
    r"(heartbeat|status|result|desktop_snapshot|desktop_event|desktop_receipt)$"
)


def create_server(
    host: str,
    port: int,
    *,
    service: ControllerService,
    api_token: str,
    runner_relay_controller_api_token: str = "",
    max_request_bytes: int,
) -> ThreadingHTTPServer:
    csrf_state: dict[str, Any] = {"token": "", "expires_at": 0.0}
    csrf_lock = threading.Lock()

    def csrf_document() -> dict[str, Any]:
        now = time.monotonic()
        with csrf_lock:
            if not csrf_state["token"] or now >= csrf_state["expires_at"]:
                csrf_state["token"] = secrets.token_urlsafe(32)
                csrf_state["expires_at"] = now + 900
            return {
                "csrf_token": csrf_state["token"],
                "csrf_expires_in_seconds": max(1, int(csrf_state["expires_at"] - now)),
            }

    class Handler(BaseHTTPRequestHandler):
        server_version = "CodexController/0.5.27"

        def log_message(self, _format: str, *_args: Any) -> None:
            return None

        def do_GET(self) -> None:  # noqa: N802
            parsed_path = urlsplit(self.path)
            path = parsed_path.path
            if path == "/healthz":
                status = service.status()
                healthy = service.watchdog_healthy(status.get("app_server"))
                self._json(
                    HTTPStatus.OK if healthy else HTTPStatus.SERVICE_UNAVAILABLE,
                    {"status": "ok" if healthy else "runtime_failed", "ready": status["ready"]},
                )
                return
            if path in {"", "/", "/index.html"}:
                self._asset(HTTPStatus.OK, "text/html; charset=utf-8", DASHBOARD_HTML.encode("utf-8"))
                return
            if path == "/desktop":
                self._redirect("desktop/")
                return
            if path in {"/desktop/", "/desktop/index.html"}:
                self._asset(
                    HTTPStatus.OK,
                    "text/html; charset=utf-8",
                    DESKTOP_DASHBOARD_HTML.encode("utf-8"),
                )
                return
            if path == "/desktop/desktop.js":
                self._asset(
                    HTTPStatus.OK,
                    "text/javascript; charset=utf-8",
                    DESKTOP_DASHBOARD_JS.encode("utf-8"),
                )
                return
            if path == "/app.js":
                self._asset(HTTPStatus.OK, "text/javascript; charset=utf-8", DASHBOARD_JS.encode("utf-8"))
                return
            if path == "/api/status":
                self._json(HTTPStatus.OK, {**service.status(), **csrf_document()})
                return
            if path == "/api/tools":
                self._json(HTTPStatus.OK, {"version": 1, "result": service.tool_status()})
                return
            if path.startswith("/api/desktop/v1/"):
                if service.desktop_controller is None:
                    self._json(HTTPStatus.NOT_FOUND, {"error": {"code": "not_found"}})
                    return
                self._call(
                    lambda: get_desktop_api(
                        service.desktop_controller,
                        path,
                        parsed_path.query,
                    )
                )
                return
            if path in {"/api/runners", "/api/runner-tasks"} or RUNNER_PATH_RE.fullmatch(path):
                if service.runner_manager is None:
                    self._json(HTTPStatus.NOT_FOUND, {"error": {"code": "not_found"}})
                    return
                self._call(lambda: get_runner_api(service.runner_manager, path))
                return
            if path == "/internal/v1/capabilities":
                if not self._authorized():
                    return
                self._call(service.capabilities)
                return
            download_match = DOWNLOAD_PATH_RE.fullmatch(path)
            if download_match:
                self._artifact(lambda: service.store.read_download_artifact(download_match.group(1)))
                return
            artifact_match = ARTIFACT_PATH_RE.fullmatch(path)
            if artifact_match:
                if not self._authorized():
                    return
                self._artifact(
                    lambda: service.store.read_job_artifact(
                        artifact_match.group(1), artifact_match.group(2)
                    )
                )
                return
            match = JOB_PATH_RE.fullmatch(path)
            if match:
                if not self._authorized():
                    return
                self._call(lambda: service.store.get_public_job(match.group(1)))
                return
            self._json(HTTPStatus.NOT_FOUND, {"error": {"code": "not_found"}})

        def do_PATCH(self) -> None:  # noqa: N802
            path = urlsplit(self.path).path
            if RUNNER_PATH_RE.fullmatch(path):
                if not self._csrf_authorized():
                    return
                payload = self._read_json()
                if payload is None:
                    return
                if service.runner_manager is None:
                    self._json(HTTPStatus.NOT_FOUND, {"error": {"code": "not_found"}})
                    return
                self._call(lambda: patch_runner_api(service.runner_manager, path, payload))
                return
            match = TOOL_PATH_RE.fullmatch(path)
            if match is None:
                self._json(HTTPStatus.NOT_FOUND, {"error": {"code": "not_found"}})
                return
            if not self._csrf_authorized():
                return
            payload = self._read_json()
            if payload is None:
                return
            if set(payload) != {"enabled", "revision", "request_id"}:
                self._json(HTTPStatus.BAD_REQUEST, {"error": {"code": "invalid_tool_policy"}})
                return
            self._call(
                lambda: service.update_tool_policy(
                    match.group(1),
                    enabled=payload.get("enabled"),
                    revision=payload.get("revision"),
                    request_id=payload.get("request_id"),
                )
            )

        def do_POST(self) -> None:  # noqa: N802
            path = urlsplit(self.path).path
            if path.startswith("/api/") and not self._csrf_authorized():
                return
            if path == "/api/auth/device/start":
                payload = self._read_json(allow_empty=True)
                if payload is not None:
                    self._call(service.begin_device_login)
                return
            if path == "/api/auth/device/cancel":
                payload = self._read_json(allow_empty=True)
                if payload is not None:
                    self._call(service.cancel_device_login)
                return
            if path == "/api/auth/api-key/retry":
                payload = self._read_json(allow_empty=True)
                if payload is not None:
                    self._call(service.begin_api_key_login)
                return
            if path == "/api/auth/logout":
                payload = self._read_json(allow_empty=True)
                if payload is not None:
                    self._call(service.logout)
                return
            if path in {
                "/internal/v2/runner-relay/enroll",
                "/internal/v2/runner-relay/authenticate",
                "/internal/v2/runner-relay/install-bootstrap",
            } or RUNNER_RELAY_EVENT_RE.fullmatch(path):
                if not self._runner_relay_authorized():
                    return
                payload = self._read_json()
                if payload is None:
                    return
                if service.runner_manager is None:
                    self._json(HTTPStatus.NOT_FOUND, {"error": {"code": "not_found"}})
                    return
                if path == "/internal/v2/runner-relay/enroll":
                    self._call(lambda: service.runner_manager.redeem_enrollment(payload))
                    return
                if path == "/internal/v2/runner-relay/authenticate":
                    self._call(lambda: service.runner_manager.authenticate_runner(payload))
                    return
                if path == "/internal/v2/runner-relay/install-bootstrap":
                    self._call(lambda: service.runner_manager.install_bootstrap(payload))
                    return
                event_match = RUNNER_RELAY_EVENT_RE.fullmatch(path)
                assert event_match is not None
                credential = self.headers.get("X-Runner-Credential", "")
                event_type = event_match.group(1)
                if event_type == "heartbeat":
                    self._call(
                        lambda: service.runner_manager.heartbeat(payload, credential=credential)
                    )
                elif event_type == "status":
                    self._call(
                        lambda: service.runner_manager.receive_status(
                            payload, credential=credential
                        )
                    )
                elif event_type == "result":
                    self._call(
                        lambda: service.runner_manager.receive_result(
                            payload, credential=credential
                        )
                    )
                else:
                    self._call(
                        lambda: service.runner_manager.receive_desktop(
                            event_type,
                            payload,
                            credential=credential,
                        )
                    )
                return
            if path.startswith("/api/desktop/v1/"):
                payload = self._read_json()
                if payload is None:
                    return
                if service.desktop_controller is None:
                    self._json(HTTPStatus.NOT_FOUND, {"error": {"code": "not_found"}})
                    return
                self._call(
                    lambda: post_desktop_api(service.desktop_controller, path, payload),
                    status=HTTPStatus.ACCEPTED,
                )
                return
            if path == "/api/runner-enrollments" or RUNNER_ACTION_RE.fullmatch(path):
                payload = self._read_json()
                if payload is None:
                    return
                if service.runner_manager is None:
                    self._json(HTTPStatus.NOT_FOUND, {"error": {"code": "not_found"}})
                    return
                self._call(
                    lambda: post_runner_api(service.runner_manager, path, payload),
                    status=HTTPStatus.CREATED if path == "/api/runner-enrollments" else HTTPStatus.OK,
                )
                return
            if path == "/internal/v2/runner-manager/work":
                if not self._authorized():
                    return
                payload = self._read_json()
                if payload is None:
                    return
                if service.runner_manager is None:
                    self._runner_v2_error(payload, StoreError("runner_manager_disabled", "Runner Manager 未配置", status=409))
                    return
                self._runner_v2_call(payload, lambda: service.runner_manager.work_command(payload))
                return
            if not path.startswith("/internal/v1/") or not self._authorized():
                return
            payload = self._read_json()
            if payload is None:
                return
            if path == "/internal/v1/jobs":
                self._call(lambda: service.submit(payload), status=HTTPStatus.ACCEPTED)
                return
            recovery_match = RECOVERY_PATH_RE.fullmatch(path)
            if recovery_match:
                self._call(
                    lambda: service.store.public_job(
                        service.store.resolve_recovery(
                            recovery_match.group(1), str(payload.get("resolution") or "")
                        )
                    )
                )
                return
            self._json(HTTPStatus.NOT_FOUND, {"error": {"code": "not_found"}})

        def do_DELETE(self) -> None:  # noqa: N802
            path = urlsplit(self.path).path
            if not RUNNER_PATH_RE.fullmatch(path):
                self._json(HTTPStatus.NOT_FOUND, {"error": {"code": "not_found"}})
                return
            if not self._csrf_authorized():
                return
            payload = self._read_json()
            if payload is None:
                return
            if service.runner_manager is None:
                self._json(HTTPStatus.NOT_FOUND, {"error": {"code": "not_found"}})
                return
            self._call(lambda: delete_runner_api(service.runner_manager, path, payload))

        def _authorized(self) -> bool:
            expected = f"Bearer {api_token}"
            actual = self.headers.get("Authorization", "")
            if not hmac.compare_digest(actual, expected):
                self._json(HTTPStatus.UNAUTHORIZED, {"error": {"code": "not_authorized"}})
                return False
            return True

        def _csrf_authorized(self) -> bool:
            expected = csrf_document()["csrf_token"]
            actual = self.headers.get("X-CSRF-Token", "")
            if not isinstance(actual, str) or not hmac.compare_digest(actual, expected):
                self._json(HTTPStatus.FORBIDDEN, {"error": {"code": "csrf_required"}})
                return False
            return True

        def _runner_relay_authorized(self) -> bool:
            if len(runner_relay_controller_api_token) < 32:
                self._json(
                    HTTPStatus.SERVICE_UNAVAILABLE,
                    {"error": {"code": "runner_relay_not_configured"}},
                )
                return False
            expected = f"Bearer {runner_relay_controller_api_token}"
            actual = self.headers.get("Authorization", "")
            if not hmac.compare_digest(actual, expected):
                self._json(HTTPStatus.UNAUTHORIZED, {"error": {"code": "not_authorized"}})
                return False
            return True

        def _read_json(self, *, allow_empty: bool = False) -> dict[str, Any] | None:
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                length = -1
            if allow_empty and length == 0:
                return {}
            if self.headers.get_content_type() != "application/json":
                self._json(HTTPStatus.UNSUPPORTED_MEDIA_TYPE, {"error": {"code": "content_type_required"}})
                return None
            if length < 1 or length > max_request_bytes:
                self._json(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, {"error": {"code": "request_size_invalid"}})
                return None
            try:
                payload = json.loads(self.rfile.read(length))
            except (UnicodeDecodeError, json.JSONDecodeError):
                self._json(HTTPStatus.BAD_REQUEST, {"error": {"code": "invalid_json"}})
                return None
            if not isinstance(payload, dict):
                self._json(HTTPStatus.BAD_REQUEST, {"error": {"code": "json_object_required"}})
                return None
            return payload

        def _call(self, callback: Any, *, status: HTTPStatus = HTTPStatus.OK) -> None:
            try:
                result = callback()
            except (StoreError, AppServerError) as exc:
                response_status = getattr(exc, "status", 409 if getattr(exc, "definitive", False) else 503)
                self._json(HTTPStatus(response_status), {"error": {"code": exc.code, "message": str(exc)}})
            except Exception:
                self._json(
                    HTTPStatus.INTERNAL_SERVER_ERROR,
                    {"error": {"code": "internal_error", "message": "Controller 操作失败，未返回私有详情。"}},
                )
            else:
                self._json(status, {"version": 1, "result": result})

        def _runner_v2_call(self, payload: dict[str, Any], callback: Any) -> None:
            try:
                result = callback()
            except StoreError as exc:
                self._runner_v2_error(payload, exc)
            except Exception:
                self._runner_v2_error(
                    payload,
                    StoreError(
                        "runner_manager_internal_error",
                        "Runner Manager 操作失败，未返回私有详情。",
                        status=500,
                    ),
                )
            else:
                self._json(HTTPStatus.OK, result)

        def _runner_v2_error(self, payload: dict[str, Any], exc: StoreError) -> None:
            request_id = payload.get("request_id")
            if not isinstance(request_id, str) or not re.fullmatch(r"WRV2-[0-9a-f]{32}", request_id):
                request_id = "WRV2-" + "0" * 32
            code = exc.code if re.fullmatch(r"[a-z][a-z0-9_]{0,127}", exc.code) else "runner_manager_error"
            self._json(
                HTTPStatus(exc.status),
                {
                    "version": 2,
                    "request_id": request_id,
                    "error_code": code,
                    "message": str(exc)[:1000],
                    "retryable": exc.status >= 500,
                },
            )

        def _asset(self, status: HTTPStatus, content_type: str, body: bytes) -> None:
            self.send_response(status)
            self._headers(content_type, len(body))
            self.end_headers()
            self.wfile.write(body)

        def _redirect(self, location: str) -> None:
            self.send_response(HTTPStatus.PERMANENT_REDIRECT)
            self.send_header("Location", location)
            self._headers("text/plain; charset=utf-8", 0)
            self.end_headers()

        def _artifact(self, callback: Any) -> None:
            try:
                metadata, body = callback()
            except StoreError as exc:
                self._json(HTTPStatus(exc.status), {"error": {"code": exc.code, "message": str(exc)}})
                return
            self.send_response(HTTPStatus.OK)
            self._headers(str(metadata["mime_type"]), len(body))
            self.send_header("X-Content-SHA256", str(metadata["sha256"]))
            self.end_headers()
            self.wfile.write(body)

        def _json(self, status: HTTPStatus, payload: dict[str, Any]) -> None:
            body = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
            self._asset(status, "application/json; charset=utf-8", body)

        def _headers(self, content_type: str, content_length: int) -> None:
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(content_length))
            self.send_header("Cache-Control", "no-store")
            self.send_header("Pragma", "no-cache")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header(
                "Content-Security-Policy",
                "default-src 'self'; style-src 'unsafe-inline'; script-src 'self'; connect-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'self'",
            )

    return ThreadingHTTPServer((host, port), Handler)


DASHBOARD_HTML = """<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<title>Codex 控制器</title><style>
:root{color-scheme:light;--bg:#f7f7f5;--surface:#fff;--surface-2:#efefec;--surface-3:#fafaf8;--line:rgba(30,32,36,.11);--line-strong:rgba(30,32,36,.18);--text:#202124;--muted:#73767b;--blue:#3768e5;--green:#1a8b67;--green-soft:#e8f4ef;--amber:#a96e16;--amber-soft:#f8efdf;--red:#bd384d;--red-soft:#f9eaed;--shadow:0 1px 3px rgba(20,23,27,.045),0 14px 36px rgba(20,23,27,.05)}*{box-sizing:border-box}html{background:var(--bg);scroll-behavior:smooth}body{margin:0;min-width:320px;background:var(--bg);color:var(--text);font:15px/1.5 -apple-system,BlinkMacSystemFont,"SF Pro Text","PingFang SC","Helvetica Neue",sans-serif;-webkit-font-smoothing:antialiased}.app-shell{min-height:100dvh}.side-rail{position:fixed;inset:0 auto 0 0;width:224px;padding:22px 14px;display:flex;flex-direction:column;gap:6px;border-right:1px solid var(--line);background:var(--surface-2)}.rail-brand{display:flex;align-items:center;gap:10px;padding:0 9px 20px;font-size:19px;font-weight:740}.brand-mark{width:36px;height:36px;display:grid;place-items:center;border-radius:11px;background:#222326;color:#fff}.side-rail a{min-height:46px;padding:0 12px;border-radius:11px;display:flex;align-items:center;color:var(--muted);text-decoration:none}.side-rail a:hover,.side-rail a.active{background:var(--surface);color:var(--text)}.side-rail .rail-task{margin-top:12px;justify-content:center;background:#222326;color:#fff}.rail-foot{margin-top:auto;padding:10px 9px;color:var(--muted);font-size:12px}.page{max-width:1450px;margin-left:224px;padding:22px 34px 90px}.topbar{display:flex;align-items:center;justify-content:space-between;gap:18px;min-height:74px}.eyebrow{color:var(--muted);font-size:11px;font-weight:620}.topbar h1{margin:2px 0;font-size:29px;line-height:1.2;letter-spacing:-.035em}.topbar p{margin:0;color:var(--muted);font-size:13px}.top-actions{display:flex;gap:8px}.grid{display:grid;grid-template-columns:repeat(5,minmax(0,1fr));gap:10px;margin:10px 0 22px}.card{background:var(--surface);border:1px solid var(--line);border-radius:14px;padding:16px;box-shadow:var(--shadow)}.metric{font-size:23px;font-weight:720;display:block;margin-top:7px}.muted{color:var(--muted)}code{color:#47705f;overflow-wrap:anywhere}button,select,input{min-height:44px;border:1px solid var(--line-strong);border-radius:10px;padding:9px 12px;background:var(--surface);color:var(--text);font:inherit}input{min-width:160px}button{cursor:pointer;background:#222326;border-color:#222326;color:#fff}button:hover:not(:disabled){filter:brightness(1.08)}button.secondary,.button-link.secondary{background:var(--surface);border-color:var(--line-strong);color:var(--text)}button.danger{background:var(--red-soft);border-color:#efd0d7;color:var(--red)}button.toggle{min-width:76px;background:var(--surface-2);border-color:var(--line);color:var(--muted)}button.toggle.on{background:var(--green-soft);border-color:transparent;color:var(--green)}button:disabled{opacity:.48;cursor:not-allowed}a{color:var(--blue)}.button-link{display:inline-flex;min-height:44px;align-items:center;border:1px solid #222326;border-radius:11px;padding:8px 14px;background:#222326;color:#fff;text-decoration:none}.section{scroll-margin-top:18px;margin-top:22px}.section-head{display:flex;align-items:end;justify-content:space-between;gap:12px;margin-bottom:10px}.section-head h2{margin:0;font-size:19px;letter-spacing:-.02em}.section-head p{margin:3px 0 0;color:var(--muted);font-size:12px}.auth-actions,.toolbar{display:flex;flex-wrap:wrap;gap:9px;align-items:center;margin:12px 0}.notice{border-left:3px solid var(--blue);border-radius:6px 12px 12px 6px;background:#eef2fc;padding:12px 14px;box-shadow:none}.table-wrap{overflow:auto;border:1px solid var(--line);border-radius:14px;background:var(--surface)}table{width:100%;border-collapse:collapse;min-width:980px;background:var(--surface)}th,td{text-align:left;padding:12px;border-bottom:1px solid var(--line);vertical-align:top}th{color:#6b6f75;background:var(--surface-3);position:sticky;top:0;font-size:12px}tbody tr:last-child td{border-bottom:0}.tool-name{font-weight:700}.technical{font:12px ui-monospace,SFMono-Regular,monospace;color:var(--muted);margin-top:4px}.badges,.runner-actions,.installation-actions,.installation-meta{display:flex;flex-wrap:wrap;gap:8px;align-items:center}.runner-actions button{min-height:38px;font-size:12px;padding:6px 9px}.badge{font-size:11px;border:0;border-radius:999px;padding:4px 8px;background:var(--surface-2);color:#666a70}.badge.good{background:var(--green-soft);color:var(--green)}.badge.warn{background:var(--amber-soft);color:var(--amber)}.badge.bad{background:var(--red-soft);color:var(--red)}.intent{max-width:310px;white-space:normal}.error{color:var(--red)}.success{color:var(--green)}.hidden{display:none!important}.secret-box{border-color:#ead8b9;background:#fffaf0}.secret-box.expired{border-color:#efd0d7}.install-command{margin:14px 0;padding:14px;border:1px solid var(--line);border-radius:10px;background:#f1f2ef;color:#33423b;white-space:pre-wrap;overflow-wrap:anywhere;max-height:260px;overflow:auto;font:13px ui-monospace,SFMono-Regular,Menlo,monospace}.installation-meta{color:var(--muted);margin-top:10px}.installation-meta strong{color:var(--text)}.form-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:10px;align-items:end}.form-grid label{display:grid;gap:5px;color:var(--muted)}.mobile-nav{display:none}
@media(max-width:980px){.side-rail{display:none}.page{margin-left:0;padding:14px 18px 92px}.grid{grid-template-columns:repeat(3,minmax(0,1fr))}.mobile-nav{position:fixed;z-index:50;left:12px;right:12px;bottom:10px;height:calc(66px + env(safe-area-inset-bottom));display:grid;grid-template-columns:repeat(4,1fr);padding:4px 7px env(safe-area-inset-bottom);border:1px solid var(--line);border-radius:18px;background:rgba(255,255,255,.95);box-shadow:0 8px 28px rgba(25,28,32,.11);backdrop-filter:blur(18px)}.mobile-nav a{min-height:56px;display:flex;align-items:center;justify-content:center;border-radius:12px;color:var(--muted);font-size:12px;text-decoration:none}.mobile-nav .primary{margin-top:-16px;height:58px;align-self:start;background:#222326;color:#fff;box-shadow:0 6px 16px rgba(0,0,0,.18)}}
@media(max-width:700px){.page{padding:0 10px 96px}.topbar{min-height:86px;padding-top:env(safe-area-inset-top)}.topbar h1{font-size:25px}.topbar p{display:none}.top-actions .secondary{display:none}.grid{display:flex;overflow:auto;margin:4px -10px 18px;padding:0 10px 2px;scrollbar-width:none}.grid .card{flex:0 0 116px;padding:12px}.metric{font-size:19px}.section{margin-top:18px}.section-head{align-items:center}.section-head p{display:none}.card{padding:13px}.auth-actions{display:grid;grid-template-columns:1fr 1fr}.auth-actions button{width:100%}.toolbar{display:grid;grid-template-columns:1fr 1fr}.toolbar label{display:grid;gap:4px;color:var(--muted);font-size:12px}.toolbar>button{grid-column:1/-1}.toolbar>span{grid-column:1/-1}.form-grid{grid-template-columns:1fr}.table-wrap{overflow:visible;border:0;background:transparent}table{min-width:0;background:transparent}thead{display:none}tbody{display:grid;gap:9px}tr{display:grid;padding:12px;border:1px solid var(--line);border-radius:13px;background:var(--surface);box-shadow:0 1px 2px rgba(20,23,27,.035)}td{display:block;padding:5px 2px;border:0}.runner-actions{min-width:0}.runner-actions button,.installation-actions button{flex:1 1 auto}.install-command{font-size:12px}.notice{font-size:13px}}
</style></head><body><div class="app-shell"><aside class="side-rail"><div class="rail-brand"><span class="brand-mark">C</span>Codex</div><a class="active" href="#overview">总览</a><a href="desktop/">任务</a><a href="#tools">工具</a><a href="#runners">Runner</a><a class="rail-task" href="desktop/">打开任务工作台</a><div class="rail-foot">远程控制状态以 Runner 收据为准</div></aside><main class="page">
<header class="topbar" id="overview"><div><div class="eyebrow">控制器总览</div><h1>Codex 控制器</h1><p>认证、工具与 Runner 管理集中在一个清晰的工作区</p></div><div class="top-actions"><a class="button-link" href="desktop/">任务工作台</a><a class="button-link secondary" href="">刷新</a></div></header>
<div class="grid"><div class="card">服务<span class="metric" id="ready">加载中</span></div><div class="card">认证<span class="metric" id="auth">加载中</span></div><div class="card">排队<span class="metric" id="queued">-</span></div><div class="card">已发布工具<span class="metric" id="published">-</span></div><div class="card">当前任务<span class="metric" id="threadShort">无活动</span></div></div>
<section class="section" id="authSection"><div class="section-head"><div><h2>正式认证</h2><p id="authHelp">认证模式由 Add-on options 显式选择，禁止自动降级或混用。</p></div></div><div class="auth-actions"><button id="login">开始设备码登录</button><button id="cancel" class="secondary">取消登录</button><button id="retryApiKey">重试 API Key 登录</button><button class="danger" id="logout">退出登录</button></div><div class="card"><div id="loginInfo" class="muted">正在读取认证配置。</div></div></section>
<section class="section" id="tools"><div class="section-head"><div><h2>MCP 工具</h2><p>查看发布状态、服务门禁与调用能力</p></div></div><div class="card notice">这里显示 Controller 已知工具、内部服务配置、管理员策略、MCP 进程真实 <code>tools/list</code> 心跳和当前可调用状态。意图示例不是固定关键词，Codex 会根据完整语义决定是否调用工具。</div>
<div class="toolbar"><label>服务 <select id="serviceFilter"><option value="all">全部</option><option value="renovation_hub">Renovation Hub</option><option value="ha_operations_broker">Operations Broker</option></select></label><label>类型 <select id="riskFilter"><option value="all">全部</option><option value="read_only">只读</option><option value="write">写入</option><option value="controlled">受控操作</option></select></label><button id="reloadTools">刷新工具状态</button><span id="toolFeedback" class="muted"></span></div>
<div class="table-wrap"><table><thead><tr><th>工具</th><th>服务 / 风险</th><th>状态</th><th>意图示例</th><th>最近调用</th><th>开关</th></tr></thead><tbody id="toolRows"></tbody></table></div></section>
<section id="runnerCenter" class="section hidden"><div class="section-head" id="runners"><div><h2>Runner Center</h2><p>管理远程执行器、注册与恢复状态</p></div></div><div class="card notice">Runner Center 只管理已注册的 Mac/Linux 执行器和确定性任务 lease，不提供网页终端、任意 Shell、SSH、路径、源码、diff、日志或秘密回显。</div>
<div id="runnerRelayMissing" class="card notice"><strong>管理功能已启用，任务执行 Relay 尚未接入</strong><p class="muted">当前可以新增、启用、排空、停用、轮换和删除 Runner；在独立 Relay 配置完成前不会向真实 Runner 发布任务。</p></div>
<div id="runnerInstallerMissing" class="card notice"><strong>安装制品尚未就绪</strong><p id="runnerInstallerHelp" class="muted">必须先配置完整、摘要匹配的公开 installer manifest 和 WSS Relay URL；否则不会创建无法使用的一次性 enrollment。</p></div>
<div class="grid"><div class="card">Runner<span class="metric" id="runnerTotal">0</span></div><div class="card">已启用<span class="metric" id="runnerEnabled">0</span></div><div class="card">在线<span class="metric" id="runnerOnline">0</span></div><div class="card">忙碌<span class="metric" id="runnerBusy">0</span></div><div class="card">需恢复<span class="metric" id="runnerRecovery">0</span></div></div>
<div class="section-head"><div><h2>新增 Runner</h2><p>生成一次性、短期有效的注册材料</p></div></div><form id="runnerForm" class="card form-grid"><label>名称<input id="runnerName" maxlength="80" required placeholder="常驻 Linux Runner"></label><label>平台<select id="runnerOs"><option value="linux">Linux</option><option value="macos">macOS</option></select></label><label>架构<select id="runnerArch"><option value="amd64">amd64</option><option value="aarch64">aarch64</option></select></label><label>项目白名单<input id="runnerProjects" required value="renovation-hub"></label><label>标签<input id="runnerLabels" value="always-on,tests"></label><button id="createRunner" type="submit" disabled>生成安装命令</button></form>
<div id="runnerSecret" class="card secret-box hidden"><strong>Runner 一次性直接安装</strong><p class="muted">安装链接 15 分钟有效，只保留在当前页面内存。安装包已内置固定 Python、Runner 与 Codex；目标机仍需已有 Git 工作区，macOS 首次运行可能显示系统信任提示。</p><div class="installation-meta"><span>平台 <strong id="runnerInstallPlatform">-</strong></span><span>Agent <strong id="runnerInstallVersion">-</strong></span><span>注册 <strong id="runnerInstallStatus">待领取</strong></span><span>剩余 <strong id="runnerInstallCountdown">--:--</strong></span></div><p class="muted">一次性 HTTPS 安装链接</p><pre id="runnerInstallLink" class="install-command"></pre><p class="muted">终端安装命令</p><pre id="runnerSecretValue" class="install-command"></pre><div class="installation-actions"><button id="copyRunnerLink" type="button">复制安装链接</button><button id="openRunnerLink" type="button" class="secondary">打开安装链接</button><button id="copyRunnerCommand" type="button">复制终端命令</button><button id="revokeRunnerEnrollment" type="button" class="danger">撤销注册</button><button id="regenerateRunnerEnrollment" type="button" class="secondary">重新生成</button><button id="closeRunnerSecret" type="button" class="secondary">关闭</button><span id="runnerInstallFeedback" class="muted"></span></div></div>
<div id="runnerCredentialSecret" class="card secret-box hidden"><strong>Runner 新凭据</strong><p class="muted">凭据只显示一次，用于既有 Runner 的受控凭据轮换；关闭或刷新页面后无法恢复。</p><pre id="runnerCredentialValue" class="install-command"></pre><div class="installation-actions"><button id="copyRunnerCredential" type="button">复制凭据</button><button id="closeRunnerCredential" type="button" class="secondary">关闭</button><span id="runnerCredentialFeedback" class="muted"></span></div></div>
<div class="toolbar"><label>管理状态 <select id="runnerStateFilter"><option value="all">全部</option><option value="pending">待启用</option><option value="enabled">已启用</option><option value="draining">排空中</option><option value="disabled">已停用</option></select></label><label>平台 <select id="runnerPlatformFilter"><option value="all">全部</option><option value="linux">Linux</option><option value="macos">macOS</option></select></label><button id="reloadRunners">刷新 Runner</button><span id="runnerFeedback" class="muted"></span></div>
<div class="table-wrap"><table><thead><tr><th>Runner</th><th>平台</th><th>状态</th><th>项目 / 标签</th><th>当前任务 / 心跳</th><th>操作</th></tr></thead><tbody id="runnerRows"></tbody></table></div>
<div id="runnerDetail" class="card hidden"><strong id="runnerDetailTitle">Runner 详情</strong><p id="runnerDetailBody" class="muted"></p></div></section>
<div id="runnerDisabled" class="card notice hidden"><strong>Runner Center v2 已由 Add-on 配置关闭</strong><p class="muted">普通 Codex 对话、MCP、Remote Work v1、微信 Poller、通知和装修业务链路继续保持原行为。</p></div>
<section class="section"><div class="section-head"><div><h2>安全状态</h2><p>用于诊断的脱敏运行摘要</p></div></div><div class="card"><p id="details" class="muted">加载中</p></div></section></main><nav class="mobile-nav" aria-label="移动端导航"><a href="#overview">总览</a><a class="primary" href="desktop/">任务</a><a href="#tools">工具</a><a href="#runners">Runner</a></nav></div><script src="app.js"></script></body></html>"""


_LEGACY_DASHBOARD_JS_031 = r"""const q=id=>document.getElementById(id);let csrf='',catalog=null,statusDoc=null;
function requestId(){const bytes=new Uint8Array(16);crypto.getRandomValues(bytes);return Array.from(bytes,b=>b.toString(16).padStart(2,'0')).join('')}
async function jsonFetch(path,options={}){const r=await fetch(path,{cache:'no-store',...options});const j=await r.json();if(!r.ok)throw new Error(j.error?.message||j.error?.code||'请求失败');return j}
async function call(path){const j=await jsonFetch(path,{method:'POST',headers:{'Content-Type':'application/json','X-CSRF-Token':csrf},body:'{}'});return j.result}
function showDeviceCode(p){const box=q('loginInfo');box.replaceChildren();if(!p){box.textContent='尚未生成设备码。';return}let url;try{url=new URL(p.verificationUrl)}catch(_){box.textContent='设备码响应无效。';return}if(url.protocol!=='https:'){box.textContent='设备码验证地址不是 HTTPS。';return}const link=document.createElement('a');link.target='_blank';link.rel='noreferrer';link.href=url.href;link.textContent=url.href;const code=document.createElement('code');code.textContent=p.userCode;box.append('打开 ',link,document.createElement('br'),'用户码：',code)}
function showApiKey(s){const endpoint=s.api_base_mode==='custom'?'自定义 Responses API':'OpenAI 官方 API';q('loginInfo').textContent=s.api_key_configured?`API Key 已通过 Add-on options 私密配置；API 端点为${endpoint}，页面不会显示 URL 或 Key 内容。`:`尚未在 Add-on options 配置 API Key；API 端点为${endpoint}。`}
function badge(text,kind=''){const span=document.createElement('span');span.className=`badge ${kind}`.trim();span.textContent=text;return span}
function riskText(v){return v==='read_only'?'只读':v==='write'?'写入':'受控操作'}
function serviceText(v){return v==='renovation_hub'?'Renovation Hub':'Operations Broker'}
function renderTools(){const body=q('toolRows');body.replaceChildren();if(!catalog)return;const sf=q('serviceFilter').value,rf=q('riskFilter').value;for(const tool of catalog.tools){if(sf!=='all'&&tool.service!==sf)continue;if(rf!=='all'&&tool.risk_type!==rf)continue;const row=document.createElement('tr');const name=document.createElement('td'),title=document.createElement('div'),tech=document.createElement('div');title.className='tool-name';title.textContent=tool.display_name;tech.className='technical';tech.textContent=tool.name;name.append(title,tech);const type=document.createElement('td');type.append(serviceText(tool.service),document.createElement('br'),riskText(tool.risk_type));const states=document.createElement('td'),badges=document.createElement('div');badges.className='badges';badges.append(badge(tool.configured?'服务已配置':'服务未配置',tool.configured?'good':'bad'),badge(tool.enabled?'策略开启':'策略关闭',tool.enabled?'good':'bad'),badge(tool.mcp_published?'MCP 已发布':tool.waiting_for_mcp_refresh?'等待 MCP 刷新':'MCP 未发布',tool.mcp_published?'good':tool.waiting_for_mcp_refresh?'warn':'bad'),badge(tool.callable?'可调用':'不可调用',tool.callable?'good':'bad'));states.append(badges);const intent=document.createElement('td');intent.className='intent';intent.textContent=tool.intent_examples.join('；');const recent=document.createElement('td');recent.className='muted';recent.textContent=tool.last_invocation?`${tool.last_invocation.outcome} · ${tool.last_invocation.error_code||'无错误'} · ${tool.last_invocation.duration_ms}ms`:'暂无';const action=document.createElement('td'),toggle=document.createElement('button');toggle.className=`toggle ${tool.enabled?'on':''}`;toggle.textContent=tool.enabled?'已开启':'已关闭';toggle.disabled=catalog.policy_error!==null;toggle.onclick=()=>setTool(tool,!tool.enabled,toggle);action.append(toggle);row.append(name,type,states,intent,recent,action);body.append(row)}if(!body.children.length){const row=document.createElement('tr'),cell=document.createElement('td');cell.colSpan=6;cell.className='muted';cell.textContent='当前筛选条件下没有工具。';row.append(cell);body.append(row)}}
async function setTool(tool,enabled,button){button.disabled=true;q('toolFeedback').textContent='正在保存策略…';try{const j=await jsonFetch(`api/tools/${encodeURIComponent(tool.name)}`,{method:'PATCH',headers:{'Content-Type':'application/json','X-CSRF-Token':csrf},body:JSON.stringify({enabled,revision:catalog.revision,request_id:requestId()})});q('toolFeedback').className='success';q('toolFeedback').textContent=`${tool.display_name}已${enabled?'开启':'关闭'}，目录 revision ${j.result.revision}`;await refreshTools()}catch(e){q('toolFeedback').className='error';q('toolFeedback').textContent=e.message;await refreshTools()}finally{button.disabled=false}}
async function refreshTools(){const j=await jsonFetch('api/tools');catalog=j.result;q('published').textContent=`${catalog.summary.published}/${catalog.summary.known}`;renderTools()}
let runnerDoc=null;
function runnerStateKind(value){return value==='online'||value==='idle'||value==='enabled'?'good':value==='stale'||value==='draining'||value==='pending'?'warn':value==='offline'||value==='recovery_required'||value==='error'||value==='revoked'?'bad':''}
function runnerStateText(value){const labels={pending:'待启用',enabled:'已启用',draining:'排空中',disabled:'已停用',revoked:'已吊销',online:'在线',stale:'过期',offline:'离线',idle:'空闲',busy:'忙碌',recovery_required:'需恢复',error:'错误'};return labels[value]||value}
function runnerButton(label,handler,kind='secondary',disabled=false){const button=document.createElement('button');button.type='button';button.textContent=label;button.className=kind;button.disabled=disabled;button.onclick=handler;return button}
async function runnerMutation(method,path,body){const j=await jsonFetch(path,{method,headers:{'Content-Type':'application/json','X-CSRF-Token':csrf},body:JSON.stringify(body)});return j.result}
async function runnerAction(runner,action){q('runnerFeedback').className='muted';q('runnerFeedback').textContent='正在执行受控管理操作…';try{let result;if(action==='enable'){result=await runnerMutation('PATCH',`api/runners/${encodeURIComponent(runner.runner_id)}`,{admin_state:'enabled',revision:runner.revision,request_id:requestId()})}else if(action==='drain'){result=await runnerMutation('POST',`api/runners/${encodeURIComponent(runner.runner_id)}/drain`,{revision:runner.revision,request_id:requestId()})}else if(action==='emergency-disable'){if(!confirm(`紧急停用 ${runner.display_name}？无法确认的运行任务会进入 recovery_required，且不会自动转移。`))return;result=await runnerMutation('POST',`api/runners/${encodeURIComponent(runner.runner_id)}/emergency-disable`,{revision:runner.revision,request_id:requestId()})}else if(action==='self-check'){result=await runnerMutation('POST',`api/runners/${encodeURIComponent(runner.runner_id)}/self-check`,{revision:runner.revision,request_id:requestId()})}else if(action==='rotate'){if(!confirm(`轮换 ${runner.display_name} 的凭据？旧凭据会立即吊销。`))return;result=await runnerMutation('POST',`api/runners/${encodeURIComponent(runner.runner_id)}/credential-rotation`,{revision:runner.revision,request_id:requestId()});showRunnerSecret(result.credential,runner.runner_id)}else if(action==='delete'){if(!confirm(`删除 ${runner.display_name} 的管理记录？只会吊销凭据并归档，不会删除服务器、worktree、分支或 Session。`))return;result=await runnerMutation('DELETE',`api/runners/${encodeURIComponent(runner.runner_id)}`,{revision:runner.revision,request_id:requestId()})}q('runnerFeedback').className='success';q('runnerFeedback').textContent=`${runner.display_name}：操作已记录`;await refreshRunners()}catch(e){q('runnerFeedback').className='error';q('runnerFeedback').textContent=e.message;await refreshRunners()}}
async function showRunnerDetail(runner){try{const j=await jsonFetch(`api/runners/${encodeURIComponent(runner.runner_id)}`),r=j.result;q('runnerDetail').classList.remove('hidden');q('runnerDetailTitle').textContent=`${r.display_name} · ${r.runner_id}`;const check=r.self_check?.ok===true?'通过':r.self_check?.ok===false?`失败 ${r.self_check.error_code||''}`:'未上报';const events=(r.events||[]).slice(0,8).map(event=>`${event.created_at} ${event.event_type}`).join('；');q('runnerDetailBody').textContent=`协议 ${r.protocol_version} · Agent ${r.agent_version||'未知'} · Codex ${r.codex_version||'未知'} · policy ${r.policy_revision} · 自检 ${check} · 最近审计 ${events||'无'}`}catch(e){q('runnerFeedback').className='error';q('runnerFeedback').textContent=e.message}}
function renderRunners(){const body=q('runnerRows');body.replaceChildren();if(!runnerDoc)return;const state=q('runnerStateFilter').value,platform=q('runnerPlatformFilter').value;for(const runner of runnerDoc.runners){if(state!=='all'&&runner.admin_state!==state)continue;if(platform!=='all'&&runner.os!==platform)continue;const row=document.createElement('tr');const name=document.createElement('td'),title=document.createElement('div'),id=document.createElement('div');title.className='tool-name';title.textContent=runner.display_name;id.className='technical';id.textContent=runner.runner_id;name.append(title,id);const platformCell=document.createElement('td');platformCell.textContent=`${runner.os} / ${runner.arch}\nAgent ${runner.agent_version||'未注册'}`;const stateCell=document.createElement('td'),states=document.createElement('div');states.className='badges';for(const value of [runner.admin_state,runner.connectivity_state,runner.work_state])states.append(badge(runnerStateText(value),runnerStateKind(value)));stateCell.append(states);const policy=document.createElement('td');policy.textContent=`${runner.allowed_projects.join('、')||'无项目'}\n${runner.labels.join('、')||'无标签'}`;const activity=document.createElement('td');activity.textContent=`${runner.current_task_id||'无活动任务'}\n${runner.last_heartbeat_at||'从未心跳'}`;const actions=document.createElement('td'),group=document.createElement('div');group.className='runner-actions';group.append(runnerButton('详情',()=>showRunnerDetail(runner)));if(runner.admin_state==='pending'||runner.admin_state==='disabled')group.append(runnerButton('启用',()=>runnerAction(runner,'enable'),'toggle'));if(runner.admin_state==='enabled')group.append(runnerButton('排空停用',()=>runnerAction(runner,'drain')));if(['enabled','draining'].includes(runner.admin_state)||runner.work_state==='busy')group.append(runnerButton('紧急停用',()=>runnerAction(runner,'emergency-disable'),'danger'));if(runner.admin_state!=='revoked')group.append(runnerButton('自检',()=>runnerAction(runner,'self-check')),runnerButton('轮换凭据',()=>runnerAction(runner,'rotate')));if(runner.admin_state==='disabled'&&runner.work_state==='idle'&&!runner.current_task_id)group.append(runnerButton('删除',()=>runnerAction(runner,'delete'),'danger'));actions.append(group);row.append(name,platformCell,stateCell,policy,activity,actions);body.append(row)}if(!body.children.length){const row=document.createElement('tr'),cell=document.createElement('td');cell.colSpan=6;cell.className='muted';cell.textContent=runnerDoc.runners.length?'当前筛选条件下没有 Runner。':'尚未注册 Runner；可在上方生成一次性注册材料。';row.append(cell);body.append(row)}}
function showRunnerSecret(secret,runnerId){const value=secret?.secret||secret?.token;if(!value){return}q('runnerSecret').classList.remove('hidden');q('runnerSecretValue').textContent=`runner_id=${runnerId}\nsecret=${value}`}
async function refreshRunners(){if(!statusDoc?.runner_manager?.enabled)return;try{const j=await jsonFetch('api/runners');runnerDoc=j.result;const s=runnerDoc.summary;q('runnerTotal').textContent=s.total;q('runnerEnabled').textContent=s.enabled;q('runnerOnline').textContent=s.online;q('runnerBusy').textContent=s.busy;q('runnerRecovery').textContent=s.recovery_required;renderRunners()}catch(e){q('runnerFeedback').className='error';q('runnerFeedback').textContent=e.message}}
function syncRunnerRelayState(){q('runnerRelayMissing').classList.toggle('hidden',Boolean(statusDoc?.runner_manager?.relay_configured))}
async function refresh(){try{const s=await jsonFetch('api/status');statusDoc=s;csrf=s.csrf_token;const a=s.app_server.account,api=s.configured_auth_mode==='api_key';q('ready').textContent=s.ready?'就绪':'未就绪';q('auth').textContent=a.auth_mode==='apiKey'?'API Key':a.auth_mode==='chatgpt'?'ChatGPT':'需要登录';q('queued').textContent=s.queue.jobs.queued;q('threadShort').textContent=s.queue.active_job?.thread_short||'无活动';q('login').hidden=api;q('cancel').hidden=api;q('retryApiKey').hidden=!api;q('authHelp').textContent=api?'当前选择 API Key。URL、模型和 Key 只能在 Add-on options 中配置，页面不接收或显示秘密值。':'当前选择 ChatGPT Device Code。HAOS 使用独立会话，不复制本机凭据。';api?showApiKey(s):showDeviceCode(s.pending_login);const runnerEnabled=Boolean(s.runner_manager?.enabled);q('runnerCenter').classList.toggle('hidden',!runnerEnabled);q('runnerDisabled').classList.toggle('hidden',runnerEnabled);q('details').textContent=`Controller ${s.version} · Codex ${s.codex_version} · intake ${s.intake_enabled?'已启用':'关闭'} · Runner Center ${runnerEnabled?'已启用':'关闭'} · Thread ${s.queue.threads} · 已知工具 ${s.tools.known} · 已配置 ${s.tools.configured} · 策略开启 ${s.tools.enabled} · MCP 心跳 ${s.tools.mcp.observed_at||'未观测'} · 策略错误 ${s.tools.policy_error||'无'} · app-server ${s.app_server.running?'运行':'停止'}`;await refreshTools();if(runnerEnabled)await refreshRunners()}catch(e){q('details').textContent=e.message}}
q('runnerForm').onsubmit=async event=>{event.preventDefault();const labels=q('runnerLabels').value.split(',').map(value=>value.trim()).filter(Boolean),projects=q('runnerProjects').value.split(',').map(value=>value.trim()).filter(Boolean);q('runnerFeedback').className='muted';q('runnerFeedback').textContent='正在创建 pending Runner…';try{const result=await runnerMutation('POST','api/runner-enrollments',{display_name:q('runnerName').value.trim(),os:q('runnerOs').value,arch:q('runnerArch').value,labels,allowed_projects:projects,max_concurrency:1,request_id:requestId()});showRunnerSecret(result.enrollment,result.runner.runner_id);q('runnerFeedback').className='success';q('runnerFeedback').textContent='Runner 已创建为 pending；完成注册和自检后仍需人工启用。';await refreshRunners()}catch(e){q('runnerFeedback').className='error';q('runnerFeedback').textContent=e.message}};
q('closeRunnerSecret').onclick=()=>{q('runnerSecretValue').textContent='';q('runnerSecret').classList.add('hidden')};q('runnerStateFilter').onchange=renderRunners;q('runnerPlatformFilter').onchange=renderRunners;q('reloadRunners').onclick=refreshRunners;q('serviceFilter').onchange=renderTools;q('riskFilter').onchange=renderTools;q('reloadTools').onclick=refresh;q('login').onclick=async()=>{try{await call('api/auth/device/start');await refresh()}catch(e){alert(e.message)}};q('cancel').onclick=async()=>{try{await call('api/auth/device/cancel');await refresh()}catch(e){alert(e.message)}};q('retryApiKey').onclick=async()=>{try{await call('api/auth/api-key/retry');await refresh()}catch(e){alert(e.message)}};q('logout').onclick=async()=>{if(confirm('确认退出 HAOS Controller 的独立 Codex 会话？'))try{await call('api/auth/logout');await refresh()}catch(e){alert(e.message)}};refresh();syncRunnerRelayState();setInterval(refresh,5000);setInterval(syncRunnerRelayState,500);"""
