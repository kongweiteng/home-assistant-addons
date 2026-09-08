"""Safe, event-driven error read model for the Controller management UI."""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import re
import threading
from collections.abc import Callable, Iterator, Mapping
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

from .store import StoreError

SHANGHAI = ZoneInfo("Asia/Shanghai")
SAFE_CODE_RE = re.compile(r"[a-z][a-z0-9_]{0,63}")
FAILURE_REF_RE = re.compile(r"ER-[A-Z2-7]{10}")
JOB_REF_RE = re.compile(r"JB-[A-Z2-7]{10}")
THREAD_REF_RE = re.compile(r"TH-[A-Z2-7]{20,52}")
CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")
_ERROR_LABELS: dict[str, tuple[str, str, bool]] = {
    "app_server_overloaded": ("Codex 服务繁忙", "warn", True),
    "upstream_http_connection_failed": ("上游连接失败", "warn", True),
    "response_stream_connection_failed": ("回复连接失败", "warn", True),
    "response_stream_disconnected": ("回复连接中断", "warn", True),
    "response_too_many_failed_attempts": ("上游连续失败", "warn", True),
    "upstream_internal_server_error": ("上游服务异常", "warn", True),
    "rate_limit_exceeded": ("上游请求受限", "warn", True),
    "context_window_exceeded": ("对话内容过长", "warn", False),
    "session_budget_exceeded": ("会话预算已用完", "warn", False),
    "usage_limit_exceeded": ("账户额度受限", "warn", True),
    "model_unavailable": ("模型不可用", "bad", False),
    "codex_unauthorized": ("Codex 认证不可用", "bad", False),
    "codex_bad_request": ("请求不受支持", "bad", False),
    "cyber_policy_rejected": ("请求未通过安全策略", "bad", False),
    "sandbox_error": ("运行环境受限", "bad", False),
    "thread_rollback_failed": ("会话恢复失败", "bad", False),
    "active_turn_not_steerable": ("当前任务不可继续", "warn", True),
    "turn_failed": ("Codex 任务执行失败", "bad", False),
    "controller_task_failed": ("任务未完成", "bad", False),
    "gateway_delivery_failed": ("微信回复未送出", "warn", True),
    "gateway_unavailable": ("微信网关不可用", "bad", True),
    "gateway_failure_sync_unavailable": ("微信错误状态暂不可读取", "warn", True),
    "desktop_thread_failed": ("任务需要处理", "bad", False),
    "desktop_recovery_required": ("任务等待人工确认", "bad", False),
    "desktop_protocol_degraded": ("任务暂时只读", "warn", False),
    "runner_manager_internal_error": ("Runner 管理器异常", "bad", True),
    "relay_publish_indeterminate": ("Runner 投递结果未知", "warn", False),
    "app_server_unavailable": ("Codex 服务不可用", "bad", True),
    "controller_start_failed": ("Controller 启动未完成", "bad", True),
    "create_identity_unknown": ("新任务身份待确认", "bad", False),
    "create_unknown": ("新任务结果未知", "warn", False),
    "create_thread_not_visible": ("新任务暂不可读", "bad", False),
    "create_turn_not_visible": ("新任务首轮暂不可见", "bad", False),
    "create_turn_interrupted": ("新任务首轮已中断", "warn", False),
    "create_turn_failed": ("新任务首轮执行失败", "bad", False),
    "create_image_queue_unknown": ("图片续传结果未知", "warn", False),
    "bridge_policy_invalid": ("App Bridge 启动策略无效", "bad", False),
    "bridge_path_invalid": ("App Bridge 路径无效", "bad", False),
    "bridge_caller_invalid": ("App Bridge 调用身份无效", "bad", False),
    "bridge_timeout_invalid": ("App Bridge 超时配置无效", "bad", False),
    "bridge_capability_unavailable": ("App Bridge 能力不可用", "bad", True),
    "bridge_projects_invalid": ("App Bridge 项目目录异常", "bad", False),
    "bridge_project_catalog_invalid": ("App Bridge 项目目录异常", "bad", False),
    "bridge_project_catalog_unavailable": ("App Bridge 项目目录不可用", "bad", True),
    "bridge_project_not_allowed": ("App Bridge 项目未获准", "bad", False),
    "bridge_image_unsupported": ("App Bridge 新建图片不受支持", "warn", False),
    "bridge_create_pending": ("App Bridge 新任务仍在准备", "warn", False),
    "bridge_create_response_invalid": ("App Bridge 新任务回执异常", "bad", False),
    "bridge_tool_failed": ("App Bridge 工具调用失败", "bad", False),
    "bridge_tool_response_invalid": ("App Bridge 工具回执异常", "bad", False),
    "bridge_request_invalid": ("App Bridge 请求无效", "bad", False),
    "bridge_request_too_large": ("App Bridge 请求过大", "bad", False),
    "bridge_request_failed": ("App Bridge 请求失败", "bad", False),
    "bridge_busy": ("App Bridge 正忙", "warn", True),
    "bridge_closed": ("App Bridge 连接已中断", "warn", True),
    "bridge_unavailable": ("App Bridge 不可用", "bad", True),
    "bridge_identity_invalid": ("App Bridge Socket 身份异常", "bad", False),
    "bridge_response_invalid": ("App Bridge 回执异常", "bad", False),
    "bridge_response_too_large": ("App Bridge 回执过大", "bad", False),
    "bridge_app_identity_invalid": ("Codex App 身份校验失败", "bad", False),
    "bridge_runtime_identity_invalid": ("Codex App 运行链校验失败", "bad", False),
    "bridge_app_signature_invalid": ("Codex App 签名校验失败", "bad", False),
    "bridge_app_pipe_unavailable": ("Codex App 工具通道不可用", "bad", True),
    "bridge_app_pipe_invalid": ("Codex App 工具通道校验失败", "bad", False),
    "bridge_app_timeout": ("Codex App 调用超时", "warn", True),
    "bridge_app_unavailable": ("Codex App 调用不可用", "bad", True),
    "bridge_app_response_invalid": ("Codex App 回执异常", "bad", False),
    "bridge_app_request_failed": ("Codex App 工具请求失败", "bad", False),
    "bridge_install_policy_invalid": ("App Bridge 安装策略无效", "bad", False),
    "bridge_install_source_invalid": ("App Bridge 安装源无效", "bad", False),
    "bridge_config_read_failed": ("App Bridge 配置读取失败", "bad", True),
    "bridge_config_invalid": ("App Bridge 配置无效", "bad", False),
    "bridge_config_conflict": ("App Bridge 配置冲突", "bad", False),
    "bridge_config_write_failed": ("App Bridge 配置写入失败", "bad", True),
    "bridge_config_verify_failed": ("App Bridge 配置验证失败", "bad", True),
}
_GATEWAY_DETAIL_TEXTS: dict[str, tuple[str, str]] = {
    "app_server_overloaded": (
        "上游服务暂时不稳定，自动重试后仍未完成。请稍后再试。",
        "等待服务恢复后重试。",
    ),
    "upstream_http_connection_failed": (
        "上游连接暂时不稳定，自动重试后仍未完成。请稍后再试。",
        "检查网络和 Runner 在线状态，恢复后重试。",
    ),
    "response_stream_connection_failed": (
        "上游响应连接中断，自动重试后仍未完成。请稍后再试。",
        "检查网络和 Runner 在线状态，恢复后重试。",
    ),
    "response_stream_disconnected": (
        "上游响应流中断，自动重试后仍未完成。请稍后再试。",
        "检查网络和 Runner 在线状态，恢复后重试。",
    ),
    "response_too_many_failed_attempts": (
        "上游连续失败，自动重试后仍未完成。请稍后再试。",
        "检查网络、Runner 和上游服务状态后重试。",
    ),
    "upstream_internal_server_error": (
        "上游服务发生内部错误，自动重试后仍未完成。请稍后再试。",
        "等待上游服务恢复后重试。",
    ),
    "rate_limit_exceeded": (
        "上游请求过于频繁，任务暂未完成。请稍后再试。",
        "等待一分钟后重试；若持续出现，请检查上游服务限流状态。",
    ),
    "context_window_exceeded": (
        "这次对话内容过长，已停止处理。请新开一个对话或缩短问题后重试。",
        "新建任务，或缩短输入和历史上下文后重试。",
    ),
    "session_budget_exceeded": (
        "Codex 当前会话预算已用完，任务未完成。请新开一个对话后再试。",
        "新建任务后重试。",
    ),
    "usage_limit_exceeded": (
        "Codex 当前使用额度受限，任务未完成。请稍后再试或检查账户额度。",
        "稍后重试，并在 Codex 中检查当前账户额度。",
    ),
    "model_unavailable": (
        "当前配置的模型在上游端点不可用，任务未开始执行。",
        "检查 Controller 当前模型与自定义 Responses API 的模型映射。",
    ),
    "codex_unauthorized": (
        "Codex 登录已失效，任务未执行完成。请在 Controller 页面重新登录后再试。",
        "在 Codex Controller 中恢复登录后重试。",
    ),
    "codex_bad_request": (
        "本次请求不受支持，且未自动重试。请调整问题后再试。",
        "调整请求内容后重新发送。",
    ),
    "cyber_policy_rejected": (
        "该请求未通过安全策略，未执行。",
        "检查请求是否超出当前安全策略允许范围。",
    ),
    "sandbox_error": (
        "任务受运行环境限制未完成，请调整请求或在 Controller 页面核对。",
        "检查任务运行环境和权限边界后重试。",
    ),
    "thread_rollback_failed": (
        "Codex 会话恢复失败，任务未完成。请新开一个对话后再试。",
        "新建任务后重试，不要继续使用当前异常会话。",
    ),
    "active_turn_not_steerable": (
        "当前 Codex 任务状态不允许继续处理，请等待现有任务结束后再试。",
        "等待当前任务结束，或在 Codex 页面停止当前任务后重试。",
    ),
    "turn_failed": (
        "任务未完成，系统保留了可用于定位问题的稳定错误码。",
        "在 Codex Controller 错误中心按错误码检查 Runner、登录和上游服务状态。",
    ),
    "controller_task_failed": (
        "任务未完成，系统保留了可用于定位问题的稳定错误码。",
        "在 Codex Controller 错误中心按错误码检查 Runner、登录和上游服务状态。",
    ),
    "gateway_delivery_failed": (
        "微信网关未能完成消息接收、提交或回复。",
        "在 Codex Controller 错误中心检查微信网关身份、网络和 Controller 连接状态。",
    ),
}

_LOCAL_DETAIL_TEXTS: dict[str, tuple[str, str]] = {
    "create_identity_unknown": (
        "Mac 已可能创建任务，但 Controller 无法安全确认它的身份。",
        "先在 Mac Codex App 中核对新任务；未确认前不要重复新建。",
    ),
    "create_unknown": (
        "新建请求可能已到达 Mac，但结果无法安全确认。",
        "先在 Mac Codex App 中核对任务列表；未确认前不要重复新建。",
    ),
    "create_thread_not_visible": (
        "Mac 已返回新任务身份，但在有界等待内仍无法读取该任务。",
        "在 Mac Codex App 中核对任务是否存在；不要自动重放新建请求。",
    ),
    "create_turn_not_visible": (
        "新任务已可读，但首条消息对应的 Turn 在有界等待内仍不可见。",
        "在 Mac Codex App 中打开该任务核对首条消息；未确认前不要重复发送。",
    ),
    "create_turn_interrupted": (
        "新任务已创建，但首个 Turn 在完成前被中断。",
        "打开已创建的任务查看上下文，确认后在原任务中继续。",
    ),
    "create_turn_failed": (
        "新任务已创建，但首个 Turn 执行失败。",
        "打开已创建的任务查看状态，处理原因后在原任务中继续。",
    ),
    "create_image_queue_unknown": (
        "新任务已创建，但图片消息的续传结果无法安全确认。",
        "先在原任务中核对图片是否已排队；未确认前不要重复发送。",
    ),
    "bridge_policy_invalid": ("App Bridge 启动参数未通过固定安全策略校验。", "检查 Runner 的 App Bridge 安装配置。"),
    "bridge_path_invalid": ("Runner 配置的 App Bridge Socket 路径无效。", "检查 Runner 的 App Bridge 路径配置。"),
    "bridge_caller_invalid": ("Runner 配置的 Codex App 调用任务身份无效。", "重新核对并安装 App Bridge 调用身份。"),
    "bridge_timeout_invalid": ("Runner 的 App Bridge 超时参数无效。", "恢复 Runner 支持的 App Bridge 超时配置。"),
    "bridge_capability_unavailable": ("App Bridge 未就绪，或未公开项目列表和新建任务能力。", "确认 Codex App 已启动，然后重连 Runner。"),
    "bridge_projects_invalid": ("Codex App 返回的项目目录未通过结构校验。", "更新或重启 App Bridge 后再核对项目目录。"),
    "bridge_project_catalog_invalid": ("Codex App 返回的项目目录未通过结构校验。", "更新或重启 App Bridge 后再核对项目目录。"),
    "bridge_project_catalog_unavailable": ("Runner 暂时无法从 Codex App 读取项目目录。", "确认 Codex App 与 App Bridge 在线后重试。"),
    "bridge_project_not_allowed": ("目标路径未唯一匹配到 Codex App 已登记项目。", "先在 Codex App 中登记唯一目标项目，再新建任务。"),
    "bridge_image_unsupported": ("App Bridge 不允许在创建请求中直接夹带图片。", "使用新任务创建后的受控图片队列通道。"),
    "bridge_create_pending": ("Codex App 返回了尚未就绪的工作树任务，无法立即确认本地任务身份。", "在 Mac Codex App 中核对该任务；不要自动重放创建请求。"),
    "bridge_create_response_invalid": ("Codex App 的新任务回执未通过身份校验。", "在 Mac Codex App 中核对任务；未确认前不要重复新建。"),
    "bridge_tool_failed": ("Codex App 拒绝或未完成 App Bridge 工具调用。", "在 Codex App 中检查登录和任务权限。"),
    "bridge_tool_response_invalid": ("Codex App 工具回执未通过固定格式校验。", "更新 App Bridge 与 Runner 后重新核对。"),
    "bridge_request_invalid": ("App Bridge 拒绝了不符合固定协议的请求。", "更新 App Bridge 与 Runner 后重新核对。"),
    "bridge_request_too_large": ("App Bridge 请求超出固定大小上限。", "缩短首条消息或减少附件后再试。"),
    "bridge_request_failed": ("App Bridge 未能完成请求，且没有暴露原始异常。", "检查 Codex App、App Bridge 和 Runner 状态。"),
    "bridge_busy": ("App Bridge 正在处理另一个受控请求。", "等待当前请求结束后重试。"),
    "bridge_closed": ("App Bridge 在返回回执前中断了连接。", "先核对 Mac 端是否已发生操作；未确认前不要重放写请求。"),
    "bridge_unavailable": ("Runner 当前无法连接 App Bridge Socket。", "Runner 会被动重试连接，不会自动打开或切换桌面任务。"),
    "bridge_identity_invalid": ("App Bridge Socket 的所有者、类型或权限未通过校验。", "停止使用该通道，重新安装并核对 App Bridge。"),
    "bridge_response_invalid": ("App Bridge 回执的结构或请求身份未通过校验。", "先核对 Mac 端状态；未确认前不要重放写请求。"),
    "bridge_response_too_large": ("App Bridge 回执超出固定大小上限。", "停止重试并更新 App Bridge 与 Runner。"),
    "bridge_app_identity_invalid": ("App Bridge 未确认调用方来自预期的 Codex App。", "停止使用该通道，核对 Codex App 安装身份。"),
    "bridge_runtime_identity_invalid": ("App Bridge 未确认当前进程链来自预期的 Codex App。", "停止使用该通道，从正常 Codex App 重新启动。"),
    "bridge_app_signature_invalid": ("Codex App 代码签名未通过 App Bridge 校验。", "停止使用该通道，核对 Codex App 的官方签名。"),
    "bridge_app_pipe_unavailable": ("App Bridge 暂时找不到 Codex App 的本地工具通道。", "确认 Codex App 已登录并正常运行；Runner 会被动重试且不会切换窗口。"),
    "bridge_app_pipe_invalid": ("Codex App 工具通道的路径、所有者、类型或权限未通过校验。", "停止使用该通道，重启正常 Codex App 后重新核对。"),
    "bridge_app_timeout": ("Codex App 在有界等待内未返回工具回执。", "先核对 Mac 端是否已发生操作；未确认前不要重放写请求。"),
    "bridge_app_unavailable": ("App Bridge 当前无法连接 Codex App 工具通道。", "确认 Codex App 已恢复；Runner 会被动重试且不会切换窗口。"),
    "bridge_app_response_invalid": ("Codex App 回执未通过长度、编码或结构校验。", "先核对 Mac 端状态；未确认前不要重放写请求。"),
    "bridge_app_request_failed": ("Codex App 工具明确返回失败，原始异常未对外暴露。", "在 Codex App 中检查登录、项目和任务权限。"),
    "bridge_install_policy_invalid": ("App Bridge 的锚点任务或项目白名单未通过安装策略校验。", "核对受控锚点任务和项目配置后重新安装。"),
    "bridge_install_source_invalid": ("App Bridge 安装使用的 Codex、Node 或脚本源不是预期普通文件。", "从已验证的 Runner 安装包重新安装。"),
    "bridge_config_read_failed": ("Runner 无法安全读取当前 Codex MCP 配置。", "检查 Codex 配置文件权限后重试安装。"),
    "bridge_config_invalid": ("当前 Codex MCP 配置不是 App Bridge 安装器支持的结构。", "保留现有配置并手工核对冲突项。"),
    "bridge_config_conflict": ("已存在的 App Bridge MCP 定义与预期安全配置不一致。", "保留现有配置，核对差异后再决定是否替换。"),
    "bridge_config_write_failed": ("Codex CLI 未能完成 App Bridge MCP 配置写入。", "检查 Codex 配置权限和磁盘状态后重试。"),
    "bridge_config_verify_failed": ("App Bridge MCP 配置写入后的回读验证未通过。", "停止使用该通道，核对 Codex MCP 配置后再重试。"),
}


def _error(code: Any, *, fallback: str = "controller_task_failed") -> dict[str, Any]:
    safe = code if isinstance(code, str) and SAFE_CODE_RE.fullmatch(code) and code in _ERROR_LABELS else fallback
    label, severity, retryable = _ERROR_LABELS[safe]
    return {"code": safe, "label": label, "severity": severity, "retryable": retryable}


def _safe_time(value: Any) -> str | None:
    if not isinstance(value, str): return None
    try: parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError: return None
    return parsed.astimezone(SHANGHAI).isoformat() if parsed.tzinfo and parsed.utcoffset() is not None else None


def _safe_gateway_detail(error: Any, code: str) -> dict[str, str]:
    """Retain only exact, bounded public wording from the Gateway contract."""
    allowed = _GATEWAY_DETAIL_TEXTS.get(code)
    if not allowed or not isinstance(error, Mapping):
        return {}
    message, action = error.get("message"), error.get("recommended_action")
    if (
        not isinstance(message, str)
        or not isinstance(action, str)
        or len(message) > 160
        or len(action) > 200
        or CONTROL_RE.search(message)
        or CONTROL_RE.search(action)
        or (message, action) != allowed
    ):
        return {}
    return {"message": message, "recommended_action": action}


def _local_detail(code: str) -> dict[str, str]:
    """Return only Controller-owned fixed text, never a runtime exception."""
    detail = _LOCAL_DETAIL_TEXTS.get(code)
    if detail is None:
        return {}
    return {"message": detail[0], "recommended_action": detail[1]}


def _create_reference(value: Any) -> str | None:
    if not isinstance(value, str) or not value or len(value) > 256 or CONTROL_RE.search(value):
        return None
    return "CR-" + hashlib.sha256(value.encode("utf-8")).hexdigest()[:12].upper()


class ErrorCenter:
    """Error cache fed by Gateway SSE; detail fetches happen only per revision."""
    def __init__(self, *, desktop_controller: Any | None, component_status: Callable[[], Mapping[str, Any]], gateway_base_url: str = "", gateway_token: str = "", now: Callable[[], dt.datetime] | None = None, opener: Callable[..., Any] = urlopen) -> None:
        self.desktop_controller, self.component_status = desktop_controller, component_status
        self.gateway_base_url, self.gateway_token = gateway_base_url.rstrip("/"), gateway_token
        self.now, self.opener = now or (lambda: dt.datetime.now(SHANGHAI)), opener
        self._condition = threading.Condition()
        self._change_sequence = 0
        self._gateway_items: list[dict[str, Any]] = []
        self._gateway_sync_error, self._gateway_revision = False, None
        self._active_local_items: dict[tuple[str, str], dict[str, Any]] = {}
        self._recovered_items: dict[tuple[str, str], dict[str, Any]] = {}
        self._stop, self._subscription = threading.Event(), None
        self._gateway_response: Any | None = None

    def start(self) -> None:
        if self._gateway_enabled():
            with self._condition:
                if self._subscription is None or not self._subscription.is_alive():
                    self._stop.clear()
                    self._subscription = threading.Thread(target=self._gateway_stream_loop, name="codex-controller-gateway-failures", daemon=True)
                    self._subscription.start()
        if self.desktop_controller is not None and hasattr(self.desktop_controller, "add_change_listener"):
            self.desktop_controller.add_change_listener(self.notify_local_change)

    def stop(self) -> None:
        self._stop.set()
        with self._condition:
            response = self._gateway_response
        if response is not None and callable(getattr(response, "close", None)):
            response.close()
        self.notify_local_change()

    def notify_local_change(self) -> None:
        with self._condition:
            self._change_sequence += 1
            self._condition.notify_all()

    def page(self, *, cursor: int = 0, limit: int = 12) -> dict[str, Any]:
        if not isinstance(cursor, int) or isinstance(cursor, bool) or cursor < 0: raise StoreError("error_cursor_invalid", "错误列表游标无效", status=400)
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 50: raise StoreError("error_limit_invalid", "错误列表每页数量无效", status=400)
        items = self._items(); visible = items[cursor:cursor + limit]; next_cursor = cursor + len(visible)
        return {"version": 1, "error_revision": self._revision(items), "summary": {"total": len(items), "current": sum(i["lifecycle"] == "current" for i in items), "historical": sum(i["lifecycle"] == "historical" for i in items), "recovered": sum(i["lifecycle"] == "recovered" for i in items), "components": sum(i["scope"] == "component" for i in items), "tasks": sum(i["scope"] == "task" for i in items), "retryable": sum(bool(i["error"]["retryable"]) for i in items if i["lifecycle"] == "current")}, "page": {"cursor": cursor, "limit": limit, "next_cursor": next_cursor if next_cursor < len(items) else None, "has_more": next_cursor < len(items)}, "items": visible}

    def stream(self, *, heartbeat_seconds: float = 12.0) -> Iterator[dict[str, Any]]:
        if isinstance(heartbeat_seconds, bool) or not isinstance(heartbeat_seconds, (int, float)) or not 10 <= heartbeat_seconds <= 15: raise StoreError("error_stream_invalid", "错误实时流配置无效", status=500)
        cursor, previous = 0, None
        while True:
            # Capture the sequence before deriving the revision.  A publisher
            # that arrives while page() is running must remain observable.
            with self._condition:
                observed = self._change_sequence
                document = self.page(cursor=0, limit=1)
            revision = document["error_revision"]
            if revision != previous:
                cursor += 1; event = "ready" if previous is None else "errors"; previous = revision
                yield self._frame(event, cursor, document); continue
            with self._condition:
                if self._change_sequence != observed:
                    continue
                changed = self._condition.wait_for(lambda: self._change_sequence != observed, timeout=heartbeat_seconds)
            if changed: continue
            cursor += 1; yield self._frame("heartbeat", cursor, document)

    def _gateway_stream_loop(self) -> None:
        delay = 1.0
        while not self._stop.is_set():
            try:
                request = Request(f"{self.gateway_base_url}/internal/v1/failures/stream", headers={"Authorization": f"Bearer {self.gateway_token}", "Accept": "text/event-stream"}, method="GET")
                with self.opener(request, timeout=15) as response:
                    with self._condition: self._gateway_response = response
                    try:
                        delay = 1.0; self._consume_gateway_sse(response)
                    finally:
                        with self._condition: self._gateway_response = None
                if not self._stop.is_set(): self._set_gateway_sync_error()
            except (HTTPError, URLError, OSError, TimeoutError, ValueError, json.JSONDecodeError): self._set_gateway_sync_error()
            if not self._stop.is_set(): self._stop.wait(delay); delay = min(15.0, delay * 2.0)

    def _consume_gateway_sse(self, response: Any) -> None:
        event, lines = "", []
        while not self._stop.is_set():
            raw = response.readline()
            if not raw: return
            line = (raw.decode("utf-8") if isinstance(raw, bytes) else str(raw)).rstrip("\r\n")
            if not line:
                if event == "failures" and len(lines) == 1: self._receive_gateway_frame(json.loads(lines[0]))
                event, lines = "", []; continue
            if line.startswith(":"): continue
            field, separator, value = line.partition(":")
            if not separator: raise ValueError("invalid_sse_frame")
            if field == "event": event = value.lstrip(" ")
            elif field == "data": lines.append(value.lstrip(" "))

    def _receive_gateway_frame(self, frame: Any) -> None:
        if not isinstance(frame, Mapping) or set(frame) != {"version", "revision", "summary"}: raise ValueError("invalid_gateway_failure_frame")
        revision, summary = frame.get("revision"), frame.get("summary")
        if frame.get("version") != 1 or isinstance(revision, bool) or not isinstance(revision, int) or revision < 0 or not isinstance(summary, Mapping) or set(summary) != {"count", "latest_occurred_at"} or isinstance(summary.get("count"), bool) or not isinstance(summary.get("count"), int) or summary.get("count") < 0 or _safe_time(summary.get("latest_occurred_at")) != summary.get("latest_occurred_at"): raise ValueError("invalid_gateway_failure_frame")
        with self._condition:
            if self._gateway_revision == revision: return
        items = self._fetch_gateway_details()
        with self._condition:
            self._gateway_revision, self._gateway_items, self._gateway_sync_error = revision, items, False
            self._change_sequence += 1; self._condition.notify_all()

    def _fetch_gateway_details(self) -> list[dict[str, Any]]:
        request = Request(f"{self.gateway_base_url}/internal/v1/failures", headers={"Authorization": f"Bearer {self.gateway_token}", "Accept": "application/json"}, method="GET")
        with self.opener(request, timeout=3) as response: body = response.read(128 * 1024 + 1)
        if len(body) > 128 * 1024: raise ValueError("response_too_large")
        document = json.loads(body); result = document.get("result") if isinstance(document, dict) else None; raw_items = result.get("items") if isinstance(result, dict) else None
        if not isinstance(raw_items, list) or len(raw_items) > 20: raise ValueError("invalid_failure_document")
        return [item for item in (self._gateway_item(value) for value in raw_items) if item]

    def _set_gateway_sync_error(self) -> None:
        with self._condition:
            if self._gateway_sync_error: return
            self._gateway_sync_error = True; self._change_sequence += 1; self._condition.notify_all()

    def _gateway_enabled(self) -> bool: return bool(self.gateway_base_url and len(self.gateway_token) >= 32)

    def _items(self) -> list[dict[str, Any]]:
        with self._condition: gateway, sync_error = list(self._gateway_items), self._gateway_sync_error
        create_items = self._create_items()
        task_items = self._task_items()
        local_current = self._component_items(sync_error) + [
            item for item in task_items if item["lifecycle"] == "current"
        ] + [
            item for item in create_items if item["lifecycle"] == "current"
        ]
        local_history = [
            item for item in task_items + create_items if item["lifecycle"] == "historical"
        ]
        recovered = self._track_local_lifecycle(
            local_current,
            terminal_keys={self._item_key(item) for item in local_history},
        )
        lifecycle_order = {"current": 2, "recovered": 1, "historical": 0}
        return sorted(
            gateway + local_current + recovered + local_history,
            key=lambda i: (
                lifecycle_order.get(str(i.get("lifecycle")), -1),
                i.get("recovered_at") or i.get("occurred_at") or "",
                {"bad": 2, "warn": 1}.get(i["error"]["severity"], 0),
                i["reference"],
            ),
            reverse=True,
        )

    def _gateway_item(self, value: Any) -> dict[str, Any] | None:
        if not isinstance(value, Mapping) or not isinstance(value.get("failure_short"), str) or not FAILURE_REF_RE.fullmatch(value["failure_short"]) or value.get("source") not in {"controller", "gateway"}: return None
        error = value.get("error")
        fallback = "controller_task_failed" if value["source"] == "controller" else "gateway_delivery_failed"
        public_error = _error(error.get("code") if isinstance(error, Mapping) else None, fallback=fallback)
        result = {"scope": "component", "origin": "wechat", "reference": value["failure_short"], "title": "微信任务未完成" if value["source"] == "controller" else "微信消息处理异常", "occurred_at": _safe_time(value.get("occurred_at")), "lifecycle": "historical", "lifecycle_label": "历史错误", "error": public_error}
        result.update(_safe_gateway_detail(error, public_error["code"]))
        if isinstance(value.get("job_short"), str) and JOB_REF_RE.fullmatch(value["job_short"]): result["task_reference"] = value["job_short"]
        return result

    def _component_items(self, sync_error: bool) -> list[dict[str, Any]]:
        try: status = self.component_status()
        except Exception: status = {"controller_start_error": "controller_start_failed"}
        result = []
        for key, title, fallback in (("controller_start_error", "Controller", "controller_start_failed"), ("controller_auth_error", "Codex 认证", "app_server_unavailable"), ("app_server_error", "Codex 服务", "app_server_unavailable"), ("runner_error", "Runner", "runner_manager_internal_error")):
            if status.get(key):
                public_error = _error(status[key], fallback=fallback)
                result.append({"scope": "component", "origin": "controller", "reference": f"CP-{key}", "title": title, "occurred_at": None, "lifecycle": "current", "lifecycle_label": "当前故障", "error": public_error, **_local_detail(public_error["code"])})
        if sync_error: result.append({"scope": "component", "origin": "wechat", "reference": "CP-gateway-sync", "title": "微信机器人", "occurred_at": None, "lifecycle": "current", "lifecycle_label": "当前故障", "error": _error("gateway_failure_sync_unavailable")})
        return result

    def _task_items(self) -> list[dict[str, Any]]:
        if self.desktop_controller is None: return []
        threads: list[Any] = []
        cursor = 0
        # Error discovery must not silently omit older tasks.  It remains
        # bounded to avoid turning a status view into an unbounded data load.
        for _page in range(10):
            try:
                document = self.desktop_controller.threads(
                    host_ref=None, project_ref=None, status=None,
                    after_cursor=cursor, limit=200, order="recent",
                )
            except Exception:
                break
            batch = document.get("threads") if isinstance(document, Mapping) else None
            if not isinstance(batch, list):
                break
            threads.extend(batch)
            if not document.get("has_more"):
                break
            next_cursor = document.get("next_cursor")
            if isinstance(next_cursor, bool) or not isinstance(next_cursor, int) or next_cursor <= cursor:
                break
            cursor = next_cursor
        result = []
        for thread in threads:
            if not isinstance(thread, Mapping):
                continue
            reference, state = thread.get("thread_ref"), thread.get("status")
            code = {"failed": "desktop_thread_failed", "recovery_required": "desktop_recovery_required", "protocol_degraded": "desktop_protocol_degraded"}.get(state)
            if isinstance(reference, str) and THREAD_REF_RE.fullmatch(reference) and code:
                lifecycle = "historical" if state == "failed" else "current"
                result.append({"scope": "task", "origin": "desktop", "reference": reference, "task_href": f"desktop/?thread_ref={reference}", "title": "Mac 任务需要处理" if lifecycle == "current" else "Mac 任务历史错误", "occurred_at": _safe_time(thread.get("updated_at")), "lifecycle": lifecycle, "lifecycle_label": "当前故障" if lifecycle == "current" else "历史错误", "error": _error(code)})
        return result

    def _create_items(self) -> list[dict[str, Any]]:
        """Read the bounded, body-free create receipt journal when available."""
        if self.desktop_controller is None:
            return []
        journal = getattr(self.desktop_controller, "_create_journal", None)
        host_commands = getattr(journal, "host_commands", None)
        hosts_method = getattr(self.desktop_controller, "hosts", None)
        if not callable(host_commands) or not callable(hosts_method):
            return []
        try:
            document = hosts_method()
        except Exception:
            return []
        hosts = document.get("hosts") if isinstance(document, Mapping) else None
        if not isinstance(hosts, list) or len(hosts) > 100:
            return []
        items: list[dict[str, Any]] = []
        for host in hosts:
            host_ref = host.get("host_ref") if isinstance(host, Mapping) else None
            if not isinstance(host_ref, str):
                continue
            try:
                commands = host_commands(host_ref)
            except Exception:
                continue
            if not isinstance(commands, list) or len(commands) > 20:
                continue
            for command in commands:
                if not isinstance(command, Mapping):
                    continue
                state = command.get("state")
                if state not in {"failed", "conflict", "expired", "unknown", "recovery_required"}:
                    continue
                reference = _create_reference(command.get("request_id"))
                if reference is None:
                    continue
                lifecycle = "current" if state in {"unknown", "recovery_required"} else "historical"
                fallback = "desktop_recovery_required" if lifecycle == "current" else "controller_task_failed"
                public_error = _error(command.get("error_code"), fallback=fallback)
                item: dict[str, Any] = {
                    "scope": "task",
                    "origin": "desktop",
                    "reference": reference,
                    "title": "Mac 新任务需要处理" if lifecycle == "current" else "Mac 新任务历史错误",
                    "occurred_at": _safe_time(command.get("updated_at")),
                    "lifecycle": lifecycle,
                    "lifecycle_label": "当前故障" if lifecycle == "current" else "历史错误",
                    "error": public_error,
                    **_local_detail(public_error["code"]),
                }
                thread_ref = command.get("thread_ref")
                if isinstance(thread_ref, str) and THREAD_REF_RE.fullmatch(thread_ref):
                    item["task_href"] = f"desktop/?thread_ref={thread_ref}"
                    item["task_reference"] = thread_ref
                items.append(item)
        return items

    @staticmethod
    def _item_key(item: Mapping[str, Any]) -> tuple[str, str]:
        # A changing diagnosis for the same incident is not a recovery.  Key by
        # its public source/reference so current -> terminal transitions cannot
        # produce a misleading recovered tombstone beside a historical error.
        return str(item.get("origin") or ""), str(item.get("reference") or "")

    def _track_local_lifecycle(
        self,
        current: list[dict[str, Any]],
        *,
        terminal_keys: set[tuple[str, str]],
    ) -> list[dict[str, Any]]:
        current_by_key = {self._item_key(item): dict(item) for item in current}
        with self._condition:
            for key, previous in self._active_local_items.items():
                if key in current_by_key or key in terminal_keys:
                    continue
                recovered = dict(previous)
                recovered["lifecycle"] = "recovered"
                recovered["lifecycle_label"] = "已恢复"
                recovered["recovered_at"] = self._now().isoformat()
                recovered["error"] = {**recovered["error"], "retryable": False}
                self._recovered_items[key] = recovered
            for key in current_by_key.keys() | terminal_keys:
                self._recovered_items.pop(key, None)
            self._active_local_items = current_by_key
            if len(self._recovered_items) > 50:
                oldest = sorted(
                    self._recovered_items,
                    key=lambda key: str(self._recovered_items[key].get("recovered_at") or ""),
                )[: len(self._recovered_items) - 50]
                for key in oldest:
                    self._recovered_items.pop(key, None)
            return [dict(item) for item in self._recovered_items.values()]

    def _frame(self, event: str, cursor: int, document: Mapping[str, Any]) -> dict[str, Any]: return {"event": event, "cursor": cursor, "data": {"version": 1, "cursor": cursor, "error_revision": document["error_revision"], "summary": document["summary"], "server_time": self._now().isoformat()}}
    @staticmethod
    def _revision(items: list[dict[str, Any]]) -> str: return hashlib.sha256(json.dumps(items, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()[:24]
    def _now(self) -> dt.datetime:
        value = self.now(); return value.astimezone(SHANGHAI) if value.tzinfo is not None and value.utcoffset() is not None else dt.datetime.now(SHANGHAI)

__all__ = ["ErrorCenter"]
