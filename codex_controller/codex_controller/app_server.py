"""Strict stdio JSONL client for the official Codex app-server."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import queue
import subprocess
import threading
from typing import Any, Callable

from .tool_catalog import TOOL_BY_NAME, ToolDefinition


class AppServerError(RuntimeError):
    """A fail-closed app-server request or protocol error."""

    def __init__(self, code: str, message: str, *, rpc_code: int | None = None, definitive: bool = False):
        super().__init__(message)
        self.code = code
        self.rpc_code = rpc_code
        self.definitive = definitive


class AppServerClient:
    """Owns one app-server process and its request/notification correlation."""

    supports_dynamic_tool_definitions = True

    BASE_DEVELOPER_INSTRUCTIONS = (
        "你通过微信作为通用 Codex 助手处理任务和讨论。普通问答、分析、写作、规划或其他不需要外部执行的请求应直接回答，"
        "不得把所有消息默认解释为装修事项。只有用户意图确实需要装修账本或 Home Assistant 操作时，或确实需要家庭备忘录时，才使用已配置的结构化 MCP 工具；"
        "当前运行能力必须以本轮 MCP 工具目录和实际只读调用结果为准，不得沿用历史对话中的旧 Mac 代理、Hermes 或未接入判断。"
        "不得使用 Shell、任意文件路径或自然语言绕过 Controller、Renovation Hub 或 Operations Broker 的服务端门禁。"
        "收到图片、视频或文件时默认只用于本轮识别和回答，不得因为存在附件就自动归档；只有用户明确要求将其归档到装修/施工/工地档案时，才允许调用媒体归档工具。"
        "不得创建 Codex Goal，也不得承诺后台持续监控或稍后主动跟进；"
        "这里仅禁止当前 Codex Turn/Goal 自身在后台等待，不限制调用已经配置、具有独立生命周期和通知通道的独立自动化服务。"
        "不要输出内部推理、Token、路径或工具秘密。"
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
        self.developer_instructions = self.build_developer_instructions(available_tools or [], "owner_legacy")
        self._instruction_lock = threading.Lock()
        self.notification_handler = notification_handler or (lambda _message: None)
        self.request_timeout = request_timeout
        self.process: subprocess.Popen[str] | None = None
        self._next_id = 1
        self._pending: dict[int, queue.Queue[dict[str, Any]]] = {}
        self._pending_lock = threading.Lock()
        self._write_lock = threading.Lock()
        self._thread_load_lock = threading.Lock()
        self._loaded_thread_contexts: dict[str, str] = {}
        self._forkable_threads: set[str] = set()
        self._stopped = threading.Event()
        self._threads: list[threading.Thread] = []
        self._protocol_error: AppServerError | None = None
        self.auth_mode: str | None = None
        self.plan_type: str | None = None
        self.account_ready = False
        self.initialized = False

    @classmethod
    def build_developer_instructions(
        cls,
        available_tools: list[str],
        capability_profile: str = "owner_legacy",
        tool_definitions: dict[str, ToolDefinition] | None = None,
    ) -> str:
        enabled = set(available_tools)
        definitions = TOOL_BY_NAME if tool_definitions is None else tool_definitions
        instructions = cls.BASE_DEVELOPER_INSTRUCTIONS
        if capability_profile == "member_read_only":
            instructions += (
                " 当前微信用户是成员，只允许普通对话和本轮目录中的确定性只读装修或家庭备忘录查询。"
                "不得尝试账本写入或备忘录写入、附件消费或归档、导入、Home Assistant Operations，也不得声称成员拥有管理员权限。"
            )
        else:
            instructions += " 当前微信用户是所有者；写入和 Operations 仍必须遵守现有结构化工具、幂等、Passkey 和服务端门禁。"
        hub_tools = sorted(
            name
            for name in enabled
            if definitions.get(name) is not None
            and definitions[name].service == "renovation_hub"
        )
        hub_read_tools = sorted(name for name in hub_tools if definitions[name].read_only)
        hub_write_tools = sorted(name for name in hub_tools if not definitions[name].read_only)
        if hub_tools:
            instructions += f" 当前会话已配置 Renovation Hub 工具：{', '.join(hub_tools)}。"
            if capability_profile != "member_read_only":
                instructions += (
                    "所有者清晰提出查询、图表、导出、记账、退款、更正、撤销、导入检查或装修媒体/事件归档等请求时，"
                    "该请求本身就是本次匹配结构化工具调用的授权，应直接执行，不得再询问是否确认、是否授权或要求 Codex 弹窗审批。"
                    "只有缺少工具必填字段，或目标、金额、记录、附件、动作等语义确实存在多种合理解释时才澄清；"
                    "讨论、假设、举例、方案比较或仅询问‘如果这样做会怎样’不得推断为写入指令。"
                )
        if hub_read_tools:
            instructions += (
                f" 当前可用于装修只读核验的工具是：{', '.join(hub_read_tools)}。"
                "这些工具均是无副作用的只读工具。"
                "用户询问账本或装修档案是否连接、是否可用、当前支出、汇总、明细、项目、阶段、空间或时间线时，"
                "必须从本轮实际可用且与意图匹配的只读工具中选择；不得要求调用本轮目录中不存在的工具。"
                "用户自然语言提出查询、查看、核验、汇总或明细请求，就已经授权本次只读调用，应直接执行，"
                "不需要 Passkey、写入确认或额外征求授权，也不得转入 Home Assistant Operations 授权流程；"
                "只有工具实际返回权限错误时才能说明权限不足。"
                "只要存在与本次意图匹配的可用只读工具，就不得回复‘未连接账本’，也不得要求用户重新发送现有账目。"
            )
        elif hub_tools:
            instructions += " 当前会话只配置了 Renovation Hub 写入工具，没有可用的装修只读查询工具；不得用写工具冒充查询，也不得把部分能力缺失误报为整个 Hub 未连接。"
        else:
            instructions += " 当前会话未配置 Renovation Hub 工具时，才可以明确说明装修账本工具目录不可用。"
        if hub_write_tools:
            instructions += f" 当前可用的装修写入工具是：{', '.join(hub_write_tools)}；写账、退款、修改、撤销或归档必须调用匹配的结构化工具并遵守服务端门禁。"
        memo_tools = sorted(
            name
            for name in enabled
            if definitions.get(name) is not None
            and definitions[name].service == "family_memo"
        )
        memo_read_tools = sorted(name for name in memo_tools if definitions[name].read_only)
        memo_write_tools = sorted(name for name in memo_tools if not definitions[name].read_only)
        if memo_tools:
            instructions += f" 当前会话已配置家庭备忘录工具：{', '.join(memo_tools)}。"
            instructions += (
                "家庭备忘录使用 Asia/Shanghai；需要到期时间时必须传入带 +08:00 的完整 ISO 8601 时间。"
                "家庭备忘录是由 Node-RED 独立持久化、调度和通知的自动化服务；memo_create 携带 due_at 创建定时提醒，"
                "不属于 Codex Goal、当前 Turn 后台等待或 Codex 自身主动推送。"
                "用户说‘记一下’、‘提醒我’或等价的明确新增表达并给出可确定时间时，必须调用 memo_create；"
                "不得回复不能创建定时提醒或主动推送，也不得建议改用手机日历替代已经配置的家庭备忘录。"
                "微信来源和幂等消息标识由 Controller 注入，禁止自行提供。"
                "完成、取消或修改自然语言只给出标题/内容时，必须先用 memo_list 查询 pending 候选；只有唯一匹配时才能写入，多个候选时必须请用户消歧。"
            )
        if memo_read_tools:
            instructions += (
                f" 当前可用的家庭备忘录只读工具是：{', '.join(memo_read_tools)}。"
                "用户询问今天、逾期、待办或某分类事项时应直接查询，不需要 Passkey 或额外确认。"
            )
        if memo_write_tools and capability_profile != "member_read_only":
            instructions += (
                f" 当前可用的家庭备忘录写入工具是：{', '.join(memo_write_tools)}。"
                "用户清晰提出新增、修改、完成或取消备忘录时，该请求本身就是对应操作授权；只有缺少必填信息或候选不唯一时才澄清。"
            )
        if "ledger_generate_chart" in enabled:
            instructions += (
                " 调用 ledger_generate_chart 成功后，图表由 Controller 私有固化并由 Weixin Gateway 自动投递。"
                "最终回复只需简短说明统计已生成；不得输出 download_ref、内部 URL、文件路径、Bearer、Base64 或自行拼接图片下载链接。"
            )
        operation_tools = sorted(
            name
            for name in enabled
            if definitions.get(name) is not None
            and definitions[name].service == "ha_operations_broker"
        )
        if operation_tools:
            instructions += (
                f" 当前会话已配置受控 Home Assistant Operations 工具：{', '.join(operation_tools)}。"
                "这些工具调用不依赖 Codex UI 审批；但真正执行仍必须经过 Broker 的不可变提案、Passkey、一次性收据、精确白名单和 execution gate，"
                "任何工具预批准都不是对实际 Home Assistant 变更的授权。"
            )
        return instructions

    def configure_developer_context(
        self,
        available_tools: list[str],
        capability_profile: str,
        tool_definitions: dict[str, ToolDefinition] | None = None,
    ) -> None:
        if capability_profile not in {"owner_legacy", "owner", "member_read_only"}:
            raise AppServerError("invalid_capability_profile", "作业能力画像无效", definitive=True)
        instructions = self.build_developer_instructions(
            available_tools,
            capability_profile,
            tool_definitions,
        )
        with self._instruction_lock:
            self.developer_instructions = instructions

    def current_developer_instructions(self) -> str:
        with self._instruction_lock:
            return self.developer_instructions

    def refresh_mcp_catalog(self, expected_tools: list[str]) -> list[str]:
        result = self.request("mcpServerStatus/list", {"detail": "full"})
        servers = result.get("data") if isinstance(result, dict) else None
        if not isinstance(servers, list):
            raise AppServerError(
                "mcp_catalog_unavailable",
                "app-server 未返回 MCP 服务目录",
                definitive=True,
            )
        matches = [
            server
            for server in servers
            if isinstance(server, dict) and server.get("name") == "home_assistant_tools"
        ]
        if len(matches) != 1 or not isinstance(matches[0].get("tools"), dict):
            raise AppServerError(
                "mcp_catalog_unavailable",
                "app-server 家庭工具目录不可用",
                definitive=True,
            )
        published = sorted(
            name for name in matches[0]["tools"] if isinstance(name, str) and name
        )
        if not set(expected_tools).issubset(published):
            raise AppServerError(
                "mcp_catalog_incomplete",
                "app-server 家庭工具目录尚未完成初始加载",
                definitive=True,
            )
        return published

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
        with self._thread_load_lock:
            self._loaded_thread_contexts.clear()
            self._forkable_threads.clear()
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
            {"clientInfo": {"name": "ha_codex_controller", "title": "Home Assistant Codex Controller", "version": "0.5.35"}},
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
        with self._thread_load_lock:
            self._loaded_thread_contexts.clear()
            self._forkable_threads.clear()

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
        with self._thread_load_lock:
            return self._start_thread_locked()

    def _start_thread_locked(self) -> str:
        result = self.request(
            "thread/start",
            {
                "cwd": str(self.workspace),
                "sandbox": "read-only",
                "approvalPolicy": "never",
                "developerInstructions": self.current_developer_instructions(),
            },
        )
        thread = result.get("thread") if isinstance(result, dict) else None
        thread_id = thread.get("id") if isinstance(thread, dict) else None
        if not isinstance(thread_id, str) or not thread_id:
            raise AppServerError("thread_unavailable", "thread/start 未返回 Thread ID")
        self._loaded_thread_contexts[thread_id] = self._developer_context_fingerprint()
        self._forkable_threads.discard(thread_id)
        return thread_id

    def resume_thread(self, thread_id: str) -> str:
        with self._thread_load_lock:
            fingerprint = self._developer_context_fingerprint()
            loaded_fingerprint = self._loaded_thread_contexts.get(thread_id)
            method = "thread/resume" if loaded_fingerprint is None else "thread/fork"
            if loaded_fingerprint == fingerprint:
                return thread_id
            if loaded_fingerprint is not None and thread_id not in self._forkable_threads:
                return self._start_thread_locked()
            params = {
                "threadId": thread_id,
                "cwd": str(self.workspace),
                "sandbox": "read-only",
                "approvalPolicy": "never",
                "developerInstructions": self.current_developer_instructions(),
            }
            result = self.request(method, params)
            thread = result.get("thread") if isinstance(result, dict) else None
            loaded_thread_id = thread.get("id") if isinstance(thread, dict) else None
            if not isinstance(loaded_thread_id, str) or not loaded_thread_id:
                raise AppServerError("thread_unavailable", f"{method} 未返回 Thread ID")
            if method == "thread/resume" and loaded_thread_id != thread_id:
                raise AppServerError("thread_unavailable", "thread/resume 返回不匹配")
            if method == "thread/fork" and loaded_thread_id == thread_id:
                raise AppServerError("thread_unavailable", "thread/fork 未生成新 Thread")
            self._loaded_thread_contexts[loaded_thread_id] = fingerprint
            self._forkable_threads.add(loaded_thread_id)
            return loaded_thread_id

    def _developer_context_fingerprint(self) -> str:
        return hashlib.sha256(self.current_developer_instructions().encode("utf-8")).hexdigest()

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
        with self._thread_load_lock:
            self._forkable_threads.add(thread_id)
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
