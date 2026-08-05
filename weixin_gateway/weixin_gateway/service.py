"""Single-poller iLink service and durable Controller delivery loop."""

from __future__ import annotations

import asyncio
import datetime as dt
import hashlib
import json
from pathlib import Path
import threading
import time
from typing import Any
from urllib.parse import urlsplit

import aiohttp

from .protocol import (
    EP_GET_BOT_QR,
    EP_GET_QR_STATUS,
    IlinkClient,
    ProtocolError,
    RATE_LIMIT_ERRCODE,
    SESSION_EXPIRED_ERRCODE,
    extract_message,
)
from .remote_work import (
    GatewayRemoteWorkRuntime,
    WorkCommand,
    WorkCommandError,
    build_command_document,
    parse_work_command,
)
from .store import GatewayStore, IdentityStore, StoreError, TokenLock, utc_now


def validate_controller_url(value: str) -> str:
    if not value:
        return ""
    parsed = urlsplit(value)
    host = (parsed.hostname or "").lower()
    if (
        parsed.scheme != "http"
        or not host
        or parsed.username
        or parsed.password
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
        or all(character.isdigit() or character == "." for character in host)
        or ":" in host
    ):
        raise StoreError("controller_url_invalid", "Controller 地址必须使用固定 http 主机名")
    port = f":{parsed.port}" if parsed.port else ""
    return f"http://{host}{port}"


class ControllerClient:
    def __init__(self, base_url: str, token: str, *, session: aiohttp.ClientSession | None = None):
        self.base_url = validate_controller_url(base_url)
        self.token = token
        self.session = session
        self._owns_session = session is None
        self._capabilities: frozenset[str] | None = None
        self._capabilities_checked_at = 0.0
        self.capability_state = "unknown"

    @property
    def configured(self) -> bool:
        return bool(self.base_url and len(self.token) >= 32)

    async def start(self) -> None:
        if self.session is None:
            self.session = aiohttp.ClientSession(trust_env=True)

    async def close(self) -> None:
        if self._owns_session and self.session is not None and not self.session.closed:
            await self.session.close()

    async def submit(self, message: dict[str, Any]) -> dict[str, Any]:
        return await self._request("POST", "/internal/v1/jobs", message)

    async def job(self, job_id: str) -> dict[str, Any]:
        return await self._request("GET", f"/internal/v1/jobs/{job_id}", None)

    async def supports_capability(self, capability: str) -> bool:
        if self._capabilities is not None and time.monotonic() - self._capabilities_checked_at < 60:
            return capability in self._capabilities
        try:
            result = await self._request("GET", "/internal/v1/capabilities", None)
        except StoreError as exc:
            if exc.status in {404, 405}:
                self._capabilities = frozenset()
                self._capabilities_checked_at = time.monotonic()
                self.capability_state = "legacy_incompatible"
                return False
            self.capability_state = "unavailable"
            raise
        values = result.get("capabilities")
        if not isinstance(values, list) or any(not isinstance(value, str) for value in values):
            self.capability_state = "invalid"
            raise StoreError("controller_invalid_response", "Controller capabilities 响应无效", status=502)
        self._capabilities = frozenset(values)
        self._capabilities_checked_at = time.monotonic()
        self.capability_state = "compatible" if capability in self._capabilities else "legacy_incompatible"
        return capability in self._capabilities

    async def _request(self, method: str, path: str, payload: dict[str, Any] | None) -> dict[str, Any]:
        if not self.configured:
            raise StoreError("controller_unavailable", "Codex Controller 未配置", status=503)
        if self.session is None:
            await self.start()
        body = None if payload is None else json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        assert self.session is not None
        try:
            async with self.session.request(
                method,
                f"{self.base_url}{path}",
                data=body,
                headers={"Authorization": f"Bearer {self.token}", "Content-Type": "application/json", "Accept": "application/json"},
                timeout=aiohttp.ClientTimeout(total=20),
            ) as response:
                raw = await response.content.read(2 * 1024 * 1024 + 1)
                if len(raw) > 2 * 1024 * 1024:
                    raise StoreError("controller_invalid_response", "Controller 响应过大", status=502)
                try:
                    document = json.loads(raw)
                except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                    raise StoreError("controller_invalid_response", "Controller 响应 JSON 无效", status=502) from exc
                if response.status < 200 or response.status >= 300:
                    error = document.get("error") if isinstance(document, dict) else {}
                    code = error.get("code") if isinstance(error, dict) else "controller_rejected"
                    raise StoreError(str(code or "controller_rejected"), "Controller 暂未接受作业", status=response.status)
                result = document.get("result") if isinstance(document, dict) else None
                if not isinstance(result, dict):
                    raise StoreError("controller_invalid_response", "Controller 响应缺少 result", status=502)
                return result
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            raise StoreError("controller_unavailable", "Codex Controller 不可用", status=503) from exc


class GatewayService:
    def __init__(
        self,
        *,
        identity_store: IdentityStore,
        store: GatewayStore,
        controller: ControllerClient,
        bootstrap_identity: dict[str, Any],
        poller_enabled: bool,
        owner_pairing_enabled: bool,
        activation_confirmation: str,
        max_media_bytes: int,
        remote_work_enabled: bool = False,
        remote_work_ttl_seconds: int = 1800,
    ):
        self.identity_store = identity_store
        self.store = store
        self.controller = controller
        self.identity = identity_store.bootstrap(bootstrap_identity)
        if self.identity is not None:
            migration = self.store.migrate_identity_allowlist(list(self.identity.get("allowed_user_ids", [])))
            owner_private_id = migration.get("owner_private_id")
            if isinstance(owner_private_id, str) and self.identity.get("allowed_user_ids") != [owner_private_id]:
                self.identity_store.mirror_owner(self.identity, owner_private_id)
        self.poller_enabled = poller_enabled
        self.owner_pairing_enabled = owner_pairing_enabled
        self.activation_confirmation = activation_confirmation
        self.max_media_bytes = max_media_bytes
        self.remote_work_enabled = remote_work_enabled
        self.remote_work_ttl_seconds = remote_work_ttl_seconds
        self.remote_work_runtime: GatewayRemoteWorkRuntime | None = None
        self.client: IlinkClient | None = None
        self.token_lock: TokenLock | None = None
        self.poller_state = "disabled"
        self.last_error: str | None = None
        self.last_poll_at: str | None = None
        self.last_message_at: str | None = None
        self.qr_state: dict[str, Any] | None = None
        self.qr_image_path = identity_store.data_dir / "qr" / "current.png"
        self._tasks: list[asyncio.Task[Any]] = []
        self._status_lock = threading.Lock()
        self._stop = asyncio.Event()
        self._outbound_lock = asyncio.Lock()
        self._authorization_lock = asyncio.Lock()

    def bind_remote_work_runtime(self, runtime: GatewayRemoteWorkRuntime) -> None:
        self.remote_work_runtime = runtime

    async def start(self) -> None:
        await self.controller.start()
        self._refresh_client()
        if self.poller_enabled:
            if self.activation_confirmation != "HERMES_POLLER_STOPPED":
                raise StoreError("activation_confirmation_required", "未确认 Hermes poller 已停止", status=409)
            if self.identity is None or self.client is None:
                raise StoreError("credential_missing", "缺少完整 iLink 身份", status=409)
            if not self.identity.get("allowed_user_ids") and not self.owner_pairing_enabled:
                raise StoreError("owner_binding_required", "新身份必须先启用一次性 owner 绑定", status=409)
            self.token_lock = self.identity_store.acquire_token_lock(self.identity["token"])
            self.token_lock.acquire()
            self.poller_state = "polling" if self.identity.get("allowed_user_ids") else "pairing"
            self._tasks.append(asyncio.create_task(self._poll_loop(), name="weixin-poller"))
        self._tasks.append(asyncio.create_task(self._delivery_loop(), name="weixin-controller-delivery"))
        self._tasks.append(asyncio.create_task(self._cleanup_loop(), name="weixin-spool-cleanup"))

    async def stop(self) -> None:
        self._stop.set()
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        if self.client is not None:
            await self.client.close()
        await self.controller.close()
        if self.token_lock is not None:
            self.token_lock.release()
        self.poller_state = "stopped"

    def _refresh_client(self) -> None:
        if self.identity is None:
            self.client = None
            return
        self.client = IlinkClient(
            base_url=self.identity["base_url"],
            cdn_base_url=self.identity["cdn_base_url"],
            token=self.identity["token"],
            max_media_bytes=self.max_media_bytes,
        )

    async def _poll_loop(self) -> None:
        assert self.identity is not None and self.client is not None
        cursor = self.identity_store.cursor(self.identity)
        timeout_ms = 35000
        failures = 0
        while not self._stop.is_set():
            try:
                response = await self.client.get_updates(cursor, timeout_ms=timeout_ms)
                suggested = response.get("longpolling_timeout_ms")
                if isinstance(suggested, int) and suggested > 0:
                    timeout_ms = min(suggested, 120000)
                ret = response.get("ret", 0)
                errcode = response.get("errcode", 0)
                if ret not in {0, None} or errcode not in {0, None}:
                    if ret == SESSION_EXPIRED_ERRCODE or errcode == SESSION_EXPIRED_ERRCODE or (
                        (ret == RATE_LIMIT_ERRCODE or errcode == RATE_LIMIT_ERRCODE)
                        and str(response.get("errmsg") or "").lower() == "unknown error"
                    ):
                        self.poller_state = "session_expired"
                        self.last_error = "session_expired"
                        return
                    failures += 1
                    self.last_error = "poll_failed"
                    await asyncio.sleep(30 if failures >= 3 else 2)
                    if failures >= 3:
                        failures = 0
                    continue
                failures = 0
                self.last_poll_at = utc_now()
                for raw_message in response.get("msgs") or []:
                    if isinstance(raw_message, dict):
                        await self._ingest(raw_message)
                new_cursor = str(response.get("get_updates_buf") or "")
                if new_cursor and new_cursor != cursor:
                    self.identity_store.set_cursor(self.identity, new_cursor)
                    cursor = new_cursor
            except asyncio.CancelledError:
                return
            except ProtocolError as exc:
                failures += 1
                self.last_error = exc.code
                await asyncio.sleep(30 if failures >= 3 else 2)
            except Exception:
                failures += 1
                self.last_error = "poll_failed"
                await asyncio.sleep(30 if failures >= 3 else 2)

    async def _ingest(self, raw_message: dict[str, Any]) -> None:
        assert self.identity is not None and self.client is not None
        message = extract_message(raw_message, self.identity["account_id"])
        if message is None or message["is_group"]:
            return
        sender_id = message["sender_id"]
        allowed_user_ids = set(self.identity.get("allowed_user_ids", []))
        if not allowed_user_ids:
            if self.owner_pairing_enabled and self.identity_store.claim_owner(
                self.identity,
                user_id=sender_id,
                text=str(message.get("text") or ""),
                context_token=str(message.get("context_token") or "") or None,
            ):
                self.identity = self.identity_store.load_identity()
                self.store.register_paired_owner(sender_id)
                self.poller_state = "polling"
                self.last_message_at = utc_now()
                await self._send_owner_pairing_confirmation(sender_id, str(message.get("context_token") or "") or None, message["message_id"])
            return
        user = self.store.user_by_private_id(sender_id)
        if user is None:
            claimed = self.store.claim_member_invitation(
                user_id=sender_id,
                text=str(message.get("text") or ""),
            )
            if claimed is not None:
                if message["context_token"]:
                    self.identity_store.set_context(self.identity, sender_id, message["context_token"])
                self.last_message_at = utc_now()
                await self._send_member_pairing_confirmation(
                    sender_id,
                    str(message.get("context_token") or "") or None,
                    message["message_id"],
                    claimed,
                )
            return
        if user["status"] != "active":
            return
        if self.store.message_exists(message["message_id"]):
            return
        if message["context_token"]:
            self.identity_store.set_context(self.identity, sender_id, message["context_token"])
        try:
            work_command = parse_work_command(str(message.get("text") or ""))
        except WorkCommandError as exc:
            user = self.store.touch_user(sender_id) or user
            await self._send_remote_work_text(message, user, str(exc), error_code=exc.code)
            self.last_message_at = utc_now()
            return
        if work_command is not None:
            user = self.store.touch_user(sender_id) or user
            if message["media"]:
                await self._send_remote_work_text(
                    message,
                    user,
                    "Remote Work 命令不能携带附件；请把开发要求直接写在命令正文中。",
                    error_code="work_attachments_unsupported",
                )
            elif user["role"] != "owner":
                await self._send_remote_work_text(
                    message,
                    user,
                    "当前账号没有 /work 权限。成员账号只允许普通讨论和装修只读查询。",
                    error_code="work_owner_required",
                )
            else:
                await self._handle_remote_work_command(message, user, work_command)
            self.last_message_at = utc_now()
            return
        media: list[tuple[dict[str, Any], bytes]] = []
        for spec in message["media"]:
            try:
                media.append((spec, await self.client.download_media(spec)))
            except ProtocolError as exc:
                self.last_error = exc.code
        if not message["text"] and not media:
            return
        user = self.store.touch_user(sender_id) or user
        self.store.store_message(
            message_id=message["message_id"],
            sender_id=sender_id,
            conversation_key=user["conversation_key"],
            text=message["text"],
            media=media,
            user_digest=user["user_hash"],
            capability_profile="owner" if user["role"] == "owner" else "member_read_only",
        )
        self.last_message_at = utc_now()

    async def _handle_remote_work_command(
        self,
        message: dict[str, Any],
        user: dict[str, Any],
        command: WorkCommand,
    ) -> None:
        if not self.remote_work_enabled:
            await self._send_remote_work_text(
                message,
                user,
                "Remote Work 适配器当前未启用；普通微信与装修业务链路不受影响。",
                error_code="remote_work_disabled",
            )
            return
        if command.operation == "status":
            assert command.task_id is not None
            try:
                task = self.store.remote_work_task(command.task_id, user_digest=user["user_hash"])
                text = self._remote_work_status_text(task)
                error_code = "remote_work_status"
            except StoreError as exc:
                text = "没有找到可由当前 owner 查看或继续的 Remote Work task。"
                error_code = exc.code
            await self._send_remote_work_text(message, user, text, error_code=error_code)
            return
        if self.remote_work_runtime is None:
            await self._send_remote_work_text(
                message,
                user,
                "Remote Work MQTT 尚未就绪，本条命令未提交。",
                error_code="remote_work_unavailable",
            )
            return

        remote_message_id = self.store.short_id("RM", str(message["message_id"]))
        task_id = command.task_id or self.store.short_id("RW", str(message["message_id"]))
        try:
            replay = self.store.remote_work_command_replay(
                remote_message_id,
                task_id=task_id,
                operation=command.operation,
                project_alias=command.project_alias,
                instruction=command.instruction,
                user_digest=user["user_hash"],
            )
        except StoreError as exc:
            await self._send_remote_work_text(message, user, str(exc), error_code=exc.code)
            return
        if replay is not None:
            await self._send_remote_work_text(
                message,
                user,
                f"命令已处理过，沿用现有 task {task_id}，当前状态 {replay['state']}。",
                error_code=f"remote_work_{command.operation}",
            )
            return
        topic, document = build_command_document(
            command,
            message_id=remote_message_id,
            task_id=task_id,
            principal_hash=user["user_hash"],
            now=dt.datetime.now().astimezone(),
            ttl_seconds=self.remote_work_ttl_seconds,
        )
        try:
            task = self.store.enqueue_remote_work_command(
                topic=topic,
                payload=document,
                sender_id=message["sender_id"],
                user_digest=user["user_hash"],
            )
            self.remote_work_runtime.publish_pending()
        except StoreError as exc:
            await self._send_remote_work_text(message, user, str(exc), error_code=exc.code)
            return
        if command.operation == "start":
            text = f"已创建 Remote Work task {task_id}，当前状态 {task['state']}。Mac 离线时不会晚到执行。"
        elif command.operation == "continue":
            text = f"已为 {task_id} 提交补充要求；只会恢复该 task 已登记的 Codex Session。"
        else:
            text = f"已为 {task_id} 提交取消请求；停止进程不代表工作树已回滚。"
        if task.get("duplicate"):
            text = f"命令已处理过，沿用现有 task {task_id}，当前状态 {task['state']}。"
        await self._send_remote_work_text(message, user, text, error_code=f"remote_work_{command.operation}")

    async def _send_remote_work_text(
        self,
        message: dict[str, Any],
        user: dict[str, Any],
        text: str,
        *,
        error_code: str,
    ) -> str | None:
        outbound = {
            "message_id": message["message_id"],
            "sender_id": message["sender_id"],
            "user_hash": user["user_hash"],
            "capability_profile": "owner" if user["role"] == "owner" else "member_read_only",
            "required_role": "owner" if user["role"] == "owner" else None,
            "controller_job_id": f"gateway-{error_code}-{message['message_id']}",
        }
        return await self._send_result(outbound, text)

    @staticmethod
    def _remote_work_status_text(task: dict[str, Any]) -> str:
        parts = [f"Remote Work task {task['task_id']}：{task['state']}"]
        if task.get("stage"):
            parts.append(f"阶段：{task['stage']}")
        result = task.get("last_result")
        if isinstance(result, dict) and result.get("summary"):
            parts.append(str(result["summary"]))
        return "\n".join(parts)

    async def _send_owner_pairing_confirmation(self, sender_id: str, context_token: str | None, message_id: str) -> None:
        if self.client is None:
            return
        client_id = "codex-weixin-pairing-" + hashlib.sha256(message_id.encode("utf-8")).hexdigest()[:32]
        try:
            async with self._outbound_lock:
                response = await self.client.send_text(
                    sender_id,
                    "微信 owner 绑定成功。现在可以直接和通用 Codex 助手交流。",
                    context_token,
                    client_id,
                )
            if response.get("ret", 0) == SESSION_EXPIRED_ERRCODE or response.get("errcode", 0) == SESSION_EXPIRED_ERRCODE:
                self.poller_state = "session_expired"
                self.last_error = "session_expired"
                return
            if response.get("ret", 0) not in {0, None} or response.get("errcode", 0) not in {0, None}:
                self.last_error = "owner_pairing_confirmation_failed"
        except Exception:
            self.last_error = "owner_pairing_confirmation_failed"

    async def _send_member_pairing_confirmation(
        self,
        sender_id: str,
        context_token: str | None,
        message_id: str,
        user: dict[str, Any],
    ) -> None:
        if self.client is None:
            return
        client_id = "codex-weixin-member-pairing-" + hashlib.sha256(message_id.encode("utf-8")).hexdigest()[:32]
        try:
            async with self._outbound_lock:
                response = await self.client.send_text(
                    sender_id,
                    "微信成员绑定成功。当前账号只允许普通讨论和已批准的装修只读查询。",
                    context_token,
                    client_id,
                )
            if response.get("ret", 0) == SESSION_EXPIRED_ERRCODE or response.get("errcode", 0) == SESSION_EXPIRED_ERRCODE:
                self.poller_state = "session_expired"
                self.last_error = "session_expired"
            elif response.get("ret", 0) not in {0, None} or response.get("errcode", 0) not in {0, None}:
                self.last_error = "member_pairing_confirmation_failed"
        except Exception:
            self.last_error = "member_pairing_confirmation_failed"

    async def _delivery_loop(self) -> None:
        while not self._stop.is_set():
            try:
                await self._deliver_remote_work_replies()
                if self.controller.configured:
                    for message in self.store.pending_controller():
                        payload = {
                            "version": 1,
                            "message_id": message["message_id"],
                            "conversation_key": message["conversation_key"],
                            "received_at": message["received_at"],
                            "text": message["text"],
                            "attachments": message["attachments"],
                            "reply_capabilities": ["text", "image", "file"],
                        }
                        stored_profile = str(message.get("capability_profile") or "owner_legacy")
                        try:
                            incompatible = False
                            async with self._authorization_lock:
                                authorization = self.store.authorize_stored_message(
                                    message.get("user_hash"), stored_profile
                                )
                                if not authorization["allowed"]:
                                    self.store.mark_finished(
                                        message["message_id"],
                                        success=False,
                                        error_code=str(authorization["error_code"]),
                                    )
                                    continue
                                profile = str(authorization["capability_profile"])
                                profile_supported = await self._controller_supports_capability_profile()
                                if profile == "member_read_only" and not profile_supported:
                                    incompatible = True
                                    job = None
                                else:
                                    if profile_supported:
                                        payload["capability_profile"] = (
                                            "owner" if profile == "owner_legacy" else profile
                                        )
                                    job = await self.controller.submit(payload)
                            if incompatible:
                                suppression = await self._send_direct_result(
                                    message,
                                    "当前 Codex Controller 尚未启用成员只读权限协商，本条消息未提交。",
                                    error_code="controller_capability_incompatible",
                                )
                                self.store.mark_finished(
                                    message["message_id"],
                                    success=False,
                                    error_code=suppression or "controller_capability_incompatible",
                                )
                                continue
                            assert job is not None
                        except StoreError as exc:
                            self.last_error = exc.code
                            break
                        self.store.mark_submitted(message["message_id"], job["job_id"])
                    for message in self.store.submitted():
                        try:
                            job = await self.controller.job(message["controller_job_id"])
                        except StoreError as exc:
                            self.last_error = exc.code
                            break
                        if job["state"] == "completed":
                            if self.poller_state == "session_expired":
                                break
                            self.store.update_conversation_link(
                                message.get("user_hash"),
                                thread_short=job.get("thread_short"),
                                job_id=message.get("controller_job_id"),
                            )
                            outbound = dict(message)
                            outbound["thread_short"] = job.get("thread_short")
                            suppression = await self._send_result(
                                outbound, str(job.get("result") or "任务已完成。")
                            )
                            if suppression:
                                self.store.mark_finished(
                                    message["message_id"],
                                    success=False,
                                    error_code=suppression,
                                )
                                continue
                            self.store.mark_finished(message["message_id"], success=True)
                        elif job["state"] in {"failed", "cancelled", "recovery_required"}:
                            if self.poller_state == "session_expired":
                                break
                            text = "任务状态需要人工核对，请在 Codex Controller 页面查看。" if job["state"] == "recovery_required" else "任务未完成，请在 Codex Controller 页面查看错误状态。"
                            outbound = dict(message)
                            outbound["thread_short"] = job.get("thread_short")
                            suppression = await self._send_result(outbound, text)
                            if suppression:
                                self.store.mark_finished(
                                    message["message_id"],
                                    success=False,
                                    error_code=suppression,
                                )
                                continue
                            self.store.mark_finished(message["message_id"], success=False, error_code=job.get("error_code") or job["state"])
                await asyncio.sleep(2)
            except asyncio.CancelledError:
                return
            except (StoreError, ProtocolError) as exc:
                self.last_error = exc.code
                await asyncio.sleep(30 if exc.code == "session_expired" else 5)
            except Exception:
                self.last_error = "delivery_failed"
                await asyncio.sleep(5)

    async def _deliver_remote_work_replies(self) -> None:
        for event in self.store.remote_work_pending_replies():
            payload = event["payload"]
            text = self._remote_work_result_text(payload)
            outbound = {
                "message_id": f"remote-result-{event['event_id']}",
                "sender_id": event["sender_id"],
                "user_hash": event["user_hash"],
                "capability_profile": "owner",
                "required_role": "owner",
                "controller_job_id": f"remote-result-{event['event_id']}",
            }
            suppression = await self._send_result(outbound, text)
            self.store.mark_remote_work_reply(
                event["event_id"],
                sent=suppression is None,
                error_code=suppression,
            )

    @staticmethod
    def _remote_work_result_text(payload: dict[str, Any]) -> str:
        parts = [f"Remote Work task {payload['task_id']}：{payload['state']}", str(payload.get("summary") or "")]
        if payload.get("branch"):
            parts.append(f"分支：{payload['branch']}")
        commits = payload.get("commits") or []
        if commits:
            parts.append("提交：" + "、".join(str(value) for value in commits))
        if payload.get("test_summary"):
            parts.append("测试：" + str(payload["test_summary"]))
        next_actions = payload.get("next_actions") or []
        if next_actions:
            parts.append("下一步：" + "；".join(str(value) for value in next_actions))
        if payload.get("error_code"):
            parts.append("错误码：" + str(payload["error_code"]))
        return "\n".join(part for part in parts if part)

    async def _controller_supports_capability_profile(self) -> bool:
        callback = getattr(self.controller, "supports_capability", None)
        if callback is None:
            return False
        return bool(await callback("job_capability_profile_v1"))

    async def _send_direct_result(self, message: dict[str, Any], text: str, *, error_code: str) -> str | None:
        direct = dict(message)
        direct["controller_job_id"] = f"gateway-{error_code}-{message['message_id']}"
        return await self._send_result(direct, text)

    async def _send_result(self, message: dict[str, Any], text: str) -> str | None:
        if self.poller_state == "session_expired":
            raise StoreError("session_expired", "iLink 会话已过期，停止微信出站", status=503)
        if self.identity is None or self.client is None:
            raise StoreError("credential_missing", "无法回传微信消息", status=503)
        text = with_thread_short(text, message.get("thread_short"))
        chunks = split_text(text, 4000)
        async with self._outbound_lock:
            async with self._authorization_lock:
                authorization = self.store.authorize_stored_message(
                    message.get("user_hash"),
                    str(message.get("capability_profile") or "owner_legacy"),
                )
                if not authorization["allowed"]:
                    return (
                        "reply_suppressed_user_inactive"
                        if authorization["error_code"] == "message_user_inactive"
                        else "reply_suppressed_authorization_invalid"
                    )
                if message.get("required_role") == "owner" and authorization.get("capability_profile") != "owner":
                    return "reply_suppressed_owner_changed"
                for index, chunk in enumerate(chunks):
                    if self.poller_state == "session_expired":
                        raise StoreError("session_expired", "iLink 会话已过期，停止微信出站", status=503)
                    client_id, already_sent = self.store.prepare_chunk(message["controller_job_id"], index)
                    if already_sent:
                        continue
                    context = self.identity_store.context(self.identity, message["sender_id"])
                    response = await self.client.send_text(message["sender_id"], chunk, context, client_id)
                    ret = response.get("ret", 0)
                    errcode = response.get("errcode", 0)
                    if ret == SESSION_EXPIRED_ERRCODE or errcode == SESSION_EXPIRED_ERRCODE:
                        self.poller_state = "session_expired"
                        self.last_error = "session_expired"
                        raise StoreError("session_expired", "iLink 会话已过期，停止微信出站", status=503)
                    if ret not in {0, None} or errcode not in {0, None}:
                        code = "send_rate_limited" if ret == RATE_LIMIT_ERRCODE or errcode == RATE_LIMIT_ERRCODE else "send_failed"
                        self.store.mark_chunk(message["controller_job_id"], index, success=False, error_code=code)
                        raise ProtocolError(code, "微信发送失败", retryable=code == "send_rate_limited")
                    self.store.mark_chunk(message["controller_job_id"], index, success=True)
                    if index + 1 < len(chunks):
                        await asyncio.sleep(0.5)
        return None

    async def send_notification(self, message_id: str, text: str) -> None:
        """Send one deterministic notification to the single bound owner."""
        if self.poller_state == "session_expired":
            raise StoreError("session_expired", "iLink 会话已过期，停止微信出站", status=503)
        if self.identity is None or self.client is None:
            raise StoreError("credential_missing", "无法发送微信通知", status=503)
        client_id = "codex-weixin-notification-" + hashlib.sha256(message_id.encode("utf-8")).hexdigest()[:32]
        async with self._outbound_lock:
            async with self._authorization_lock:
                if self.poller_state == "session_expired":
                    raise StoreError("session_expired", "iLink 会话已过期，停止微信出站", status=503)
                owner_id, context = self.notification_owner_context()
                response = await self.client.send_text(owner_id, text, context, client_id)
        ret = response.get("ret", 0)
        errcode = response.get("errcode", 0)
        if ret == SESSION_EXPIRED_ERRCODE or errcode == SESSION_EXPIRED_ERRCODE:
            self.poller_state = "session_expired"
            self.last_error = "session_expired"
            raise StoreError("session_expired", "iLink 会话已过期，停止微信出站", status=503)
        if ret not in {0, None} or errcode not in {0, None}:
            code = "send_rate_limited" if ret == RATE_LIMIT_ERRCODE or errcode == RATE_LIMIT_ERRCODE else "send_failed"
            raise ProtocolError(code, "微信通知发送失败", retryable=code == "send_rate_limited")

    def notification_owner_context(self) -> tuple[str, str]:
        """Return the only authorized notification target or fail closed."""
        if self.identity is None:
            raise StoreError("credential_missing", "无法发送微信通知", status=503)
        owner = self.store.active_owner()
        owner_id = owner["private_user_id"]
        owners = self.identity.get("allowed_user_ids", [])
        if owners != [owner_id]:
            raise StoreError("notification_owner_mirror_invalid", "微信 owner 镜像不一致", status=409)
        context = self.identity_store.context(self.identity, owner_id)
        if not context:
            raise StoreError("notification_context_missing", "微信 owner 缺少当前会话上下文", status=409)
        return owner_id, context

    async def _cleanup_loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.store.cleanup_spool()
                self.store.expire_remote_work_tasks()
                await asyncio.sleep(300)
            except asyncio.CancelledError:
                return
            except Exception:
                await asyncio.sleep(60)

    async def start_qr_login(self) -> dict[str, Any]:
        if self.poller_state not in {"disabled", "stopped"}:
            raise StoreError("poller_active", "真实 Poller 运行时不能替换 iLink 身份", status=409)
        if self.qr_state and self.qr_state.get("state") in {"waiting", "scanned"}:
            return self.public_qr_state()
        client = IlinkClient(base_url="https://ilinkai.weixin.qq.com", cdn_base_url="https://novac2c.cdn.weixin.qq.com/c2c", token="", max_media_bytes=self.max_media_bytes)
        await client.start()
        response = await client.api_get(f"{EP_GET_BOT_QR}?bot_type=3")
        qrcode_value = str(response.get("qrcode") or "")
        qrcode_content = str(response.get("qrcode_img_content") or qrcode_value)
        if not qrcode_value:
            await client.close()
            raise ProtocolError("qr_invalid", "iLink 未返回二维码")
        self.qr_image_path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
        try:
            import qrcode

            image = qrcode.make(qrcode_content)
            image.save(self.qr_image_path)
            self.qr_image_path.chmod(0o600)
        except Exception as exc:
            await client.close()
            raise ProtocolError("qr_render_failed", "二维码生成失败") from exc
        self.qr_state = {"state": "waiting", "qrcode": qrcode_value, "has_image": True, "base_url": "https://ilinkai.weixin.qq.com"}
        self._tasks.append(asyncio.create_task(self._poll_qr(client), name="weixin-qr-login"))
        return self.public_qr_state()

    async def _poll_qr(self, client: IlinkClient) -> None:
        try:
            for _ in range(480):
                if self.qr_state is None:
                    return
                response = await client.api_get(
                    f"{EP_GET_QR_STATUS}?qrcode={self.qr_state['qrcode']}", base_url=self.qr_state["base_url"]
                )
                state = str(response.get("status") or "wait")
                if state == "scaned":
                    self.qr_state["state"] = "scanned"
                elif state == "scaned_but_redirect" and response.get("redirect_host"):
                    self.qr_state["base_url"] = f"https://{response['redirect_host']}"
                elif state == "expired":
                    self.qr_state["state"] = "expired"
                    return
                elif state == "confirmed":
                    async with self._authorization_lock:
                        self.identity_store.clear_owner_pairing()
                        previous_account = None if self.identity is None else self.identity.get("account_id")
                        identity = {
                            "account_id": str(response.get("ilink_bot_id") or ""),
                            "token": str(response.get("bot_token") or ""),
                            "base_url": str(response.get("baseurl") or "https://ilinkai.weixin.qq.com"),
                            "cdn_base_url": "https://novac2c.cdn.weixin.qq.com/c2c",
                            "user_id": str(response.get("ilink_user_id") or ""),
                            "allowed_user_ids": [],
                            "get_updates_buf": "",
                            "context_tokens": {},
                        }
                        if self.identity is not None and self.identity.get("account_id") != identity["account_id"]:
                            self.store.reset_access_directory_for_identity_replacement()
                        self.identity_store.save_identity(identity)
                        self.identity = self.identity_store.load_identity()
                        if previous_account is not None and previous_account == identity["account_id"]:
                            migration = self.store.migrate_identity_allowlist([])
                            owner_private_id = migration.get("owner_private_id")
                            if isinstance(owner_private_id, str) and self.identity is not None:
                                self.identity_store.mirror_owner(self.identity, owner_private_id)
                        self._refresh_client()
                        self.qr_state["state"] = "credential_ready"
                    return
                await asyncio.sleep(1)
        except (ProtocolError, StoreError, asyncio.CancelledError):
            if self.qr_state is not None and self.qr_state.get("state") not in {"credential_ready", "expired"}:
                self.qr_state["state"] = "failed"
        finally:
            await client.close()

    def public_qr_state(self) -> dict[str, Any]:
        if self.qr_state is None:
            return {"state": "idle", "has_image": False}
        return {"state": self.qr_state["state"], "has_image": bool(self.qr_state.get("has_image"))}

    def inspect_migration(self, reference: str) -> dict[str, Any]:
        return self.identity_store.inspect_migration(reference)

    def import_migration(self, reference: str, key: str) -> dict[str, Any]:
        if self.poller_state not in {"disabled", "stopped"}:
            raise StoreError("poller_active", "真实 Poller 运行时不能导入 iLink 身份", status=409)
        self.identity_store.clear_owner_pairing()
        previous_account = None if self.identity is None else self.identity.get("account_id")
        result = self.identity_store.import_migration(reference, key)
        self.identity = self.identity_store.load_identity()
        if self.identity is not None:
            if previous_account is not None and previous_account != self.identity.get("account_id"):
                self.store.reset_access_directory_for_identity_replacement()
            migration = self.store.migrate_identity_allowlist(list(self.identity.get("allowed_user_ids", [])))
            owner_private_id = migration.get("owner_private_id")
            if isinstance(owner_private_id, str) and self.identity.get("allowed_user_ids") != [owner_private_id]:
                self.identity_store.mirror_owner(self.identity, owner_private_id)
        self._refresh_client()
        return result

    def start_owner_pairing(self) -> dict[str, Any]:
        if not self.owner_pairing_enabled:
            raise StoreError("owner_pairing_disabled", "一次性 owner 绑定未启用", status=409)
        if self.identity is None:
            raise StoreError("credential_missing", "缺少完整 iLink 身份", status=409)
        if self.poller_state != "pairing":
            raise StoreError("owner_pairing_unavailable", "请先启动新身份配对 Poller", status=409)
        return self.identity_store.start_owner_pairing(self.identity)

    def users(self) -> dict[str, Any]:
        document = self.store.list_users()
        contexts = {} if self.identity is None else self.identity.get("context_tokens", {})
        private_by_short = {
            self.store.short_id("WX", user["user_hash"]): user["private_user_id"]
            for user in self._private_users()
        }
        for user in document["users"]:
            private_id = private_by_short.get(user["wx_short"])
            user["has_context"] = bool(private_id and contexts.get(private_id))
        return document

    def conversations(self) -> dict[str, Any]:
        return self.store.list_conversations()

    def create_member_invitation(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self.store.create_member_invitation(
            expected_revision=payload.get("revision"),
            request_id=payload.get("request_id"),
            ttl_seconds=payload.get("ttl_seconds", 15 * 60),
        )

    def cancel_member_invitation(self, invite_short: str, payload: dict[str, Any]) -> dict[str, Any]:
        return self.store.cancel_member_invitation(
            invite_short=invite_short,
            expected_revision=payload.get("revision"),
            request_id=payload.get("request_id"),
        )

    def update_user_alias(self, wx_short: str, payload: dict[str, Any]) -> dict[str, Any]:
        return self.store.update_alias(
            wx_short=wx_short,
            alias=str(payload.get("alias") or ""),
            expected_revision=payload.get("revision"),
            request_id=payload.get("request_id"),
        )

    async def change_user_state(self, wx_short: str, action: str, payload: dict[str, Any]) -> dict[str, Any]:
        async with self._authorization_lock:
            return self.store.change_user_state(
                wx_short=wx_short,
                action=action,
                expected_revision=payload.get("revision"),
                request_id=payload.get("request_id"),
            )

    async def transfer_owner(self, payload: dict[str, Any]) -> dict[str, Any]:
        if self.identity is None:
            raise StoreError("credential_missing", "缺少完整 iLink 身份", status=409)
        async with self._authorization_lock:
            result = self.store.transfer_owner(
                target_wx_short=str(payload.get("target_wx_short") or ""),
                expected_revision=payload.get("revision"),
                request_id=payload.get("request_id"),
                confirmation=str(payload.get("confirmation") or ""),
            )
            if result.pop("replayed", False):
                return result
            target_private_id = result.pop("owner_private_id")
            previous_private_id = result.pop("previous_owner_private_id")
            try:
                self.identity_store.mirror_owner(self.identity, target_private_id)
            except Exception as exc:
                self.store.restore_owner_after_mirror_failure(
                    previous_private_id,
                    target_private_id,
                    str(payload.get("request_id") or ""),
                )
                raise StoreError("owner_identity_mirror_failed", "Owner 转移未能同步身份镜像", status=500) from exc
            return result

    def _private_users(self) -> list[dict[str, Any]]:
        with self.store._connect() as connection:
            return [self.store._private_user_document(row) for row in connection.execute("SELECT * FROM weixin_users")]

    def status(self) -> dict[str, Any]:
        identity = None if self.identity is None else self.identity_store.public_summary(self.identity)
        users = self.users()
        active_users = sum(1 for user in users["users"] if user["status"] == "active")
        return {
            "version": "0.2.1",
            "poller_enabled": self.poller_enabled,
            "poller_state": self.poller_state,
            "identity": identity,
            "owner_pairing": self.identity_store.owner_pairing_summary(self.identity),
            "controller_configured": self.controller.configured,
            "controller_capability_state": getattr(self.controller, "capability_state", "unknown"),
            "users": {
                "revision": users["revision"],
                "total": len(users["users"]),
                "active": active_users,
                "members": sum(1 for user in users["users"] if user["role"] == "member"),
            },
            "invitations": self.store.invitation_summary(),
            "last_poll_at": self.last_poll_at,
            "last_message_at": self.last_message_at,
            "last_error": self.last_error,
            "qr": self.public_qr_state(),
            "queue": self.store.status(),
            "remote_work": {
                "enabled": self.remote_work_enabled,
                **self.store.remote_work_status(),
            },
        }


def split_text(text: str, limit: int) -> list[str]:
    if len(text) <= limit:
        return [text]
    chunks: list[str] = []
    remaining = text
    while remaining:
        if len(remaining) <= limit:
            chunks.append(remaining)
            break
        boundary = max(remaining.rfind("\n", 0, limit + 1), remaining.rfind("。", 0, limit + 1))
        if boundary < limit // 2:
            boundary = limit
        else:
            boundary += 1
        chunks.append(remaining[:boundary])
        remaining = remaining[boundary:]
    return chunks


def with_thread_short(text: str, thread_short: Any) -> str:
    """Append the public Thread identifier once to every Controller reply."""
    if not isinstance(thread_short, str) or not re_fullmatch_thread_short(thread_short):
        return text
    marker = f"Thread：{thread_short}"
    return text if marker in text else f"{text.rstrip()}\n\n{marker}"


def re_fullmatch_thread_short(value: str) -> bool:
    return len(value) == 13 and value.startswith("TH-") and all(character in "ABCDEFGHIJKLMNOPQRSTUVWXYZ234567" for character in value[3:])
