"""Codex Controller runtime entrypoint."""

from __future__ import annotations

import os
from pathlib import Path

from .api import create_server
from .app_server import AppServerClient
from .service import ControllerService
from .store import ControllerStore
from .tool_proxy import ToolProxyServer, ToolRouter


def write_codex_config(codex_home: Path, socket_path: Path) -> None:
    codex_home.mkdir(parents=True, mode=0o700, exist_ok=True)
    config = codex_home / "config.toml"
    expected = (
        "[mcp_servers.home_assistant_tools]\n"
        'command = "/usr/bin/python3"\n'
        'args = ["-m", "codex_controller.mcp_proxy"]\n'
        f'env = {{ CONTROLLER_MCP_SOCKET = "{socket_path}" }}\n'
    )
    if config.exists() and config.is_symlink():
        raise RuntimeError("CODEX_HOME config.toml 不能是符号链接")
    config.write_text(expected, encoding="utf-8")
    os.chmod(config, 0o600)


def main() -> None:
    data_dir = Path(os.environ.get("CONTROLLER_DATA_DIR", "/data")).resolve()
    codex_home = Path(os.environ.get("CONTROLLER_CODEX_HOME", data_dir / "codex-home")).resolve()
    workspace = Path(os.environ.get("CONTROLLER_WORKSPACE", data_dir / "workspace")).resolve()
    socket_path = Path(os.environ.get("CONTROLLER_MCP_SOCKET", data_dir / "runtime" / "tool-proxy.sock")).resolve()
    if data_dir not in codex_home.parents or data_dir not in workspace.parents or data_dir not in socket_path.parents:
        raise RuntimeError("Controller 私有路径必须位于 /data 边界内")

    router = ToolRouter(
        ledger_base_url=os.environ.get("CONTROLLER_LEDGER_BASE_URL", ""),
        ledger_token=os.environ.get("CONTROLLER_LEDGER_API_TOKEN", ""),
        gateway_base_url=os.environ.get("CONTROLLER_GATEWAY_BASE_URL", ""),
        gateway_token=os.environ.get("CONTROLLER_GATEWAY_ATTACHMENT_TOKEN", ""),
        operations_base_url=os.environ.get("CONTROLLER_OPERATIONS_BASE_URL", ""),
        operations_token=os.environ.get("CONTROLLER_OPERATIONS_API_TOKEN", ""),
    )
    proxy = ToolProxyServer(socket_path, router)
    proxy.start()
    write_codex_config(codex_home, socket_path)

    store = ControllerStore(
        os.environ.get("CONTROLLER_DATABASE_PATH", data_dir / "controller.sqlite3"),
        max_queue=int(os.environ.get("CONTROLLER_MAX_QUEUE", "200")),
        max_result_chars=int(os.environ.get("CONTROLLER_MAX_RESULT_CHARS", "12000")),
    )
    binary = os.environ.get("CODEX_BINARY", "/opt/codex/node_modules/.bin/codex")
    app_server = AppServerClient([binary, "app-server", "--listen", "stdio://"], codex_home=codex_home, workspace=workspace)
    service = ControllerService(
        store,
        app_server,
        intake_enabled=os.environ.get("CONTROLLER_INTAKE_ENABLED", "false").lower() == "true",
    )
    service.start()
    server = create_server(
        "0.0.0.0",
        8102,
        service=service,
        api_token=os.environ["CONTROLLER_API_TOKEN"],
        max_request_bytes=int(os.environ.get("CONTROLLER_MAX_REQUEST_BYTES", "1048576")),
    )
    try:
        server.serve_forever()
    finally:
        service.stop()
        proxy.stop()


if __name__ == "__main__":
    main()
