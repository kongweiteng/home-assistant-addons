"""Secret-isolating Unix socket router for fixed Ledger and Broker tools."""

from __future__ import annotations

import base64
import hashlib
import json
from pathlib import Path
import re
import socketserver
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlsplit
from urllib.request import Request, urlopen


HOST_RE = re.compile(r"^[a-z0-9](?:[a-z0-9._-]{0,251}[a-z0-9])?$")
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
OPERATIONS_TOOLS = {
    "ha_operations_preflight",
    "ha_operations_authorization_request",
    "ha_operations_authorization_status",
}
ATTACHMENT_REF_RE = re.compile(r"^[A-Za-z0-9_-]{32,128}$")
ATTACHMENT_MIME_TYPES = {"image/jpeg", "image/png", "image/webp", "application/pdf", "text/plain"}
MAX_GATEWAY_ATTACHMENT_BYTES = 20 * 1024 * 1024


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
    ):
        self.ledger_base_url = validate_base_url(ledger_base_url)
        self.ledger_token = ledger_token
        self.gateway_base_url = validate_base_url(gateway_base_url)
        self.gateway_token = gateway_token
        self.operations_base_url = validate_base_url(operations_base_url)
        self.operations_token = operations_token
        self.request_json = request_json or _request_json
        self.request_bytes = request_bytes or _request_bytes

    def available_tools(self) -> list[str]:
        tools: list[str] = []
        if self.ledger_base_url and len(self.ledger_token) >= 32:
            ledger_tools = set(LEDGER_TOOLS)
            if not self.gateway_base_url or len(self.gateway_token) < 32:
                ledger_tools.discard("ledger_attach")
            tools.extend(sorted(ledger_tools))
        if self.operations_base_url and len(self.operations_token) >= 32:
            tools.extend(sorted(OPERATIONS_TOOLS))
        return tools

    def call(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(arguments, dict):
            raise ToolProxyError("invalid_arguments", "工具参数必须是对象")
        if name in LEDGER_TOOLS:
            if not self.ledger_base_url or len(self.ledger_token) < 32:
                raise ToolProxyError("ledger_unavailable", "Renovation Ledger 未配置")
            ledger_arguments = dict(arguments)
            if name == "ledger_attach":
                ledger_arguments = self._resolve_gateway_attachment(arguments)
            return self.request_json(
                "POST",
                f"{self.ledger_base_url}/internal/v1/tools/call",
                self.ledger_token,
                {"name": name, "arguments": ledger_arguments, "actor_hash": "sha256:codex-controller"},
            )
        if name == "ha_operations_preflight":
            return self._operations("POST", "/v1/preflight", arguments)
        if name == "ha_operations_authorization_request":
            return self._operations("POST", "/v1/authorization/requests", arguments)
        if name == "ha_operations_authorization_status":
            approval_id = arguments.get("approval_id")
            if not isinstance(approval_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{8,160}", approval_id):
                raise ToolProxyError("invalid_approval_id", "approval_id 无效")
            return self._operations("GET", f"/v1/authorization/requests/{quote(approval_id, safe='')}", None)
        raise ToolProxyError("unknown_tool", "工具不在允许清单")

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
