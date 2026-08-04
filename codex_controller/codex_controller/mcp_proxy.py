"""Minimal stdio MCP server that forwards only fixed tools over a Unix socket."""

from __future__ import annotations

import json
import os
import socket
import sys
from typing import Any


def tool_catalog(enabled_names: list[str] | None = None) -> list[dict[str, Any]]:
    ledger_names = [
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
    ]
    renovation_names = [
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
    ]
    tools: list[dict[str, Any]] = [
        {
            "name": name,
            "description": "调用 Renovation Hub 的 Ledger v1 兼容工具；写操作必须携带稳定 idempotency_key。",
            "inputSchema": {"type": "object", "additionalProperties": True},
        }
        for name in ledger_names
    ]
    tools.extend(
        {
            "name": name,
            "description": "调用 Renovation Hub 的项目、阶段、空间、时间线或统计工具。",
            "inputSchema": {"type": "object", "additionalProperties": True},
        }
        for name in renovation_names
    )
    tools.extend(
        [
            {
                "name": "ha_operations_propose_restart",
                "description": "为一个精确 Add-on slug 创建不可变重启提案；不会执行重启。",
                "inputSchema": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["target"],
                    "properties": {
                        "target": {"type": "string", "pattern": "^[a-z0-9][a-z0-9_]{0,63}$"}
                    },
                },
            },
            {
                "name": "ha_operations_authorization_request",
                "description": "为 Broker 已创建的精确提案生成 Passkey 授权请求；不会执行操作。",
                "inputSchema": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["action_id"],
                    "properties": {"action_id": {"type": "string", "pattern": "^OPS-[0-9]{8}-[A-F0-9]{12}$"}},
                },
            },
            {
                "name": "ha_operations_authorization_status",
                "description": "读取已有授权请求和一次性收据状态。",
                "inputSchema": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["approval_id"],
                    "properties": {"approval_id": {"type": "string", "minLength": 8, "maxLength": 160}},
                },
            },
            {
                "name": "ha_operations_execute_restart",
                "description": "消费已完成 Passkey 授权的一次性收据，并仅执行提案中的精确 Add-on 重启。",
                "inputSchema": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["receipt_id", "action_id", "proposal_hash", "idempotency_key"],
                    "properties": {
                        "receipt_id": {"type": "string", "pattern": "^RCPT-[A-F0-9]{32}$"},
                        "action_id": {"type": "string", "pattern": "^OPS-[0-9]{8}-[A-F0-9]{12}$"},
                        "proposal_hash": {"type": "string", "pattern": "^sha256:[a-f0-9]{64}$"},
                        "idempotency_key": {"type": "string", "pattern": "^sha256:[a-f0-9]{64}$"},
                    },
                },
            },
            {
                "name": "ha_operations_execution_status",
                "description": "读取精确 Add-on 重启执行的状态与脱敏验证结果。",
                "inputSchema": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["action_id"],
                    "properties": {"action_id": {"type": "string", "pattern": "^OPS-[0-9]{8}-[A-F0-9]{12}$"}},
                },
            },
        ]
    )
    if enabled_names is None:
        return tools
    by_name = {tool["name"]: tool for tool in tools}
    return [by_name[name] for name in enabled_names if name in by_name]


def socket_call(socket_path: str, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    payload = json.dumps({"name": name, "arguments": arguments}, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8") + b"\n"
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.settimeout(20)
        client.connect(socket_path)
        client.sendall(payload)
        response = b""
        while not response.endswith(b"\n") and len(response) <= 2 * 1024 * 1024:
            chunk = client.recv(65536)
            if not chunk:
                break
            response += chunk
    if len(response) > 2 * 1024 * 1024:
        raise RuntimeError("tool proxy response too large")
    result = json.loads(response)
    if not isinstance(result, dict):
        raise RuntimeError("tool proxy response invalid")
    return result


def emit(payload: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n")
    sys.stdout.flush()


def main() -> None:
    socket_path = os.environ.get("CONTROLLER_MCP_SOCKET", "")
    if not socket_path:
        raise SystemExit("CONTROLLER_MCP_SOCKET is required")
    for line in sys.stdin:
        message: dict[str, Any] = {}
        try:
            message = json.loads(line)
            if not isinstance(message, dict):
                continue
            request_id = message.get("id")
            method = message.get("method")
            params = message.get("params") if isinstance(message.get("params"), dict) else {}
            if request_id is None:
                continue
            if method == "initialize":
                result = {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "ha-controller-tools", "version": "0.1.6"},
                }
            elif method == "tools/list":
                catalog_response = socket_call(socket_path, "__catalog__", {})
                catalog = catalog_response.get("result") if catalog_response.get("ok") else None
                enabled = catalog.get("tools") if isinstance(catalog, dict) and isinstance(catalog.get("tools"), list) else []
                result = {"tools": tool_catalog([name for name in enabled if isinstance(name, str)])}
            elif method == "tools/call":
                name = params.get("name")
                arguments = params.get("arguments", {})
                if not isinstance(name, str) or not isinstance(arguments, dict):
                    raise ValueError("invalid tool call")
                response = socket_call(socket_path, name, arguments)
                if response.get("ok"):
                    result = {
                        "content": [{"type": "text", "text": json.dumps(response["result"], ensure_ascii=False, sort_keys=True)}],
                        "structuredContent": response["result"],
                        "isError": False,
                    }
                else:
                    error = response.get("error") or {"code": "tool_failed", "message": "工具调用失败"}
                    result = {
                        "content": [{"type": "text", "text": json.dumps({"error": error}, ensure_ascii=False, sort_keys=True)}],
                        "isError": True,
                    }
            else:
                emit({"jsonrpc": "2.0", "id": request_id, "error": {"code": -32601, "message": "Method not found"}})
                continue
            emit({"jsonrpc": "2.0", "id": request_id, "result": result})
        except Exception:
            request_id = message.get("id") if isinstance(message, dict) else None
            if request_id is not None:
                emit({"jsonrpc": "2.0", "id": request_id, "error": {"code": -32603, "message": "Internal tool proxy error"}})


if __name__ == "__main__":
    main()
