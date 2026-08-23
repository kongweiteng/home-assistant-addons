"""Renovation Hub HTTP entrypoints and legacy Ledger tool API."""

from __future__ import annotations

from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import hmac
import json
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlsplit

from .business_tools import business_manifest, dispatch_business_tool
from .hub import RenovationHubStore
from .ledger import LedgerError, LedgerStore


def create_server(
    host: str,
    port: int,
    *,
    store: LedgerStore,
    api_token: str,
    max_request_bytes: int,
) -> ThreadingHTTPServer:
    class Handler(BaseHTTPRequestHandler):
        server_version = "RenovationHub/0.3.1"

        def log_message(self, _format: str, *_args: Any) -> None:
            return None

        def do_GET(self) -> None:  # noqa: N802
            parsed = urlsplit(self.path)
            path = parsed.path
            if path == "/healthz":
                self._json(HTTPStatus.OK, store.status())
                return
            if path in {"", "/", "/index.html"}:
                self._asset(HTTPStatus.OK, "text/html; charset=utf-8", render_dashboard(store.status()).encode("utf-8"))
                return
            if path == "/api/status":
                self._json(HTTPStatus.OK, store.status())
                return
            if isinstance(store, RenovationHubStore) and path.startswith("/api/v1/"):
                try:
                    query = self._query(parsed.query)
                    if path == "/api/v1/projects":
                        result: Any = {"items": store.list_projects(query)}
                    elif path == "/api/v1/stages":
                        result = {"items": store.list_stages(str(query.get("project_id") or ""))}
                    elif path == "/api/v1/areas":
                        result = {"items": store.list_areas(str(query.get("project_id") or ""))}
                    elif path == "/api/v1/timeline":
                        if "limit" in query:
                            query["limit"] = int(query["limit"])
                        result = {"items": store.timeline(query)}
                    elif path == "/api/v1/dashboard":
                        result = store.dashboard(str(query.get("project_id") or ""))
                    else:
                        self._json(HTTPStatus.NOT_FOUND, {"error": {"code": "not_found"}})
                        return
                except (LedgerError, ValueError) as exc:
                    status = HTTPStatus(exc.status) if isinstance(exc, LedgerError) else HTTPStatus.BAD_REQUEST
                    code = exc.code if isinstance(exc, LedgerError) else "invalid_input"
                    self._json(status, {"error": {"code": code, "message": str(exc)}})
                else:
                    self._json(HTTPStatus.OK, {"version": 1, "result": result})
                return
            if path == "/internal/v1/status":
                if self._authorized():
                    self._json(HTTPStatus.OK, store.status())
                return
            if path == "/internal/v1/mcp/manifest":
                if self._authorized():
                    self._json(HTTPStatus.OK, business_manifest())
                return
            if path.startswith("/internal/v1/downloads/chart/"):
                if not self._authorized():
                    return
                reference = unquote(path.rsplit("/", 1)[-1])
                self._download(store.charts_dir, reference, "image/png")
                return
            if path == "/internal/v1/downloads/portable/current":
                if not self._authorized():
                    return
                target = store.share_dir / "current" / "kanhuwan-renovation-ledger.zip"
                self._file(HTTPStatus.OK, "application/zip", target)
                return
            self._json(HTTPStatus.NOT_FOUND, {"error": {"code": "not_found"}})

        def do_POST(self) -> None:  # noqa: N802
            path = urlsplit(self.path).path
            if not path.startswith("/internal/v1/") or not self._authorized():
                return
            payload = self._read_json()
            if payload is None:
                return
            try:
                if path == "/internal/v1/tools/call":
                    result = dispatch_tool(store, payload)
                elif path == "/internal/v1/admin/writer-mode":
                    target = payload.get("target")
                    if target == "suspended":
                        result = store.suspend_writer(str(payload.get("reason") or "admin_suspend"))
                    elif target == "read_only" and store.writer_mode() == "read_only":
                        result = {"previous": "read_only", "current": "read_only"}
                    else:
                        raise LedgerError(
                            "cutover_manifest_required",
                            "writer 状态只能通过 aiohttp cutover manifest API 推进",
                            status=409,
                        )
                else:
                    self._json(HTTPStatus.NOT_FOUND, {"error": {"code": "not_found"}})
                    return
            except LedgerError as exc:
                self._json(HTTPStatus(exc.status), {"error": {"code": exc.code, "message": str(exc)}})
            except Exception:
                self._json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": {"code": "internal_error", "message": "账本操作失败，未返回私有详情。"}})
            else:
                self._json(HTTPStatus.OK, {"version": 1, "result": result})

        def _authorized(self) -> bool:
            expected = f"Bearer {api_token}"
            actual = self.headers.get("Authorization", "")
            if not hmac.compare_digest(actual, expected):
                self._json(HTTPStatus.UNAUTHORIZED, {"error": {"code": "not_authorized"}})
                return False
            return True

        def _read_json(self) -> dict[str, Any] | None:
            if self.headers.get_content_type() != "application/json":
                self._json(HTTPStatus.UNSUPPORTED_MEDIA_TYPE, {"error": {"code": "content_type_required"}})
                return None
            try:
                length = int(self.headers.get("Content-Length", "-1"))
            except ValueError:
                length = -1
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

        def _download(self, root: Path, reference: str, content_type: str) -> None:
            if not reference or Path(reference).name != reference:
                self._json(HTTPStatus.BAD_REQUEST, {"error": {"code": "invalid_reference"}})
                return
            self._file(HTTPStatus.OK, content_type, root / reference)

        @staticmethod
        def _query(value: str) -> dict[str, Any]:
            return {key: items[-1] for key, items in parse_qs(value, keep_blank_values=False).items() if items}

        def _file(self, status: HTTPStatus, content_type: str, path: Path) -> None:
            try:
                resolved = path.resolve(strict=True)
            except FileNotFoundError:
                self._json(HTTPStatus.NOT_FOUND, {"error": {"code": "not_found"}})
                return
            if not resolved.is_file():
                self._json(HTTPStatus.NOT_FOUND, {"error": {"code": "not_found"}})
                return
            body = resolved.read_bytes()
            self._asset(status, content_type, body)

        def _asset(self, status: HTTPStatus, content_type: str, body: bytes) -> None:
            self.send_response(status)
            self._headers(content_type, len(body))
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
            self.send_header("Content-Security-Policy", "default-src 'self'; style-src 'unsafe-inline'; script-src 'none'; img-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'self'")

    return ThreadingHTTPServer((host, port), Handler)


def dispatch_tool(
    store: LedgerStore,
    payload: dict[str, Any],
    *,
    media: Any | None = None,
) -> dict[str, Any]:
    """Compatibility wrapper around the single business-tool registry."""

    return dispatch_business_tool(store, payload, media=media)


def render_dashboard(status: dict[str, Any]) -> str:
    counts = status["counts"]
    return f"""<!doctype html>
<html lang=\"zh-CN\"><head><meta charset=\"utf-8\"><meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">
<title>装修账本</title><style>body{{margin:0;background:#0b1220;color:#edf4ff;font:15px system-ui}}main{{max-width:900px;margin:auto;padding:24px}}.grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:12px}}.card{{background:#121c2e;border:1px solid #263751;border-radius:12px;padding:16px}}b{{font-size:24px;display:block;margin-top:8px}}.muted{{color:#91a4bd}}code{{color:#42d392}}</style></head>
<body><main><h1>装修账本</h1><p class=\"muted\">页面默认不展示金额、商家、备注和附件正文。</p><div class=\"grid\">
<div class=\"card\">Writer 模式<b><code>{status['writer_mode']}</code></b></div>
<div class=\"card\">有效付款<b>{counts['payments']}</b></div><div class=\"card\">有效退款<b>{counts['refunds']}</b></div>
<div class=\"card\">附件<b>{counts['attachments']}</b></div><div class=\"card\">审计事件<b>{counts['audit_events']}</b></div>
<div class=\"card\">便携镜像<b><code>{status['portable_export_state']}</code></b></div></div>
<h2>安全状态</h2><p>Schema <code>{status['schema_version']}</code> · 格式 <code>{status['format_id']}</code></p>
<p class=\"muted\">正式切换、恢复、导入和历史清理均需独立确认。</p></main></body></html>"""
