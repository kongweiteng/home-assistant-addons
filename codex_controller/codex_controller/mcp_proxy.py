"""Minimal stdio MCP server that forwards only fixed tools over a Unix socket."""

from __future__ import annotations

import json
import os
import socket
import sys
from typing import Any


def tool_catalog() -> list[dict[str, Any]]:
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
    tools = [
        {
            "name": name,
            "description": "调用独立 Renovation Ledger 的结构化工具；写操作必须携带稳定 idempotency_key。",
            "inputSchema": {"type": "object", "additionalProperties": True},
        }
        for name in ledger_names
    ]
    tools.extend(
        [
            {
                "name": "ha_operations_preflight",
                "description": "对冻结的 Home Assistant 操作提案执行只读预检，不执行变更。",
                "inputSchema": {"type": "object", "additionalProperties": True},
            },
            {
                "name": "ha_operations_authorization_request",
                "description": "创建独立 Passkey 授权请求；不会执行 Home Assistant 操作。",
                "inputSchema": {"type": "object", "additionalProperties": True},
            },
            {
                "name": "ha_operations_authorization_status",
                "description": "读取已有授权请求和收据状态。",
                "inputSchema": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["approval_id"],
                    "properties": {"approval_id": {"type": "string", "minLength": 8, "maxLength": 160}},
                },
            },
        ]
    )
    return tools


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
                    "serverInfo": {"name": "ha-controller-tools", "version": "0.1.0"},
                }
            elif method == "tools/list":
                result = {"tools": tool_catalog()}
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
