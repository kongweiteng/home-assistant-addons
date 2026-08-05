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
import time
from typing import Any
from urllib.parse import unquote, urlsplit

from .protocol import ProtocolError
from .service import GatewayService
from .store import StoreError


ATTACHMENT_RE = re.compile(r"^/internal/v1/attachments/([A-Za-z0-9_-]{32,128})$")
ATTACHMENT_PREVIEW_RE = re.compile(r"^/internal/v1/attachments/([A-Za-z0-9_-]{32,128})/preview$")
USER_RE = re.compile(r"^/api/users/(WX-[A-Z2-7]{10})$")
USER_ACTION_RE = re.compile(r"^/api/users/(WX-[A-Z2-7]{10})/(suspend|resume|revoke)$")
INVITATION_CANCEL_RE = re.compile(r"^/api/users/invitations/(IV-[A-Z2-7]{10})/cancel$")
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
        server_version = "WeixinGateway/0.2.3"

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
            if path.startswith("/api/") and not self._csrf_valid():
                return
            payload = self._read_json(allow_empty=path in {"/api/qr/start", "/api/owner-pairing/start"})
            if payload is None:
                return
            if path == "/api/qr/start":
                self._async_call(service.start_qr_login())
                return
            if path == "/api/owner-pairing/start":
                self._call(service.start_owner_pairing)
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
<style>body{margin:0;background:#0b1220;color:#edf4ff;font:15px system-ui}main{max-width:1120px;margin:auto;padding:24px}.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:12px}.card,.panel{background:#121c2e;border:1px solid #263751;border-radius:12px;padding:16px}.panel{margin-top:16px}.panel-head{display:flex;align-items:flex-start;justify-content:space-between;gap:12px}.panel-head h2{margin:7px 0 0}.scope{display:inline-block;padding:3px 8px;border-radius:999px;background:#263751;color:#b9c9dd;font-size:12px}.scope.global{background:#253f65;color:#bcd9ff}.scope.user{background:#214c42;color:#a8ead6}.summary{padding:12px;border:1px solid #263751;border-radius:10px;background:#0d1728}.danger-note{padding:10px 12px;border-left:3px solid #ffcb6b;background:#1b2230}b{font-size:22px;display:block;margin-top:8px}.muted{color:#91a4bd}.ok{color:#42d392}.warn{color:#ffcb6b}.error{color:#ff7b8b}code{color:#42d392}button{border:0;border-radius:9px;padding:9px 12px;background:#2374e1;color:white;cursor:pointer;margin:2px}button.secondary{background:#314158}button.danger{background:#a83c4b}button:disabled{opacity:.45;cursor:not-allowed}table{width:100%;border-collapse:collapse;min-width:760px}th,td{text-align:left;padding:10px 8px;border-bottom:1px solid #263751;vertical-align:top}.scroll{overflow:auto}.badge{display:inline-block;padding:3px 7px;border-radius:999px;background:#263751;font-size:12px}img{max-width:min(320px,calc(100% - 24px));background:white;padding:12px;border-radius:12px}.actions{display:flex;flex-wrap:wrap;gap:4px}.banner{min-height:22px;margin:10px 0}[hidden]{display:none!important}@media(max-width:640px){main{padding:16px}.panel-head{display:block}.panel-head .badge{margin-top:10px}}</style></head>
<body><main><h1>微信网关</h1><p class="muted">一套 iLink 机器人身份、一个 Poller；每位私聊用户拥有独立会话和 Codex Thread。模型、账本和 HA 权限不在本 Add-on。</p><p id="banner" class="banner muted">正在加载安全状态…</p>
<div class="grid"><div class="card">Poller<b id="poller">加载中</b></div><div class="card">机器人身份<b id="identity">加载中</b></div><div class="card">Owner 状态<b id="pairing">加载中</b></div><div class="card">有效用户<b id="activeUsers">-</b></div><div class="card">待提交<b id="pending">-</b></div><div class="card">待回复<b id="submitted">-</b></div></div>
<section class="panel" id="identityPanel"><div class="panel-head"><div><span class="scope global">全局机器人</span><h2>机器人身份</h2></div><span id="identityAccount" class="badge">尚未登录</span></div><p>二维码登录认证的是这套 Gateway 唯一的 iLink 机器人，不属于某个微信用户。所有 Owner 和 Member 都通过同一个机器人私聊。</p><p id="identityStatus" class="muted">正在读取身份状态…</p><p id="qrImpact" class="danger-note warn" hidden>更换为不同机器人账号时，旧身份的用户、邀请和会话关联会被清空；同账号刷新凭据会保留当前用户目录。</p><button id="qrStart">扫码登录机器人</button><p id="qrState" class="muted">尚未生成二维码</p><img id="qrImage" hidden alt="iLink 机器人登录二维码"></section>
<section class="panel" id="ownerSetupPanel"><div class="panel-head"><div><span class="scope global">身份初始化</span><h2>新身份 Owner 绑定</h2></div><span id="ownerSetupState" class="badge">等待状态</span></div><p>此操作不需要在页面选择用户。<strong>第一个在机器人私聊中发送正确绑定码的微信用户将成为 Owner</strong>，绑定消息不会进入 Codex。</p><p id="ownerSetupText" class="muted">正在读取 Owner 初始化状态…</p><button id="pairStart">生成一次性绑定码</button><p id="pairCodeRow" hidden>本次绑定码：<code id="pairCode">尚未生成</code></p><p id="pairExpiry" class="muted"></p></section>
<section class="panel" id="currentOwnerPanel" hidden><div class="panel-head"><div><span class="scope user">用户级身份</span><h2>当前 Owner</h2></div><span class="badge ok">已绑定</span></div><p id="currentOwner" class="summary">正在读取当前 Owner…</p><p class="muted">首次绑定已经完成。更换 Owner 请在下方用户列表中把一个 active Member“转为 Owner”，不要重新生成首次绑定码。</p></section>
<section class="panel"><div class="panel-head"><div><span class="scope user">用户级权限</span><h2>用户与权限</h2></div><span id="userLimit" class="badge">最多 32 人</span></div><p>成员邀请码为一次性高熵口令，明文只显示一次、15 分钟过期。新成员默认仅允许普通讨论和已批准的装修只读查询，不获得记账写入、Operations 或主动通知权限。</p><p id="inviteHint" class="muted">Owner 绑定完成后可以邀请 Member。</p><button id="inviteStart">生成成员邀请码</button><button id="inviteCancel" class="secondary" disabled>取消本次邀请码</button><p>本次邀请码：<code id="inviteCode">尚未生成</code></p><p id="inviteState" class="muted">等待中的邀请码：0</p><div class="scroll"><table><thead><tr><th>用户</th><th>角色/状态</th><th>会话标识</th><th>Thread</th><th>最近活动</th><th>操作</th></tr></thead><tbody id="usersBody"></tbody></table></div><p class="muted">页面只展示 HMAC 短标识，不展示原始微信 ID、完整 conversation key、Thread/job ID 或邀请码历史。</p></section>
<section class="panel"><div class="panel-head"><div><span class="scope user">用户级会话</span><h2>会话排障</h2></div></div><div class="scroll"><table><thead><tr><th>用户</th><th>会话</th><th>Thread</th><th>最近作业</th><th>最近活动</th></tr></thead><tbody id="conversationsBody"></tbody></table></div></section>
<section class="panel"><h2>安全状态</h2><p id="details" class="muted">加载中</p></section><script src="app.js"></script></main></body></html>"""


DASHBOARD_JS = r"""const q=id=>document.getElementById(id);let csrf='',revision=0,currentInvite='',currentPairCode='',currentIdentityPresent=false;
const requestId=()=>globalThis.crypto?.randomUUID?.().replaceAll('-','')||`${Date.now()}-${Math.random().toString(36).slice(2)}`;
const cell=text=>{const td=document.createElement('td');td.textContent=text??'—';return td};
const button=(label,kind,handler)=>{const value=document.createElement('button');value.textContent=label;if(kind)value.className=kind;value.onclick=handler;return value};
const formatTime=value=>{if(!value)return'';const date=new Date(value);return Number.isNaN(date.getTime())?value:date.toLocaleString('zh-CN',{hour12:false})};
function banner(message,kind='muted'){q('banner').textContent=message;q('banner').className=`banner ${kind}`}
async function readJson(path){const response=await fetch(path,{cache:'no-store'}),document=await response.json();if(!response.ok)throw new Error(document.error?.message||document.error?.code||'读取失败');return document.result??document}
async function mutate(path,method,payload){const response=await fetch(path,{method,headers:{'Content-Type':'application/json','X-CSRF-Token':csrf},body:JSON.stringify(payload)}),document=await response.json();if(!response.ok)throw new Error(document.error?.message||document.error?.code||'操作失败');return document.result}
async function userAction(user,action){try{const result=await mutate(`api/users/${user.wx_short}/${action}`,'POST',{revision,request_id:requestId()});revision=result.revision;banner('用户状态已更新','ok');await refresh()}catch(error){banner(error.message,'error')}}
async function rename(user){const alias=prompt('输入新的用户别名',user.alias);if(alias===null)return;try{const result=await mutate(`api/users/${user.wx_short}`,'PATCH',{alias,revision,request_id:requestId()});revision=result.revision;banner('别名已更新','ok');await refresh()}catch(error){banner(error.message,'error')}}
async function transfer(user){const confirmation=prompt(`确认把 Owner 转移给 ${user.alias}？请输入 TRANSFER_OWNER`,'');if(confirmation===null)return;try{const result=await mutate('api/owner-transfer','POST',{target_wx_short:user.wx_short,confirmation,revision,request_id:requestId()});revision=result.revision;banner('Owner 已原子转移，主动通知接收人同步更新','ok');await refresh()}catch(error){banner(error.message,'error')}}
function renderUsers(users){const body=q('usersBody');body.replaceChildren();if(!users.length){const row=document.createElement('tr');const td=cell('尚无用户');td.colSpan=6;row.append(td);body.append(row);return}for(const user of users){const row=document.createElement('tr');row.append(cell(`${user.alias}\n${user.wx_short}`),cell(`${user.role} / ${user.status}${user.has_context?' / context 已有':' / context 缺少'}`),cell(user.conversation_short),cell(user.thread_short),cell(user.last_seen_at));const actions=document.createElement('td'),wrap=document.createElement('div');wrap.className='actions';wrap.append(button('改名','secondary',()=>rename(user)));if(user.role==='member'&&user.status==='active'){wrap.append(button('暂停','secondary',()=>userAction(user,'suspend')),button('移除','danger',()=>userAction(user,'revoke')),button('转为 Owner','',()=>transfer(user)))}else if(user.role==='member'&&user.status==='suspended'){wrap.append(button('恢复','',()=>userAction(user,'resume')),button('移除','danger',()=>userAction(user,'revoke')))}actions.append(wrap);row.append(actions);body.append(row)}}
function renderConversations(items){const body=q('conversationsBody');body.replaceChildren();if(!items.length){const row=document.createElement('tr');const td=cell('尚无会话');td.colSpan=5;row.append(td);body.append(row);return}for(const item of items){const row=document.createElement('tr');row.append(cell(`${item.alias} · ${item.wx_short}`),cell(item.conversation_short),cell(item.thread_short),cell(item.last_job_short),cell(item.last_seen_at));body.append(row)}}
function renderIdentityScope(status,users){const pairingState=status.owner_pairing?.state||'unavailable',owner=users.find(user=>user.role==='owner'&&user.status==='active'),pollerStopped=['disabled','stopped'].includes(status.poller_state);currentIdentityPresent=Boolean(status.identity);q('identityAccount').textContent=currentIdentityPresent?`账号 ${status.identity.account_hash.slice(0,12)}`:'尚未登录';q('identityStatus').textContent=currentIdentityPresent?`机器人身份已就绪；当前 Poller 为 ${status.poller_state}。`:'尚未建立 iLink 机器人身份，请先完成扫码登录。';q('qrStart').textContent=currentIdentityPresent?'更换机器人身份':'扫码登录机器人';q('qrStart').className=currentIdentityPresent?'danger':'';q('qrStart').disabled=!pollerStopped;q('qrImpact').hidden=!currentIdentityPresent;if(!pollerStopped)q('identityStatus').textContent+=' 真实 Poller 运行期间不能替换身份，请先在 Add-on 配置中安全停止。';const bound=pairingState==='bound';q('ownerSetupPanel').hidden=bound;q('currentOwnerPanel').hidden=!bound;q('ownerSetupState').textContent=pairingState;q('pairStart').disabled=!currentIdentityPresent||status.poller_state!=='pairing'||pairingState==='waiting';if(!currentIdentityPresent){q('ownerSetupText').textContent='请先完成全局机器人扫码登录。'}else if(status.poller_state!=='pairing'){q('ownerSetupText').textContent='首次绑定仅在 Poller 进入 pairing 后开放；当前不会接收绑定码。'}else if(pairingState==='waiting'){q('ownerSetupText').textContent='绑定码正在等待领取；第一个正确发送者将成为 Owner。'}else{q('ownerSetupText').textContent='可以生成一次性绑定码；错误码、普通消息和图片不会进入 Codex。'}q('pairCodeRow').hidden=pairingState!=='waiting';if(pairingState==='waiting')q('pairCode').textContent=currentPairCode||'已生成，明文仅在生成时显示';else currentPairCode='';q('pairExpiry').textContent=pairingState==='waiting'&&status.owner_pairing?.expires_at?`有效期至 ${formatTime(status.owner_pairing.expires_at)}`:'';q('currentOwner').textContent=owner?`${owner.alias} · ${owner.wx_short} · ${owner.has_context?'context 已有':'context 缺少'}`:'Owner 状态为 bound，但用户目录未找到唯一 active Owner，请检查安全状态。';const userManagementReady=bound&&Boolean(owner);q('inviteStart').disabled=!userManagementReady;q('inviteCancel').disabled=!userManagementReady||!currentInvite;q('inviteHint').textContent=userManagementReady?'当前 Owner 已绑定，可以邀请新的 Member。':'Owner 初始化完成后才能邀请 Member。'}
async function refresh(){try{const [status,users,conversations]=await Promise.all([readJson('api/status'),readJson('api/users'),readJson('api/conversations')]);csrf=status.csrf_token;revision=users.revision;q('poller').textContent=status.poller_state;q('identity').textContent=status.identity?'已就绪':'缺少';q('pairing').textContent=status.owner_pairing?.state||'不可用';q('activeUsers').textContent=`${status.users.active} / ${status.users.total}`;q('pending').textContent=status.queue.messages.pending_controller;q('submitted').textContent=status.queue.messages.controller_submitted;q('qrState').textContent=status.qr.state;q('qrImage').hidden=!status.qr.has_image;if(status.qr.has_image)q('qrImage').src=`api/qr/image?${Date.now()}`;q('inviteState').textContent=`等待 ${status.invitations.waiting} · 已领取 ${status.invitations.claimed} · 过期 ${status.invitations.expired}`;q('details').textContent=`Controller ${status.controller_configured?'已配置':'未配置'} · capability ${status.controller_capability_state} · Remote Work ${status.remote_work.enabled?'已启用':'关闭'} · Agent ${status.remote_work.agent?.online?'online':'offline/unknown'} · Remote outbox ${status.remote_work.pending_outbox} · 单 owner 镜像 ${status.identity?.allowed_user_count??0} · context ${status.identity?.context_count??0} · spool ${status.queue.spool_bytes} bytes · error ${status.last_error||'无'}`;renderIdentityScope(status,users.users);renderUsers(users.users);renderConversations(conversations.conversations);banner('状态已刷新','ok')}catch(error){banner(error.message,'error');q('details').textContent='状态读取失败'}}
q('inviteStart').onclick=async()=>{try{const result=await mutate('api/users/invitations','POST',{revision,request_id:requestId(),ttl_seconds:900});q('inviteCode').textContent=result.code||'该请求已处理，邀请码明文不会再次显示';currentInvite=result.invite_short;q('inviteCancel').disabled=!currentInvite;revision=result.revision;banner('成员邀请码已生成，请通过可信渠道一次性提供给新成员','warn');await refresh()}catch(error){banner(error.message,'error')}};
q('inviteCancel').onclick=async()=>{if(!currentInvite)return;try{const result=await mutate(`api/users/invitations/${currentInvite}/cancel`,'POST',{revision,request_id:requestId()});revision=result.revision;currentInvite='';q('inviteCancel').disabled=true;q('inviteCode').textContent='已取消';banner('邀请码已取消','ok');await refresh()}catch(error){banner(error.message,'error')}};
q('qrStart').onclick=async()=>{if(currentIdentityPresent&&!confirm('更换全局机器人身份可能使旧身份失效；如果扫码为不同账号，旧用户、邀请和会话关联会被清空。确认继续？'))return;try{await mutate('api/qr/start','POST',{});await refresh()}catch(error){banner(error.message,'error')}};
q('pairStart').onclick=async()=>{try{const result=await mutate('api/owner-pairing/start','POST',{});currentPairCode=result.code;q('pairCode').textContent=currentPairCode;await refresh()}catch(error){banner(error.message,'error')}};
refresh();setInterval(refresh,5000);"""
