"""Secret-isolating Unix socket router for dynamic Hub and fixed Broker tools."""

from __future__ import annotations

import base64
import hashlib
import http.client
import json
from pathlib import Path
import re
import socketserver
import sqlite3
import threading
import time
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode, urlsplit
from urllib.request import Request, urlopen

from .hub_manifest import (
    BOOTSTRAP_MANIFEST,
    HubManifestError,
    ValidatedHubManifest,
    validate_hub_manifest,
)
from .store import ControllerStore, StoreError
from .prepare_car import (
    HomeAssistantPrepareCarError,
    HomeAssistantRequest,
    PREPARE_CAR_CONFIRMATION_TTL_SECONDS,
    PREPARE_CAR_ENTITY_ID,
    PREPARE_CAR_TOOL_NAMES,
    request_home_assistant_json,
    safe_entity_state,
)
from .tool_catalog import (
    AITO_PREPARE_CAR_DEFINITIONS,
    MEMBER_ALLOWED_TOOL_NAMES,
    MEMO_DEFINITIONS,
    MEMO_TOOLS,
    OPERATION_DEFINITIONS,
    ToolDefinition,
)

HOST_RE = re.compile(r"^[a-z0-9](?:[a-z0-9._-]{0,251}[a-z0-9])?$")
ADDON_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9_]{0,63}$")
ACTION_ID_RE = re.compile(r"^OPS-[0-9]{8}-[A-F0-9]{12}$")
RECEIPT_ID_RE = re.compile(r"^RCPT-[A-F0-9]{32}$")
SHA256_ID_RE = re.compile(r"^sha256:[a-f0-9]{64}$")
ATTACHMENT_REF_RE = re.compile(r"^[A-Za-z0-9_-]{32,128}$")
MEMO_ID_RE = re.compile(r"^memo-[a-f0-9]{32}$")
MEMO_DUE_AT_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\+08:00$")
CHART_REF_RE = re.compile(r"^summary-[a-f0-9]{32}\.png$")
ATTACHMENT_MIME_TYPES = {"image/jpeg", "image/png", "image/webp", "application/pdf", "text/plain"}
MAX_GATEWAY_ATTACHMENT_BYTES = 20 * 1024 * 1024
MEDIA_MIME_TYPES = {"image/jpeg", "image/png", "image/webp", "image/heic", "image/heif", "video/mp4", "video/quicktime", "video/webm"}
DEFAULT_MAX_GATEWAY_MEDIA_BYTES = 1024 * 1024 * 1024
MAX_JOB_ARTIFACT_BYTES = 20 * 1024 * 1024
MAX_UPSTREAM_ERROR_BYTES = 16 * 1024
MAX_UPSTREAM_ERROR_MESSAGE_CHARS = 500
SAFE_UPSTREAM_ERROR_CODES = frozenset(
    {
        "area_not_found",
        "attachment_consumed",
        "attachment_digest_mismatch",
        "attachment_expired",
        "attachment_invalid",
        "attachment_missing",
        "attachment_not_found",
        "idempotency_conflict",
        "invalid_amount",
        "invalid_date",
        "invalid_date_range",
        "invalid_datetime",
        "invalid_idempotency_key",
        "invalid_input",
        "invalid_content",
        "invalid_due_at",
        "invalid_memo_id",
        "invalid_patch",
        "invalid_priority",
        "invalid_tags",
        "media_invalid",
        "media_link_invalid",
        "media_missing",
        "media_not_found",
        "media_not_ready",
        "media_size_invalid",
        "media_type_rejected",
        "memo_not_found",
        "payment_has_refunds",
        "payment_not_found",
        "project_not_found",
        "refund_exceeds_payment",
        "stage_not_found",
        "transaction_not_found",
        "version_conflict",
        "version_required",
        "writer_disabled",
    }
)


class ToolProxyError(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def validate_base_url(value: str) -> str:
    if not value:
        return ""
    parsed = urlsplit(value)
    if (
        parsed.scheme != "http"
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
        or not HOST_RE.fullmatch(parsed.hostname)
        or re.fullmatch(r"[0-9.]+", parsed.hostname)
        or ":" in parsed.hostname
    ):
        raise ToolProxyError("invalid_internal_url", "内部服务地址必须使用固定 http 主机名")
    port = f":{parsed.port}" if parsed.port is not None else ""
    return f"http://{parsed.hostname}{port}"


class ToolRouter:
    def __init__(
        self,
        *,
        ledger_base_url: str = "",
        ledger_token: str = "",
        gateway_base_url: str = "",
        gateway_token: str = "",
        operations_base_url: str = "",
        operations_token: str = "",
        memo_base_url: str = "",
        memo_http_username: str = "",
        memo_http_password: str = "",
        memo_api_token: str = "",
        home_assistant_token: str = "",
        request_json: Callable[..., dict[str, Any]] | None = None,
        request_memo_json: Callable[..., dict[str, Any]] | None = None,
        request_bytes: Callable[..., tuple[dict[str, Any], bytes]] | None = None,
        request_artifact: Callable[..., tuple[dict[str, Any], bytes]] | None = None,
        request_home_assistant: HomeAssistantRequest | None = None,
        stream_media: Callable[..., dict[str, Any]] | None = None,
        max_media_bytes: int = DEFAULT_MAX_GATEWAY_MEDIA_BYTES,
        store: ControllerStore | None = None,
        manifest_poll_interval: float = 30.0,
    ):
        self.ledger_base_url = validate_base_url(ledger_base_url)
        self.ledger_token = ledger_token
        self.gateway_base_url = validate_base_url(gateway_base_url)
        self.gateway_token = gateway_token
        self.operations_base_url = validate_base_url(operations_base_url)
        self.operations_token = operations_token
        self.memo_base_url = validate_base_url(memo_base_url)
        self.memo_http_username = memo_http_username
        self.memo_http_password = memo_http_password
        self.memo_api_token = memo_api_token
        self.home_assistant_token = home_assistant_token
        self.request_json = request_json or _request_json
        self.request_memo_json = request_memo_json or _request_memo_json
        self.request_bytes = request_bytes or _request_bytes
        self.request_artifact = request_artifact or _request_hub_chart
        self.request_home_assistant = request_home_assistant or request_home_assistant_json
        self.stream_media = stream_media or _stream_gateway_to_hub
        self.max_media_bytes = max_media_bytes
        self.store = store
        self.manifest_poll_interval = max(0.1, float(manifest_poll_interval))
        self._context_lock = threading.Lock()
        self._active_context: dict[str, Any] | None = None
        self._definition_lock = threading.RLock()
        self._hub_manifest: ValidatedHubManifest = BOOTSTRAP_MANIFEST
        self._fallback_revision = 1
        self._manifest_stop = threading.Event()
        self._manifest_thread: threading.Thread | None = None
        self._load_last_good_manifest()

    def begin_job(
        self,
        job_id: str,
        message_id: str,
        capability_profile: str = "owner_legacy",
        *,
        conversation_key: str = "",
        media_archive_authorized: bool | None = None,
    ) -> None:
        if not job_id or not message_id:
            raise ToolProxyError("tool_context_invalid", "工具调用上下文无效")
        if capability_profile not in {"owner_legacy", "owner", "member_read_only"}:
            raise ToolProxyError("tool_context_invalid", "工具能力画像无效")
        with self._context_lock:
            if self._active_context is not None:
                raise ToolProxyError("tool_context_busy", "已有活动工具调用上下文")
            self._active_context = {
                "job_id": job_id,
                "message_id": message_id,
                "turn_id": "",
                "capability_profile": capability_profile,
                "conversation_key": conversation_key,
            }
            if media_archive_authorized is not None:
                self._active_context["media_archive_authorized"] = media_archive_authorized

    def bind_turn(self, job_id: str, turn_id: str) -> None:
        with self._context_lock:
            if self._active_context is None or self._active_context["job_id"] != job_id:
                raise ToolProxyError("tool_context_invalid", "无法绑定工具调用 Turn")
            self._active_context["turn_id"] = turn_id

    def end_turn(self, turn_id: str) -> None:
        with self._context_lock:
            if self._active_context is not None and self._active_context.get("turn_id") == turn_id:
                self._active_context = None

    def clear_job(self, job_id: str) -> None:
        with self._context_lock:
            if self._active_context is not None and self._active_context["job_id"] == job_id:
                self._active_context = None

    def _load_last_good_manifest(self) -> None:
        if self.store is None:
            return
        try:
            document = self.store.load_hub_manifest_document()
        except StoreError:
            return
        if document is None:
            return
        try:
            manifest = validate_hub_manifest(document)
        except HubManifestError as exc:
            try:
                self.store.record_hub_manifest_error(exc.code)
            except StoreError:
                pass
            return
        with self._definition_lock:
            self._hub_manifest = manifest

    def tool_definitions(self) -> tuple[ToolDefinition, ...]:
        with self._definition_lock:
            hub_definitions = self._hub_manifest.definitions
        return hub_definitions + MEMO_DEFINITIONS + OPERATION_DEFINITIONS + AITO_PREPARE_CAR_DEFINITIONS

    def tool_definitions_by_name(self) -> dict[str, ToolDefinition]:
        return {definition.name: definition for definition in self.tool_definitions()}

    def _tool_definition(self, name: str) -> ToolDefinition | None:
        with self._definition_lock:
            for definition in self._hub_manifest.definitions:
                if definition.name == name:
                    return definition
        for definition in MEMO_DEFINITIONS + OPERATION_DEFINITIONS + AITO_PREPARE_CAR_DEFINITIONS:
            if definition.name == name:
                return definition
        return None

    def _hub_configured(self) -> bool:
        return bool(self.ledger_base_url and len(self.ledger_token) >= 32)

    def _gateway_configured(self) -> bool:
        return bool(self.gateway_base_url and len(self.gateway_token) >= 32)

    def _memo_configured(self) -> bool:
        username = self.memo_http_username
        password = self.memo_http_password
        return bool(
            self.memo_base_url
            and isinstance(username, str)
            and 1 <= len(username) <= 128
            and ":" not in username
            and username == username.strip()
            and not any(ord(character) < 32 or ord(character) == 127 for character in username)
            and isinstance(password, str)
            and 1 <= len(password) <= 4096
            and not any(ord(character) < 32 or ord(character) == 127 for character in password)
            and len(self.memo_api_token) >= 32
        )

    def _prepare_car_configured(self) -> bool:
        return isinstance(self.home_assistant_token, str) and len(self.home_assistant_token) >= 32

    def configured_tools(self) -> frozenset[str]:
        tools: set[str] = set()
        hub_configured = self._hub_configured()
        gateway_configured = self._gateway_configured()
        for definition in self.tool_definitions():
            if definition.service == "renovation_hub":
                if not hub_configured:
                    continue
                if definition.transport == "gateway_attachment" and not gateway_configured:
                    continue
                tools.add(definition.name)
            elif definition.service == "ha_operations_broker":
                if self.operations_base_url and len(self.operations_token) >= 32:
                    tools.add(definition.name)
            elif definition.service == "family_memo" and self._memo_configured():
                tools.add(definition.name)
            elif definition.service == "home_assistant_prepare_car" and self._prepare_car_configured():
                tools.add(definition.name)
        return frozenset(tools)

    def route_ready_tools(self) -> frozenset[str]:
        tools = set(self.configured_tools())
        if not self._gateway_configured():
            definitions = self.tool_definitions_by_name()
            tools = {
                name
                for name in tools
                if definitions[name].transport not in {"gateway_attachment", "gateway_media_stream"}
            }
        return frozenset(tools)

    def available_tools(
        self,
        capability_profile: str | None = None,
        *,
        media_archive_authorized: bool | None = None,
    ) -> list[str]:
        configured = set(self.configured_tools())
        if self.store is not None:
            try:
                configured &= set(self.store.tool_policy_snapshot()["enabled"])
            except StoreError:
                return []
        if capability_profile == "member_read_only":
            configured &= set(MEMBER_ALLOWED_TOOL_NAMES)
        elif capability_profile == "owner_legacy":
            configured -= set(PREPARE_CAR_TOOL_NAMES)
        elif capability_profile not in {None, "owner"}:
            return []
        if media_archive_authorized is False:
            configured.discard("renovation_media_ingest")
        return sorted(configured)

    def catalog_payload(self) -> dict[str, Any]:
        definitions = self.tool_definitions()
        configured = set(self.configured_tools())
        if self.store is None:
            enabled = set(self.available_tools())
            return {
                "tools": [
                    definition.mcp_document()
                    for definition in definitions
                    if definition.name in enabled
                ],
                "revision": self._fallback_revision,
                "policy_error": None,
            }
        try:
            snapshot = self.store.tool_policy_snapshot()
        except StoreError as exc:
            return {"tools": [], "revision": None, "policy_error": exc.code}
        enabled = set(snapshot["enabled"]) & configured
        return {
            "tools": [
                definition.mcp_document()
                for definition in definitions
                if definition.name in enabled
            ],
            "revision": snapshot["revision"],
            "policy_error": None,
        }

    def catalog_revision(self) -> dict[str, Any]:
        if self.store is None:
            return {"revision": self._fallback_revision}
        try:
            return {"revision": self.store.tool_catalog_revision()}
        except StoreError as exc:
            raise ToolProxyError(exc.code, "工具策略不可用") from exc

    def observe_catalog(self, arguments: dict[str, Any]) -> dict[str, Any]:
        if self.store is None:
            return {"observed": True}
        revision = arguments.get("revision")
        tools = arguments.get("tools")
        if revision is not None and not isinstance(revision, int):
            raise ToolProxyError("invalid_catalog_observation", "MCP 目录版本无效")
        if not isinstance(tools, list) or any(not isinstance(name, str) for name in tools):
            raise ToolProxyError("invalid_catalog_observation", "MCP 目录工具无效")
        expected = self.catalog_payload()
        expected_names = sorted(
            tool["name"]
            for tool in expected["tools"]
            if isinstance(tool, dict) and isinstance(tool.get("name"), str)
        )
        if revision != expected["revision"] or sorted(set(tools)) != expected_names:
            raise ToolProxyError("catalog_observation_stale", "MCP 目录回报已过期")
        try:
            return self.store.record_mcp_catalog(revision, tools)
        except StoreError as exc:
            raise ToolProxyError(exc.code, str(exc)) from exc

    def sync_hub_manifest(self) -> dict[str, Any]:
        if not self._hub_configured():
            raise ToolProxyError("hub_manifest_unconfigured", "Renovation Hub manifest 接口未配置")
        try:
            document = self.request_json(
                "GET",
                f"{self.ledger_base_url}/internal/v1/mcp/manifest",
                self.ledger_token,
                None,
            )
            manifest = validate_hub_manifest(document)
            if self.store is None:
                with self._definition_lock:
                    changed = self._hub_manifest.digest != manifest.digest
                    if changed:
                        self._fallback_revision += 1
                    revision = self._fallback_revision
                    self._hub_manifest = manifest
                return {
                    "changed": changed,
                    "revision": revision,
                    "hub_revision": manifest.revision,
                    "catalog_digest": manifest.digest,
                }
            with self._definition_lock:
                result = self.store.apply_hub_manifest(manifest.document)
                self._hub_manifest = manifest
            return result
        except HubManifestError as exc:
            self._record_manifest_error(exc.code)
            raise ToolProxyError(exc.code, "Renovation Hub manifest 无效") from exc
        except StoreError as exc:
            self._record_manifest_error("hub_manifest_store_failed")
            raise ToolProxyError(exc.code, "Renovation Hub manifest 无法保存") from exc
        except ToolProxyError as exc:
            code = (
                "hub_manifest_unavailable"
                if exc.code.startswith("upstream_")
                else exc.code
            )
            self._record_manifest_error(code)
            raise
        except Exception as exc:
            self._record_manifest_error("hub_manifest_sync_failed")
            raise ToolProxyError("hub_manifest_sync_failed", "Renovation Hub manifest 同步失败") from exc

    def _record_manifest_error(self, code: str) -> None:
        if self.store is None:
            return
        try:
            self.store.record_hub_manifest_error(code)
        except StoreError:
            pass

    def start_manifest_sync(self) -> None:
        if not self._hub_configured():
            return
        if self._manifest_thread is not None and self._manifest_thread.is_alive():
            return
        self._manifest_stop.clear()

        def synchronize() -> None:
            while not self._manifest_stop.is_set():
                try:
                    self.sync_hub_manifest()
                except ToolProxyError:
                    pass
                if self._manifest_stop.wait(self.manifest_poll_interval):
                    break

        self._manifest_thread = threading.Thread(
            target=synchronize,
            name="controller-hub-manifest-sync",
            daemon=True,
        )
        self._manifest_thread.start()

    def stop_manifest_sync(self) -> None:
        self._manifest_stop.set()
        if self._manifest_thread is not None:
            self._manifest_thread.join(timeout=2)
        self._manifest_thread = None

    def tool_status(self) -> dict[str, Any]:
        if self.store is None:
            raise ToolProxyError("tool_policy_invalid", "工具策略存储未配置")
        return self.store.tool_control_document(
            self.configured_tools(),
            self.route_ready_tools(),
            definitions=self.tool_definitions(),
        )

    def update_tool_policy(
        self,
        tool_name: str,
        *,
        enabled: bool,
        revision: int,
        request_id: str,
    ) -> dict[str, Any]:
        if self.store is None:
            raise StoreError("tool_policy_invalid", "工具策略存储未配置", status=503)
        if self._tool_definition(tool_name) is None:
            raise StoreError("tool_not_found", "工具不存在", status=404)
        return self.store.update_tool_policy(
            tool_name,
            enabled=enabled,
            revision=revision,
            request_id=request_id,
        )

    def preview_attachment(self, reference: str) -> tuple[dict[str, Any], bytes]:
        """Fetch a verified, non-consuming attachment preview for Codex input."""
        if not self.gateway_base_url or len(self.gateway_token) < 32:
            raise ToolProxyError("gateway_unavailable", "Weixin Gateway 附件接口未配置")
        if not isinstance(reference, str) or not ATTACHMENT_REF_RE.fullmatch(reference):
            raise ToolProxyError("attachment_ref_invalid", "attachment_ref 无效")
        return self.request_bytes(
            "GET",
            f"{self.gateway_base_url}/internal/v1/attachments/{quote(reference, safe='')}/preview",
            self.gateway_token,
            MAX_GATEWAY_ATTACHMENT_BYTES,
        )

    def call(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        started = time.monotonic()
        outcome = "failed"
        error_code: str | None = None
        with self._context_lock:
            invocation_context = None if self._active_context is None else dict(self._active_context)
        try:
            definition = self._authorize_tool(name)
            if name == "ledger_generate_chart":
                with self._context_lock:
                    context = None if self._active_context is None else dict(self._active_context)
                if context is None or not context.get("job_id"):
                    raise ToolProxyError("tool_context_unavailable", "图表只能在活动 Controller 作业中生成")
            result = self._call_authorized(definition, arguments)
            if name == "ledger_generate_chart":
                result = self._capture_chart_result(result)
            outcome = "succeeded"
            return result
        except ToolProxyError as exc:
            error_code = exc.code
            outcome = "rejected" if exc.code in {
                "unknown_tool",
                "tool_unconfigured",
                "tool_disabled",
                "tool_policy_invalid",
                "tool_not_allowed_for_profile",
            } else "failed"
            raise
        finally:
            if self.store is not None:
                try:
                    self.store.record_tool_invocation(
                        name,
                        outcome=outcome,
                        error_code=error_code,
                        duration_ms=int((time.monotonic() - started) * 1000),
                        job_id=None if invocation_context is None else invocation_context.get("job_id"),
                        turn_id=None if invocation_context is None else invocation_context.get("turn_id") or None,
                    )
                except (StoreError, sqlite3.DatabaseError, OSError):
                    pass

    def _capture_chart_result(self, result: dict[str, Any]) -> dict[str, Any]:
        if self.store is None:
            raise ToolProxyError("artifact_store_unavailable", "Controller artifact 存储不可用")
        if not isinstance(result, dict):
            raise ToolProxyError("artifact_metadata_invalid", "Renovation Hub 图表响应无效")
        chart = result.get("result") if result.get("version") == 1 else result
        if not isinstance(chart, dict):
            raise ToolProxyError("artifact_metadata_invalid", "Renovation Hub 图表结果无效")
        reference = chart.get("download_ref")
        if not isinstance(reference, str) or not CHART_REF_RE.fullmatch(reference):
            raise ToolProxyError("artifact_reference_invalid", "Renovation Hub 图表引用无效")
        with self._context_lock:
            context = None if self._active_context is None else dict(self._active_context)
        if context is None or not context.get("job_id"):
            raise ToolProxyError("tool_context_unavailable", "图表只能在活动 Controller 作业中生成")
        try:
            response_meta, content = self.request_artifact(
                "GET",
                f"{self.ledger_base_url}/internal/v1/downloads/chart/{quote(reference, safe='')}",
                self.ledger_token,
                min(self.store.max_artifact_bytes, MAX_JOB_ARTIFACT_BYTES),
            )
            if response_meta.get("mime_type") != "image/png":
                raise StoreError("artifact_content_type_invalid", "Hub 图表类型不是 PNG", status=502)
            artifact = self.store.capture_chart_artifact(context["job_id"], chart, content)
        except StoreError as exc:
            raise ToolProxyError(exc.code, str(exc)) from exc
        safe_artifact = {key: value for key, value in artifact.items() if key != "fallback_path"}
        return {
            "delivery": "weixin_gateway_automatic",
            "artifact": safe_artifact,
            "summary": chart.get("summary"),
        }

    def _authorize_tool(self, name: str) -> ToolDefinition:
        definition = self._tool_definition(name)
        if definition is None:
            raise ToolProxyError("unknown_tool", "工具不在允许清单")
        with self._context_lock:
            context = None if self._active_context is None else dict(self._active_context)
        profile = None if context is None else context.get("capability_profile")
        if name in PREPARE_CAR_TOOL_NAMES and profile != "owner":
            raise ToolProxyError("tool_not_allowed_for_profile", "只有 owner 可以调用备车工具")
        if profile == "member_read_only" and name not in MEMBER_ALLOWED_TOOL_NAMES:
            raise ToolProxyError("tool_not_allowed_for_profile", "当前微信成员没有调用该工具的权限")
        if name not in self.configured_tools():
            raise ToolProxyError("tool_unconfigured", "工具所属内部服务未配置")
        if self.store is not None:
            try:
                enabled = self.store.tool_policy_snapshot()["enabled"]
            except StoreError as exc:
                raise ToolProxyError("tool_policy_invalid", "工具策略不可用") from exc
            if name not in enabled:
                raise ToolProxyError("tool_disabled", "工具已由管理员关闭")
        if definition.requires_job_context and context is None:
            raise ToolProxyError("tool_context_unavailable", "该工具只能在活动 Controller 作业中调用")
        if (
            definition.transport == "gateway_media_stream"
            and context is not None
            and context.get("media_archive_authorized") is False
        ):
            raise ToolProxyError(
                "media_archive_not_authorized",
                "只有用户明确请求归档到装修档案时才能保存该附件",
            )
        return definition

    def _call_authorized(self, definition: ToolDefinition, arguments: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(arguments, dict):
            raise ToolProxyError("invalid_arguments", "工具参数必须是对象")
        name = definition.name
        if definition.service == "renovation_hub" and definition.transport == "gateway_media_stream":
            if not self.ledger_base_url or len(self.ledger_token) < 32:
                raise ToolProxyError("ledger_unavailable", "Renovation Hub 媒体接口未配置")
            if not self.gateway_base_url or len(self.gateway_token) < 32:
                raise ToolProxyError("gateway_unavailable", "Weixin Gateway 附件接口未配置")
            reference = arguments.get("attachment_ref")
            if not isinstance(reference, str) or not ATTACHMENT_REF_RE.fullmatch(reference):
                raise ToolProxyError("attachment_ref_invalid", "attachment_ref 无效")
            if definition.idempotent_write:
                arguments = self._with_deterministic_idempotency(name, arguments)
            return self.stream_media(
                self.gateway_base_url,
                self.gateway_token,
                self.ledger_base_url,
                self.ledger_token,
                reference,
                {key: value for key, value in arguments.items() if key != "attachment_ref"},
                self.max_media_bytes,
            )
        if definition.service == "renovation_hub":
            if not self.ledger_base_url or len(self.ledger_token) < 32:
                raise ToolProxyError("ledger_unavailable", "Renovation Hub 账本接口未配置")
            if definition.transport == "gateway_attachment" and not self._gateway_configured():
                raise ToolProxyError("gateway_unavailable", "Weixin Gateway 附件接口未配置")
            if definition.transport not in {"json", "gateway_attachment"}:
                raise ToolProxyError("tool_transport_invalid", "工具传输方式不受支持")
            if definition.idempotent_write:
                arguments = self._with_deterministic_idempotency(name, arguments)
            ledger_arguments = dict(arguments)
            if definition.transport == "gateway_attachment":
                ledger_arguments = self._resolve_gateway_attachment(arguments)
            return self.request_json(
                "POST",
                f"{self.ledger_base_url}/internal/v1/tools/call",
                self.ledger_token,
                {"name": name, "arguments": ledger_arguments, "actor_hash": "sha256:codex-controller"},
            )
        if definition.service == "family_memo":
            return self._memo_call(name, arguments)
        if definition.service == "home_assistant_prepare_car":
            return self._prepare_car_call(name, arguments)
        if name == "ha_operations_propose_restart":
            self._require_exact_keys(arguments, {"target"})
            target = arguments.get("target")
            if not isinstance(target, str) or not ADDON_SLUG_RE.fullmatch(target):
                raise ToolProxyError("invalid_target", "Add-on slug 无效")
            idempotency_key = self._deterministic_idempotency(name, arguments)
            return self._operations(
                "POST",
                "/v1/proposals",
                {
                    "version": 1,
                    "action_type": "restart_addon",
                    "target": target,
                    "idempotency_key": idempotency_key,
                },
            )
        if name == "ha_operations_authorization_request":
            self._require_exact_keys(arguments, {"action_id"})
            return self._operations(
                "POST",
                "/v1/authorization/requests",
                {"version": 1, "action_id": self._action_id(arguments)},
            )
        if name == "ha_operations_authorization_status":
            self._require_exact_keys(arguments, {"approval_id"})
            approval_id = arguments.get("approval_id")
            if not isinstance(approval_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{8,160}", approval_id):
                raise ToolProxyError("invalid_approval_id", "approval_id 无效")
            return self._operations("GET", f"/v1/authorization/requests/{quote(approval_id, safe='')}", None)
        if name == "ha_operations_execute_restart":
            required = {"receipt_id", "action_id", "proposal_hash", "idempotency_key"}
            self._require_exact_keys(arguments, required)
            payload = {"version": 1, **{key: arguments[key] for key in sorted(required)}}
            if (
                not isinstance(payload["receipt_id"], str)
                or not RECEIPT_ID_RE.fullmatch(payload["receipt_id"])
                or not isinstance(payload["action_id"], str)
                or not ACTION_ID_RE.fullmatch(payload["action_id"])
                or not isinstance(payload["proposal_hash"], str)
                or not SHA256_ID_RE.fullmatch(payload["proposal_hash"])
                or not isinstance(payload["idempotency_key"], str)
                or not SHA256_ID_RE.fullmatch(payload["idempotency_key"])
            ):
                raise ToolProxyError("invalid_arguments", "Operations 执行参数无效")
            return self._operations("POST", "/v1/executions", payload)
        if name == "ha_operations_execution_status":
            self._require_exact_keys(arguments, {"action_id"})
            action_id = self._action_id(arguments)
            return self._operations("GET", f"/v1/executions/{quote(action_id, safe='')}", None)
        raise ToolProxyError("unknown_tool", "工具不在允许清单")

    def cancel_pending_prepare_car(self, conversation_key: str, message_id: str) -> bool:
        if self.store is None:
            return False
        try:
            return self.store.cancel_prepare_car_pending(conversation_key, message_id)
        except StoreError as exc:
            raise ToolProxyError(exc.code, str(exc)) from exc

    def _prepare_car_call(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        if name not in PREPARE_CAR_TOOL_NAMES:
            raise ToolProxyError("unknown_tool", "备车工具不在允许清单")
        if not self._prepare_car_configured() or self.store is None:
            raise ToolProxyError("prepare_car_unavailable", "备车工具未配置")
        with self._context_lock:
            context = None if self._active_context is None else dict(self._active_context)
        if context is None or context.get("capability_profile") != "owner":
            raise ToolProxyError("tool_not_allowed_for_profile", "只有 owner 可以调用备车工具")
        if name == "aito_prepare_car_status":
            self._require_exact_keys(arguments, set())
            try:
                document = self.request_home_assistant(
                    "GET",
                    f"/states/{PREPARE_CAR_ENTITY_ID}",
                    self.home_assistant_token,
                    None,
                )
                return safe_entity_state(document)
            except HomeAssistantPrepareCarError as exc:
                return {"status": "unavailable", "error_code": exc.code}
        self._require_exact_keys(arguments, {"target"})
        target = arguments.get("target")
        if not isinstance(target, bool):
            raise ToolProxyError("invalid_arguments", "target 必须是布尔值")
        conversation_key = context.get("conversation_key")
        message_id = context.get("message_id")
        if not isinstance(conversation_key, str) or not isinstance(message_id, str):
            raise ToolProxyError("tool_context_unavailable", "备车工具缺少微信会话上下文")
        if name == "aito_prepare_car_request":
            try:
                return self.store.prepare_car_request(
                    conversation_key,
                    message_id,
                    target,
                    ttl_seconds=PREPARE_CAR_CONFIRMATION_TTL_SECONDS,
                )
            except StoreError as exc:
                raise ToolProxyError(exc.code, str(exc)) from exc
        try:
            claim = self.store.claim_prepare_car_execute(conversation_key, message_id, target)
        except StoreError as exc:
            if exc.code in {"CONFIRMATION_MISSING", "CONFIRMATION_EXPIRED", "CONFIRMATION_ACTION_MISMATCH"}:
                return {"status": "rejected", "error_code": exc.code, "target": target}
            raise ToolProxyError(exc.code, str(exc)) from exc
        if claim.get("status") != "executing":
            replay = dict(claim)
            replay["idempotent_replay"] = True
            return replay
        confirmation_id = claim["confirmation_id"]
        try:
            state = safe_entity_state(
                self.request_home_assistant(
                    "GET",
                    f"/states/{PREPARE_CAR_ENTITY_ID}",
                    self.home_assistant_token,
                    None,
                )
            )
        except HomeAssistantPrepareCarError as exc:
            return self.store.finish_prepare_car_execute(
                confirmation_id,
                {"status": "failed", "error_code": exc.code, "target": target},
            )
        if state.get("status") != "available":
            return self.store.finish_prepare_car_execute(
                confirmation_id,
                {"status": "failed", "error_code": "HA_ENTITY_UNAVAILABLE", "target": target},
            )
        if state.get("entity_state") == ("on" if target else "off") and state.get("command_state") == "confirmed":
            return self.store.finish_prepare_car_execute(
                confirmation_id,
                {"status": "already_confirmed", "target": target},
            )
        try:
            self.request_home_assistant(
                "POST",
                "/services/switch/turn_on" if target else "/services/switch/turn_off",
                self.home_assistant_token,
                {"entity_id": PREPARE_CAR_ENTITY_ID},
            )
        except HomeAssistantPrepareCarError as exc:
            return self.store.finish_prepare_car_execute(
                confirmation_id,
                {
                    "status": "unknown" if exc.outcome_unknown else "failed",
                    "error_code": exc.code,
                    "target": target,
                },
            )
        return self.store.finish_prepare_car_execute(
            confirmation_id,
            {"status": "submitted", "target": target},
        )

    def _memo_call(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        if name not in MEMO_TOOLS:
            raise ToolProxyError("unknown_tool", "家庭备忘录工具不在允许清单")
        if not self._memo_configured():
            raise ToolProxyError("memo_unavailable", "家庭备忘录接口未配置")
        base = f"{self.memo_base_url}/endpoint/api/memos"
        if name == "memo_create":
            allowed = {"title", "content", "priority", "category", "due_at"}
            self._require_allowed_keys(arguments, allowed, {"content"})
            payload = self._normalize_memo_fields(arguments, allow_id=False, require_change=False)
            payload.update(
                {
                    "source": "wechat",
                    "source_message_id": self._memo_source_message_id(),
                }
            )
            return self._memo_request("POST", base, payload)
        if name == "memo_list":
            allowed = {"status", "category", "date", "overdue", "limit"}
            self._require_allowed_keys(arguments, allowed, set())
            query: dict[str, str] = {}
            status = arguments.get("status")
            if status is not None:
                if status not in {"pending", "completed", "cancelled"}:
                    raise ToolProxyError("invalid_arguments", "备忘录状态无效")
                query["status"] = status
            category = arguments.get("category")
            if category is not None:
                query["category"] = self._memo_text(category, "category", 1, 100)
            date = arguments.get("date")
            if date is not None:
                if date != "today":
                    raise ToolProxyError("invalid_arguments", "备忘录日期筛选无效")
                query["date"] = date
            overdue = arguments.get("overdue")
            if overdue is not None:
                if not isinstance(overdue, bool):
                    raise ToolProxyError("invalid_arguments", "overdue 必须是布尔值")
                query["overdue"] = "true" if overdue else "false"
            limit = arguments.get("limit", 100)
            if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 100:
                raise ToolProxyError("invalid_arguments", "limit 必须在 1 到 100 之间")
            query["limit"] = str(limit)
            return self._memo_request("GET", f"{base}?{urlencode(query)}", None)
        if name == "memo_update":
            allowed = {"id", "title", "content", "priority", "category", "due_at"}
            self._require_allowed_keys(arguments, allowed, {"id"})
            memo_id = self._memo_id(arguments.get("id"))
            payload = self._normalize_memo_fields(arguments, allow_id=True, require_change=True)
            return self._memo_request("PATCH", f"{base}/{quote(memo_id, safe='')}", payload)
        if name in {"memo_complete", "memo_cancel"}:
            self._require_allowed_keys(arguments, {"id"}, {"id"})
            memo_id = self._memo_id(arguments.get("id"))
            action = "complete" if name == "memo_complete" else "cancel"
            return self._memo_request("POST", f"{base}/{quote(memo_id, safe='')}/{action}", {})
        raise ToolProxyError("unknown_tool", "家庭备忘录工具不在允许清单")

    def _memo_request(
        self,
        method: str,
        url: str,
        payload: dict[str, Any] | None,
    ) -> dict[str, Any]:
        return self.request_memo_json(
            method,
            url,
            self.memo_http_username,
            self.memo_http_password,
            self.memo_api_token,
            payload,
        )

    def _memo_source_message_id(self) -> str:
        with self._context_lock:
            context = None if self._active_context is None else dict(self._active_context)
        if context is None or not context.get("message_id"):
            raise ToolProxyError("tool_context_unavailable", "新增备忘录需要当前微信消息上下文")
        digest = hashlib.sha256(context["message_id"].encode("utf-8")).hexdigest()
        return f"wechat:{digest}"

    @staticmethod
    def _memo_id(value: Any) -> str:
        if not isinstance(value, str) or not MEMO_ID_RE.fullmatch(value):
            raise ToolProxyError("invalid_arguments", "备忘录 ID 无效")
        return value

    @staticmethod
    def _memo_text(value: Any, field: str, minimum: int, maximum: int) -> str:
        if not isinstance(value, str):
            raise ToolProxyError("invalid_arguments", f"{field} 必须是字符串")
        normalized = value.strip()
        if not minimum <= len(normalized) <= maximum:
            raise ToolProxyError("invalid_arguments", f"{field} 长度无效")
        return normalized

    @classmethod
    def _normalize_memo_fields(
        cls,
        arguments: dict[str, Any],
        *,
        allow_id: bool,
        require_change: bool,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {}
        if "title" in arguments:
            payload["title"] = None if arguments["title"] is None else cls._memo_text(arguments["title"], "title", 1, 200)
        if "content" in arguments:
            payload["content"] = cls._memo_text(arguments["content"], "content", 1, 2000)
        if "priority" in arguments:
            priority = arguments["priority"]
            if priority not in {"low", "normal", "high", "urgent"}:
                raise ToolProxyError("invalid_arguments", "priority 不在允许范围")
            payload["priority"] = priority
        if "category" in arguments:
            payload["category"] = None if arguments["category"] is None else cls._memo_text(arguments["category"], "category", 1, 100)
        if "due_at" in arguments:
            due_at = arguments["due_at"]
            if due_at is not None and (not isinstance(due_at, str) or not MEMO_DUE_AT_RE.fullmatch(due_at)):
                raise ToolProxyError("invalid_arguments", "due_at 必须使用 Asia/Shanghai ISO 8601 时间")
            payload["due_at"] = due_at
        if require_change and not payload:
            raise ToolProxyError("invalid_arguments", "修改备忘录至少需要一个变更字段")
        if not allow_id and "id" in arguments:
            raise ToolProxyError("invalid_arguments", "新增备忘录不接受 id")
        return payload

    @staticmethod
    def _require_allowed_keys(
        arguments: dict[str, Any],
        allowed: set[str],
        required: set[str],
    ) -> None:
        if not required.issubset(arguments) or any(key not in allowed for key in arguments):
            raise ToolProxyError("invalid_arguments", "家庭备忘录工具参数字段不匹配")

    @staticmethod
    def _require_exact_keys(arguments: dict[str, Any], expected: set[str]) -> None:
        if set(arguments) != expected:
            raise ToolProxyError("invalid_arguments", "工具参数字段不匹配")

    @staticmethod
    def _action_id(arguments: dict[str, Any]) -> str:
        action_id = arguments.get("action_id")
        if not isinstance(action_id, str) or not ACTION_ID_RE.fullmatch(action_id):
            raise ToolProxyError("invalid_action_id", "action_id 无效")
        return action_id

    def _with_deterministic_idempotency(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        normalized = dict(arguments)
        normalized.pop("idempotency_key", None)
        normalized["idempotency_key"] = self._deterministic_idempotency(name, normalized)
        return normalized

    def _deterministic_idempotency(self, name: str, arguments: dict[str, Any]) -> str:
        with self._context_lock:
            context = None if self._active_context is None else dict(self._active_context)
        if context is None:
            raise ToolProxyError("tool_context_unavailable", "写工具只能在活动 Controller 作业中调用")
        canonical = json.dumps(arguments, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        digest = hashlib.sha256(
            f"codex-controller-tool-v1\n{context['message_id']}\n{name}\n{canonical}".encode("utf-8")
        ).hexdigest()
        return f"sha256:{digest}"

    def _resolve_gateway_attachment(self, arguments: dict[str, Any]) -> dict[str, Any]:
        if not self.gateway_base_url or len(self.gateway_token) < 32:
            raise ToolProxyError("gateway_unavailable", "Weixin Gateway 附件接口未配置")
        reference = arguments.get("attachment_ref")
        if not isinstance(reference, str) or not ATTACHMENT_REF_RE.fullmatch(reference):
            raise ToolProxyError("attachment_ref_invalid", "attachment_ref 无效")
        metadata, content = self.request_bytes(
            "GET",
            f"{self.gateway_base_url}/internal/v1/attachments/{quote(reference, safe='')}",
            self.gateway_token,
            MAX_GATEWAY_ATTACHMENT_BYTES,
        )
        resolved = {key: value for key, value in arguments.items() if key != "attachment_ref"}
        resolved.update(
            {
                "original_filename": metadata["original_filename"],
                "mime_type": metadata["mime_type"],
                "content_base64": base64.b64encode(content).decode("ascii"),
            }
        )
        return resolved

    def _operations(self, method: str, path: str, payload: dict[str, Any] | None) -> dict[str, Any]:
        if not self.operations_base_url or len(self.operations_token) < 32:
            raise ToolProxyError("operations_unavailable", "HA Operations Broker 未配置")
        return self.request_json(method, f"{self.operations_base_url}{path}", self.operations_token, payload)


def _request_json(method: str, url: str, token: str, payload: dict[str, Any] | None) -> dict[str, Any]:
    body = None if payload is None else json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    request = Request(
        url,
        data=body,
        method=method,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json", "Accept": "application/json"},
    )
    try:
        with urlopen(request, timeout=15) as response:
            data = response.read(2 * 1024 * 1024 + 1)
    except HTTPError as exc:
        try:
            structured_error = _safe_structured_http_error(exc)
        finally:
            exc.close()
        if structured_error is not None:
            raise structured_error from exc
        raise ToolProxyError("upstream_rejected", f"内部服务拒绝请求：HTTP {exc.code}") from exc
    except (URLError, TimeoutError, OSError) as exc:
        raise ToolProxyError("upstream_unavailable", "内部服务不可用") from exc
    if len(data) > 2 * 1024 * 1024:
        raise ToolProxyError("upstream_response_too_large", "内部服务响应过大")
    try:
        result = json.loads(data)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ToolProxyError("upstream_invalid_json", "内部服务响应无效") from exc
    if not isinstance(result, dict):
        raise ToolProxyError("upstream_invalid_json", "内部服务响应不是对象")
    return result


def _request_memo_json(
    method: str,
    url: str,
    username: str,
    password: str,
    token: str,
    payload: dict[str, Any] | None,
) -> dict[str, Any]:
    body = None if payload is None else json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    basic = base64.b64encode(f"{username}:{password}".encode("utf-8")).decode("ascii")
    request = Request(
        url,
        data=body,
        method=method,
        headers={
            "Authorization": f"Basic {basic}",
            "X-Family-Memo-Token": token,
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
    )
    try:
        with urlopen(request, timeout=15) as response:
            data = response.read(2 * 1024 * 1024 + 1)
    except HTTPError as exc:
        try:
            structured_error = _safe_structured_http_error(exc)
        finally:
            exc.close()
        if structured_error is not None:
            raise structured_error from exc
        if exc.code == 401:
            raise ToolProxyError("memo_not_authorized", "家庭备忘录接口认证失败") from exc
        if exc.code == 503:
            raise ToolProxyError("memo_unavailable", "家庭备忘录接口尚未配置") from exc
        raise ToolProxyError("memo_rejected", f"家庭备忘录接口拒绝请求：HTTP {exc.code}") from exc
    except (URLError, TimeoutError, OSError) as exc:
        raise ToolProxyError("memo_unavailable", "家庭备忘录接口不可用") from exc
    if len(data) > 2 * 1024 * 1024:
        raise ToolProxyError("memo_response_too_large", "家庭备忘录响应过大")
    try:
        result = json.loads(data)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ToolProxyError("memo_invalid_response", "家庭备忘录响应无效") from exc
    if not isinstance(result, dict):
        raise ToolProxyError("memo_invalid_response", "家庭备忘录响应不是对象")
    return result


def _safe_structured_http_error(exc: HTTPError) -> ToolProxyError | None:
    if exc.code not in {400, 404, 409, 410, 422}:
        return None
    try:
        if exc.headers.get_content_type() != "application/json":
            return None
        data = exc.read(MAX_UPSTREAM_ERROR_BYTES + 1)
    except (AttributeError, OSError):
        return None
    if not data or len(data) > MAX_UPSTREAM_ERROR_BYTES:
        return None
    try:
        document = json.loads(data)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(document, dict) or set(document) != {"error"}:
        return None
    error = document.get("error")
    if not isinstance(error, dict) or set(error) != {"code", "message"}:
        return None
    code = error.get("code")
    message = error.get("message")
    if code not in SAFE_UPSTREAM_ERROR_CODES:
        return None
    if (
        not isinstance(message, str)
        or not 1 <= len(message) <= MAX_UPSTREAM_ERROR_MESSAGE_CHARS
        or message != message.strip()
        or any(ord(character) < 32 or ord(character) == 127 for character in message)
    ):
        return None
    return ToolProxyError(code, message)


def _request_bytes(method: str, url: str, token: str, max_bytes: int) -> tuple[dict[str, Any], bytes]:
    request = Request(
        url,
        method=method,
        headers={"Authorization": f"Bearer {token}", "Accept": "application/octet-stream"},
    )
    try:
        with urlopen(request, timeout=30) as response:
            length_header = response.headers.get("Content-Length")
            if length_header:
                try:
                    declared_length = int(length_header)
                except ValueError as exc:
                    raise ToolProxyError("attachment_invalid", "Gateway 附件长度无效") from exc
                if declared_length < 1 or declared_length > max_bytes:
                    raise ToolProxyError("attachment_too_large", "Gateway 附件超过 Controller 上限")
            data = response.read(max_bytes + 1)
            encoded_filename = response.headers.get("X-Attachment-Filename", "")
            mime_type = response.headers.get_content_type()
            digest_header = response.headers.get("X-Attachment-Sha256", "")
    except HTTPError as exc:
        exc.close()
        raise ToolProxyError("attachment_unavailable", f"Gateway 拒绝附件读取：HTTP {exc.code}") from exc
    except (URLError, TimeoutError, OSError) as exc:
        raise ToolProxyError("gateway_unavailable", "Weixin Gateway 附件接口不可用") from exc
    if not data or len(data) > max_bytes:
        raise ToolProxyError("attachment_too_large", "Gateway 附件大小无效")
    try:
        filename = base64.urlsafe_b64decode(encoded_filename.encode("ascii")).decode("utf-8")
    except (ValueError, UnicodeDecodeError) as exc:
        raise ToolProxyError("attachment_invalid", "Gateway 附件文件名无效") from exc
    if not filename or len(filename) > 255 or Path(filename).name != filename:
        raise ToolProxyError("attachment_invalid", "Gateway 附件文件名越界")
    if mime_type not in ATTACHMENT_MIME_TYPES:
        raise ToolProxyError("attachment_invalid", "Gateway 附件类型不在 Ledger 白名单")
    digest = hashlib.sha256(data).hexdigest()
    if digest_header != f"sha256:{digest}":
        raise ToolProxyError("attachment_invalid", "Gateway 附件摘要不一致")
    return {
        "original_filename": filename,
        "mime_type": mime_type,
        "size_bytes": len(data),
        "sha256": digest_header,
    }, data


def _request_hub_chart(method: str, url: str, token: str, max_bytes: int) -> tuple[dict[str, Any], bytes]:
    request = Request(
        url,
        method=method,
        headers={"Authorization": f"Bearer {token}", "Accept": "image/png"},
    )
    try:
        with urlopen(request, timeout=30) as response:
            mime_type = response.headers.get_content_type()
            length_header = response.headers.get("Content-Length")
            if mime_type != "image/png":
                raise ToolProxyError("artifact_content_type_invalid", "Hub 图表类型不是 PNG")
            if not length_header:
                raise ToolProxyError("artifact_size_invalid", "Hub 图表缺少长度")
            try:
                declared_length = int(length_header)
            except ValueError as exc:
                raise ToolProxyError("artifact_size_invalid", "Hub 图表长度无效") from exc
            if not 1 <= declared_length <= max_bytes:
                raise ToolProxyError("artifact_too_large", "Hub 图表超过 Controller 上限")
            data = response.read(max_bytes + 1)
    except HTTPError as exc:
        exc.close()
        raise ToolProxyError("artifact_unavailable", f"Hub 拒绝图表读取：HTTP {exc.code}") from exc
    except (URLError, TimeoutError, OSError) as exc:
        raise ToolProxyError("upstream_unavailable", "Renovation Hub 图表接口不可用") from exc
    if len(data) != declared_length or len(data) > max_bytes:
        raise ToolProxyError("artifact_size_invalid", "Hub 图表实际长度不一致")
    if not data.startswith(b"\x89PNG\r\n\x1a\n"):
        raise ToolProxyError("artifact_content_invalid", "Hub 图表不是有效 PNG")
    return {"mime_type": mime_type, "size_bytes": len(data)}, data


def _stream_gateway_to_hub(
    gateway_base_url: str,
    gateway_token: str,
    hub_base_url: str,
    hub_token: str,
    reference: str,
    arguments: dict[str, Any],
    max_bytes: int,
) -> dict[str, Any]:
    idempotency_key = arguments.get("idempotency_key")
    if not isinstance(idempotency_key, str) or len(idempotency_key) < 16 or len(idempotency_key) > 256:
        raise ToolProxyError("invalid_idempotency_key", "媒体写入需要稳定 idempotency_key")
    source_ref_hash = hashlib.sha256(reference.encode("utf-8")).hexdigest()

    def confirm_consumption(result: dict[str, Any], digest: str) -> dict[str, Any]:
        try:
            _request_json(
                "POST",
                f"{gateway_base_url}/internal/v1/attachments/{quote(reference, safe='')}/ack",
                gateway_token,
                {"sha256": digest},
            )
        except ToolProxyError as exc:
            result.setdefault("result", {})["attachment_consumption"] = "pending"
            result.setdefault("result", {})["attachment_ack_error"] = exc.code
            result.setdefault("result", {})["warning_code"] = "attachment_ack_pending"
        else:
            result.setdefault("result", {})["attachment_consumption"] = "confirmed"
        return result

    replay_url = f"{hub_base_url}/internal/v1/media/replay?{urlencode({'idempotency_key': idempotency_key, 'source_ref_hash': source_ref_hash})}"
    replay_request = Request(replay_url, method="GET", headers={"Authorization": f"Bearer {hub_token}", "Accept": "application/json"})
    try:
        with urlopen(replay_request, timeout=10) as response:
            replay_data = response.read(2 * 1024 * 1024 + 1)
    except HTTPError as exc:
        if exc.code != 404:
            exc.close()
            raise ToolProxyError("upstream_rejected", f"Renovation Hub 拒绝媒体幂等检查：HTTP {exc.code}") from exc
        exc.close()
    except (URLError, TimeoutError, OSError) as exc:
        raise ToolProxyError("upstream_unavailable", "Renovation Hub 媒体接口不可用") from exc
    else:
        replay_result = _decode_json_response(replay_data)
        digest = str(replay_result.get("result", {}).get("media", {}).get("sha256") or "")
        return confirm_consumption(replay_result, digest) if digest else replay_result

    request = Request(
        f"{gateway_base_url}/internal/v1/attachments/{quote(reference, safe='')}/stream",
        method="GET",
        headers={"Authorization": f"Bearer {gateway_token}", "Accept": "application/octet-stream"},
    )
    try:
        gateway_response = urlopen(request, timeout=60)
    except HTTPError as exc:
        try:
            structured_error = _safe_structured_http_error(exc)
        finally:
            exc.close()
        if structured_error is not None:
            raise structured_error from exc
        raise ToolProxyError("attachment_unavailable", f"Gateway 拒绝附件读取：HTTP {exc.code}") from exc
    except (URLError, TimeoutError, OSError) as exc:
        raise ToolProxyError("gateway_unavailable", "Weixin Gateway 附件接口不可用") from exc
    connection: http.client.HTTPConnection | None = None
    try:
        length_header = gateway_response.headers.get("Content-Length", "")
        try:
            declared_length = int(length_header)
        except ValueError as exc:
            raise ToolProxyError("attachment_invalid", "Gateway 媒体长度无效") from exc
        if declared_length < 1 or declared_length > max_bytes:
            raise ToolProxyError("attachment_too_large", "Gateway 媒体超过 Controller 上限")
        encoded_filename = gateway_response.headers.get("X-Attachment-Filename", "")
        digest_header = gateway_response.headers.get("X-Attachment-Sha256", "")
        mime_type = gateway_response.headers.get_content_type()
        try:
            filename = base64.urlsafe_b64decode(encoded_filename.encode("ascii")).decode("utf-8")
        except (ValueError, UnicodeDecodeError) as exc:
            raise ToolProxyError("attachment_invalid", "Gateway 媒体文件名无效") from exc
        if not filename or len(filename) > 255 or Path(filename).name != filename or mime_type not in MEDIA_MIME_TYPES:
            raise ToolProxyError("attachment_invalid", "Gateway 媒体元数据无效")
        metadata = {
            **arguments,
            "source": "weixin",
            "source_ref_hash": source_ref_hash,
        }
        metadata_header = base64.urlsafe_b64encode(json.dumps(metadata, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")).decode("ascii")
        target = urlsplit(hub_base_url)
        connection = http.client.HTTPConnection(target.hostname, target.port or 80, timeout=120)
        connection.putrequest("POST", "/internal/v1/media/ingest")
        connection.putheader("Authorization", f"Bearer {hub_token}")
        connection.putheader("Content-Type", mime_type)
        connection.putheader("Content-Length", str(declared_length))
        connection.putheader("X-Attachment-Filename", encoded_filename)
        connection.putheader("X-Attachment-Sha256", digest_header)
        connection.putheader("X-Renovation-Metadata", metadata_header)
        connection.endheaders()
        digest = hashlib.sha256()
        sent = 0
        while sent < declared_length:
            chunk = gateway_response.read(min(1024 * 1024, declared_length - sent))
            if not chunk:
                break
            sent += len(chunk)
            digest.update(chunk)
            connection.send(chunk)
        if sent != declared_length:
            raise ToolProxyError("attachment_invalid", "Gateway 媒体正文不完整")
        if digest_header != f"sha256:{digest.hexdigest()}":
            raise ToolProxyError("attachment_invalid", "Gateway 媒体摘要不一致")
        response = connection.getresponse()
        data = response.read(2 * 1024 * 1024 + 1)
        if response.status < 200 or response.status >= 300:
            raise ToolProxyError("upstream_rejected", f"Renovation Hub 拒绝媒体：HTTP {response.status}")
        return confirm_consumption(_decode_json_response(data), digest_header)
    except (TimeoutError, OSError, http.client.HTTPException) as exc:
        raise ToolProxyError("upstream_unavailable", "媒体流式转发失败") from exc
    finally:
        gateway_response.close()
        if connection is not None:
            connection.close()


def _decode_json_response(data: bytes) -> dict[str, Any]:
    if len(data) > 2 * 1024 * 1024:
        raise ToolProxyError("upstream_response_too_large", "内部服务响应过大")
    try:
        result = json.loads(data)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ToolProxyError("upstream_invalid_json", "内部服务响应无效") from exc
    if not isinstance(result, dict):
        raise ToolProxyError("upstream_invalid_json", "内部服务响应不是对象")
    return result


class ToolProxyServer:
    def __init__(self, socket_path: str | Path, router: ToolRouter):
        self.socket_path = Path(socket_path)
        self.router = router
        self.server: socketserver.ThreadingUnixStreamServer | None = None

    def start(self) -> None:
        self.socket_path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
        if self.socket_path.exists() or self.socket_path.is_symlink():
            self.socket_path.unlink()
        router = self.router

        class Handler(socketserver.StreamRequestHandler):
            def handle(self) -> None:
                line = self.rfile.readline(2 * 1024 * 1024 + 1)
                if not line or len(line) > 2 * 1024 * 1024:
                    return
                try:
                    request = json.loads(line)
                    if not isinstance(request, dict):
                        raise ToolProxyError("invalid_request", "请求必须是对象")
                    name = request.get("name")
                    arguments = request.get("arguments", {})
                    if not isinstance(name, str):
                        raise ToolProxyError("invalid_request", "缺少工具名")
                    if name == "__catalog__":
                        result = router.catalog_payload()
                    elif name == "__catalog_revision__":
                        result = router.catalog_revision()
                    elif name == "__catalog_observed__":
                        result = router.observe_catalog(arguments)
                    else:
                        result = router.call(name, arguments)
                    response = {"ok": True, "result": result}
                except ToolProxyError as exc:
                    response = {"ok": False, "error": {"code": exc.code, "message": str(exc)}}
                except (UnicodeDecodeError, json.JSONDecodeError):
                    response = {"ok": False, "error": {"code": "invalid_json", "message": "请求 JSON 无效"}}
                self.wfile.write(json.dumps(response, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8") + b"\n")

        self.server = socketserver.ThreadingUnixStreamServer(str(self.socket_path), Handler)
        self.server.daemon_threads = True
        import threading

        threading.Thread(target=self.server.serve_forever, name="controller-tool-proxy", daemon=True).start()

    def stop(self) -> None:
        if self.server is not None:
            self.server.shutdown()
            self.server.server_close()
        if self.socket_path.exists() or self.socket_path.is_symlink():
            self.socket_path.unlink()
