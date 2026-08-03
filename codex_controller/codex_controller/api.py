"""Chinese Ingress UI and authenticated internal job API."""

from __future__ import annotations

from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import hmac
import json
import re
from typing import Any
from urllib.parse import urlsplit

from .app_server import AppServerError
from .service import ControllerService
from .store import StoreError


JOB_PATH_RE = re.compile(r"^/internal/v1/jobs/([0-9a-f-]{36})$")


def create_server(
    host: str,
    port: int,
    *,
    service: ControllerService,
    api_token: str,
    max_request_bytes: int,
) -> ThreadingHTTPServer:
    class Handler(BaseHTTPRequestHandler):
        server_version = "CodexController/0.1"

        def log_message(self, _format: str, *_args: Any) -> None:
            return None

        def do_GET(self) -> None:  # noqa: N802
            path = urlsplit(self.path).path
            if path == "/healthz":
                self._json(HTTPStatus.OK, {"status": "ok", "ready": service.status()["ready"]})
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
            match = JOB_PATH_RE.fullmatch(path)
            if match:
                if not self._authorized():
                    return
                self._call(lambda: service.store.get_job(match.group(1)))
                return
            self._json(HTTPStatus.NOT_FOUND, {"error": {"code": "not_found"}})

        def do_POST(self) -> None:  # noqa: N802
            path = urlsplit(self.path).path
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
            if not path.startswith("/internal/v1/") or not self._authorized():
                return
            payload = self._read_json()
            if payload is None:
                return
            if path == "/internal/v1/jobs":
                self._call(lambda: service.submit(payload), status=HTTPStatus.ACCEPTED)
                return
            self._json(HTTPStatus.NOT_FOUND, {"error": {"code": "not_found"}})

        def _authorized(self) -> bool:
            expected = f"Bearer {api_token}"
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
                "default-src 'self'; style-src 'unsafe-inline'; script-src 'self'; connect-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'self'",
            )

    return ThreadingHTTPServer((host, port), Handler)


DASHBOARD_HTML = """<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Codex 控制器</title><style>body{margin:0;background:#0b1220;color:#edf4ff;font:15px system-ui}main{max-width:960px;margin:auto;padding:24px}.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(190px,1fr));gap:12px}.card{background:#121c2e;border:1px solid #263751;border-radius:12px;padding:16px}b{font-size:22px;display:block;margin-top:8px}.muted{color:#91a4bd}code{color:#42d392}button{border:0;border-radius:9px;padding:10px 14px;margin:4px;background:#2374e1;color:white;cursor:pointer}button.danger{background:#a63232}a{color:#7eb6ff}</style></head>
<body><main><h1>Codex 控制器</h1><p class="muted">官方 app-server · 显式双认证 · 多 Thread · 全局单活动 Turn</p>
<div class="grid"><div class="card">服务<b id="ready">加载中</b></div><div class="card">认证<b id="auth">加载中</b></div><div class="card">排队<b id="queued">-</b></div><div class="card">恢复核对<b id="recovery">-</b></div></div>
<h2>正式认证</h2><p id="authHelp">认证模式由 Add-on options 显式选择，禁止自动降级或混用。</p>
<button id="login">开始设备码登录</button><button id="cancel">取消登录</button><button id="retryApiKey">重试 API Key 登录</button><button class="danger" id="logout">退出登录</button>
<div class="card"><div id="loginInfo" class="muted">正在读取认证配置。</div></div>
<h2>安全状态</h2><p id="details" class="muted">加载中</p><script src="app.js"></script></main></body></html>"""


DASHBOARD_JS = r"""const q=id=>document.getElementById(id);
async function call(path){const r=await fetch(path,{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'});const j=await r.json();if(!r.ok)throw new Error(j.error?.message||j.error?.code||'请求失败');return j.result}
function showDeviceCode(p){const box=q('loginInfo');box.replaceChildren();if(!p){box.textContent='尚未生成设备码。';return}let url;try{url=new URL(p.verificationUrl)}catch(_){box.textContent='设备码响应无效。';return}if(url.protocol!=='https:'){box.textContent='设备码验证地址不是 HTTPS。';return}const link=document.createElement('a');link.target='_blank';link.rel='noreferrer';link.href=url.href;link.textContent=url.href;const code=document.createElement('code');code.textContent=p.userCode;box.append('打开 ',link,document.createElement('br'),'用户码：',code)}
function showApiKey(s){q('loginInfo').textContent=s.api_key_configured?'API Key 已通过 Add-on options 私密配置；页面不会显示 Key 内容。':'尚未在 Add-on options 配置 API Key。'}
async function refresh(){try{const r=await fetch('api/status',{cache:'no-store'}),s=await r.json(),a=s.app_server.account,api=s.configured_auth_mode==='api_key';q('ready').textContent=s.ready?'就绪':'未就绪';q('auth').textContent=a.auth_mode==='apiKey'?'API Key':a.auth_mode==='chatgpt'?'ChatGPT':'需要登录';q('queued').textContent=s.queue.jobs.queued;q('recovery').textContent=s.queue.jobs.recovery_required;q('login').hidden=api;q('cancel').hidden=api;q('retryApiKey').hidden=!api;q('authHelp').textContent=api?'当前选择 API Key。Key 只能在 Add-on options 中配置，页面不接收或显示秘密值。':'当前选择 ChatGPT Device Code。HAOS 使用独立会话，不复制本机凭据。';api?showApiKey(s):showDeviceCode(s.pending_login);q('details').textContent=`Codex ${s.codex_version} · 模式 ${s.configured_auth_mode} · 认证错误 ${s.auth_error||'无'} · intake ${s.intake_enabled?'已启用':'关闭'} · Thread ${s.queue.threads} · app-server ${s.app_server.running?'运行':'停止'}`}catch(e){q('details').textContent=e.message}}
q('login').onclick=async()=>{try{await call('api/auth/device/start');await refresh()}catch(e){alert(e.message)}};
q('cancel').onclick=async()=>{try{await call('api/auth/device/cancel');await refresh()}catch(e){alert(e.message)}};
q('retryApiKey').onclick=async()=>{try{await call('api/auth/api-key/retry');await refresh()}catch(e){alert(e.message)}};
q('logout').onclick=async()=>{if(confirm('确认退出 HAOS Controller 的独立 Codex 会话？'))try{await call('api/auth/logout');await refresh()}catch(e){alert(e.message)}};
refresh();setInterval(refresh,3000);"""
