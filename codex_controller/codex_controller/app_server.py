"""Strict stdio JSONL client for the official Codex app-server."""

from __future__ import annotations

import json
import os
from pathlib import Path
import queue
import subprocess
import threading
from typing import Any, Callable


class AppServerError(RuntimeError):
    """A fail-closed app-server request or protocol error."""

    def __init__(self, code: str, message: str, *, rpc_code: int | None = None, definitive: bool = False):
        super().__init__(message)
        self.code = code
        self.rpc_code = rpc_code
        self.definitive = definitive


class AppServerClient:
    """Owns one app-server process and its request/notification correlation."""

    BASE_DEVELOPER_INSTRUCTIONS = (
        "你通过微信作为所有者的通用 Codex 助手处理任务和讨论。普通问答、分析、写作、规划或其他不需要外部执行的请求应直接回答，"
        "不得把所有消息默认解释为装修事项。只有用户意图确实需要装修账本或 Home Assistant 操作时，才使用已配置的结构化 MCP 工具；"
        "当前运行能力必须以本轮 MCP 工具目录和实际只读调用结果为准，不得沿用历史对话中的旧 Mac 代理、Hermes 或未接入判断。"
        "不得使用 Shell、任意文件路径或自然语言绕过审批。不要输出内部推理、Token、路径或工具秘密。"
    )

    SAFE_ENV_KEYS = {
        "PATH",
        "LANG",
        "LC_ALL",
        "TZ",
        "SSL_CERT_FILE",
        "SSL_CERT_DIR",
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "NO_PROXY",
        "http_proxy",
        "https_proxy",
        "no_proxy",
    }
    SUPPORTED_ACCOUNT_TYPES = {"chatgpt", "apiKey"}

    def __init__(
        self,
        command: list[str],
        *,
        codex_home: str | Path,
        workspace: str | Path,
        available_tools: list[str] | None = None,
        notification_handler: Callable[[dict[str, Any]], None] | None = None,
        request_timeout: float = 30.0,
    ):
        self.command = command
        self.codex_home = Path(codex_home)
        self.workspace = Path(workspace)
        self.developer_instructions = self.build_developer_instructions(available_tools or [])
        self.notification_handler = notification_handler or (lambda _message: None)
        self.request_timeout = request_timeout
        self.process: subprocess.Popen[str] | None = None
        self._next_id = 1
        self._pending: dict[int, queue.Queue[dict[str, Any]]] = {}
        self._pending_lock = threading.Lock()
        self._write_lock = threading.Lock()
        self._stopped = threading.Event()
        self._threads: list[threading.Thread] = []
        self._protocol_error: AppServerError | None = None
        self.auth_mode: str | None = None
        self.plan_type: str | None = None
        self.account_ready = False
        self.initialized = False

    @classmethod
    def build_developer_instructions(cls, available_tools: list[str]) -> str:
        enabled = set(available_tools)
        instructions = cls.BASE_DEVELOPER_INSTRUCTIONS
        if {"ledger_summary", "ledger_query", "renovation_dashboard"}.issubset(enabled):
            instructions += (
                " 当前会话已配置 Renovation Hub 装修账本和装修档案工具。用户询问账本是否连接、是否可用、当前支出、汇总或明细时，"
                "必须先调用 renovation_dashboard、ledger_summary、ledger_query 或其他合适的只读工具核验；"
                "只要工具调用可用，就不得回复‘未连接账本’，也不得要求用户重新发送现有账目。写账、退款、修改和撤销必须调用对应结构化工具。"
            )
        else:
            instructions += " 当前会话未配置 Renovation Hub 工具时，才可以明确说明装修账本工具目录不可用。"
        if "ha_operations_propose_restart" in enabled:
            instructions += (
                " 当前会话已配置受控 Home Assistant Operations 工具，但每次写操作仍必须遵守不可变提案、Passkey、精确白名单和执行门禁。"
            )
        return instructions

    def build_child_env(self, source: dict[str, str] | None = None) -> dict[str, str]:
        original = os.environ if source is None else source
        environment = {key: value for key, value in original.items() if key in self.SAFE_ENV_KEYS}
        environment.update(
            {
                "CODEX_HOME": str(self.codex_home),
                "HOME": str(self.codex_home),
                "RUST_LOG": original.get("RUST_LOG", "warn"),
            }
        )
        return environment

    def start(self) -> None:
        if self.process is not None and self.process.poll() is None:
            return
        self.codex_home.mkdir(parents=True, mode=0o700, exist_ok=True)
        self.workspace.mkdir(parents=True, mode=0o700, exist_ok=True)
        self._stopped.clear()
        try:
            self.process = subprocess.Popen(
                self.command,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                bufsize=1,
                env=self.build_child_env(),
            )
        except OSError as exc:
            raise AppServerError("app_server_start_failed", "无法启动官方 Codex app-server") from exc
        self._threads = [
            threading.Thread(target=self._reader_loop, name="codex-app-server-reader", daemon=True),
            threading.Thread(target=self._stderr_loop, name="codex-app-server-stderr", daemon=True),
        ]
        for thread in self._threads:
            thread.start()
        initialize = self.request(
            "initialize",
            {"clientInfo": {"name": "ha_codex_controller", "title": "Home Assistant Codex Controller", "version": "0.1.7"}},
        )
        if not isinstance(initialize, dict):
            raise AppServerError("app_server_protocol_error", "initialize 响应无效")
        self.notify("initialized", {})
        self.initialized = True
        self.refresh_account()

    def stop(self) -> None:
        self._stopped.set()
        process = self.process
        if process is not None and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
        if process is not None:
            for stream in (process.stdin, process.stdout, process.stderr):
                if stream is not None and not stream.closed:
                    stream.close()
        for thread in self._threads:
            thread.join(timeout=1)
        self._threads = []

    def request(self, method: str, params: dict[str, Any] | None = None) -> Any:
        if self.process is None or self.process.poll() is not None:
            raise AppServerError("app_server_start_failed", "app-server 未运行")
        with self._pending_lock:
            request_id = self._next_id
            self._next_id += 1
            response_queue: queue.Queue[dict[str, Any]] = queue.Queue(maxsize=1)
            self._pending[request_id] = response_queue
        self._send({"method": method, "id": request_id, "params": params or {}})
        try:
            response = response_queue.get(timeout=self.request_timeout)
        except queue.Empty as exc:
            with self._pending_lock:
                self._pending.pop(request_id, None)
            raise AppServerError("app_server_protocol_error", f"app-server 请求超时：{method}") from exc
        if "error" in response:
            error = response.get("error") or {}
            rpc_code = error.get("code") if isinstance(error.get("code"), int) else None
            if rpc_code == -32099:
                raise AppServerError("app_server_protocol_error", "app-server 协议通道已关闭")
            code = "app_server_overloaded" if rpc_code == -32001 else "app_server_request_failed"
            raise AppServerError(code, "app-server 明确拒绝请求", rpc_code=rpc_code, definitive=True)
        if "result" not in response:
            raise AppServerError("app_server_protocol_error", "app-server 响应缺少 result")
        return response["result"]

    def notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        self._send({"method": method, "params": params or {}})

    def refresh_account(self) -> dict[str, Any]:
        result = self.request("account/read", {})
        if not isinstance(result, dict):
            raise AppServerError("app_server_protocol_error", "账户响应无效")
        account = result.get("account")
        mode = account.get("type") if isinstance(account, dict) else None
        self.auth_mode = self._normalize_auth_mode(mode)
        self.plan_type = (
            account.get("planType")
            if isinstance(account, dict) and self.auth_mode == "chatgpt"
            else None
        )
        self.account_ready = self.auth_mode in self.SUPPORTED_ACCOUNT_TYPES
        return self.account_status()

    def start_device_login(self) -> dict[str, Any]:
        result = self.request("account/login/start", {"type": "chatgptDeviceCode"})
        if not isinstance(result, dict) or result.get("type") != "chatgptDeviceCode":
            raise AppServerError("auth_mode_rejected", "app-server 未返回 ChatGPT Device Code 登录")
        required = ("loginId", "verificationUrl", "userCode")
        if any(not isinstance(result.get(field), str) or not result[field] for field in required):
            raise AppServerError("app_server_protocol_error", "设备码登录响应字段不完整")
        return {field: result[field] for field in ("type", *required)}

    def start_api_key_login(self, api_key: str) -> dict[str, Any]:
        if (
            not isinstance(api_key, str)
            or not api_key
            or len(api_key) > 4096
            or api_key.strip() != api_key
            or "\n" in api_key
            or "\r" in api_key
        ):
            raise AppServerError("api_key_missing", "API Key 未配置或格式无效", definitive=True)
        result = self.request("account/login/start", {"type": "apiKey", "apiKey": api_key})
        if not isinstance(result, dict) or result.get("type") != "apiKey":
            raise AppServerError("auth_mode_rejected", "app-server 未接受 API Key 登录")
        account = self.refresh_account()
        if account["auth_mode"] != "apiKey":
            raise AppServerError("auth_mode_rejected", "app-server 账户类型与 API Key 模式不匹配")
        return {"type": "apiKey", "ready": True}

    def cancel_login(self, login_id: str) -> Any:
        if not login_id or len(login_id) > 256:
            raise AppServerError("invalid_login_id", "loginId 无效", definitive=True)
        return self.request("account/login/cancel", {"loginId": login_id})

    def logout(self) -> Any:
        result = self.request("account/logout", {})
        self.auth_mode = None
        self.plan_type = None
        self.account_ready = False
        return result

    def start_thread(self) -> str:
        result = self.request(
            "thread/start",
            {
                "cwd": str(self.workspace),
                "sandbox": "read-only",
                "approvalPolicy": "never",
                "developerInstructions": self.developer_instructions,
            },
        )
        thread = result.get("thread") if isinstance(result, dict) else None
        thread_id = thread.get("id") if isinstance(thread, dict) else None
        if not isinstance(thread_id, str) or not thread_id:
            raise AppServerError("thread_unavailable", "thread/start 未返回 Thread ID")
        return thread_id

    def resume_thread(self, thread_id: str) -> None:
        result = self.request(
            "thread/resume",
            {
                "threadId": thread_id,
                "cwd": str(self.workspace),
                "sandbox": "read-only",
                "approvalPolicy": "never",
                "developerInstructions": self.developer_instructions,
            },
        )
        thread = result.get("thread") if isinstance(result, dict) else None
        if not isinstance(thread, dict) or thread.get("id") != thread_id:
            raise AppServerError("thread_unavailable", "thread/resume 返回不匹配")

    def start_turn(
        self,
        thread_id: str,
        text: str,
        message_id: str,
        *,
        input_items: list[dict[str, Any]] | None = None,
    ) -> str:
        result = self.request(
            "turn/start",
            {
                "threadId": thread_id,
                "clientUserMessageId": message_id,
                "input": input_items if input_items is not None else [{"type": "text", "text": text}],
                "approvalPolicy": "never",
            },
        )
        turn = result.get("turn") if isinstance(result, dict) else None
        turn_id = turn.get("id") if isinstance(turn, dict) else None
        if not isinstance(turn_id, str) or not turn_id:
            raise AppServerError("turn_state_unknown", "turn/start 未返回 Turn ID")
        return turn_id

    def interrupt_turn(self, thread_id: str, turn_id: str) -> Any:
        return self.request("turn/interrupt", {"threadId": thread_id, "turnId": turn_id})

    def account_status(self) -> dict[str, Any]:
        return {"auth_mode": self.auth_mode, "plan_type": self.plan_type, "ready": self.account_ready}

    def status(self) -> dict[str, Any]:
        running = self.process is not None and self.process.poll() is None
        return {
            "running": running,
            "initialized": self.initialized,
            "protocol_error": None if self._protocol_error is None else self._protocol_error.code,
            "account": self.account_status(),
        }

    def _send(self, message: dict[str, Any]) -> None:
        process = self.process
        if process is None or process.stdin is None or process.poll() is not None:
            raise AppServerError("app_server_protocol_error", "app-server stdin 不可用")
        serialized = json.dumps(message, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        with self._write_lock:
            try:
                process.stdin.write(serialized + "\n")
                process.stdin.flush()
            except (BrokenPipeError, OSError) as exc:
                raise AppServerError("app_server_protocol_error", "app-server stdin 已关闭") from exc

    def _reader_loop(self) -> None:
        process = self.process
        if process is None or process.stdout is None:
            return
        try:
            for line in process.stdout:
                if self._stopped.is_set():
                    break
                try:
                    message = json.loads(line)
                except json.JSONDecodeError:
                    self._set_protocol_error("stdout 出现非 JSON 行")
                    break
                if not isinstance(message, dict):
                    self._set_protocol_error("stdout JSON 不是对象")
                    break
                self._handle_message(message)
        finally:
            if not self._stopped.is_set():
                self._set_protocol_error("app-server stdout 已关闭")

    def _stderr_loop(self) -> None:
        process = self.process
        if process is None or process.stderr is None:
            return
        for _line in process.stderr:
            if self._stopped.is_set():
                break
            # stderr may contain private task text; intentionally drain without echoing.

    def _handle_message(self, message: dict[str, Any]) -> None:
        if "id" in message and "method" not in message:
            request_id = message.get("id")
            if not isinstance(request_id, int):
                self._set_protocol_error("响应 ID 类型无效")
                return
            with self._pending_lock:
                response_queue = self._pending.pop(request_id, None)
            if response_queue is None:
                self._set_protocol_error("收到未知响应 ID")
                return
            response_queue.put(message)
            return
        method = message.get("method")
        if not isinstance(method, str):
            self._set_protocol_error("消息缺少 method")
            return
        if "id" in message:
            request_id = message.get("id")
            self._send(
                {
                    "id": request_id,
                    "error": {"code": -32601, "message": "Controller 不允许 app-server 主动请求未批准能力"},
                }
            )
            return
        params = message.get("params") if isinstance(message.get("params"), dict) else {}
        if method == "account/updated":
            mode = params.get("authMode")
            self.auth_mode = self._normalize_auth_mode(mode)
            self.plan_type = params.get("planType") if self.auth_mode == "chatgpt" else None
            self.account_ready = self.auth_mode in self.SUPPORTED_ACCOUNT_TYPES
        elif method == "account/login/completed":
            threading.Thread(target=self._safe_refresh_account, daemon=True).start()
        self.notification_handler(message)

    @staticmethod
    def _normalize_auth_mode(value: Any) -> str | None:
        if value == "apikey":
            return "apiKey"
        if value in {"apiKey", "chatgpt"}:
            return str(value)
        return None

    def _safe_refresh_account(self) -> None:
        try:
            self.refresh_account()
        except AppServerError:
            self.account_ready = False

    def _set_protocol_error(self, message: str) -> None:
        error = AppServerError("app_server_protocol_error", message)
        self._protocol_error = error
        with self._pending_lock:
            pending = list(self._pending.values())
            self._pending.clear()
        for response_queue in pending:
            response_queue.put({"error": {"code": -32099, "message": "protocol closed"}})
        process = self.process
        if process is not None and process.poll() is None:
            process.terminate()
