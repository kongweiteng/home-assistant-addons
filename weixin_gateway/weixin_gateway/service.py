"""Single-poller iLink service and durable Controller delivery loop."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
import datetime as dt
import hashlib
import hmac
import json
from pathlib import Path
import re
import threading
import time
from typing import Any
from urllib.parse import urlsplit

import aiohttp

from .protocol import (
    IlinkClient,
    ProtocolError,
    RATE_LIMIT_ERRCODE,
    SESSION_EXPIRED_ERRCODE,
    TYPING_STATUS_START,
    TYPING_STATUS_STOP,
    extract_message,
    is_stale_context_response,
)
from .remote_work import (
    GatewayRemoteWorkRuntime,
    WorkCommand,
    WorkCommandError,
    build_command_document,
    parse_work_command,
)
from .remote_work_v2 import (
    ROUTE_REJECT,
    ROUTE_V1,
    ROUTE_V2,
    RunnerManagerContractError,
    RunnerManagerRequest,
    RunnerManagerResponseError,
    RunnerManagerResult,
    WorkCommand as RunnerManagerWorkCommand,
    build_runner_manager_request,
    parse_runner_manager_response,
    select_work_route,
)
from .store import (
    GatewayStore,
    IdentityStore,
    StoreError,
    TokenLock,
    account_hash,
    routed_message_id,
    utc_now,
)


TYPING_REFRESH_SECONDS = 5.0
TYPING_TICKET_TTL_SECONDS = 23 * 60 * 60
POLLER_MAINTENANCE_MIN_SECONDS = 30
POLLER_MAINTENANCE_MAX_SECONDS = 30 * 60
RUNNER_MANAGER_V2_TERMINAL_STATES = frozenset({"completed", "failed", "cancelled", "expired"})
CONTROLLER_FAILURE_MESSAGES = {
    "context_window_exceeded": "这次对话内容过长，已停止处理。请新开一个对话或缩短问题后重试。",
    "session_budget_exceeded": "Codex 当前会话预算已用完，任务未完成。请新开一个对话后再试。",
    "usage_limit_exceeded": "Codex 当前使用额度受限，任务未完成。请稍后再试或检查账户额度。",
    "codex_unauthorized": "Codex 登录已失效，任务未执行完成。请在 Controller 页面重新登录后再试。",
    "codex_bad_request": "本次请求不受支持，且未自动重试。请调整问题后再试。",
    "cyber_policy_rejected": "该请求未通过安全策略，未执行。",
    "sandbox_error": "任务受运行环境限制未完成，请调整请求或在 Controller 页面核对。",
    "thread_rollback_failed": "Codex 会话恢复失败，任务未完成。请新开一个对话后再试。",
    "active_turn_not_steerable": "当前 Codex 任务状态不允许继续处理，请等待现有任务结束后再试。",
    "app_server_overloaded": "上游服务暂时不稳定，自动重试后仍未完成。请稍后再试。",
    "upstream_http_connection_failed": "上游连接暂时不稳定，自动重试后仍未完成。请稍后再试。",
    "response_stream_connection_failed": "上游响应连接中断，自动重试后仍未完成。请稍后再试。",
    "response_stream_disconnected": "上游响应流中断，自动重试后仍未完成。请稍后再试。",
    "response_too_many_failed_attempts": "上游连续失败，自动重试后仍未完成。请稍后再试。",
    "upstream_internal_server_error": "上游服务发生内部错误，自动重试后仍未完成。请稍后再试。",
}


def controller_failure_message(state: str, error_code: Any) -> str:
    if state == "recovery_required":
        return "任务状态需要人工核对，请在 Codex Controller 页面查看。"
    if state == "cancelled":
        return "任务已取消。"
    if isinstance(error_code, str):
        mapped = CONTROLLER_FAILURE_MESSAGES.get(error_code)
        if mapped is not None:
            return mapped
    return "任务未完成，请在 Codex Controller 页面查看错误状态。"


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


def validate_controller_ingress_base_url(value: str) -> str:
    if not value:
        return ""
    if len(value) > 2048:
        raise StoreError("controller_ingress_url_invalid", "Controller Ingress 地址过长")
    parsed = urlsplit(value)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        raise StoreError("controller_ingress_url_invalid", "Controller Ingress 必须是固定 HTTPS 地址")
    port = f":{parsed.port}" if parsed.port else ""
    path = parsed.path.rstrip("/")
    return f"https://{parsed.hostname.lower()}{port}{path}"


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

    async def artifact(self, job_id: str, artifact: dict[str, Any], *, max_bytes: int) -> bytes:
        artifact_id = artifact.get("artifact_id")
        expected_size = artifact.get("size_bytes")
        expected_digest = artifact.get("sha256")
        if not isinstance(job_id, str) or not re.fullmatch(r"[0-9a-f-]{36}", job_id):
            raise StoreError("artifact_invalid", "Controller artifact job_id 无效", status=502)
        if not isinstance(artifact_id, str) or not re.fullmatch(r"AR-[A-Z2-7]{26}", artifact_id):
            raise StoreError("artifact_invalid", "Controller artifact_id 无效", status=502)
        artifact_type = artifact.get("type")
        mime_type = artifact.get("mime_type")
        filename = artifact.get("filename") or ("artifact.png" if mime_type == "image/png" else "artifact.zip")
        if artifact_type not in {"image", "file"} or mime_type not in {"image/png", "application/zip"}:
            raise StoreError("artifact_invalid", "Controller artifact 类型无效", status=502)
        if artifact_type == "image" and mime_type != "image/png":
            raise StoreError("artifact_invalid", "图片 artifact 类型无效", status=502)
        if not isinstance(filename, str) or not filename or len(filename) > 255 or Path(filename).name != filename:
            raise StoreError("artifact_invalid", "Controller artifact 文件名无效", status=502)
        if (
            isinstance(expected_size, bool)
            or not isinstance(expected_size, int)
            or not 1 <= expected_size <= max_bytes
            or not isinstance(expected_digest, str)
            or not re.fullmatch(r"sha256:[a-f0-9]{64}", expected_digest)
        ):
            raise StoreError("artifact_invalid", "Controller artifact 元数据无效", status=502)
        if not self.configured:
            raise StoreError("controller_unavailable", "Codex Controller 未配置", status=503)
        if self.session is None:
            await self.start()
        assert self.session is not None
        try:
            async with self.session.get(
                f"{self.base_url}/internal/v1/jobs/{job_id}/artifacts/{artifact_id}",
                headers={"Authorization": f"Bearer {self.token}", "Accept": str(mime_type)},
                timeout=aiohttp.ClientTimeout(total=30),
            ) as response:
                if response.status < 200 or response.status >= 300:
                    await response.content.read(64 * 1024)
                    raise StoreError("artifact_unavailable", "Controller artifact 暂不可用", status=response.status)
                length_header = response.headers.get("Content-Length")
                digest_header = response.headers.get("X-Content-SHA256")
                if response.content_type != mime_type or not length_header:
                    raise StoreError("artifact_invalid", "Controller artifact 响应头无效", status=502)
                try:
                    declared_size = int(length_header)
                except ValueError as exc:
                    raise StoreError("artifact_invalid", "Controller artifact 长度无效", status=502) from exc
                if declared_size != expected_size or digest_header != expected_digest:
                    raise StoreError("artifact_invalid", "Controller artifact 响应元数据不一致", status=502)
                content_buffer = bytearray()
                while len(content_buffer) <= expected_size:
                    remaining = expected_size + 1 - len(content_buffer)
                    chunk = await response.content.read(min(64 * 1024, remaining))
                    if not chunk:
                        break
                    content_buffer.extend(chunk)
                    if len(content_buffer) > expected_size or len(content_buffer) > max_bytes:
                        raise StoreError("artifact_invalid", "Controller artifact 实际大小超限", status=502)
                content = bytes(content_buffer)
        except StoreError:
            raise
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            raise StoreError("artifact_unavailable", "Controller artifact 下载失败", status=503) from exc
        if len(content) != expected_size or len(content) > max_bytes:
            raise StoreError("artifact_invalid", "Controller artifact 实际大小不一致", status=502)
        if hashlib.sha256(content).hexdigest() != expected_digest.removeprefix("sha256:"):
            raise StoreError("artifact_invalid", "Controller artifact 摘要不一致", status=502)
        return content

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

    async def runner_manager(self, request: RunnerManagerRequest) -> RunnerManagerResult:
        """Call the deterministic v2 API and validate its bounded response."""
        if not self.configured:
            raise StoreError("controller_unavailable", "Codex Controller 未配置", status=503)
        if self.session is None:
            await self.start()
        assert self.session is not None
        body = json.dumps(
            request.body,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        try:
            async with self.session.request(
                request.method,
                f"{self.base_url}{request.path}",
                data=body,
                headers={
                    "Authorization": f"Bearer {self.token}",
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                    "X-Body-Digest": request.body_digest,
                },
                timeout=aiohttp.ClientTimeout(total=20),
            ) as response:
                raw = await response.content.read(32 * 1024 + 1)
                if len(raw) > 32 * 1024:
                    raise StoreError("controller_invalid_response", "Runner Manager 响应过大", status=502)
                try:
                    document = json.loads(raw)
                except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                    raise StoreError("controller_invalid_response", "Runner Manager 响应 JSON 无效", status=502) from exc
                try:
                    return parse_runner_manager_response(
                        response.status,
                        document,
                        expected_request_id=request.request_id,
                        expected_operation=str(request.body["operation"]),
                    )
                except RunnerManagerResponseError as exc:
                    raise StoreError(exc.code, str(exc), status=exc.status_code) from exc
                except RunnerManagerContractError as exc:
                    raise StoreError("controller_invalid_response", str(exc), status=502) from exc
        except StoreError:
            raise
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            raise StoreError("controller_unavailable", "Runner Manager 不可用", status=503) from exc

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


@dataclass
class TypingSession:
    sender_id: str
    ticket: str
    message_ids: set[str] = field(default_factory=set)
    task: asyncio.Task[Any] | None = None


@dataclass
class IdentityRuntime:
    identity: dict[str, Any]
    client: IlinkClient
    token_lock: TokenLock | None = None
    poller_state: str = "disabled"
    last_error: str | None = None
    last_poll_at: str | None = None
    last_message_at: str | None = None
    pairing_session_id: str | None = None
    outbound_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    poll_task: asyncio.Task[Any] | None = None
    typing_tickets: dict[str, str] = field(default_factory=dict)
    typing_ticket_fetched_at: dict[str, float] = field(default_factory=dict)
    typing_sessions: dict[str, TypingSession] = field(default_factory=dict)

    @property
    def identity_id(self) -> str:
        return str(self.identity["identity_id"])


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
        controller_ingress_base_url: str = "",
        remote_work_enabled: bool = False,
        runner_manager_v2_enabled: bool = False,
        remote_work_ttl_seconds: int = 1800,
        max_active_identities: int = 5,
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
            self.store.migrate_legacy_identity(
                identity_identifier=self.identity["identity_id"],
                account_digest=account_hash(self.identity["account_id"]),
            )
        self.poller_default_enabled = poller_enabled
        stored_poller = self.store.poller_control()
        self.poller_enabled = (
            poller_enabled
            if stored_poller["override"] is None
            else stored_poller["override"] == "enabled"
        )
        self.owner_pairing_enabled = owner_pairing_enabled
        self.activation_confirmation = activation_confirmation
        self.max_media_bytes = max_media_bytes
        self.controller_ingress_base_url = validate_controller_ingress_base_url(
            controller_ingress_base_url
        )
        self.remote_work_enabled = remote_work_enabled
        self.runner_manager_v2_enabled = runner_manager_v2_enabled
        self.remote_work_ttl_seconds = remote_work_ttl_seconds
        if not isinstance(max_active_identities, int) or isinstance(max_active_identities, bool) or not 1 <= max_active_identities <= 32:
            raise StoreError("identity_limit_invalid", "活动 ClawBot 上限无效")
        self.max_active_identities = max_active_identities
        self.remote_work_runtime: GatewayRemoteWorkRuntime | None = None
        self.client: IlinkClient | None = None
        self.token_lock: TokenLock | None = None
        self.poller_state = "disabled"
        self.last_error: str | None = None
        self.last_poll_at: str | None = None
        self.last_message_at: str | None = None
        self.qr_state: dict[str, Any] | None = None
        self.qr_image_path = identity_store.data_dir / "qr" / "current.png"
        self.member_qr_state: dict[str, Any] | None = None
        self.member_qr_image_path = identity_store.data_dir / "qr" / "member-current.png"
        self._qr_task: asyncio.Task[Any] | None = None
        self._member_qr_task: asyncio.Task[Any] | None = None
        self._tasks: list[asyncio.Task[Any]] = []
        self._status_lock = threading.Lock()
        self._stop = asyncio.Event()
        self._outbound_lock = asyncio.Lock()
        self._authorization_lock = asyncio.Lock()
        self._poller_control_lock = asyncio.Lock()
        self._poller_maintenance_task: asyncio.Task[Any] | None = None
        self._poller_maintenance_request_id: str | None = None
        self._poller_maintenance_expires_at: str | None = None
        self._poller_maintenance_resume_enabled = self.poller_enabled
        self._runtimes: dict[str, IdentityRuntime] = {}
        self._refresh_client()
        self._ensure_owner_runtime()
        self._load_secondary_runtimes()

    def bind_remote_work_runtime(self, runtime: GatewayRemoteWorkRuntime) -> None:
        self.remote_work_runtime = runtime

    async def start(self) -> None:
        await self.controller.start()
        self._refresh_client()
        self._ensure_owner_runtime()
        if self.poller_enabled:
            async with self._poller_control_lock:
                await self._start_pollers_unlocked()
        self._tasks.append(asyncio.create_task(self._delivery_loop(), name="weixin-controller-delivery"))
        self._tasks.append(asyncio.create_task(self._cleanup_loop(), name="weixin-spool-cleanup"))

    async def start_poller(self, payload: dict[str, Any]) -> dict[str, Any]:
        async with self._poller_control_lock:
            self._cancel_poller_maintenance_unlocked()
            response = self.store.set_poller_enabled(
                True,
                expected_revision=payload.get("revision"),
                request_id=str(payload.get("request_id") or ""),
            )
            self.poller_enabled = True
            await self._start_pollers_unlocked()
            return {**response, "poller_state": self.poller_state}

    async def stop_poller(self, payload: dict[str, Any]) -> dict[str, Any]:
        async with self._poller_control_lock:
            self._cancel_poller_maintenance_unlocked()
            response = self.store.set_poller_enabled(
                False,
                expected_revision=payload.get("revision"),
                request_id=str(payload.get("request_id") or ""),
            )
            self.poller_enabled = False
            await self._stop_pollers_unlocked()
            return {**response, "poller_state": self.poller_state}

    async def pause_poller_maintenance(self, payload: dict[str, Any]) -> dict[str, Any]:
        request_id = str(payload.get("request_id") or "")
        self.store.validate_request_id(request_id)
        duration_seconds = payload.get("duration_seconds")
        if (
            isinstance(duration_seconds, bool)
            or not isinstance(duration_seconds, int)
            or not POLLER_MAINTENANCE_MIN_SECONDS <= duration_seconds <= POLLER_MAINTENANCE_MAX_SECONDS
        ):
            raise StoreError("maintenance_duration_invalid", "维护暂停时长必须为 30～1800 秒")
        async with self._poller_control_lock:
            if self._poller_maintenance_request_id == request_id:
                return self._poller_maintenance_document(replayed=True)
            if self._poller_maintenance_task is not None:
                raise StoreError("maintenance_pause_active", "已有维护暂停正在生效", status=409)
            self._poller_maintenance_request_id = request_id
            self._poller_maintenance_resume_enabled = self.poller_enabled
            expires = dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=duration_seconds)
            self._poller_maintenance_expires_at = expires.isoformat()
            if self.poller_enabled:
                await self._stop_pollers_unlocked()
            task = asyncio.create_task(
                self._poller_maintenance_timeout(request_id, duration_seconds),
                name="weixin-poller-maintenance-timeout",
            )
            self._poller_maintenance_task = task
            self._tasks.append(task)
            return self._poller_maintenance_document()

    async def resume_poller_maintenance(self, payload: dict[str, Any]) -> dict[str, Any]:
        request_id = str(payload.get("request_id") or "")
        self.store.validate_request_id(request_id)
        async with self._poller_control_lock:
            active_request_id = self._poller_maintenance_request_id
            if active_request_id is None:
                return self._poller_maintenance_document(replayed=True)
            if request_id != active_request_id:
                raise StoreError("maintenance_request_conflict", "维护恢复 request_id 与当前租约不一致", status=409)
            resume_enabled = self._poller_maintenance_resume_enabled
            self._cancel_poller_maintenance_unlocked()
            if resume_enabled and self.poller_enabled:
                await self._start_pollers_unlocked()
            return self._poller_maintenance_document()

    async def _poller_maintenance_timeout(self, request_id: str, duration_seconds: int) -> None:
        await asyncio.sleep(duration_seconds)
        async with self._poller_control_lock:
            if self._poller_maintenance_request_id != request_id:
                return
            resume_enabled = self._poller_maintenance_resume_enabled
            self._clear_poller_maintenance_unlocked()
            if resume_enabled and self.poller_enabled:
                await self._start_pollers_unlocked()

    def _poller_maintenance_document(self, *, replayed: bool = False) -> dict[str, Any]:
        active = self._poller_maintenance_request_id is not None
        return {
            "active": active,
            "request_id": self._poller_maintenance_request_id,
            "expires_at": self._poller_maintenance_expires_at,
            "resume_enabled": self._poller_maintenance_resume_enabled if active else None,
            "poller_state": self.poller_state,
            "poller_enabled": self.poller_enabled,
            "replayed": replayed,
        }

    def _clear_poller_maintenance_unlocked(self) -> None:
        task = self._poller_maintenance_task
        if task is not None:
            self._tasks = [item for item in self._tasks if item is not task]
        self._poller_maintenance_task = None
        self._poller_maintenance_request_id = None
        self._poller_maintenance_expires_at = None
        self._poller_maintenance_resume_enabled = self.poller_enabled

    def _cancel_poller_maintenance_unlocked(self) -> None:
        task = self._poller_maintenance_task
        if task is not None and task is not asyncio.current_task():
            task.cancel()
        self._clear_poller_maintenance_unlocked()

    async def _start_pollers_unlocked(self) -> None:
        if self.identity is None or self.client is None or not self._runtimes:
            self.poller_state = "disabled"
            self.last_error = "credential_missing"
            return
        if not self.identity.get("allowed_user_ids") and not self.owner_pairing_enabled:
            raise StoreError("owner_binding_required", "新身份必须先启用一次性 owner 绑定", status=409)
        for runtime in list(self._runtimes.values()):
            if runtime.poll_task is not None and not runtime.poll_task.done():
                continue
            record = self.store.identity_record(runtime.identity_id)
            if record["state"] not in {"active", "pending_pairing"}:
                continue
            runtime.token_lock = self.identity_store.acquire_token_lock(runtime.identity["token"])
            try:
                runtime.token_lock.acquire()
            except StoreError as exc:
                self._set_runtime_state(runtime, "token_conflict", error_code=exc.code)
                continue
            if self.identity is not None and runtime.identity_id == self.identity["identity_id"]:
                self.token_lock = runtime.token_lock
            state = "pairing" if record["state"] == "pending_pairing" or not runtime.identity.get("allowed_user_ids") else "polling"
            self._set_runtime_state(runtime, state)
            runtime.poll_task = asyncio.create_task(
                self._poll_loop(runtime),
                name=f"weixin-poller-{runtime.identity_id[-8:]}",
            )
            self._tasks.append(runtime.poll_task)

    async def _stop_pollers_unlocked(self) -> None:
        for runtime in self._runtimes.values():
            await self._stop_all_typing(runtime)
        poll_tasks = {runtime.poll_task for runtime in self._runtimes.values() if runtime.poll_task is not None}
        for task in poll_tasks:
            if task is not None:
                task.cancel()
        if poll_tasks:
            await asyncio.gather(*poll_tasks, return_exceptions=True)
        self._tasks = [task for task in self._tasks if task not in poll_tasks]
        for runtime in self._runtimes.values():
            runtime.poll_task = None
            if runtime.token_lock is not None:
                runtime.token_lock.release()
                runtime.token_lock = None
            self._set_runtime_state(runtime, "stopped")
        self.token_lock = None
        self.poller_state = "stopped"

    async def stop(self) -> None:
        self._stop.set()
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        async with self._poller_control_lock:
            self._cancel_poller_maintenance_unlocked()
            await self._stop_pollers_unlocked()
        closed_clients: set[int] = set()
        for runtime in self._runtimes.values():
            if id(runtime.client) not in closed_clients:
                await runtime.client.close()
                closed_clients.add(id(runtime.client))
        await self.controller.close()
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

        runtime = self._runtimes.get(self.identity["identity_id"])
        if runtime is None:
            runtime = IdentityRuntime(
                identity=self.identity,
                client=self.client,
                outbound_lock=self._outbound_lock,
            )
            self._runtimes[runtime.identity_id] = runtime
        else:
            runtime.identity = self.identity
            runtime.client = self.client
            runtime.outbound_lock = self._outbound_lock

    def _ensure_owner_runtime(self) -> None:
        if self.identity is None or self.client is None:
            return
        identity_identifier = self.identity["identity_id"]
        runtime = self._runtimes.get(identity_identifier)
        if runtime is None:
            self._runtimes[identity_identifier] = IdentityRuntime(
                identity=self.identity,
                client=self.client,
                outbound_lock=self._outbound_lock,
            )
            return
        runtime.identity = self.identity
        runtime.client = self.client
        runtime.outbound_lock = self._outbound_lock

    def _load_secondary_runtimes(self) -> None:
        owner_identity_id = None if self.identity is None else self.identity["identity_id"]
        records = {record["account_hash"]: record for record in self.store.identity_records()}
        for identity in self.identity_store.load_all_identities():
            identity_identifier = identity["identity_id"]
            if identity_identifier == owner_identity_id or identity_identifier in self._runtimes:
                continue
            record = records.get(account_hash(identity["account_id"]))
            if record is None or record["state"] not in {"active", "pending_pairing", "paused", "session_expired"}:
                continue
            runtime = IdentityRuntime(
                identity=identity,
                client=IlinkClient(
                    base_url=identity["base_url"],
                    cdn_base_url=identity["cdn_base_url"],
                    token=identity["token"],
                    max_media_bytes=self.max_media_bytes,
                ),
                poller_state=str(record["runtime_state"] or "stopped"),
                last_error=record["last_error"],
            )
            if record["state"] == "pending_pairing":
                onboarding = self.store.pending_onboarding_for_identity(identity_identifier)
                runtime.pairing_session_id = None if onboarding is None else str(onboarding["session_id"])
            self._runtimes[identity_identifier] = runtime

    def _runtime_for_identity(self, identity_identifier: str | None) -> IdentityRuntime:
        if identity_identifier is None and self.identity is not None:
            identity_identifier = self.identity["identity_id"]
        runtime = None if identity_identifier is None else self._runtimes.get(identity_identifier)
        if runtime is None:
            raise StoreError("identity_runtime_unavailable", "ClawBot 运行时不可用", status=503)
        if self.identity is not None and identity_identifier == self.identity["identity_id"]:
            if self.client is None:
                raise StoreError("credential_missing", "无法回传微信消息", status=503)
            runtime.identity = self.identity
            runtime.client = self.client
            runtime.token_lock = self.token_lock
            runtime.poller_state = self.poller_state
            runtime.last_error = self.last_error
            runtime.last_poll_at = self.last_poll_at
            runtime.last_message_at = self.last_message_at
        return runtime

    def _set_runtime_state(
        self,
        runtime: IdentityRuntime,
        poller_state: str,
        *,
        error_code: str | None = None,
        identity_state: str | None = None,
    ) -> None:
        runtime.poller_state = poller_state
        runtime.last_error = error_code
        if self.identity is not None and runtime.identity_id == self.identity["identity_id"]:
            self.poller_state = poller_state
            self.last_error = error_code
        try:
            self.store.set_identity_runtime_state(
                runtime.identity_id,
                poller_state,
                error_code=error_code,
                identity_state=identity_state,
            )
        except StoreError as exc:
            if exc.code != "identity_not_found":
                raise

    def _touch_runtime_message(self, runtime: IdentityRuntime) -> None:
        runtime.last_message_at = utc_now()
        if self.identity is not None and runtime.identity_id == self.identity["identity_id"]:
            self.last_message_at = runtime.last_message_at

    @staticmethod
    def _ilink_response_code(response: dict[str, Any]) -> int:
        for key in ("ret", "errcode"):
            value = response.get(key)
            if value not in {None, 0}:
                try:
                    return int(value)
                except (TypeError, ValueError):
                    return 1
        return 0

    def _accept_typing_error(self, runtime: IdentityRuntime, code: str) -> None:
        if code == "session_expired":
            try:
                self._set_runtime_state(
                    runtime,
                    "session_expired",
                    error_code="session_expired",
                    identity_state="session_expired",
                )
            except StoreError:
                runtime.last_error = "session_expired"
            return
        runtime.last_error = code

    def _accept_typing_response(self, runtime: IdentityRuntime, response: dict[str, Any]) -> bool:
        code = self._ilink_response_code(response)
        if code == SESSION_EXPIRED_ERRCODE:
            self._accept_typing_error(runtime, "session_expired")
            return False
        if code != 0:
            runtime.last_error = "typing_send_failed"
            return False
        return True

    async def _start_typing(self, message: dict[str, Any]) -> None:
        """Best-effort start and keepalive for a Controller-bound conversation."""
        try:
            runtime = self._runtime_for_identity(message.get("identity_id"))
        except StoreError:
            return
        if runtime.poller_state == "session_expired":
            return
        sender_id = str(message.get("sender_id") or "")
        message_id = str(message.get("message_id") or "")
        if not sender_id or not message_id:
            return
        existing = runtime.typing_sessions.get(sender_id)
        if existing is not None:
            existing.message_ids.add(message_id)
            return
        context_token = self.identity_store.context(runtime.identity, sender_id) or None
        async with runtime.outbound_lock:
            existing = runtime.typing_sessions.get(sender_id)
            if existing is not None:
                existing.message_ids.add(message_id)
                return
            ticket = runtime.typing_tickets.get(sender_id)
            ticket_fetched_at = runtime.typing_ticket_fetched_at.get(sender_id, 0.0)
            if ticket and time.monotonic() - ticket_fetched_at >= TYPING_TICKET_TTL_SECONDS:
                ticket = None
            try:
                if not ticket:
                    response = await runtime.client.get_config(sender_id, context_token)
                    if not self._accept_typing_response(runtime, response):
                        return
                    ticket_value = response.get("typing_ticket")
                    if not isinstance(ticket_value, str) or not ticket_value.strip():
                        runtime.last_error = "typing_ticket_missing"
                        return
                    ticket = ticket_value.strip()
                    runtime.typing_tickets[sender_id] = ticket
                    runtime.typing_ticket_fetched_at[sender_id] = time.monotonic()
                response = await runtime.client.send_typing(sender_id, ticket, TYPING_STATUS_START)
            except ProtocolError as exc:
                self._accept_typing_error(runtime, exc.code)
                return
            except Exception:
                runtime.last_error = "typing_send_failed"
                return
            if not self._accept_typing_response(runtime, response):
                return
            session = TypingSession(sender_id=sender_id, ticket=ticket, message_ids={message_id})
            runtime.typing_sessions[sender_id] = session
            session.task = asyncio.create_task(
                self._typing_keepalive(runtime, sender_id),
                name=f"weixin-typing-{runtime.identity_id[-8:]}-{sender_id[-8:]}",
            )

    async def _typing_keepalive(self, runtime: IdentityRuntime, sender_id: str) -> None:
        try:
            while not self._stop.is_set():
                await asyncio.sleep(TYPING_REFRESH_SECONDS)
                async with runtime.outbound_lock:
                    session = runtime.typing_sessions.get(sender_id)
                    if session is None or not session.message_ids:
                        return
                    if runtime.poller_state == "session_expired":
                        return
                    try:
                        response = await runtime.client.send_typing(
                            sender_id,
                            session.ticket,
                            TYPING_STATUS_START,
                        )
                    except ProtocolError as exc:
                        self._accept_typing_error(runtime, exc.code)
                        if runtime.poller_state == "session_expired":
                            return
                        continue
                    except Exception:
                        runtime.last_error = "typing_send_failed"
                        continue
                    if not self._accept_typing_response(runtime, response) and runtime.poller_state == "session_expired":
                        return
        except asyncio.CancelledError:
            return

    async def _stop_typing_for_message(self, message: dict[str, Any]) -> None:
        try:
            runtime = self._runtime_for_identity(message.get("identity_id"))
        except StoreError:
            return
        sender_id = str(message.get("sender_id") or "")
        message_id = str(message.get("message_id") or "")
        if not sender_id or not message_id:
            return
        async with runtime.outbound_lock:
            session = runtime.typing_sessions.get(sender_id)
            if session is None:
                return
            session.message_ids.discard(message_id)
            if session.message_ids:
                return
            runtime.typing_sessions.pop(sender_id, None)
            task = session.task
            if task is not None and task is not asyncio.current_task():
                task.cancel()
        if task is not None and task is not asyncio.current_task():
            await asyncio.gather(task, return_exceptions=True)
        async with runtime.outbound_lock:
            if sender_id in runtime.typing_sessions or runtime.poller_state == "session_expired":
                return
            try:
                response = await runtime.client.send_typing(sender_id, session.ticket, TYPING_STATUS_STOP)
            except ProtocolError as exc:
                self._accept_typing_error(runtime, exc.code)
                return
            except Exception:
                return
            self._accept_typing_response(runtime, response)

    async def _stop_all_typing(self, runtime: IdentityRuntime) -> None:
        for sender_id in list(runtime.typing_sessions):
            async with runtime.outbound_lock:
                session = runtime.typing_sessions.pop(sender_id, None)
                if session is None:
                    continue
                task = session.task
                if task is not None and task is not asyncio.current_task():
                    task.cancel()
            if task is not None and task is not asyncio.current_task():
                await asyncio.gather(task, return_exceptions=True)
            if runtime.poller_state == "session_expired":
                continue
            async with runtime.outbound_lock:
                if sender_id in runtime.typing_sessions:
                    continue
                try:
                    response = await runtime.client.send_typing(
                        sender_id,
                        session.ticket,
                        TYPING_STATUS_STOP,
                    )
                except ProtocolError as exc:
                    self._accept_typing_error(runtime, exc.code)
                    continue
                except Exception:
                    continue
                self._accept_typing_response(runtime, response)

    async def _stop_identity_runtime(self, runtime: IdentityRuntime) -> None:
        await self._stop_all_typing(runtime)
        if runtime.poll_task is not None:
            runtime.poll_task.cancel()
            await asyncio.gather(runtime.poll_task, return_exceptions=True)
            runtime.poll_task = None
        if runtime.token_lock is not None:
            runtime.token_lock.release()
            runtime.token_lock = None
        self._set_runtime_state(runtime, "stopped")

    async def _resume_identity_runtime(self, runtime: IdentityRuntime) -> None:
        if not self.poller_enabled:
            self._set_runtime_state(runtime, "stopped")
            return
        if runtime.poll_task is not None and not runtime.poll_task.done():
            return
        runtime.token_lock = self.identity_store.acquire_token_lock(runtime.identity["token"])
        try:
            runtime.token_lock.acquire()
        except StoreError as exc:
            self._set_runtime_state(runtime, "token_conflict", error_code=exc.code)
            return
        record = self.store.identity_record(runtime.identity_id)
        pairing = record["state"] == "pending_pairing" or not runtime.identity.get("allowed_user_ids")
        self._set_runtime_state(
            runtime,
            "pairing" if pairing else "polling",
            identity_state="pending_pairing" if pairing else "active",
        )
        runtime.poll_task = asyncio.create_task(
            self._poll_loop(runtime),
            name=f"weixin-poller-{runtime.identity_id[-8:]}",
        )
        self._tasks.append(runtime.poll_task)

    async def _remove_identity_runtime(self, runtime: IdentityRuntime) -> None:
        await self._stop_identity_runtime(runtime)
        await runtime.client.close()
        self._runtimes.pop(runtime.identity_id, None)

    async def _discard_identity_runtime(self, runtime: IdentityRuntime) -> None:
        """Stop a non-owner runtime and remove credentials that must not survive a terminal revoke."""
        await self._stop_all_typing(runtime)
        if runtime.poll_task is not None and runtime.poll_task is not asyncio.current_task():
            runtime.poll_task.cancel()
            await asyncio.gather(runtime.poll_task, return_exceptions=True)
        runtime.poll_task = None
        if runtime.token_lock is not None:
            runtime.token_lock.release()
            runtime.token_lock = None
        await runtime.client.close()
        self._runtimes.pop(runtime.identity_id, None)
        self.identity_store.remove_identity(runtime.identity)

    async def _poll_loop(self, runtime: IdentityRuntime | None = None) -> None:
        current = runtime or self._runtime_for_identity(None)
        cursor = self.identity_store.cursor(current.identity)
        timeout_ms = 35000
        failures = 0
        while not self._stop.is_set():
            try:
                response = await current.client.get_updates(cursor, timeout_ms=timeout_ms)
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
                        self._set_runtime_state(
                            current,
                            "session_expired",
                            error_code="session_expired",
                            identity_state="session_expired",
                        )
                        return
                    failures += 1
                    current.last_error = "poll_failed"
                    if self.identity is not None and current.identity_id == self.identity["identity_id"]:
                        self.last_error = "poll_failed"
                    await asyncio.sleep(30 if failures >= 3 else 2)
                    if failures >= 3:
                        failures = 0
                    continue
                failures = 0
                current.last_poll_at = utc_now()
                if self.identity is not None and current.identity_id == self.identity["identity_id"]:
                    self.last_poll_at = current.last_poll_at
                self.store.set_identity_runtime_state(current.identity_id, current.poller_state)
                for raw_message in response.get("msgs") or []:
                    if isinstance(raw_message, dict):
                        await self._ingest(raw_message, current)
                        if self.store.identity_record(current.identity_id)["state"] == "revoked":
                            return
                new_cursor = str(response.get("get_updates_buf") or "")
                if new_cursor and new_cursor != cursor:
                    self.identity_store.set_cursor(current.identity, new_cursor)
                    cursor = new_cursor
            except asyncio.CancelledError:
                return
            except ProtocolError as exc:
                failures += 1
                current.last_error = exc.code
                if self.identity is not None and current.identity_id == self.identity["identity_id"]:
                    self.last_error = exc.code
                await asyncio.sleep(30 if failures >= 3 else 2)
            except Exception:
                failures += 1
                current.last_error = "poll_failed"
                if self.identity is not None and current.identity_id == self.identity["identity_id"]:
                    self.last_error = "poll_failed"
                await asyncio.sleep(30 if failures >= 3 else 2)

    async def _ingest(self, raw_message: dict[str, Any], runtime: IdentityRuntime | None = None) -> None:
        current = runtime or self._runtime_for_identity(None)
        message = extract_message(raw_message, current.identity["account_id"])
        if message is None or message["is_group"]:
            return
        sender_id = message["sender_id"]
        owner_runtime = self.identity is not None and current.identity_id == self.identity["identity_id"]
        allowed_user_ids = set(current.identity.get("allowed_user_ids", []))
        if not owner_runtime and current.poller_state == "pairing":
            claimed = self.store.claim_onboarding(
                identity_identifier=current.identity_id,
                private_user_id=sender_id,
                text=str(message.get("text") or ""),
            )
            if claimed is not None:
                updated = dict(current.identity)
                updated["allowed_user_ids"] = [sender_id]
                if message["context_token"]:
                    contexts = dict(updated.get("context_tokens", {}))
                    contexts[sender_id] = message["context_token"]
                    updated["context_tokens"] = contexts
                self.identity_store.save_identity(updated, make_active=False)
                current.identity.clear()
                current.identity.update(updated)
                self._set_runtime_state(current, "polling", identity_state="active")
                if (
                    current.pairing_session_id
                    and self.member_qr_state is not None
                    and self.member_qr_state.get("session_id") == current.pairing_session_id
                ):
                    self.member_qr_state["state"] = "active"
                    self.member_qr_state["has_image"] = False
                self._touch_runtime_message(current)
                await self._send_member_pairing_confirmation(
                    sender_id,
                    str(message.get("context_token") or "") or None,
                    message["message_id"],
                    claimed,
                    runtime=current,
                )
            elif current.pairing_session_id:
                session = self.store.onboarding_session_by_id(current.pairing_session_id)
                if session is not None and session["state"] == "failed":
                    self._set_runtime_state(
                        current,
                        "error",
                        error_code="pairing_attempts_exceeded",
                        identity_state="revoked",
                    )
                    await self._discard_identity_runtime(current)
            return
        if not allowed_user_ids:
            if owner_runtime and self.owner_pairing_enabled and self.identity_store.claim_owner(
                current.identity,
                user_id=sender_id,
                text=str(message.get("text") or ""),
                context_token=str(message.get("context_token") or "") or None,
            ):
                self.identity = self.identity_store.load_identity()
                self.store.register_paired_owner(sender_id)
                assert self.identity is not None
                current.identity = self.identity
                self.store.migrate_legacy_identity(
                    identity_identifier=current.identity_id,
                    account_digest=account_hash(current.identity["account_id"]),
                )
                self._set_runtime_state(current, "polling", identity_state="active")
                self._touch_runtime_message(current)
                await self._send_owner_pairing_confirmation(
                    sender_id,
                    str(message.get("context_token") or "") or None,
                    message["message_id"],
                    runtime=current,
                )
            return
        user = self.store.user_by_identity_sender(current.identity_id, sender_id)
        if user is None:
            if not owner_runtime:
                return
            claimed = self.store.claim_member_invitation(
                user_id=sender_id,
                text=str(message.get("text") or ""),
            )
            if claimed is not None:
                if message["context_token"]:
                    self.identity_store.set_context(current.identity, sender_id, message["context_token"])
                self._touch_runtime_message(current)
                await self._send_member_pairing_confirmation(
                    sender_id,
                    str(message.get("context_token") or "") or None,
                    message["message_id"],
                    claimed,
                    runtime=current,
                )
            return
        if user["status"] != "active":
            return
        upstream_message_id = message["message_id"]
        storage_message_id = (
            upstream_message_id
            if owner_runtime
            else routed_message_id(current.identity_id, upstream_message_id)
        )
        if self.store.message_exists(storage_message_id):
            return
        message["upstream_message_id"] = upstream_message_id
        message["message_id"] = storage_message_id
        message["identity_id"] = current.identity_id
        message["principal_id"] = user["principal_id"]
        if message["context_token"]:
            self.identity_store.set_context(current.identity, sender_id, message["context_token"])
        route = select_work_route(
            message.get("text"),
            role=str(user["role"]),
            has_attachments=bool(message["media"]),
            v2_enabled=self.runner_manager_v2_enabled,
            v1_available=True,
        )
        if route.route == ROUTE_REJECT:
            user = self.store.touch_user(sender_id, identity_identifier=current.identity_id) or user
            await self._send_remote_work_text(
                message,
                user,
                str(route.public_message),
                error_code=str(route.error_code),
            )
            self._touch_runtime_message(current)
            return
        if route.route == ROUTE_V2:
            user = self.store.touch_user(sender_id, identity_identifier=current.identity_id) or user
            assert route.command is not None
            await self._handle_runner_manager_v2(message, user, route.command)
            self._touch_runtime_message(current)
            return
        work_command: WorkCommand | None = None
        if route.route == ROUTE_V1:
            try:
                work_command = parse_work_command(str(message.get("text") or ""))
            except WorkCommandError as exc:
                user = self.store.touch_user(sender_id, identity_identifier=current.identity_id) or user
                await self._send_remote_work_text(message, user, str(exc), error_code=exc.code)
                self._touch_runtime_message(current)
                return
        if work_command is not None:
            user = self.store.touch_user(sender_id, identity_identifier=current.identity_id) or user
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
            self._touch_runtime_message(current)
            return
        media: list[tuple[dict[str, Any], bytes]] = []
        for spec in message["media"]:
            try:
                media.append((spec, await current.client.download_media(spec)))
            except ProtocolError as exc:
                current.last_error = exc.code
                if owner_runtime:
                    self.last_error = exc.code
        if not message["text"] and not media:
            return
        user = self.store.touch_user(sender_id, identity_identifier=current.identity_id) or user
        self.store.store_message(
            message_id=message["message_id"],
            upstream_message_id=upstream_message_id,
            identity_identifier=current.identity_id,
            principal_id_value=user["principal_id"],
            sender_id=sender_id,
            conversation_key=user["conversation_key"],
            text=message["text"],
            media=media,
            user_digest=user["user_hash"],
            capability_profile="owner" if user["role"] == "owner" else "member_read_only",
        )
        self._touch_runtime_message(current)

    async def _handle_runner_manager_v2(
        self,
        message: dict[str, Any],
        user: dict[str, Any],
        command: RunnerManagerWorkCommand,
    ) -> None:
        request = build_runner_manager_request(
            command,
            identity_id=str(message["identity_id"]),
            message_id=str(message["message_id"]),
            principal_hash=str(user["user_hash"]),
        )
        try:
            result = await self.controller.runner_manager(request)
        except StoreError as exc:
            await self._send_remote_work_text(
                message,
                user,
                str(exc),
                error_code=f"runner_manager_{exc.code}",
            )
            return
        if command.task_id is not None and result.task_id != command.task_id:
            await self._send_remote_work_text(
                message,
                user,
                "Runner Manager 返回的 task 与请求不一致，本条结果已拒绝。",
                error_code="runner_manager_task_mismatch",
            )
            return
        watch_replayed = False
        if command.operation != "status":
            try:
                watch = self.store.track_runner_manager_v2(
                    task_id=result.task_id,
                    source_message_id=str(message["message_id"]),
                    sender_id=str(message["sender_id"]),
                    user_digest=str(user["user_hash"]),
                    identity_identifier=str(message["identity_id"]),
                    principal_id_value=str(user["principal_id"]),
                    principal_hash=str(user["user_hash"]),
                    state=result.state,
                    stage=result.stage,
                    updated_at=result.updated_at,
                    rearm=command.operation in {"continue", "cancel"},
                )
            except StoreError as exc:
                await self._send_remote_work_text(
                    message,
                    user,
                    str(exc),
                    error_code=f"runner_manager_{exc.code}",
                )
                return
            watch_replayed = bool(watch.get("replayed"))
        text = self._runner_manager_v2_result_text(result)
        notification_key = self._runner_manager_v2_notification_key(text)
        outbound = {
            "message_id": message["message_id"],
            "sender_id": message["sender_id"],
            "user_hash": user["user_hash"],
            "identity_id": message.get("identity_id"),
            "principal_id": user.get("principal_id"),
            "capability_profile": "owner",
            "required_role": "owner",
            "controller_job_id": self._runner_manager_v2_controller_job_id(
                result.task_id,
                notification_key,
                seed=(
                    f"status:{message['message_id']}"
                    if command.operation == "status"
                    else f"watch:{message['message_id']}"
                ),
            ),
        }
        suppression = await self._send_result(outbound, text)
        if command.operation == "status" or not watch_replayed:
            self._mark_runner_manager_v2_delivery(
                result,
                notification_key=notification_key,
                suppression=suppression,
            )

    @staticmethod
    def _runner_manager_v2_result_text(result: RunnerManagerResult) -> str:
        parts = [f"Runner task {result.task_id}：{result.state}"]
        if result.stage:
            parts.append(f"阶段：{result.stage}")
        if result.summary:
            parts.append(result.summary)
        if result.test_summary:
            parts.append(f"测试：{result.test_summary}")
        if result.candidate_id:
            parts.append(f"候选：{result.candidate_id}")
        if result.changed_path_count is not None:
            parts.append(f"变更路径：{result.changed_path_count}")
        if result.queue_position is not None:
            parts.append(f"队列位置：{result.queue_position}")
        if result.error_code:
            parts.append(f"错误码：{result.error_code}")
        if result.action_required:
            parts.append(f"需要处理：{result.action_required}")
        if result.next_actions:
            parts.append("下一步：" + "；".join(result.next_actions))
        return "\n".join(parts)

    @staticmethod
    def _runner_manager_v2_notification_key(text: str) -> str:
        return hashlib.sha256(text.encode("utf-8")).hexdigest()

    @staticmethod
    def _runner_manager_v2_controller_job_id(
        task_id: str,
        notification_key: str,
        *,
        seed: str,
    ) -> str:
        digest = hashlib.sha256(
            f"runner-manager-v2\x00{task_id}\x00{notification_key}\x00{seed}".encode("utf-8")
        ).hexdigest()
        return f"runner-v2-{digest}"

    def _mark_runner_manager_v2_delivery(
        self,
        result: RunnerManagerResult,
        *,
        notification_key: str,
        suppression: str | None,
    ) -> None:
        try:
            self.store.mark_runner_manager_v2_notified(
                result.task_id,
                notification_key=notification_key,
                state=result.state,
                stage=result.stage,
                updated_at=result.updated_at,
                closed=suppression is not None or result.state in RUNNER_MANAGER_V2_TERMINAL_STATES,
                error_code=suppression,
            )
        except StoreError as exc:
            if exc.code != "runner_manager_watch_missing":
                raise

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
                identity_identifier=message.get("identity_id"),
                principal_id_value=user.get("principal_id"),
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
            "identity_id": message.get("identity_id"),
            "principal_id": user.get("principal_id"),
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

    async def _send_owner_pairing_confirmation(
        self,
        sender_id: str,
        context_token: str | None,
        message_id: str,
        *,
        runtime: IdentityRuntime | None = None,
    ) -> None:
        current = runtime or self._runtime_for_identity(None)
        client_id = "codex-weixin-pairing-" + hashlib.sha256(message_id.encode("utf-8")).hexdigest()[:32]
        try:
            async with current.outbound_lock:
                response = await current.client.send_text(
                    sender_id,
                    "微信 owner 绑定成功。现在可以直接和通用 Codex 助手交流。",
                    context_token,
                    client_id,
            )
            if response.get("ret", 0) == SESSION_EXPIRED_ERRCODE or response.get("errcode", 0) == SESSION_EXPIRED_ERRCODE:
                self._set_runtime_state(
                    current,
                    "session_expired",
                    error_code="session_expired",
                    identity_state="session_expired",
                )
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
        *,
        runtime: IdentityRuntime | None = None,
    ) -> None:
        current = runtime or self._runtime_for_identity(None)
        client_id = "codex-weixin-member-pairing-" + hashlib.sha256(message_id.encode("utf-8")).hexdigest()[:32]
        try:
            async with current.outbound_lock:
                response = await current.client.send_text(
                    sender_id,
                    "微信成员绑定成功。当前账号只允许普通讨论和已批准的装修只读查询。",
                    context_token,
                    client_id,
            )
            if response.get("ret", 0) == SESSION_EXPIRED_ERRCODE or response.get("errcode", 0) == SESSION_EXPIRED_ERRCODE:
                self._set_runtime_state(
                    current,
                    "session_expired",
                    error_code="session_expired",
                    identity_state="session_expired",
                )
            elif response.get("ret", 0) not in {0, None} or response.get("errcode", 0) not in {0, None}:
                self.last_error = "member_pairing_confirmation_failed"
        except Exception:
            self.last_error = "member_pairing_confirmation_failed"

    async def _delivery_loop(self) -> None:
        try:
            while not self._stop.is_set():
                try:
                    await self._deliver_remote_work_replies()
                    await self._deliver_runner_manager_v2_updates()
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
                            if message.get("media_archive_context"):
                                payload["media_archive_context"] = message["media_archive_context"]
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
                            await self._start_typing(message)
                            self.store.mark_submitted(message["message_id"], job["job_id"])
                        for message in self.store.submitted():
                            try:
                                job = await self.controller.job(message["controller_job_id"])
                            except StoreError as exc:
                                self.last_error = exc.code
                                break
                            if job["state"] == "completed":
                                self.store.update_conversation_link(
                                    message.get("user_hash"),
                                    thread_short=job.get("thread_short"),
                                    job_id=message.get("controller_job_id"),
                                )
                                outbound = dict(message)
                                outbound["thread_short"] = job.get("thread_short")
                                try:
                                    suppression = await self._send_completed_job(outbound, job)
                                except StoreError as exc:
                                    if exc.code in {"session_expired", "identity_runtime_unavailable", "credential_missing"}:
                                        continue
                                    raise
                                finally:
                                    await self._stop_typing_for_message(message)
                                if suppression:
                                    self.store.mark_finished(
                                        message["message_id"],
                                        success=False,
                                        error_code=suppression,
                                    )
                                    continue
                                self.store.mark_finished(message["message_id"], success=True)
                            elif job["state"] in {"failed", "cancelled", "recovery_required"}:
                                text = controller_failure_message(job["state"], job.get("error_code"))
                                outbound = dict(message)
                                outbound["thread_short"] = job.get("thread_short")
                                try:
                                    suppression = await self._send_result(outbound, text)
                                except StoreError as exc:
                                    if exc.code in {"session_expired", "identity_runtime_unavailable", "credential_missing"}:
                                        continue
                                    raise
                                finally:
                                    await self._stop_typing_for_message(message)
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
                    raise
                except (StoreError, ProtocolError) as exc:
                    self.last_error = exc.code
                    await asyncio.sleep(30 if exc.code == "session_expired" else 5)
                except Exception:
                    self.last_error = "delivery_failed"
                    await asyncio.sleep(5)
        finally:
            for runtime in self._runtimes.values():
                await self._stop_all_typing(runtime)

    async def _deliver_runner_manager_v2_updates(self) -> None:
        if not self.runner_manager_v2_enabled or not self.controller.configured:
            return
        for watch in self.store.pending_runner_manager_v2_watches():
            task_id = str(watch["task_id"])
            command = RunnerManagerWorkCommand(operation="status", task_id=task_id)
            try:
                request = build_runner_manager_request(
                    command,
                    identity_id=str(watch["identity_id"]),
                    message_id=f"runner-status-{task_id}",
                    principal_hash=str(watch["principal_hash"]),
                )
                result = await self.controller.runner_manager(request)
            except StoreError as exc:
                self.store.mark_runner_manager_v2_error(task_id, f"runner_manager_{exc.code}")
                self.last_error = exc.code
                continue
            if result.task_id != task_id:
                self.store.mark_runner_manager_v2_error(task_id, "runner_manager_task_mismatch")
                self.last_error = "runner_manager_task_mismatch"
                continue
            text = self._runner_manager_v2_result_text(result)
            notification_key = self._runner_manager_v2_notification_key(text)
            if hmac.compare_digest(
                str(watch.get("last_notification_key") or ""),
                notification_key,
            ):
                self._mark_runner_manager_v2_delivery(
                    result,
                    notification_key=notification_key,
                    suppression=None,
                )
                continue
            outbound = {
                "message_id": f"runner-v2-status-{task_id}",
                "sender_id": watch["sender_id"],
                "user_hash": watch["user_hash"],
                "identity_id": watch["identity_id"],
                "principal_id": watch["principal_id"],
                "capability_profile": "owner",
                "required_role": "owner",
                "controller_job_id": self._runner_manager_v2_controller_job_id(
                    task_id,
                    notification_key,
                    seed=f"watch:{watch['source_message_id']}",
                ),
            }
            try:
                suppression = await self._send_result(outbound, text)
            except (StoreError, ProtocolError) as exc:
                self.store.mark_runner_manager_v2_error(task_id, exc.code)
                self.last_error = exc.code
                continue
            self._mark_runner_manager_v2_delivery(
                result,
                notification_key=notification_key,
                suppression=suppression,
            )

    async def _deliver_remote_work_replies(self) -> None:
        for event in self.store.remote_work_pending_replies():
            payload = event["payload"]
            text = self._remote_work_result_text(payload)
            outbound = {
                "message_id": f"remote-result-{event['event_id']}",
                "sender_id": event["sender_id"],
                "user_hash": event["user_hash"],
                "identity_id": event.get("identity_id"),
                "principal_id": event.get("principal_id"),
                "capability_profile": "owner",
                "required_role": "owner",
                "controller_job_id": f"remote-result-{event['event_id']}",
            }
            try:
                suppression = await self._send_result(outbound, text)
            except StoreError as exc:
                if exc.code in {"session_expired", "identity_runtime_unavailable", "credential_missing"}:
                    continue
                raise
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

    async def _send_completed_job(self, message: dict[str, Any], job: dict[str, Any]) -> str | None:
        artifacts = job.get("artifacts")
        if artifacts is None or artifacts == []:
            return await self._send_result(message, str(job.get("result") or "任务已完成。"))
        if not isinstance(artifacts, list) or not 1 <= len(artifacts) <= 4:
            raise StoreError("artifact_invalid", "Controller artifacts 响应无效", status=502)
        summary = job.get("result_summary")
        if (
            not isinstance(summary, str)
            or not 1 <= len(summary) <= 500
            or "\n" in summary
            or "\r" in summary
        ):
            raise StoreError("artifact_summary_invalid", "Controller 统计摘要无效", status=502)
        prepared: list[dict[str, Any]] = []
        temporary_paths: list[Path] = []
        try:
            for artifact in artifacts:
                if not isinstance(artifact, dict):
                    raise StoreError("artifact_invalid", "Controller artifact 条目无效", status=502)
                state = self.store.prepare_artifact(message["controller_job_id"], artifact)
                item = {"artifact": artifact, "state": state, "path": None}
                if state["state"] == "pending":
                    try:
                        content = await self.controller.artifact(
                            message["controller_job_id"],
                            artifact,
                            max_bytes=self.max_media_bytes,
                        )
                        item["path"] = self.store.stage_outbound_artifact(artifact, content)
                        temporary_paths.append(item["path"])
                    except StoreError as exc:
                        self.store.mark_artifact(
                            message["controller_job_id"],
                            artifact["artifact_id"],
                            success=False,
                            error_code=exc.code,
                        )
                        item["state"] = self.store.prepare_artifact(
                            message["controller_job_id"], artifact
                        )
                prepared.append(item)
            return await self._send_artifact_result_locked(message, summary, prepared)
        finally:
            for path in temporary_paths:
                if path.is_file() and not path.is_symlink():
                    path.unlink()

    async def _send_artifact_result_locked(
        self,
        message: dict[str, Any],
        summary: str,
        prepared: list[dict[str, Any]],
    ) -> str | None:
        current = self._runtime_for_identity(message.get("identity_id"))
        if current.poller_state == "session_expired":
            raise StoreError("session_expired", "iLink 会话已过期，停止微信出站", status=503)
        async with current.outbound_lock:
            async with self._authorization_lock:
                suppression = self._authorization_suppression(message)
                if suppression:
                    return suppression
                await self._send_text_locked(message, summary, current)
                for item in prepared:
                    artifact = item["artifact"]
                    state = item["state"]
                    if state["state"] == "sent":
                        continue
                    if state["state"] == "pending":
                        path = item.get("path")
                        if not isinstance(path, Path):
                            self.store.mark_artifact(
                                message["controller_job_id"],
                                artifact["artifact_id"],
                                success=False,
                                error_code="artifact_prefetch_missing",
                            )
                            state = self.store.prepare_artifact(message["controller_job_id"], artifact)
                        else:
                            context = self.identity_store.context(current.identity, message["sender_id"])
                            try:
                                await current.client.send_media(
                                    message["sender_id"],
                                    path,
                                    context,
                                    state["client_id"],
                                )
                            except ProtocolError as exc:
                                if exc.code == "session_expired":
                                    self._set_runtime_state(
                                        current,
                                        "session_expired",
                                        error_code="session_expired",
                                        identity_state="session_expired",
                                    )
                                    raise StoreError(
                                        "session_expired",
                                        "iLink 会话已过期，停止微信出站",
                                        status=503,
                                    ) from exc
                                error_code = "delivery_state_unknown" if exc.delivery_unknown else exc.code
                                self.store.mark_artifact(
                                    message["controller_job_id"],
                                    artifact["artifact_id"],
                                    success=False,
                                    error_code=error_code,
                                )
                                state = self.store.prepare_artifact(
                                    message["controller_job_id"], artifact
                                )
                            else:
                                self.store.mark_artifact(
                                    message["controller_job_id"],
                                    artifact["artifact_id"],
                                    success=True,
                                )
                                continue
                    fallback_error = await self._send_artifact_fallback_locked(
                        message,
                        artifact,
                        state,
                        current,
                    )
                    if fallback_error:
                        return fallback_error
        return None

    async def _send_artifact_fallback_locked(
        self,
        message: dict[str, Any],
        artifact: dict[str, Any],
        state: dict[str, Any],
        runtime: IdentityRuntime,
    ) -> str | None:
        if state["fallback_state"] == "sent":
            return None
        try:
            fallback_url = self._artifact_fallback_url(artifact)
        except StoreError as exc:
            self.store.mark_artifact_fallback(
                message["controller_job_id"],
                artifact["artifact_id"],
                success=False,
                error_code=exc.code,
            )
            return exc.code
        prefix = (
            "文件发送状态暂无法确认，可在 24 小时内下载："
            if state.get("error_code") == "delivery_state_unknown"
            else "文件发送失败，可在 24 小时内下载："
        )
        context = self.identity_store.context(runtime.identity, message["sender_id"])
        response = await runtime.client.send_text(
            message["sender_id"],
            f"{prefix}{fallback_url}",
            context,
            state["fallback_client_id"],
        )
        ret = response.get("ret", 0)
        errcode = response.get("errcode", 0)
        if ret == SESSION_EXPIRED_ERRCODE or errcode == SESSION_EXPIRED_ERRCODE:
            self._set_runtime_state(
                runtime,
                "session_expired",
                error_code="session_expired",
                identity_state="session_expired",
            )
            raise StoreError("session_expired", "iLink 会话已过期，停止微信出站", status=503)
        if ret not in {0, None} or errcode not in {0, None}:
            code = "send_rate_limited" if ret == RATE_LIMIT_ERRCODE or errcode == RATE_LIMIT_ERRCODE else "send_failed"
            self.store.mark_artifact_fallback(
                message["controller_job_id"],
                artifact["artifact_id"],
                success=False,
                error_code=code,
            )
            raise ProtocolError(code, "微信下载链接发送失败", retryable=code == "send_rate_limited")
        self.store.mark_artifact_fallback(
            message["controller_job_id"],
            artifact["artifact_id"],
            success=True,
        )
        return None

    def _artifact_fallback_url(self, artifact: dict[str, Any]) -> str:
        path = artifact.get("fallback_path")
        if not self.controller_ingress_base_url:
            raise StoreError("artifact_fallback_unconfigured", "Controller Ingress 下载地址未配置", status=503)
        if not isinstance(path, str) or not re.fullmatch(r"/downloads/artifacts/[A-Za-z0-9_-]{43}", path):
            raise StoreError("artifact_fallback_invalid", "Controller artifact 下载路径无效", status=502)
        return f"{self.controller_ingress_base_url}{path}"

    async def _send_result(self, message: dict[str, Any], text: str) -> str | None:
        current = self._runtime_for_identity(message.get("identity_id"))
        if current.poller_state == "session_expired":
            raise StoreError("session_expired", "iLink 会话已过期，停止微信出站", status=503)
        async with current.outbound_lock:
            async with self._authorization_lock:
                suppression = self._authorization_suppression(message)
                if suppression:
                    return suppression
                await self._send_text_locked(message, text, current)
        return None

    def _authorization_suppression(self, message: dict[str, Any]) -> str | None:
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
        if message.get("identity_id") or message.get("principal_id"):
            route = self.store.identity_route_for_principal(str(message.get("principal_id") or ""))
            if (
                route is None
                or route["identity_id"] != message.get("identity_id")
                or route["state"] != "active"
            ):
                return "reply_suppressed_identity_unavailable"
        return None

    async def _send_text_locked(
        self,
        message: dict[str, Any],
        text: str,
        runtime: IdentityRuntime,
    ) -> None:
        text = with_thread_short(text, message.get("thread_short"))
        chunks = split_text(text, 4000)
        for index, chunk in enumerate(chunks):
            if runtime.poller_state == "session_expired":
                raise StoreError("session_expired", "iLink 会话已过期，停止微信出站", status=503)
            client_id, already_sent = self.store.prepare_chunk(message["controller_job_id"], index)
            if already_sent:
                continue
            response = await self._send_text_with_context_fallback_locked(
                runtime,
                str(message["sender_id"]),
                chunk,
                client_id,
            )
            ret = response.get("ret", 0)
            errcode = response.get("errcode", 0)
            if ret == SESSION_EXPIRED_ERRCODE or errcode == SESSION_EXPIRED_ERRCODE:
                self._set_runtime_state(
                    runtime,
                    "session_expired",
                    error_code="session_expired",
                    identity_state="session_expired",
                )
                raise StoreError("session_expired", "iLink 会话已过期，停止微信出站", status=503)
            if ret not in {0, None} or errcode not in {0, None}:
                code = "send_rate_limited" if ret == RATE_LIMIT_ERRCODE or errcode == RATE_LIMIT_ERRCODE else "send_failed"
                self.store.mark_chunk(message["controller_job_id"], index, success=False, error_code=code)
                raise ProtocolError(code, "微信发送失败", retryable=code == "send_rate_limited")
            self.store.mark_chunk(message["controller_job_id"], index, success=True)
            if index + 1 < len(chunks):
                await asyncio.sleep(0.5)

    async def _send_text_with_context_fallback_locked(
        self,
        runtime: IdentityRuntime,
        sender_id: str,
        text: str,
        client_id: str,
    ) -> dict[str, Any]:
        context = self.identity_store.context(runtime.identity, sender_id)
        response = await runtime.client.send_text(sender_id, text, context, client_id)
        if context and is_stale_context_response(response):
            self.identity_store.clear_context(runtime.identity, sender_id)
            runtime.typing_tickets.pop(sender_id, None)
            runtime.typing_ticket_fetched_at.pop(sender_id, None)
            response = await runtime.client.send_text(sender_id, text, None, client_id)
        return response

    async def send_notification(self, message_id: str, text: str) -> None:
        """Send one deterministic notification to the single bound owner."""
        runtime, owner_id, _context = self._notification_owner_delivery()
        if runtime.poller_state == "session_expired":
            raise StoreError("session_expired", "iLink 会话已过期，停止微信出站", status=503)
        client_id = "codex-weixin-notification-" + hashlib.sha256(message_id.encode("utf-8")).hexdigest()[:32]
        async with runtime.outbound_lock:
            async with self._authorization_lock:
                if runtime.poller_state == "session_expired":
                    raise StoreError("session_expired", "iLink 会话已过期，停止微信出站", status=503)
                runtime, owner_id, _context = self._notification_owner_delivery()
                response = await self._send_text_with_context_fallback_locked(
                    runtime,
                    owner_id,
                    text,
                    client_id,
                )
        ret = response.get("ret", 0)
        errcode = response.get("errcode", 0)
        if ret == SESSION_EXPIRED_ERRCODE or errcode == SESSION_EXPIRED_ERRCODE:
            self._set_runtime_state(
                runtime,
                "session_expired",
                error_code="session_expired",
                identity_state="session_expired",
            )
            raise StoreError("session_expired", "iLink 会话已过期，停止微信出站", status=503)
        if ret not in {0, None} or errcode not in {0, None}:
            code = "send_rate_limited" if ret == RATE_LIMIT_ERRCODE or errcode == RATE_LIMIT_ERRCODE else "send_failed"
            raise ProtocolError(code, "微信通知发送失败", retryable=code == "send_rate_limited")

    def notification_owner_context(self) -> tuple[str, str]:
        """Return the only authorized notification target or fail closed."""
        _runtime, owner_id, context = self._notification_owner_delivery()
        return owner_id, context

    def _notification_owner_delivery(self) -> tuple[IdentityRuntime, str, str]:
        route = self.store.owner_identity_route()
        runtime = self._runtime_for_identity(route["identity_id"])
        owner_id = str(route["private_user_id"])
        if runtime.identity.get("allowed_user_ids") != [owner_id]:
            raise StoreError("notification_owner_mirror_invalid", "微信 owner 镜像不一致", status=409)
        context = self.identity_store.context(runtime.identity, owner_id)
        if not context:
            raise StoreError("notification_context_missing", "微信 owner 缺少当前会话上下文", status=409)
        return runtime, owner_id, context

    async def _cleanup_loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.store.cleanup_spool()
                self.store.cleanup_outbound_artifacts()
                self.store.expire_remote_work_tasks()
                for identity_identifier in self.store.expire_onboarding_sessions():
                    runtime = self._runtimes.get(identity_identifier)
                    if runtime is not None:
                        await self._discard_identity_runtime(runtime)
                await asyncio.sleep(300)
            except asyncio.CancelledError:
                return
            except Exception:
                await asyncio.sleep(60)

    def _new_qr_client(self) -> IlinkClient:
        return IlinkClient(
            base_url="https://ilinkai.weixin.qq.com",
            cdn_base_url="https://novac2c.cdn.weixin.qq.com/c2c",
            token="",
            max_media_bytes=self.max_media_bytes,
        )

    @staticmethod
    def _validate_verify_code(value: str) -> str:
        code = value.strip()
        if not re.fullmatch(r"[0-9]{1,12}", code):
            raise StoreError("verify_code_invalid", "请输入微信显示的数字验证码")
        return code

    def _render_qr_image(self, target: Path, content: str) -> None:
        target.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
        try:
            import qrcode

            image = qrcode.make(content)
            image.save(target)
            target.chmod(0o600)
        except Exception as exc:
            raise ProtocolError("qr_render_failed", "二维码生成失败") from exc

    async def _refresh_qr(self, client: IlinkClient, state: dict[str, Any], image_path: Path) -> None:
        response = await client.create_bot_qr(self.identity_store.recent_tokens())
        qrcode_value = str(response.get("qrcode") or "")
        qrcode_content = str(response.get("qrcode_img_content") or qrcode_value)
        if not qrcode_value or not qrcode_content:
            raise ProtocolError("qr_invalid", "iLink 未返回二维码")
        self._render_qr_image(image_path, qrcode_content)
        state.update(
            {
                "state": "waiting",
                "qrcode": qrcode_value,
                "has_image": True,
                "base_url": "https://ilinkai.weixin.qq.com",
                "verify_code": None,
            }
        )

    async def start_qr_login(self) -> dict[str, Any]:
        if self.poller_state not in {"disabled", "stopped"}:
            raise StoreError("poller_active", "真实 Poller 运行时不能重新认证 Owner ClawBot", status=409)
        if self.qr_state and self.qr_state.get("state") in {
            "waiting",
            "scanned",
            "need_verifycode",
            "redirecting",
        }:
            return self.public_qr_state()
        client = self._new_qr_client()
        await client.start()
        state: dict[str, Any] = {"refresh_count": 0}
        try:
            await self._refresh_qr(client, state, self.qr_image_path)
        except Exception:
            await client.close()
            raise
        self.qr_state = state
        self._qr_task = asyncio.create_task(self._poll_qr(client), name="weixin-owner-qr-login")
        self._tasks.append(self._qr_task)
        return self.public_qr_state()

    def submit_qr_verify_code(self, payload: dict[str, Any]) -> dict[str, Any]:
        if self.qr_state is None or self.qr_state.get("state") != "need_verifycode":
            raise StoreError("verify_code_not_requested", "当前二维码不需要验证码", status=409)
        self.qr_state["verify_code"] = self._validate_verify_code(str(payload.get("verify_code") or ""))
        self.qr_state["state"] = "verifying"
        return self.public_qr_state()

    async def _refresh_qr_after_terminal_status(
        self,
        client: IlinkClient,
        state: dict[str, Any],
        image_path: Path,
    ) -> bool:
        state["refresh_count"] = int(state.get("refresh_count") or 0) + 1
        if state["refresh_count"] > 3:
            return False
        await self._refresh_qr(client, state, image_path)
        return True

    async def _poll_qr(self, client: IlinkClient) -> None:
        try:
            for _ in range(480):
                state = self.qr_state
                if state is None:
                    return
                if state.get("state") == "need_verifycode" and not state.get("verify_code"):
                    await asyncio.sleep(1)
                    continue
                response = await client.get_bot_qr_status(
                    str(state["qrcode"]),
                    base_url=str(state["base_url"]),
                    verify_code=state.get("verify_code"),
                )
                status = str(response.get("status") or "wait")
                if status == "wait":
                    pass
                elif status == "scaned":
                    state["verify_code"] = None
                    state["state"] = "scanned"
                elif status == "need_verifycode":
                    state["verify_code"] = None
                    state["state"] = "need_verifycode"
                elif status == "scaned_but_redirect":
                    redirect_host = str(response.get("redirect_host") or "").strip()
                    if redirect_host:
                        state["base_url"] = f"https://{redirect_host}"
                    state["state"] = "redirecting"
                elif status in {"expired", "verify_code_blocked"}:
                    if not await self._refresh_qr_after_terminal_status(client, state, self.qr_image_path):
                        state["state"] = status
                        return
                elif status == "binded_redirect":
                    state["state"] = "already_connected"
                    return
                elif status == "confirmed":
                    await self._accept_owner_qr(response)
                    state["state"] = "credential_ready"
                    return
                await asyncio.sleep(1)
            if self.qr_state is not None:
                self.qr_state["state"] = "expired"
        except asyncio.CancelledError:
            raise
        except (ProtocolError, StoreError) as exc:
            if self.qr_state is not None and self.qr_state.get("state") not in {
                "credential_ready",
                "expired",
                "already_connected",
            }:
                self.qr_state["state"] = "failed"
                self.qr_state["error_code"] = exc.code
        finally:
            await client.close()

    async def _accept_owner_qr(self, response: dict[str, Any]) -> None:
        account_id_value = str(response.get("ilink_bot_id") or "")
        token = str(response.get("bot_token") or "")
        scanner_id = str(response.get("ilink_user_id") or "")
        if not account_id_value or not token or not scanner_id:
            raise ProtocolError("qr_credentials_missing", "iLink 未返回完整身份凭据")
        async with self._authorization_lock:
            previous = self.identity
            owner: dict[str, Any] | None = None
            try:
                owner = self.store.active_owner()
            except StoreError as exc:
                if exc.code != "notification_owner_unavailable":
                    raise
            if previous is not None and previous.get("account_id") != account_id_value:
                raise StoreError(
                    "owner_identity_mismatch",
                    "Owner 二维码只允许重新认证同一个 ClawBot；新成员请使用成员接入流程。",
                    status=409,
                )
            if owner is not None and not hmac.compare_digest(owner["private_user_id"], scanner_id):
                raise StoreError("owner_reauth_user_mismatch", "扫码者不是当前 Owner", status=409)
            identity = {
                "account_id": account_id_value,
                "token": token,
                "base_url": str(response.get("baseurl") or "https://ilinkai.weixin.qq.com"),
                "cdn_base_url": "https://novac2c.cdn.weixin.qq.com/c2c",
                "user_id": scanner_id,
                "allowed_user_ids": [] if owner is None else [owner["private_user_id"]],
                "get_updates_buf": "" if previous is None else previous.get("get_updates_buf", ""),
                "context_tokens": {} if previous is None else dict(previous.get("context_tokens", {})),
            }
            self.identity_store.save_identity(identity)
            self.identity = self.identity_store.load_identity()
            assert self.identity is not None
            self.identity_store.clear_owner_pairing()
            self.store.migrate_legacy_identity(
                identity_identifier=self.identity["identity_id"],
                account_digest=account_hash(self.identity["account_id"]),
            )
            self._refresh_client()
            self._ensure_owner_runtime()

    def public_qr_state(self) -> dict[str, Any]:
        if self.qr_state is None:
            return {"state": "idle", "has_image": False}
        return {
            "state": self.qr_state["state"],
            "has_image": bool(self.qr_state.get("has_image")),
            "error_code": self.qr_state.get("error_code"),
        }

    async def start_member_onboarding(self, payload: dict[str, Any]) -> dict[str, Any]:
        if not self.poller_enabled:
            raise StoreError("poller_disabled", "成员接入要求 Gateway Poller 已启用", status=409)
        created = self.store.create_onboarding_session(
            expected_revision=payload.get("revision"),
            request_id=payload.get("request_id"),
            alias=str(payload.get("alias") or ""),
            target_wx_short=(str(payload.get("target_wx_short") or "") or None),
            ttl_seconds=payload.get("ttl_seconds", 15 * 60),
            max_active_identities=self.max_active_identities,
        )
        if "session_id" not in created:
            return {
                **created,
                "qr": self.public_member_qr_state(),
            }
        client = self._new_qr_client()
        await client.start()
        state: dict[str, Any] = {
            "session_id": created["session_id"],
            "session_short": created["session_short"],
            "expires_at": created["expires_at"],
            "refresh_count": 0,
        }
        try:
            await self._refresh_qr(client, state, self.member_qr_image_path)
            self.store.set_onboarding_qr_state(
                session_id=created["session_id"],
                qr_state="waiting",
            )
        except Exception as exc:
            code = exc.code if isinstance(exc, (StoreError, ProtocolError)) else "qr_start_failed"
            self.store.set_onboarding_qr_state(
                session_id=created["session_id"],
                qr_state="failed",
                error_code=code,
                terminal_state="failed",
            )
            await client.close()
            raise
        self.member_qr_state = state
        self._member_qr_task = asyncio.create_task(
            self._poll_member_qr(client),
            name=f"weixin-member-qr-{created['session_short'][-6:]}",
        )
        self._tasks.append(self._member_qr_task)
        return {
            "state": created["state"],
            "session_short": created["session_short"],
            "expires_at": created["expires_at"],
            "revision": created["revision"],
            "code": created["code"],
            "qr": self.public_member_qr_state(),
        }

    def submit_member_onboarding_verify_code(
        self,
        session_short: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        state = self.member_qr_state
        if (
            state is None
            or state.get("session_short") != session_short
            or state.get("state") != "need_verifycode"
        ):
            raise StoreError("verify_code_not_requested", "当前成员二维码不需要验证码", status=409)
        state["verify_code"] = self._validate_verify_code(str(payload.get("verify_code") or ""))
        state["state"] = "verifying"
        return self.public_member_qr_state()

    async def _poll_member_qr(self, client: IlinkClient) -> None:
        try:
            for _ in range(480):
                state = self.member_qr_state
                if state is None:
                    return
                session_id = str(state["session_id"])
                if state.get("state") == "need_verifycode" and not state.get("verify_code"):
                    await asyncio.sleep(1)
                    continue
                response = await client.get_bot_qr_status(
                    str(state["qrcode"]),
                    base_url=str(state["base_url"]),
                    verify_code=state.get("verify_code"),
                )
                status = str(response.get("status") or "wait")
                if status == "wait":
                    pass
                elif status == "scaned":
                    state["verify_code"] = None
                    state["state"] = "scanned"
                    self.store.set_onboarding_qr_state(session_id=session_id, qr_state="scanned")
                elif status == "need_verifycode":
                    state["verify_code"] = None
                    state["state"] = "need_verifycode"
                    self.store.set_onboarding_qr_state(
                        session_id=session_id,
                        qr_state="need_verifycode",
                        error_code="verify_code_required",
                    )
                elif status == "scaned_but_redirect":
                    redirect_host = str(response.get("redirect_host") or "").strip()
                    if redirect_host:
                        state["base_url"] = f"https://{redirect_host}"
                    state["state"] = "redirecting"
                    self.store.set_onboarding_qr_state(session_id=session_id, qr_state="redirecting")
                elif status in {"expired", "verify_code_blocked"}:
                    self.store.set_onboarding_qr_state(
                        session_id=session_id,
                        qr_state=status,
                        error_code=status,
                    )
                    if not await self._refresh_qr_after_terminal_status(
                        client,
                        state,
                        self.member_qr_image_path,
                    ):
                        state["state"] = status
                        self.store.set_onboarding_qr_state(
                            session_id=session_id,
                            qr_state=status,
                            error_code=status,
                            terminal_state="expired" if status == "expired" else "failed",
                        )
                        return
                    self.store.set_onboarding_qr_state(session_id=session_id, qr_state="waiting")
                elif status == "binded_redirect":
                    state["state"] = "already_bound"
                    self.store.set_onboarding_qr_state(
                        session_id=session_id,
                        qr_state="already_bound",
                        error_code="identity_already_bound",
                        terminal_state="already_bound",
                    )
                    return
                elif status == "confirmed":
                    await self._accept_member_qr(response, state)
                    state["state"] = "pending_pairing"
                    state["has_image"] = False
                    return
                await asyncio.sleep(1)
            if self.member_qr_state is not None:
                self.member_qr_state["state"] = "expired"
                self.store.set_onboarding_qr_state(
                    session_id=str(self.member_qr_state["session_id"]),
                    qr_state="expired",
                    error_code="qr_timeout",
                    terminal_state="expired",
                )
        except asyncio.CancelledError:
            raise
        except (ProtocolError, StoreError) as exc:
            state = self.member_qr_state
            if state is not None and state.get("state") not in {
                "pending_pairing",
                "expired",
                "cancelled",
                "already_bound",
            }:
                state["state"] = "failed"
                state["error_code"] = exc.code
                self.store.set_onboarding_qr_state(
                    session_id=str(state["session_id"]),
                    qr_state="failed",
                    error_code=exc.code,
                    terminal_state="failed",
                )
        finally:
            await client.close()

    async def _accept_member_qr(self, response: dict[str, Any], state: dict[str, Any]) -> None:
        account_id_value = str(response.get("ilink_bot_id") or "")
        token = str(response.get("bot_token") or "")
        scanner_id = str(response.get("ilink_user_id") or "")
        if not account_id_value or not token or not scanner_id:
            raise ProtocolError("qr_credentials_missing", "iLink 未返回完整成员身份凭据")
        identity = {
            "account_id": account_id_value,
            "token": token,
            "base_url": str(response.get("baseurl") or "https://ilinkai.weixin.qq.com"),
            "cdn_base_url": "https://novac2c.cdn.weixin.qq.com/c2c",
            "user_id": scanner_id,
            "allowed_user_ids": [],
            "get_updates_buf": "",
            "context_tokens": {},
        }
        normalized = self.identity_store.validate_identity(identity)
        self.identity_store.save_identity(normalized, make_active=False)
        self.store.attach_onboarding_identity(
            session_id=str(state["session_id"]),
            identity_identifier=normalized["identity_id"],
            account_digest=account_hash(normalized["account_id"]),
            scanned_private_user_id=scanner_id,
        )
        runtime = IdentityRuntime(
            identity=normalized,
            client=IlinkClient(
                base_url=normalized["base_url"],
                cdn_base_url=normalized["cdn_base_url"],
                token=normalized["token"],
                max_media_bytes=self.max_media_bytes,
            ),
            poller_state="stopped",
            pairing_session_id=str(state["session_id"]),
        )
        self._runtimes[runtime.identity_id] = runtime
        await self._resume_identity_runtime(runtime)

    async def cancel_member_onboarding(
        self,
        session_short: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        session = self.store.onboarding_session(session_short)
        async with self._authorization_lock:
            result = self.store.cancel_onboarding_session(
                session_short=session_short,
                expected_revision=payload.get("revision"),
                request_id=payload.get("request_id"),
            )
            state = self.member_qr_state
            if state is not None and state.get("session_short") == session_short:
                state["state"] = "cancelled"
                state["has_image"] = False
                if self._member_qr_task is not None and self._member_qr_task is not asyncio.current_task():
                    self._member_qr_task.cancel()
                    await asyncio.gather(self._member_qr_task, return_exceptions=True)
                    self._member_qr_task = None
            identity_identifier = session.get("identity_id")
            runtime = None if not identity_identifier else self._runtimes.get(identity_identifier)
            if runtime is not None:
                await self._discard_identity_runtime(runtime)
            if self.member_qr_image_path.is_file() and not self.member_qr_image_path.is_symlink():
                self.member_qr_image_path.unlink()
            return result

    def public_member_qr_state(self) -> dict[str, Any]:
        state = self.member_qr_state
        if state is None:
            return {"state": "idle", "has_image": False}
        active = state.get("state") in {
            "waiting",
            "scanned",
            "need_verifycode",
            "verifying",
            "redirecting",
            "pending_pairing",
        }
        return {
            "state": state.get("state", "idle"),
            "has_image": bool(state.get("has_image")),
            "session_short": state.get("session_short") if active else None,
            "expires_at": state.get("expires_at"),
            "error_code": state.get("error_code"),
        }

    def inspect_migration(self, reference: str) -> dict[str, Any]:
        return self.identity_store.inspect_migration(reference)

    def import_migration(self, reference: str, key: str) -> dict[str, Any]:
        if self.poller_state not in {"disabled", "stopped"}:
            raise StoreError("poller_active", "真实 Poller 运行时不能导入 iLink 身份", status=409)
        previous_account = None if self.identity is None else self.identity.get("account_id")
        result = self.identity_store.import_migration(
            reference,
            key,
            make_active=False,
            expected_account_id=previous_account,
        )
        imported_account_hash = str(result.get("account_hash") or "")
        imported = self.identity_store.load_identity_by_hash(imported_account_hash)
        if imported is None:
            raise StoreError("migration_invalid", "迁移身份未能安全保存", status=500)
        self.identity_store.set_active_identity(imported)
        self.identity = self.identity_store.load_identity()
        self.identity_store.clear_owner_pairing()
        if self.identity is not None:
            migration = self.store.migrate_identity_allowlist(list(self.identity.get("allowed_user_ids", [])))
            owner_private_id = migration.get("owner_private_id")
            if isinstance(owner_private_id, str) and self.identity.get("allowed_user_ids") != [owner_private_id]:
                self.identity_store.mirror_owner(self.identity, owner_private_id)
        self._refresh_client()
        return {
            "state": result["state"],
            "identity_short": self.store.short_id("CB", imported["identity_id"]),
            "allowed_user_count": result["allowed_user_count"],
            "has_cursor": result["has_cursor"],
            "context_count": result["context_count"],
        }

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
        private_by_short = {
            self.store.short_id("WX", user["user_hash"]): user
            for user in self._private_users()
        }
        for user in document["users"]:
            private = private_by_short.get(user["wx_short"])
            if private is None:
                user["has_context"] = False
                continue
            route = self.store.identity_route_for_principal(private["principal_id"])
            runtime = None if route is None else self._runtimes.get(route["identity_id"])
            user["has_context"] = bool(
                route
                and runtime
                and runtime.identity.get("context_tokens", {}).get(route["private_user_id"])
            )
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
            private = self.store.user_by_short(wx_short)
            route = self.store.identity_route_for_principal(
                private["principal_id"],
                include_inactive=True,
            )
            result = self.store.change_user_state(
                wx_short=wx_short,
                action=action,
                expected_revision=payload.get("revision"),
                request_id=payload.get("request_id"),
            )
            if route is not None and route["binding_type"] == "primary":
                runtime = self._runtimes.get(route["identity_id"])
                if runtime is not None:
                    if action in {"suspend", "revoke"}:
                        if action == "revoke":
                            await self._discard_identity_runtime(runtime)
                        else:
                            await self._stop_identity_runtime(runtime)
                    elif action == "resume":
                        await self._resume_identity_runtime(runtime)
            return result

    async def transfer_owner(self, payload: dict[str, Any]) -> dict[str, Any]:
        if self.identity is None:
            raise StoreError("credential_missing", "缺少完整 iLink 身份", status=409)
        async with self._authorization_lock:
            previous_route = self.store.owner_identity_route()
            previous_runtime = self._runtime_for_identity(previous_route["identity_id"])
            result = self.store.transfer_owner(
                target_wx_short=str(payload.get("target_wx_short") or ""),
                expected_revision=payload.get("revision"),
                request_id=payload.get("request_id"),
                confirmation=str(payload.get("confirmation") or ""),
            )
            if result.pop("replayed", False):
                return result
            result.pop("owner_private_id", None)
            result.pop("previous_owner_private_id", None)
            target_principal_id = result.pop("owner_principal_id")
            previous_principal_id = result.pop("previous_owner_principal_id")
            try:
                target_route = self.store.identity_route_for_principal(target_principal_id)
                if target_route is None or target_route["state"] != "active":
                    raise StoreError(
                        "owner_identity_unavailable",
                        "目标 Owner 的 ClawBot 身份不可用",
                        status=409,
                    )
                target_runtime = self._runtime_for_identity(target_route["identity_id"])
                self.identity_store.mirror_owner(
                    target_runtime.identity,
                    target_route["private_user_id"],
                )
                self.identity = target_runtime.identity
                self.client = target_runtime.client
                self.token_lock = target_runtime.token_lock
                self.poller_state = target_runtime.poller_state
                self.last_error = target_runtime.last_error
                self.last_poll_at = target_runtime.last_poll_at
                self.last_message_at = target_runtime.last_message_at
                self._outbound_lock = target_runtime.outbound_lock
            except Exception as exc:
                self.store.restore_owner_after_mirror_failure(
                    previous_principal_id,
                    target_principal_id,
                    str(payload.get("request_id") or ""),
                )
                try:
                    self.identity_store.mirror_owner(
                        previous_runtime.identity,
                        previous_route["private_user_id"],
                    )
                    self.identity = previous_runtime.identity
                    self.client = previous_runtime.client
                    self.token_lock = previous_runtime.token_lock
                    self.poller_state = previous_runtime.poller_state
                    self._outbound_lock = previous_runtime.outbound_lock
                except Exception:
                    pass
                raise StoreError("owner_identity_mirror_failed", "Owner 转移未能同步身份镜像", status=500) from exc
            return result

    def _private_users(self) -> list[dict[str, Any]]:
        with self.store._connect() as connection:
            return [self.store._private_user_document(row) for row in connection.execute("SELECT * FROM weixin_users")]

    def status(self) -> dict[str, Any]:
        identity = None
        if self.identity is not None:
            identity = self.identity_store.public_summary(self.identity)
            identity.pop("account_hash", None)
            identity["identity_short"] = self.store.short_id("CB", self.identity["identity_id"])
        users = self.users()
        active_users = sum(1 for user in users["users"] if user["status"] == "active")
        identities = self.store.list_identities()
        identities["limits"]["max_active_identities"] = self.max_active_identities
        poller_control = self.store.poller_control()
        return {
            "version": "0.4.7",
            "poller_enabled": self.poller_enabled,
            "poller_default_enabled": self.poller_default_enabled,
            "poller_override": poller_control["override"],
            "poller_revision": poller_control["revision"],
            "poller_state": self.poller_state,
            "poller_maintenance": self._poller_maintenance_document(),
            "identity": identity,
            "identities": identities,
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
            "onboarding": {
                **self.store.onboarding_summary(),
                "qr": self.public_member_qr_state(),
            },
            "queue": self.store.status(),
            "remote_work": {
                "enabled": self.remote_work_enabled,
                "runner_manager_v2_enabled": self.runner_manager_v2_enabled,
                "runner_manager_v2": self.store.runner_manager_v2_status(),
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
