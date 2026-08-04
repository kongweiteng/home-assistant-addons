"""Secret-isolating Unix socket router for fixed Ledger and Broker tools."""

from __future__ import annotations

import base64
import hashlib
import http.client
import json
from pathlib import Path
import re
import socketserver
import threading
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode, urlsplit
from urllib.request import Request, urlopen


HOST_RE = re.compile(r"^[a-z0-9](?:[a-z0-9._-]{0,251}[a-z0-9])?$")
ADDON_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9_]{0,63}$")
ACTION_ID_RE = re.compile(r"^OPS-[0-9]{8}-[A-F0-9]{12}$")
RECEIPT_ID_RE = re.compile(r"^RCPT-[A-F0-9]{32}$")
SHA256_ID_RE = re.compile(r"^sha256:[a-f0-9]{64}$")
LEDGER_TOOLS = {
    "ledger_add_payment",
    "ledger_add_refund",
    "ledger_correct_payment",
    "ledger_undo",
    "ledger_attach",
    "ledger_show",
    "ledger_query",
    "ledger_summary",
    "ledger_generate_chart",
    "ledger_export",
    "ledger_verify_export",
    "ledger_import_inspect",
    "ledger_import_shadow",
}
RENOVATION_TOOLS = {
    "renovation_project_create",
    "renovation_project_update",
    "renovation_project_list",
    "renovation_stage_create",
    "renovation_stage_update",
    "renovation_stage_list",
    "renovation_area_create",
    "renovation_area_update",
    "renovation_area_list",
    "renovation_event_create",
    "renovation_event_update",
    "renovation_timeline",
    "renovation_dashboard",
    "renovation_media_ingest",
}
OPERATIONS_TOOLS = {
    "ha_operations_propose_restart",
    "ha_operations_authorization_request",
    "ha_operations_authorization_status",
    "ha_operations_execute_restart",
    "ha_operations_execution_status",
}
LEDGER_WRITE_TOOLS = {
    "ledger_add_payment",
    "ledger_add_refund",
    "ledger_correct_payment",
    "ledger_undo",
    "ledger_attach",
}
RENOVATION_WRITE_TOOLS = {
    "renovation_project_create",
    "renovation_project_update",
    "renovation_stage_create",
    "renovation_stage_update",
    "renovation_area_create",
    "renovation_area_update",
    "renovation_event_create",
    "renovation_event_update",
    "renovation_media_ingest",
}
ATTACHMENT_REF_RE = re.compile(r"^[A-Za-z0-9_-]{32,128}$")
ATTACHMENT_MIME_TYPES = {"image/jpeg", "image/png", "image/webp", "application/pdf", "text/plain"}
MAX_GATEWAY_ATTACHMENT_BYTES = 20 * 1024 * 1024
MEDIA_MIME_TYPES = {"image/jpeg", "image/png", "image/webp", "image/heic", "image/heif", "video/mp4", "video/quicktime", "video/webm"}
DEFAULT_MAX_GATEWAY_MEDIA_BYTES = 1024 * 1024 * 1024


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
        request_json: Callable[..., dict[str, Any]] | None = None,
        request_bytes: Callable[..., tuple[dict[str, Any], bytes]] | None = None,
        stream_media: Callable[..., dict[str, Any]] | None = None,
        max_media_bytes: int = DEFAULT_MAX_GATEWAY_MEDIA_BYTES,
    ):
        self.ledger_base_url = validate_base_url(ledger_base_url)
        self.ledger_token = ledger_token
        self.gateway_base_url = validate_base_url(gateway_base_url)
        self.gateway_token = gateway_token
        self.operations_base_url = validate_base_url(operations_base_url)
        self.operations_token = operations_token
        self.request_json = request_json or _request_json
        self.request_bytes = request_bytes or _request_bytes
        self.stream_media = stream_media or _stream_gateway_to_hub
        self.max_media_bytes = max_media_bytes
        self._context_lock = threading.Lock()
        self._active_context: dict[str, str] | None = None

    def begin_job(self, job_id: str, message_id: str) -> None:
        if not job_id or not message_id:
            raise ToolProxyError("tool_context_invalid", "工具调用上下文无效")
        with self._context_lock:
            if self._active_context is not None:
                raise ToolProxyError("tool_context_busy", "已有活动工具调用上下文")
            self._active_context = {"job_id": job_id, "message_id": message_id, "turn_id": ""}

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

    def available_tools(self) -> list[str]:
        tools: list[str] = []
        if self.ledger_base_url and len(self.ledger_token) >= 32:
            ledger_tools = set(LEDGER_TOOLS)
            if not self.gateway_base_url or len(self.gateway_token) < 32:
                ledger_tools.discard("ledger_attach")
            tools.extend(sorted(ledger_tools))
            tools.extend(sorted(RENOVATION_TOOLS))
        if self.operations_base_url and len(self.operations_token) >= 32:
            tools.extend(sorted(OPERATIONS_TOOLS))
        return tools

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
        if not isinstance(arguments, dict):
            raise ToolProxyError("invalid_arguments", "工具参数必须是对象")
        if name == "renovation_media_ingest":
            if not self.ledger_base_url or len(self.ledger_token) < 32:
                raise ToolProxyError("ledger_unavailable", "Renovation Hub 媒体接口未配置")
            if not self.gateway_base_url or len(self.gateway_token) < 32:
                raise ToolProxyError("gateway_unavailable", "Weixin Gateway 附件接口未配置")
            reference = arguments.get("attachment_ref")
            if not isinstance(reference, str) or not ATTACHMENT_REF_RE.fullmatch(reference):
                raise ToolProxyError("attachment_ref_invalid", "attachment_ref 无效")
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
        if name in LEDGER_TOOLS or name in RENOVATION_TOOLS:
            if not self.ledger_base_url or len(self.ledger_token) < 32:
                raise ToolProxyError("ledger_unavailable", "Renovation Hub 账本接口未配置")
            if name == "ledger_attach" and (not self.gateway_base_url or len(self.gateway_token) < 32):
                raise ToolProxyError("gateway_unavailable", "Weixin Gateway 附件接口未配置")
            if name in LEDGER_WRITE_TOOLS or name in RENOVATION_WRITE_TOOLS:
                arguments = self._with_deterministic_idempotency(name, arguments)
            ledger_arguments = dict(arguments)
            if name == "ledger_attach":
                ledger_arguments = self._resolve_gateway_attachment(arguments)
            return self.request_json(
                "POST",
                f"{self.ledger_base_url}/internal/v1/tools/call",
                self.ledger_token,
                {"name": name, "arguments": ledger_arguments, "actor_hash": "sha256:codex-controller"},
            )
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
        exc.close()
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
        return _decode_json_response(replay_data)

    request = Request(
        f"{gateway_base_url}/internal/v1/attachments/{quote(reference, safe='')}",
        method="GET",
        headers={"Authorization": f"Bearer {gateway_token}", "Accept": "application/octet-stream"},
    )
    try:
        gateway_response = urlopen(request, timeout=60)
    except HTTPError as exc:
        exc.close()
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
        return _decode_json_response(data)
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
                    result = (
                        {"tools": router.available_tools()}
                        if name == "__catalog__"
                        else router.call(name, arguments)
                    )
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
