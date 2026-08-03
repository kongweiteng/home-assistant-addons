"""Chinese Ingress status, QR/migration controls and one-time attachment API."""

from __future__ import annotations

import asyncio
import base64
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import hmac
import json
import re
from typing import Any
from urllib.parse import unquote, urlsplit

from .protocol import ProtocolError
from .service import GatewayService
from .store import StoreError


ATTACHMENT_RE = re.compile(r"^/internal/v1/attachments/([A-Za-z0-9_-]{32,128})$")


def create_server(
    host: str,
    port: int,
    *,
    service: GatewayService,
    loop: asyncio.AbstractEventLoop,
    attachment_api_token: str,
    max_request_bytes: int = 1024 * 1024,
) -> ThreadingHTTPServer:
    class Handler(BaseHTTPRequestHandler):
        server_version = "WeixinGateway/0.1"

        def log_message(self, _format: str, *_args: Any) -> None:
            return None

        def do_GET(self) -> None:  # noqa: N802
            path = urlsplit(self.path).path
            if path == "/healthz":
                self._json(HTTPStatus.OK, {"status": "ok", "poller_state": service.poller_state})
                return
            if path in {"", "/", "/index.html"}:
                self._asset(HTTPStatus.OK, "text/html; charset=utf-8", DASHBOARD_HTML.encode("utf-8"))
                return
            if path == "/app.js":
                self._asset(HTTPStatus.OK, "text/javascript; charset=utf-8", DASHBOARD_JS.encode("utf-8"))
                return
            if path == "/api/status":
                self._json(HTTPStatus.OK, service.status())
                return
            if path == "/api/qr/image":
                qr = service.qr_image_path
                if qr.is_file() and not qr.is_symlink():
                    self._asset(HTTPStatus.OK, "image/png", qr.read_bytes())
                else:
                    self._json(HTTPStatus.NOT_FOUND, {"error": {"code": "qr_not_found"}})
                return
            match = ATTACHMENT_RE.fullmatch(path)
            if match:
                if not self._authorized():
                    return
                try:
                    metadata, content = service.store.consume_attachment(unquote(match.group(1)))
                except StoreError as exc:
                    self._json(HTTPStatus(exc.status), {"error": {"code": exc.code, "message": str(exc)}})
                    return
                self.send_response(HTTPStatus.OK)
                self._headers(metadata["mime_type"], len(content))
                self.send_header("X-Attachment-Filename", base64.urlsafe_b64encode(metadata["original_filename"].encode("utf-8")).decode("ascii"))
                self.send_header("X-Attachment-Sha256", metadata["sha256"])
                self.end_headers()
                self.wfile.write(content)
                return
            self._json(HTTPStatus.NOT_FOUND, {"error": {"code": "not_found"}})

        def do_POST(self) -> None:  # noqa: N802
            path = urlsplit(self.path).path
            payload = self._read_json(allow_empty=path == "/api/qr/start")
            if payload is None:
                return
            if path == "/api/qr/start":
                self._async_call(service.start_qr_login())
                return
            if path == "/api/migration/inspect":
                self._call(lambda: service.inspect_migration(str(payload.get("package_ref") or "")))
                return
            if path == "/api/migration/import":
                self._call(
                    lambda: service.import_migration(
                        str(payload.get("package_ref") or ""), str(payload.get("one_time_key") or "")
                    )
                )
                return
            self._json(HTTPStatus.NOT_FOUND, {"error": {"code": "not_found"}})

        def _authorized(self) -> bool:
            expected = f"Bearer {attachment_api_token}"
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

        def _async_call(self, coroutine: Any) -> None:
            try:
                future = asyncio.run_coroutine_threadsafe(coroutine, loop)
                result = future.result(timeout=45)
            except (StoreError, ProtocolError) as exc:
                self._json(HTTPStatus(getattr(exc, "status", 409)), {"error": {"code": exc.code, "message": str(exc)}})
            except Exception:
                self._json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": {"code": "internal_error", "message": "微信网关操作失败，未返回私有详情。"}})
            else:
                self._json(HTTPStatus.OK, {"version": 1, "result": result})

        def _call(self, callback: Any) -> None:
            try:
                result = callback()
            except (StoreError, ProtocolError) as exc:
                self._json(HTTPStatus(getattr(exc, "status", 409)), {"error": {"code": exc.code, "message": str(exc)}})
            except Exception:
                self._json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": {"code": "internal_error", "message": "微信网关操作失败，未返回私有详情。"}})
            else:
                self._json(HTTPStatus.OK, {"version": 1, "result": result})

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
            self.send_header(
                "Content-Security-Policy",
                "default-src 'self'; style-src 'unsafe-inline'; script-src 'self'; connect-src 'self'; img-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'self'",
            )

    return ThreadingHTTPServer((host, port), Handler)


DASHBOARD_HTML = """<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>微信网关</title>
<style>body{margin:0;background:#0b1220;color:#edf4ff;font:15px system-ui}main{max-width:960px;margin:auto;padding:24px}.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:12px}.card{background:#121c2e;border:1px solid #263751;border-radius:12px;padding:16px}b{font-size:22px;display:block;margin-top:8px}.muted{color:#91a4bd}code{color:#42d392}button{border:0;border-radius:9px;padding:10px 14px;background:#2374e1;color:white;cursor:pointer}img{max-width:320px;background:white;padding:12px;border-radius:12px}</style></head>
<body><main><h1>微信网关</h1><p class="muted">最小 iLink 传输层；模型、账本和 HA 权限均不在本 Add-on。</p>
<div class="grid"><div class="card">Poller<b id="poller">加载中</b></div><div class="card">身份<b id="identity">加载中</b></div><div class="card">待提交<b id="pending">-</b></div><div class="card">待回复<b id="submitted">-</b></div></div>
<h2>二维码备用登录</h2><p>正式迁移优先使用加密身份包。二维码可能产生不同身份，必须重新做微信 E2E 验收。</p><button id="qrStart">生成二维码</button><p id="qrState" class="muted">尚未生成</p><img id="qrImage" hidden alt="iLink 登录二维码">
<h2>安全状态</h2><p id="details" class="muted">加载中</p><script src="app.js"></script></main></body></html>"""


DASHBOARD_JS = r"""const q=id=>document.getElementById(id);async function refresh(){try{const r=await fetch('api/status',{cache:'no-store'}),s=await r.json();q('poller').textContent=s.poller_state;q('identity').textContent=s.identity?'已就绪':'缺少';q('pending').textContent=s.queue.messages.pending_controller;q('submitted').textContent=s.queue.messages.controller_submitted;q('qrState').textContent=s.qr.state;q('qrImage').hidden=!s.qr.has_image;if(s.qr.has_image)q('qrImage').src='api/qr/image?'+Date.now();q('details').textContent=`Controller ${s.controller_configured?'已配置':'未配置'} · allowlist ${s.identity?.allowed_user_count??0} · context ${s.identity?.context_count??0} · spool ${s.queue.spool_bytes} bytes · error ${s.last_error||'无'}`}catch(e){q('details').textContent=e.message}}q('qrStart').onclick=async()=>{try{const r=await fetch('api/qr/start',{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'}),j=await r.json();if(!r.ok)throw new Error(j.error?.message||'生成失败');await refresh()}catch(e){alert(e.message)}};refresh();setInterval(refresh,3000);"""
