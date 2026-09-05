"""Minimal stdio MCP server that forwards the Controller's current tool catalog."""

from __future__ import annotations

import json
import os
import socket
import sys
import threading
from typing import Any

from .tool_catalog import mcp_tool_catalog


tool_catalog = mcp_tool_catalog


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


_EMIT_LOCK = threading.Lock()


def emit(payload: dict[str, Any]) -> None:
    with _EMIT_LOCK:
        sys.stdout.write(json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n")
        sys.stdout.flush()


def watch_catalog(socket_path: str, initial_revision: int | None, stop: threading.Event) -> None:
    last_revision = initial_revision
    while not stop.wait(0.5):
        try:
            response = socket_call(socket_path, "__catalog_revision__", {})
            result = response.get("result") if response.get("ok") else None
            revision = result.get("revision") if isinstance(result, dict) else None
            if not isinstance(revision, int):
                continue
            if last_revision is None:
                last_revision = revision
            elif revision != last_revision:
                last_revision = revision
                emit({"jsonrpc": "2.0", "method": "notifications/tools/list_changed"})
        except (OSError, RuntimeError, ValueError, json.JSONDecodeError):
            continue


def main() -> None:
    socket_path = os.environ.get("CONTROLLER_MCP_SOCKET", "")
    if not socket_path:
        raise SystemExit("CONTROLLER_MCP_SOCKET is required")
    stop = threading.Event()
    watcher: threading.Thread | None = None
    try:
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
                    revision_response = socket_call(socket_path, "__catalog_revision__", {})
                    revision_result = revision_response.get("result") if revision_response.get("ok") else None
                    revision = revision_result.get("revision") if isinstance(revision_result, dict) else None
                    if watcher is None:
                        watcher = threading.Thread(
                            target=watch_catalog,
                            args=(socket_path, revision if isinstance(revision, int) else None, stop),
                            name="controller-mcp-catalog-watch",
                            daemon=True,
                        )
                        watcher.start()
                    result = {
                        "protocolVersion": "2024-11-05",
                        "capabilities": {"tools": {"listChanged": True}},
                        "serverInfo": {"name": "ha-controller-tools", "version": "0.5.36"},
                    }
                elif method == "tools/list":
                    catalog_response = socket_call(socket_path, "__catalog__", {})
                    catalog = catalog_response.get("result") if catalog_response.get("ok") else None
                    documents = catalog.get("tools") if isinstance(catalog, dict) and isinstance(catalog.get("tools"), list) else []
                    revision = catalog.get("revision") if isinstance(catalog, dict) else None
                    published_documents = [
                        document
                        for document in documents
                        if isinstance(document, dict) and isinstance(document.get("name"), str)
                    ]
                    published = [document["name"] for document in published_documents]
                    result = {"tools": published_documents}
                    socket_call(
                        socket_path,
                        "__catalog_observed__",
                        {"revision": revision, "tools": published},
                    )
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
    finally:
        stop.set()
        if watcher is not None:
            watcher.join(timeout=1)


if __name__ == "__main__":
    main()
