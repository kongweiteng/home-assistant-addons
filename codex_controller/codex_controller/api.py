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
from .service import ControllerService
from .store import StoreError


JOB_PATH_RE = re.compile(r"^/internal/v1/jobs/([0-9a-f-]{36})$")
RECOVERY_PATH_RE = re.compile(r"^/internal/v1/jobs/([0-9a-f-]{36})/recovery-resolution$")
TOOL_PATH_RE = re.compile(r"^/api/tools/([a-z0-9_]{1,96})$")


def create_server(
    host: str,
    port: int,
    *,
    service: ControllerService,
    api_token: str,
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
        server_version = "CodexController/0.2.0"

        def log_message(self, _format: str, *_args: Any) -> None:
            return None

        def do_GET(self) -> None:  # noqa: N802
            path = urlsplit(self.path).path
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
            if path == "/app.js":
                self._asset(HTTPStatus.OK, "text/javascript; charset=utf-8", DASHBOARD_JS.encode("utf-8"))
                return
            if path == "/api/status":
                self._json(HTTPStatus.OK, {**service.status(), **csrf_document()})
                return
            if path == "/api/tools":
                self._json(HTTPStatus.OK, {"version": 1, "result": service.tool_status()})
                return
            if path == "/internal/v1/capabilities":
                if not self._authorized():
                    return
                self._call(service.capabilities)
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
<title>Codex 控制器</title><style>
:root{color-scheme:dark;--bg:#0b1220;--card:#121c2e;--line:#263751;--text:#edf4ff;--muted:#91a4bd;--blue:#4f9cff;--green:#42d392;--amber:#f1b84b;--red:#ef6b73}*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);font:15px system-ui,-apple-system,sans-serif}main{max-width:1240px;margin:auto;padding:24px}.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:12px}.card{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:16px}.metric{font-size:24px;font-weight:720;display:block;margin-top:8px}.muted{color:var(--muted)}code{color:var(--green)}button,select{border:1px solid #375174;border-radius:9px;padding:9px 12px;background:#17253a;color:var(--text)}button{cursor:pointer;background:#2374e1;border-color:#2374e1}button.danger{background:#9d3035;border-color:#9d3035}button.toggle{min-width:76px;background:#263751;border-color:#375174}button.toggle.on{background:#176c50;border-color:#2a9d75}button:disabled{opacity:.5;cursor:not-allowed}a{color:#7eb6ff}.toolbar{display:flex;flex-wrap:wrap;gap:10px;align-items:center;margin:14px 0}.notice{border-left:4px solid var(--blue);padding:12px 14px}.table-wrap{overflow:auto;border:1px solid var(--line);border-radius:14px}table{width:100%;border-collapse:collapse;min-width:980px;background:var(--card)}th,td{text-align:left;padding:12px;border-bottom:1px solid var(--line);vertical-align:top}th{color:#b8c8dc;background:#101a2a;position:sticky;top:0}.tool-name{font-weight:700}.technical{font:12px ui-monospace,SFMono-Regular,monospace;color:var(--muted);margin-top:4px}.badges{display:flex;flex-wrap:wrap;gap:5px}.badge{font-size:12px;border:1px solid #3a4c65;border-radius:999px;padding:3px 7px;color:#c7d4e5}.badge.good{border-color:#28775d;color:#69deb3}.badge.warn{border-color:#8c6723;color:#f6cb72}.badge.bad{border-color:#8f3d44;color:#ff969c}.intent{max-width:310px;white-space:normal}.error{color:#ff969c}.success{color:#69deb3}@media(max-width:700px){main{padding:14px}h1{font-size:25px}}
</style></head><body><main>
<h1>Codex 控制器</h1><p class="muted">官方 app-server · 多 Thread · 全局单活动 Turn · MCP 工具服务端门禁</p>
<div class="grid"><div class="card">服务<span class="metric" id="ready">加载中</span></div><div class="card">认证<span class="metric" id="auth">加载中</span></div><div class="card">排队<span class="metric" id="queued">-</span></div><div class="card">实际发布工具<span class="metric" id="published">-</span></div><div class="card">当前 Thread<span class="metric" id="threadShort">无活动</span></div></div>
<h2>正式认证</h2><p id="authHelp">认证模式由 Add-on options 显式选择，禁止自动降级或混用。</p>
<button id="login">开始设备码登录</button><button id="cancel">取消登录</button><button id="retryApiKey">重试 API Key 登录</button><button class="danger" id="logout">退出登录</button>
<div class="card"><div id="loginInfo" class="muted">正在读取认证配置。</div></div>
<h2>MCP 工具控制台</h2><div class="card notice">这里显示 Controller 已知工具、内部服务配置、管理员策略、MCP 进程真实 <code>tools/list</code> 心跳和当前可调用状态。意图示例不是固定关键词，Codex 会根据完整语义决定是否调用工具。</div>
<div class="toolbar"><label>服务 <select id="serviceFilter"><option value="all">全部</option><option value="renovation_hub">Renovation Hub</option><option value="ha_operations_broker">Operations Broker</option></select></label><label>类型 <select id="riskFilter"><option value="all">全部</option><option value="read_only">只读</option><option value="write">写入</option><option value="controlled">受控操作</option></select></label><button id="reloadTools">刷新工具状态</button><span id="toolFeedback" class="muted"></span></div>
<div class="table-wrap"><table><thead><tr><th>工具</th><th>服务 / 风险</th><th>状态</th><th>意图示例</th><th>最近调用</th><th>开关</th></tr></thead><tbody id="toolRows"></tbody></table></div>
<h2>安全状态</h2><p id="details" class="muted">加载中</p><script src="app.js"></script></main></body></html>"""


DASHBOARD_JS = r"""const q=id=>document.getElementById(id);let csrf='',catalog=null,statusDoc=null;
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
async function refresh(){try{const s=await jsonFetch('api/status');statusDoc=s;csrf=s.csrf_token;const a=s.app_server.account,api=s.configured_auth_mode==='api_key';q('ready').textContent=s.ready?'就绪':'未就绪';q('auth').textContent=a.auth_mode==='apiKey'?'API Key':a.auth_mode==='chatgpt'?'ChatGPT':'需要登录';q('queued').textContent=s.queue.jobs.queued;q('threadShort').textContent=s.queue.active_job?.thread_short||'无活动';q('login').hidden=api;q('cancel').hidden=api;q('retryApiKey').hidden=!api;q('authHelp').textContent=api?'当前选择 API Key。URL、模型和 Key 只能在 Add-on options 中配置，页面不接收或显示秘密值。':'当前选择 ChatGPT Device Code。HAOS 使用独立会话，不复制本机凭据。';api?showApiKey(s):showDeviceCode(s.pending_login);q('details').textContent=`Controller ${s.version} · Codex ${s.codex_version} · intake ${s.intake_enabled?'已启用':'关闭'} · Thread ${s.queue.threads} · 已知工具 ${s.tools.known} · 已配置 ${s.tools.configured} · 策略开启 ${s.tools.enabled} · MCP 心跳 ${s.tools.mcp.observed_at||'未观测'} · 策略错误 ${s.tools.policy_error||'无'} · app-server ${s.app_server.running?'运行':'停止'}`;await refreshTools()}catch(e){q('details').textContent=e.message}}
q('serviceFilter').onchange=renderTools;q('riskFilter').onchange=renderTools;q('reloadTools').onclick=refresh;q('login').onclick=async()=>{try{await call('api/auth/device/start');await refresh()}catch(e){alert(e.message)}};q('cancel').onclick=async()=>{try{await call('api/auth/device/cancel');await refresh()}catch(e){alert(e.message)}};q('retryApiKey').onclick=async()=>{try{await call('api/auth/api-key/retry');await refresh()}catch(e){alert(e.message)}};q('logout').onclick=async()=>{if(confirm('确认退出 HAOS Controller 的独立 Codex 会话？'))try{await call('api/auth/logout');await refresh()}catch(e){alert(e.message)}};refresh();setInterval(refresh,5000);"""
