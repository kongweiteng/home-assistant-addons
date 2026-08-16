"""Chinese Ingress status, QR/migration controls and one-time attachment API."""

from __future__ import annotations

import asyncio
import base64
import hashlib
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import hmac
import json
import re
import secrets
import shutil
import time
from typing import Any
from urllib.parse import unquote, urlsplit

from .protocol import ProtocolError
from .service import GatewayService
from .store import StoreError


ATTACHMENT_RE = re.compile(r"^/internal/v1/attachments/([A-Za-z0-9_-]{32,128})$")
ATTACHMENT_PREVIEW_RE = re.compile(r"^/internal/v1/attachments/([A-Za-z0-9_-]{32,128})/preview$")
ATTACHMENT_STREAM_RE = re.compile(r"^/internal/v1/attachments/([A-Za-z0-9_-]{32,128})/stream$")
ATTACHMENT_ACK_RE = re.compile(r"^/internal/v1/attachments/([A-Za-z0-9_-]{32,128})/ack$")
USER_RE = re.compile(r"^/api/users/(WX-[A-Z2-7]{10})$")
USER_ACTION_RE = re.compile(r"^/api/users/(WX-[A-Z2-7]{10})/(suspend|resume|revoke)$")
INVITATION_CANCEL_RE = re.compile(r"^/api/users/invitations/(IV-[A-Z2-7]{10})/cancel$")
ONBOARDING_VERIFY_RE = re.compile(r"^/api/onboarding/(OB-[A-Z2-7]{10})/verify$")
ONBOARDING_CANCEL_RE = re.compile(r"^/api/onboarding/(OB-[A-Z2-7]{10})/cancel$")
CSRF_BUCKET_SECONDS = 15 * 60


def create_server(
    host: str,
    port: int,
    *,
    service: GatewayService,
    loop: asyncio.AbstractEventLoop,
    attachment_api_token: str,
    max_request_bytes: int = 1024 * 1024,
) -> ThreadingHTTPServer:
    csrf_secret = secrets.token_bytes(32)

    def csrf_token(bucket: int | None = None) -> str:
        current = int(time.time() // CSRF_BUCKET_SECONDS) if bucket is None else bucket
        return base64.urlsafe_b64encode(
            hmac.new(csrf_secret, f"gateway-admin:{current}".encode("ascii"), hashlib.sha256).digest()
        ).decode("ascii").rstrip("=")

    class Handler(BaseHTTPRequestHandler):
        server_version = "WeixinGateway/0.4.5"

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
                document = service.status()
                document["csrf_token"] = csrf_token()
                self._json(HTTPStatus.OK, document)
                return
            if path == "/api/users":
                self._json(HTTPStatus.OK, {"version": 1, "result": service.users()})
                return
            if path == "/api/conversations":
                self._json(HTTPStatus.OK, {"version": 1, "result": service.conversations()})
                return
            if path == "/api/qr/image":
                qr = service.qr_image_path
                if qr.is_file() and not qr.is_symlink():
                    self._asset(HTTPStatus.OK, "image/png", qr.read_bytes())
                else:
                    self._json(HTTPStatus.NOT_FOUND, {"error": {"code": "qr_not_found"}})
                return
            if path == "/api/onboarding/qr/image":
                qr = service.member_qr_image_path
                if qr.is_file() and not qr.is_symlink():
                    self._asset(HTTPStatus.OK, "image/png", qr.read_bytes())
                else:
                    self._json(HTTPStatus.NOT_FOUND, {"error": {"code": "qr_not_found"}})
                return
            preview_match = ATTACHMENT_PREVIEW_RE.fullmatch(path)
            if preview_match:
                if not self._authorized():
                    return
                try:
                    metadata, content = service.store.preview_attachment(unquote(preview_match.group(1)))
                except StoreError as exc:
                    self._json(HTTPStatus(exc.status), {"error": {"code": exc.code, "message": str(exc)}})
                    return
                self._attachment(metadata, content)
                return
            stream_match = ATTACHMENT_STREAM_RE.fullmatch(path)
            if stream_match:
                if not self._authorized():
                    return
                handle = None
                try:
                    metadata, handle = service.store.open_stream_attachment(unquote(stream_match.group(1)))
                    self._stream_attachment(metadata, handle)
                except StoreError as exc:
                    self._json(HTTPStatus(exc.status), {"error": {"code": exc.code, "message": str(exc)}})
                finally:
                    if handle is not None:
                        handle.close()
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
                self._attachment(metadata, content)
                return
            self._json(HTTPStatus.NOT_FOUND, {"error": {"code": "not_found"}})

        def do_POST(self) -> None:  # noqa: N802
            path = urlsplit(self.path).path
            ack_match = ATTACHMENT_ACK_RE.fullmatch(path)
            if ack_match:
                if not self._authorized():
                    return
                payload = self._read_json()
                if payload is None:
                    return
                self._call(
                    lambda: service.store.acknowledge_attachment(
                        unquote(ack_match.group(1)),
                        str(payload.get("sha256") or ""),
                    )
                )
                return
            if path.startswith("/api/") and not self._csrf_valid():
                return
            payload = self._read_json(allow_empty=path in {"/api/qr/start", "/api/owner-pairing/start"})
            if payload is None:
                return
            if path == "/api/qr/start":
                self._async_call(service.start_qr_login())
                return
            if path == "/api/qr/verify":
                self._call(lambda: service.submit_qr_verify_code(payload))
                return
            if path == "/api/owner-pairing/start":
                self._call(service.start_owner_pairing)
                return
            if path == "/api/poller/start":
                self._async_call(service.start_poller(payload))
                return
            if path == "/api/poller/stop":
                self._async_call(service.stop_poller(payload))
                return
            if path == "/api/poller/maintenance/pause":
                self._async_call(service.pause_poller_maintenance(payload))
                return
            if path == "/api/poller/maintenance/resume":
                self._async_call(service.resume_poller_maintenance(payload))
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
            if path == "/api/users/invitations":
                self._call(lambda: service.create_member_invitation(payload))
                return
            if path == "/api/onboarding/start":
                self._async_call(service.start_member_onboarding(payload))
                return
            onboarding_verify = ONBOARDING_VERIFY_RE.fullmatch(path)
            if onboarding_verify:
                self._call(
                    lambda: service.submit_member_onboarding_verify_code(
                        onboarding_verify.group(1),
                        payload,
                    )
                )
                return
            onboarding_cancel = ONBOARDING_CANCEL_RE.fullmatch(path)
            if onboarding_cancel:
                self._async_call(service.cancel_member_onboarding(onboarding_cancel.group(1), payload))
                return
            invitation_match = INVITATION_CANCEL_RE.fullmatch(path)
            if invitation_match:
                self._call(lambda: service.cancel_member_invitation(invitation_match.group(1), payload))
                return
            action_match = USER_ACTION_RE.fullmatch(path)
            if action_match:
                self._async_call(service.change_user_state(action_match.group(1), action_match.group(2), payload))
                return
            if path == "/api/owner-transfer":
                self._async_call(service.transfer_owner(payload))
                return
            self._json(HTTPStatus.NOT_FOUND, {"error": {"code": "not_found"}})

        def do_PATCH(self) -> None:  # noqa: N802
            path = urlsplit(self.path).path
            if not self._csrf_valid():
                return
            payload = self._read_json()
            if payload is None:
                return
            match = USER_RE.fullmatch(path)
            if match:
                self._call(lambda: service.update_user_alias(match.group(1), payload))
                return
            self._json(HTTPStatus.NOT_FOUND, {"error": {"code": "not_found"}})

        def _csrf_valid(self) -> bool:
            actual = self.headers.get("X-CSRF-Token", "")
            bucket = int(time.time() // CSRF_BUCKET_SECONDS)
            if not any(hmac.compare_digest(actual, csrf_token(candidate)) for candidate in (bucket, bucket - 1)):
                self._json(HTTPStatus.FORBIDDEN, {"error": {"code": "csrf_invalid", "message": "页面令牌已失效，请刷新后重试。"}})
                return False
            return True

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

        def _attachment(self, metadata: dict[str, Any], content: bytes) -> None:
            self.send_response(HTTPStatus.OK)
            self._headers(metadata["mime_type"], len(content))
            self.send_header(
                "X-Attachment-Filename",
                base64.urlsafe_b64encode(metadata["original_filename"].encode("utf-8")).decode("ascii"),
            )
            self.send_header("X-Attachment-Sha256", metadata["sha256"])
            self.end_headers()
            self.wfile.write(content)

        def _stream_attachment(self, metadata: dict[str, Any], handle: Any) -> None:
            self.send_response(HTTPStatus.OK)
            self._headers(metadata["mime_type"], metadata["size_bytes"])
            self.send_header(
                "X-Attachment-Filename",
                base64.urlsafe_b64encode(metadata["original_filename"].encode("utf-8")).decode("ascii"),
            )
            self.send_header("X-Attachment-Sha256", metadata["sha256"])
            self.end_headers()
            shutil.copyfileobj(handle, self.wfile, length=1024 * 1024)

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
<style>body{margin:0;background:#0b1220;color:#edf4ff;font:15px system-ui}main{max-width:1180px;margin:auto;padding:24px}.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:12px}.card,.panel{background:#121c2e;border:1px solid #263751;border-radius:12px;padding:16px}.panel{margin-top:16px}.panel-head,.form-row{display:flex;align-items:flex-start;justify-content:space-between;gap:12px}.panel-head h2{margin:7px 0 0}.form-row{justify-content:flex-start;align-items:center;flex-wrap:wrap}.scope{display:inline-block;padding:3px 8px;border-radius:999px;background:#263751;color:#b9c9dd;font-size:12px}.scope.identity{background:#253f65;color:#bcd9ff}.scope.user{background:#214c42;color:#a8ead6}.summary{padding:12px;border:1px solid #263751;border-radius:10px;background:#0d1728}.notice{padding:10px 12px;border-left:3px solid #ffcb6b;background:#1b2230}b{font-size:22px;display:block;margin-top:8px}.muted{color:#91a4bd}.ok{color:#42d392}.warn{color:#ffcb6b}.error{color:#ff7b8b}code{color:#42d392;overflow-wrap:anywhere}button,input{border-radius:9px;padding:9px 12px;font:inherit}button{border:0;background:#2374e1;color:white;cursor:pointer;margin:2px}button.secondary{background:#314158}button.danger{background:#a83c4b}button:disabled{opacity:.45;cursor:not-allowed}input{border:1px solid #40516a;background:#0d1728;color:#edf4ff;min-width:220px}table{width:100%;border-collapse:collapse;min-width:820px}th,td{text-align:left;padding:10px 8px;border-bottom:1px solid #263751;vertical-align:top;white-space:pre-line}.scroll{overflow:auto}.badge{display:inline-block;padding:3px 7px;border-radius:999px;background:#263751;font-size:12px}img{max-width:min(320px,calc(100% - 24px));background:white;padding:12px;border-radius:12px}.actions{display:flex;flex-wrap:wrap;gap:4px}.banner{min-height:22px;margin:10px 0}[hidden]{display:none!important}@media(max-width:640px){main{padding:16px}.panel-head{display:block}.panel-head .badge{margin-top:10px}}</style></head>
<body><main><h1>微信网关</h1><p class="muted">一人一个 ClawBot，多身份共用同一 Controller/Codex；每个身份独立 Poller、会话路由和故障状态。</p><p id="banner" class="banner muted">正在加载安全状态…</p>
<div class="grid"><div class="card">Owner Poller<b id="poller">加载中</b></div><div class="card">Poller 配置<b id="pollerDesired">加载中</b></div><div class="card">ClawBot 身份<b id="identityCount">-</b></div><div class="card">Owner 状态<b id="pairing">加载中</b></div><div class="card">有效用户<b id="activeUsers">-</b></div><div class="card">待提交<b id="pending">-</b></div><div class="card">待回复<b id="submitted">-</b></div></div>
<section class="panel"><div class="panel-head"><div><span class="scope identity">Owner 身份</span><h2>Owner ClawBot</h2></div><span id="identityAccount" class="badge">尚未登录</span></div><p>这里仅初始化或重新认证当前 Owner 的 ClawBot。已有 Owner 时只接受同一 ClawBot、同一 Owner 扫码，不会清空成员、会话或历史任务。</p><p id="identityStatus" class="muted">正在读取身份状态…</p><button id="qrStart">扫码登录 Owner ClawBot</button><p id="qrState" class="muted">尚未生成二维码</p><img id="qrImage" hidden alt="Owner ClawBot 登录二维码"><div id="ownerVerify" class="form-row" hidden><input id="ownerVerifyCode" inputmode="numeric" autocomplete="one-time-code" placeholder="输入微信显示的数字"><button id="ownerVerifySubmit">提交验证码</button></div></section>
<section class="panel" id="ownerSetupPanel"><div class="panel-head"><div><span class="scope user">首次初始化</span><h2>绑定 Owner</h2></div><span id="ownerSetupState" class="badge">等待状态</span></div><p>第一个向 Owner ClawBot 私聊发送正确绑定码的微信用户成为 Owner；绑定消息不会进入 Codex。</p><p id="ownerSetupText" class="muted">正在读取状态…</p><button id="pairStart">生成一次性绑定码</button><p id="pairCodeRow" hidden>本次绑定码：<code id="pairCode">尚未生成</code></p><p id="pairExpiry" class="muted"></p></section>
<section class="panel" id="currentOwnerPanel" hidden><div class="panel-head"><div><span class="scope user">权限主体</span><h2>当前 Owner</h2></div><span class="badge ok">已绑定</span></div><p id="currentOwner" class="summary">正在读取当前 Owner…</p><p class="muted">Owner 转移只改变权限与主动通知目标；新的 Owner 必须已有可用的独立 ClawBot。</p></section>
<section class="panel"><div class="panel-head"><div><span class="scope identity">成员接入</span><h2>添加成员 ClawBot</h2></div><span id="identityLimit" class="badge">读取上限…</span></div><p>Owner 生成二维码和一次性接入码后，将二维码私发给成员。成员用自己的微信扫码，再向新 ClawBot 发送接入码；扫码人和发送人必须一致。</p><div class="form-row"><input id="memberAlias" maxlength="40" placeholder="成员别名，例如：张三"><button id="onboardingStart">生成成员接入二维码</button><button id="onboardingCancel" class="secondary" disabled>取消本次接入</button></div><p>一次性接入码：<code id="onboardingCode">尚未生成</code></p><p id="onboardingState" class="muted">暂无进行中的接入。</p><img id="onboardingImage" hidden alt="成员 ClawBot 接入二维码"><div id="memberVerify" class="form-row" hidden><input id="memberVerifyCode" inputmode="numeric" autocomplete="one-time-code" placeholder="输入成员微信显示的数字"><button id="memberVerifySubmit">提交验证码</button></div><p class="notice warn">接入码明文只在创建成功时显示一次。二维码过期或验证码多次错误会失败关闭，不会自动改投其他身份。</p></section>
<section class="panel"><div class="panel-head"><div><span class="scope identity">运行时</span><h2>ClawBot 身份</h2></div><div class="actions"><button id="pollerStart">开启全部 Poller</button><button id="pollerStop" class="danger">关闭全部 Poller</button></div></div><p id="pollerControlText" class="muted">Poller 控制状态读取中…</p><div class="scroll"><table><thead><tr><th>身份</th><th>身份状态</th><th>Poller</th><th>绑定用户</th><th>最近活动/错误</th></tr></thead><tbody id="identitiesBody"></tbody></table></div></section>
<section class="panel"><div class="panel-head"><div><span class="scope user">用户级权限</span><h2>用户与权限</h2></div><span id="userLimit" class="badge">最多 32 人</span></div><div class="scroll"><table><thead><tr><th>用户</th><th>角色/状态</th><th>ClawBot</th><th>会话标识</th><th>Thread</th><th>最近活动</th><th>操作</th></tr></thead><tbody id="usersBody"></tbody></table></div><p class="muted">页面只展示脱敏短标识，不展示原始微信 ID、Token、context、完整 conversation key 或 Thread/job ID。</p></section>
<section class="panel"><div class="panel-head"><div><span class="scope user">会话排障</span><h2>会话</h2></div></div><div class="scroll"><table><thead><tr><th>用户</th><th>会话</th><th>Thread</th><th>最近作业</th><th>最近活动</th></tr></thead><tbody id="conversationsBody"></tbody></table></div></section>
<section class="panel"><h2>安全状态</h2><p id="details" class="muted">加载中</p></section><script src="app.js"></script></main></body></html>"""


DASHBOARD_JS = r"""const q=id=>document.getElementById(id);let csrf='',revision=0,pollerRevision=0,currentPairCode='',currentIdentityPresent=false,currentOnboarding='';
const requestId=()=>globalThis.crypto?.randomUUID?.().replaceAll('-','')||`${Date.now()}-${Math.random().toString(36).slice(2)}`;
const cell=text=>{const td=document.createElement('td');td.textContent=text??'—';return td};
const button=(label,kind,handler)=>{const value=document.createElement('button');value.textContent=label;if(kind)value.className=kind;value.onclick=handler;return value};
const formatTime=value=>{if(!value)return'';const date=new Date(value);return Number.isNaN(date.getTime())?value:date.toLocaleString('zh-CN',{hour12:false})};
function banner(message,kind='muted'){q('banner').textContent=message;q('banner').className=`banner ${kind}`}
async function readJson(path){const response=await fetch(path,{cache:'no-store'}),document=await response.json();if(!response.ok)throw new Error(document.error?.message||document.error?.code||'读取失败');return document.result??document}
async function mutate(path,method,payload){const response=await fetch(path,{method,headers:{'Content-Type':'application/json','X-CSRF-Token':csrf},body:JSON.stringify(payload)}),document=await response.json();if(!response.ok)throw new Error(document.error?.message||document.error?.code||'操作失败');return document.result}
async function userAction(user,action){try{const result=await mutate(`api/users/${user.wx_short}/${action}`,'POST',{revision,request_id:requestId()});revision=result.revision;banner('用户状态已更新','ok');await refresh()}catch(error){banner(error.message,'error')}}
async function rename(user){const alias=prompt('输入新的用户别名',user.alias);if(alias===null)return;try{const result=await mutate(`api/users/${user.wx_short}`,'PATCH',{alias,revision,request_id:requestId()});revision=result.revision;banner('别名已更新','ok');await refresh()}catch(error){banner(error.message,'error')}}
async function transfer(user){const confirmation=prompt(`确认把 Owner 转移给 ${user.alias}？请输入 TRANSFER_OWNER`,'');if(confirmation===null)return;try{const result=await mutate('api/owner-transfer','POST',{target_wx_short:user.wx_short,confirmation,revision,request_id:requestId()});revision=result.revision;banner('Owner 已转移，主动通知目标同步更新','ok');await refresh()}catch(error){banner(error.message,'error')}}
async function pollerAction(action){try{const result=await mutate(`api/poller/${action}`,'POST',{revision:pollerRevision,request_id:requestId()});pollerRevision=result.revision;banner(action==='start'?'全部 Poller 已开启':'全部 Poller 已关闭','ok');await refresh()}catch(error){banner(error.message,'error');await refresh()}}
async function startOnboarding(user=null){const alias=user?.alias||q('memberAlias').value.trim();if(!alias){banner('请先填写成员别名','warn');return}try{const result=await mutate('api/onboarding/start','POST',{alias,target_wx_short:user?.wx_short||null,revision,request_id:requestId(),ttl_seconds:900});revision=result.revision;currentOnboarding=result.session_short;q('onboardingCode').textContent=result.code||'请求已处理，接入码明文不会再次显示';banner(user?'已为现有成员生成独立 ClawBot 接入二维码':'成员接入二维码已生成，请通过可信渠道私发','warn');await refresh()}catch(error){banner(error.message,'error')}}
function renderUsers(users){const body=q('usersBody');body.replaceChildren();if(!users.length){const row=document.createElement('tr');const td=cell('尚无用户');td.colSpan=7;row.append(td);body.append(row);return}for(const user of users){const row=document.createElement('tr');row.append(cell(`${user.alias}\n${user.wx_short}`),cell(`${user.role} / ${user.status}${user.has_context?' / context 已有':' / context 缺少'}`),cell(`${user.identity_short||'未绑定'}\n${user.binding_type||'—'} / ${user.identity_state||'—'} / ${user.identity_runtime_state||'—'}`),cell(user.conversation_short),cell(user.thread_short),cell(user.last_seen_at));const actions=document.createElement('td'),wrap=document.createElement('div');wrap.className='actions';wrap.append(button('改名','secondary',()=>rename(user)));if(user.role==='member'&&user.status==='active'){if(user.binding_type==='legacy_shared')wrap.append(button('绑定独立 ClawBot','',()=>startOnboarding(user)));wrap.append(button('暂停','secondary',()=>userAction(user,'suspend')),button('移除','danger',()=>userAction(user,'revoke')));if(user.binding_type==='primary'&&user.identity_state==='active')wrap.append(button('转为 Owner','',()=>transfer(user)))}else if(user.role==='member'&&user.status==='suspended'){wrap.append(button('恢复','',()=>userAction(user,'resume')),button('移除','danger',()=>userAction(user,'revoke')))}actions.append(wrap);row.append(actions);body.append(row)}}
function renderIdentities(statusDocument){const body=q('identitiesBody');body.replaceChildren();const items=statusDocument.identities||[];if(!items.length){const row=document.createElement('tr');const td=cell('尚无 ClawBot 身份');td.colSpan=5;row.append(td);body.append(row);return}for(const item of items){const bindings=(item.bindings||[]).map(value=>`${value.alias} · ${value.wx_short} · ${value.role}/${value.binding_type}`).join('\n')||'等待绑定';const row=document.createElement('tr');row.append(cell(item.identity_short),cell(item.state),cell(item.runtime_state),cell(bindings),cell(`${formatTime(item.last_seen_at)||'—'}\n${item.last_error||'无错误'}`));body.append(row)}}
function renderConversations(items){const body=q('conversationsBody');body.replaceChildren();if(!items.length){const row=document.createElement('tr');const td=cell('尚无会话');td.colSpan=5;row.append(td);body.append(row);return}for(const item of items){const row=document.createElement('tr');row.append(cell(`${item.alias} · ${item.wx_short}`),cell(item.conversation_short),cell(item.thread_short),cell(item.last_job_short),cell(formatTime(item.last_seen_at)));body.append(row)}}
function renderPollerControls(status){const enabled=Boolean(status.poller_enabled),override=status.poller_override||'跟随配置默认',maintenance=status.poller_maintenance||{};q('pollerDesired').textContent=enabled?'已开启':'已关闭';q('pollerStart').disabled=enabled;q('pollerStop').disabled=!enabled;q('pollerControlText').textContent=`当前运行态：${status.poller_state} · desired：${enabled?'enabled':'disabled'} · 覆盖：${override} · 配置默认：${status.poller_default_enabled?'enabled':'disabled'} · revision：${status.poller_revision}${maintenance.active?` · 维护暂停至 ${formatTime(maintenance.expires_at)}`:''}`}
function renderOwner(status,users){const pairingState=status.owner_pairing?.state||'unavailable',owner=users.find(user=>user.role==='owner'&&user.status==='active'),pollerStopped=['disabled','stopped'].includes(status.poller_state);currentIdentityPresent=Boolean(status.identity);q('identityAccount').textContent=currentIdentityPresent?status.identity.identity_short:'尚未登录';q('identityStatus').textContent=currentIdentityPresent?`Owner ClawBot 已就绪；当前 Poller 为 ${status.poller_state}。`:'尚未建立 Owner ClawBot，请先扫码登录。';q('qrStart').textContent=currentIdentityPresent?'重新认证同一 ClawBot':'扫码登录 Owner ClawBot';q('qrStart').disabled=!pollerStopped;q('qrState').textContent=`二维码状态：${status.qr.state}${status.qr.error_code?` / ${status.qr.error_code}`:''}`;q('qrImage').hidden=!status.qr.has_image;if(status.qr.has_image)q('qrImage').src=`api/qr/image?${Date.now()}`;q('ownerVerify').hidden=status.qr.state!=='need_verifycode';const bound=pairingState==='bound';q('ownerSetupPanel').hidden=bound;q('currentOwnerPanel').hidden=!bound;q('ownerSetupState').textContent=pairingState;q('pairStart').disabled=!currentIdentityPresent||status.poller_state!=='pairing'||pairingState==='waiting';q('ownerSetupText').textContent=!currentIdentityPresent?'请先完成 Owner ClawBot 扫码登录。':status.poller_state!=='pairing'?'首次绑定仅在 Poller 进入 pairing 后开放。':pairingState==='waiting'?'绑定码正在等待 Owner 发送。':'可以生成一次性 Owner 绑定码。';q('pairCodeRow').hidden=pairingState!=='waiting';if(pairingState==='waiting')q('pairCode').textContent=currentPairCode||'已生成，明文仅在生成时显示';else currentPairCode='';q('pairExpiry').textContent=pairingState==='waiting'&&status.owner_pairing?.expires_at?`有效期至 ${formatTime(status.owner_pairing.expires_at)}`:'';q('currentOwner').textContent=owner?`${owner.alias} · ${owner.wx_short} · ${owner.identity_short||'无 ClawBot'} · ${owner.has_context?'context 已有':'context 缺少'}`:'未找到唯一 active Owner。';q('onboardingStart').disabled=!bound||!status.poller_enabled}
function renderOnboarding(status){const onboarding=status.onboarding||{},qr=onboarding.qr||{},current=qr.session_short?qr:onboarding.current||{};currentOnboarding=current.session_short||'';q('onboardingState').textContent=currentOnboarding?`会话 ${currentOnboarding} · ${qr.state||current.state}${current.expires_at?` · 有效期至 ${formatTime(current.expires_at)}`:''}${qr.error_code?` · ${qr.error_code}`:''}`:'暂无进行中的接入。';q('onboardingCancel').disabled=!currentOnboarding;q('onboardingImage').hidden=!qr.has_image;if(qr.has_image)q('onboardingImage').src=`api/onboarding/qr/image?${Date.now()}`;q('memberVerify').hidden=qr.state!=='need_verifycode'}
async function refresh(){try{const [status,users,conversations]=await Promise.all([readJson('api/status'),readJson('api/users'),readJson('api/conversations')]);csrf=status.csrf_token;revision=users.revision;pollerRevision=status.poller_revision;q('poller').textContent=status.poller_state;renderPollerControls(status);q('identityCount').textContent=`${status.identities.identities.length} / ${status.identities.limits.max_active_identities}`;q('identityLimit').textContent=`最多 ${status.identities.limits.max_active_identities} 个活动身份`;q('pairing').textContent=status.owner_pairing?.state||'不可用';q('activeUsers').textContent=`${status.users.active} / ${status.users.total}`;q('pending').textContent=status.queue.messages.pending_controller;q('submitted').textContent=status.queue.messages.controller_submitted;q('details').textContent=`Controller ${status.controller_configured?'已配置':'未配置'} · capability ${status.controller_capability_state} · Remote Work ${status.remote_work.enabled?'已启用':'关闭'} · Agent ${status.remote_work.agent?.online?'online':'offline/unknown'} · Remote outbox ${status.remote_work.pending_outbox} · Owner context ${status.identity?.context_count??0} · spool ${status.queue.spool_bytes} bytes · error ${status.last_error||'无'}`;renderOwner(status,users.users);renderOnboarding(status);renderIdentities(status.identities);renderUsers(users.users);renderConversations(conversations.conversations);banner('状态已刷新','ok')}catch(error){banner(error.message,'error');q('details').textContent='状态读取失败'}}
q('onboardingStart').onclick=()=>startOnboarding();
q('pollerStart').onclick=()=>pollerAction('start');
q('pollerStop').onclick=()=>pollerAction('stop');
q('onboardingCancel').onclick=async()=>{if(!currentOnboarding)return;try{const result=await mutate(`api/onboarding/${currentOnboarding}/cancel`,'POST',{revision,request_id:requestId()});revision=result.revision;currentOnboarding='';q('onboardingCode').textContent='已取消';banner('成员接入已取消','ok');await refresh()}catch(error){banner(error.message,'error')}};
q('memberVerifySubmit').onclick=async()=>{if(!currentOnboarding)return;try{await mutate(`api/onboarding/${currentOnboarding}/verify`,'POST',{verify_code:q('memberVerifyCode').value});q('memberVerifyCode').value='';banner('验证码已提交','ok');await refresh()}catch(error){banner(error.message,'error')}};
q('ownerVerifySubmit').onclick=async()=>{try{await mutate('api/qr/verify','POST',{verify_code:q('ownerVerifyCode').value});q('ownerVerifyCode').value='';banner('Owner 验证码已提交','ok');await refresh()}catch(error){banner(error.message,'error')}};
q('qrStart').onclick=async()=>{if(currentIdentityPresent&&!confirm('只允许重新认证同一个 Owner ClawBot，且必须由当前 Owner 扫码。继续？'))return;try{await mutate('api/qr/start','POST',{});await refresh()}catch(error){banner(error.message,'error')}};
q('pairStart').onclick=async()=>{try{const result=await mutate('api/owner-pairing/start','POST',{});currentPairCode=result.code;q('pairCode').textContent=currentPairCode;await refresh()}catch(error){banner(error.message,'error')}};
refresh();setInterval(refresh,5000);"""
