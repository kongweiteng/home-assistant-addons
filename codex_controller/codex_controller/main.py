"""Codex Controller runtime entrypoint."""

from __future__ import annotations

import ipaddress
import json
import os
from pathlib import Path
import re
import socket
import tempfile
from typing import Callable
from urllib.parse import unquote_to_bytes, urlsplit, urlunsplit

from .api import create_server
from .app_server import AppServerClient
from .media_input import TurnMediaManager
from .runner_relay import RelayPublisher, RunnerInstallerCatalog, validate_relay_auth_config
from .runner_service import RunnerManagerService
from .runner_store import RunnerStore
from .service import ControllerService
from .store import ControllerStore
from .tool_catalog import ALL_TOOL_NAMES
from .tool_proxy import ToolProxyServer, ToolRouter


def read_api_key_from_fd() -> str:
    value = os.environ.get("CONTROLLER_OPENAI_API_KEY_FD", "")
    if not value:
        return ""
    try:
        descriptor = int(value)
    except ValueError as exc:
        raise RuntimeError("Controller API Key 文件描述符无效") from exc
    try:
        with os.fdopen(descriptor, "r", encoding="utf-8", closefd=True) as stream:
            return stream.read(4097)
    except OSError as exc:
        raise RuntimeError("Controller 无法读取 API Key") from exc


Resolver = Callable[..., list[tuple]]

PRIVATE_API_HOSTS = {
    "localhost",
    "supervisor",
    "homeassistant",
    "hassio",
    "codex-controller",
    "renovation-hub",
    "weixin-gateway",
    "ha-operations-broker",
}
PRIVATE_API_SUFFIXES = (".localhost", ".local", ".internal", ".home.arpa")
CODEX_MODEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$")


def normalize_codex_model(value: str, *, auth_mode: str) -> str:
    """Validate an optional API-key model override without exposing provider configuration."""
    if not isinstance(value, str):
        raise ValueError("codex_model 类型无效")
    if value == "":
        return ""
    if auth_mode != "api_key":
        raise ValueError("codex_model 只允许用于 API Key 模式")
    if not CODEX_MODEL_RE.fullmatch(value):
        raise ValueError("codex_model 格式无效")
    return value


def normalize_openai_base_url(
    value: str,
    *,
    auth_mode: str,
    resolver: Resolver = socket.getaddrinfo,
) -> str:
    """Return a canonical public HTTPS Responses API base URL or fail closed."""
    if not isinstance(value, str):
        raise ValueError("openai_base_url 类型无效")
    if value == "":
        return ""
    if auth_mode != "api_key":
        raise ValueError("openai_base_url 只允许用于 API Key 模式")
    if value.strip() != value or any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise ValueError("openai_base_url 包含空白或控制字符")
    if "\\" in value:
        raise ValueError("openai_base_url 包含反斜杠")
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError as exc:
        raise ValueError("openai_base_url 结构无效") from exc
    if parsed.scheme != "https":
        raise ValueError("openai_base_url 只允许 HTTPS")
    if not parsed.hostname or parsed.username is not None or parsed.password is not None:
        raise ValueError("openai_base_url 主机或用户信息无效")
    if parsed.query or parsed.fragment:
        raise ValueError("openai_base_url 不允许 query 或 fragment")

    decoded_path = unquote_to_bytes(parsed.path)
    if b"\\" in decoded_path or any(byte < 32 or byte == 127 for byte in decoded_path):
        raise ValueError("openai_base_url 路径无效")

    hostname = parsed.hostname.rstrip(".").lower()
    if not hostname:
        raise ValueError("openai_base_url 主机为空")
    if hostname in PRIVATE_API_HOSTS or hostname.endswith(PRIVATE_API_SUFFIXES):
        raise ValueError("openai_base_url 指向内部主机")
    try:
        address = ipaddress.ip_address(hostname)
    except ValueError:
        try:
            ascii_hostname = hostname.encode("idna").decode("ascii")
        except UnicodeError as exc:
            raise ValueError("openai_base_url 主机名无效") from exc
        try:
            records = resolver(ascii_hostname, port or 443, type=socket.SOCK_STREAM)
        except OSError as exc:
            raise ValueError("openai_base_url DNS 解析失败") from exc
        resolved_addresses: set[ipaddress.IPv4Address | ipaddress.IPv6Address] = set()
        for record in records:
            try:
                resolved_addresses.add(ipaddress.ip_address(record[4][0]))
            except (IndexError, ValueError, TypeError) as exc:
                raise ValueError("openai_base_url DNS 响应无效") from exc
        if not resolved_addresses:
            raise ValueError("openai_base_url DNS 没有地址")
        if any(not resolved.is_global for resolved in resolved_addresses):
            raise ValueError("openai_base_url 解析到非公网地址")
        normalized_hostname = ascii_hostname
    else:
        if not address.is_global:
            raise ValueError("openai_base_url IP 不是公网地址")
        normalized_hostname = address.compressed

    host_for_url = f"[{normalized_hostname}]" if ":" in normalized_hostname else normalized_hostname
    netloc = host_for_url if port is None else f"{host_for_url}:{port}"
    path = parsed.path.rstrip("/")
    return urlunsplit(("https", netloc, path, "", ""))


def write_codex_config(
    codex_home: Path,
    socket_path: Path,
    *,
    openai_base_url: str = "",
    codex_model: str = "",
    mcp_python: str = "/usr/bin/python3",
    mcp_pythonpath: str = "/opt/codex-controller",
) -> None:
    if not Path(mcp_python).is_absolute() or not Path(mcp_pythonpath).is_absolute():
        raise ValueError("MCP Python 路径必须是绝对路径")
    codex_home.mkdir(parents=True, mode=0o700, exist_ok=True)
    config = codex_home / "config.toml"
    runtime_config = ""
    if codex_model:
        runtime_config += f"model = {json.dumps(codex_model, ensure_ascii=False)}\n"
    if openai_base_url:
        runtime_config += f"openai_base_url = {json.dumps(openai_base_url, ensure_ascii=False)}\n"
    if runtime_config:
        runtime_config += "\n"
    expected = runtime_config + (
        "[mcp_servers.home_assistant_tools]\n"
        f"command = {json.dumps(mcp_python, ensure_ascii=False)}\n"
        'args = ["-m", "codex_controller.mcp_proxy"]\n'
        'default_tools_approval_mode = "approve"\n'
        "env = { "
        f"CONTROLLER_MCP_SOCKET = {json.dumps(str(socket_path), ensure_ascii=False)}, "
        f"PYTHONPATH = {json.dumps(mcp_pythonpath, ensure_ascii=False)}"
        " }\n\n"
        + "\n".join(
            f"[mcp_servers.home_assistant_tools.tools.{name}]\napproval_mode = \"approve\"\n"
            for name in sorted(ALL_TOOL_NAMES)
        )
    )
    if config.exists() and config.is_symlink():
        raise RuntimeError("CODEX_HOME config.toml 不能是符号链接")
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=codex_home,
            prefix=".config.toml.",
            delete=False,
        ) as stream:
            temporary_path = Path(stream.name)
            os.chmod(stream.fileno(), 0o600)
            stream.write(expected)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_path, config)
        os.chmod(config, 0o600)
    finally:
        if temporary_path is not None and temporary_path.exists():
            temporary_path.unlink()


def codex_app_server_command(binary: str) -> list[str]:
    return [binary, "app-server", "--disable", "goals", "--listen", "stdio://"]


def main() -> None:
    data_dir = Path(os.environ.get("CONTROLLER_DATA_DIR", "/data")).resolve()
    codex_home = Path(os.environ.get("CONTROLLER_CODEX_HOME", data_dir / "codex-home")).resolve()
    workspace = Path(os.environ.get("CONTROLLER_WORKSPACE", data_dir / "workspace")).resolve()
    socket_path = Path(os.environ.get("CONTROLLER_MCP_SOCKET", data_dir / "runtime" / "tool-proxy.sock")).resolve()
    if data_dir not in codex_home.parents or data_dir not in workspace.parents or data_dir not in socket_path.parents:
        raise RuntimeError("Controller 私有路径必须位于 /data 边界内")

    auth_mode = os.environ.get("CONTROLLER_AUTH_MODE", "chatgpt_device_code")
    openai_base_url = normalize_openai_base_url(
        os.environ.get("CONTROLLER_OPENAI_BASE_URL", ""),
        auth_mode=auth_mode,
    )
    codex_model = normalize_codex_model(
        os.environ.get("CONTROLLER_CODEX_MODEL", ""),
        auth_mode=auth_mode,
    )
    write_codex_config(
        codex_home,
        socket_path,
        openai_base_url=openai_base_url,
        codex_model=codex_model,
    )

    database_path = os.environ.get("CONTROLLER_DATABASE_PATH", data_dir / "controller.sqlite3")
    store = ControllerStore(
        database_path,
        max_queue=int(os.environ.get("CONTROLLER_MAX_QUEUE", "200")),
        max_result_chars=int(os.environ.get("CONTROLLER_MAX_RESULT_CHARS", "12000")),
    )
    runner_store = RunnerStore(
        database_path,
        online_after_seconds=int(os.environ.get("CONTROLLER_RUNNER_ONLINE_SECONDS", "30")),
        offline_after_seconds=int(os.environ.get("CONTROLLER_RUNNER_OFFLINE_SECONDS", "90")),
        lease_ttl_seconds=int(os.environ.get("CONTROLLER_RUNNER_LEASE_TTL_SECONDS", "60")),
        task_ttl_seconds=int(os.environ.get("CONTROLLER_RUNNER_TASK_TTL_SECONDS", "1800")),
    )
    relay_base_url = os.environ.get("CONTROLLER_RUNNER_RELAY_BASE_URL", "")
    relay_api_token = os.environ.get("CONTROLLER_RUNNER_RELAY_API_TOKEN", "")
    relay_controller_api_token = os.environ.get(
        "CONTROLLER_RUNNER_RELAY_CONTROLLER_API_TOKEN", ""
    )
    try:
        relay_base_url = validate_relay_auth_config(
            relay_base_url,
            relay_api_token,
            relay_controller_api_token,
        )
    except ValueError as exc:
        raise RuntimeError(str(exc)) from exc
    publisher = (
        RelayPublisher(
            relay_base_url,
            relay_api_token,
            timeout_seconds=int(os.environ.get("CONTROLLER_RUNNER_RELAY_TIMEOUT_SECONDS", "10")),
        )
        if relay_base_url
        else None
    )
    installer_manifest_url = os.environ.get("CONTROLLER_RUNNER_INSTALLER_MANIFEST_URL", "")
    installer_manifest_sha256 = os.environ.get("CONTROLLER_RUNNER_INSTALLER_MANIFEST_SHA256", "")
    relay_public_url = os.environ.get("CONTROLLER_RUNNER_RELAY_PUBLIC_URL", "")
    installer_values = (installer_manifest_url, installer_manifest_sha256, relay_public_url)
    if any(installer_values) and not all(installer_values):
        raise RuntimeError("Runner installer manifest URL、SHA-256 与公开 WSS URL 必须同时配置")
    installer = (
        RunnerInstallerCatalog(
            installer_manifest_url,
            installer_manifest_sha256,
            relay_public_url,
            timeout_seconds=int(os.environ.get("CONTROLLER_RUNNER_RELAY_TIMEOUT_SECONDS", "10")),
            pinned_manifest_body=(Path(__file__).with_name("runner_manifest_v035.json").read_bytes()),
        )
        if installer_manifest_url
        else None
    )
    runner_manager = RunnerManagerService(
        runner_store,
        enabled=os.environ.get("CONTROLLER_RUNNER_CENTER_V2_ENABLED", "true").lower() == "true",
        publisher=publisher,
        installer=installer,
    )
    router = ToolRouter(
        ledger_base_url=os.environ.get("CONTROLLER_LEDGER_BASE_URL", ""),
        ledger_token=os.environ.get("CONTROLLER_LEDGER_API_TOKEN", ""),
        gateway_base_url=os.environ.get("CONTROLLER_GATEWAY_BASE_URL", ""),
        gateway_token=os.environ.get("CONTROLLER_GATEWAY_ATTACHMENT_TOKEN", ""),
        operations_base_url=os.environ.get("CONTROLLER_OPERATIONS_BASE_URL", ""),
        operations_token=os.environ.get("CONTROLLER_OPERATIONS_API_TOKEN", ""),
        memo_base_url=os.environ.get("CONTROLLER_MEMO_BASE_URL", ""),
        memo_http_username=os.environ.get("CONTROLLER_MEMO_HTTP_USERNAME", ""),
        memo_http_password=os.environ.get("CONTROLLER_MEMO_HTTP_PASSWORD", ""),
        memo_api_token=os.environ.get("CONTROLLER_MEMO_API_TOKEN", ""),
        max_media_bytes=int(os.environ.get("CONTROLLER_MAX_MEDIA_BYTES", str(1024 * 1024 * 1024))),
        store=store,
        manifest_poll_interval=float(os.environ.get("CONTROLLER_HUB_MANIFEST_POLL_SECONDS", "30")),
    )
    proxy = ToolProxyServer(socket_path, router)
    proxy.start()
    router.start_manifest_sync()

    turn_media = TurnMediaManager(data_dir / "turn-media", router.preview_attachment)

    binary = os.environ.get("CODEX_BINARY", "/opt/codex/node_modules/.bin/codex")
    app_server = AppServerClient(
        codex_app_server_command(binary),
        codex_home=codex_home,
        workspace=workspace,
        available_tools=router.available_tools(),
    )
    service = ControllerService(
        store,
        app_server,
        intake_enabled=os.environ.get("CONTROLLER_INTAKE_ENABLED", "false").lower() == "true",
        auth_mode=auth_mode,
        api_key=read_api_key_from_fd(),
        api_base_mode="custom" if openai_base_url else "official",
        codex_model_mode="custom" if codex_model else "default",
        turn_media=turn_media,
        tool_context=router,
        runner_manager=runner_manager,
    )
    service.start()
    server = create_server(
        "0.0.0.0",
        8102,
        service=service,
        api_token=os.environ["CONTROLLER_API_TOKEN"],
        runner_relay_controller_api_token=relay_controller_api_token,
        max_request_bytes=int(os.environ.get("CONTROLLER_MAX_REQUEST_BYTES", "1048576")),
    )
    try:
        server.serve_forever()
    finally:
        service.stop()
        router.stop_manifest_sync()
        proxy.stop()


if __name__ == "__main__":
    main()
