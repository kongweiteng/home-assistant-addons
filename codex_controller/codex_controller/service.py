"""Controller orchestration for authentication, queue scheduling and app-server events."""

from __future__ import annotations

import re
import threading
import time
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from .app_server import AppServerClient, AppServerError
from .media_input import MediaInputError, TurnMediaManager
from .runner_service import RunnerManagerService
from .desktop_service import DesktopControllerService
from .source_identity import runtime_source_identity
from .store import ControllerStore, StoreError
from .tool_proxy import ToolProxyError
from .turn_retry import (
    classify_turn_error,
    item_observations,
    retry_delay_seconds,
    turn_observations,
)


NEW_THREAD_COMMANDS = frozenset({"打开新会话", "/new"})
NEW_THREAD_RESULT = "新会话已建立。下一条消息将使用当前 Codex 配置和工具目录。"
MEDIA_ARCHIVE_ACTION_RE = re.compile(r"(?:归档|存档|保存|添加|加入|关联|记录)")
MEDIA_ARCHIVE_TARGET_RE = re.compile(r"(?:装修|施工|工地|现场).{0,12}(?:档案|记录|媒体库|资料库)")
MEDIA_ARCHIVE_MEDIA_ACTION_RE = re.compile(
    r"(?:装修|施工|工地|现场).{0,12}(?:照片|图片|视频|媒体).{0,8}(?:归档|存档|归入|收录|记录)"
    r"|(?:归档|存档|归入|收录|记录).{0,8}(?:装修|施工|工地|现场).{0,12}(?:照片|图片|视频|媒体)"
)
QUOTE_ARCHIVE_TARGET_RE = re.compile(r"(?:询价|报价|报价单|价格单|供应商|名片|商品规格|产品规格)")
QUOTE_ARCHIVE_MEDIA_ACTION_RE = re.compile(
    r"(?:询价|报价|报价单|价格单|名片|商品规格|产品规格).{0,10}(?:归档|存档|保存|添加|加入|关联|记录)"
    r"|(?:归档|存档|保存|添加|加入|关联|记录).{0,10}(?:询价|报价|报价单|价格单|名片|商品规格|产品规格)"
)
MEDIA_ARCHIVE_NEGATION_RE = re.compile(r"(?:不要|别|无需|不用|不需要).{0,12}(?:归档|存档|保存|添加|加入|关联|记录)")
MEMO_CREATE_PREFIX_RE = re.compile(
    r"^\s*(?:请)?(?:帮我)?(?:记一下|记下|记录一下|添加(?:一个)?备忘录|新增(?:一个)?备忘录|提醒我)[，,：:\s]*"
)
MEMO_DUE_RE = re.compile(
    r"^(?:(?P<relative>今天|明天|后天)|(?P<month>\d{1,2})月(?P<day>\d{1,2})日)?\s*"
    r"(?P<period>凌晨|早上|上午|中午|下午|晚上)?\s*"
    r"(?P<hour>\d{1,2}|[零〇一二两三四五六七八九十]{1,3})"
    r"(?:(?:点|时)(?:(?P<half>半)|(?P<minute>[零〇一二两三四五六七八九十\d]{1,3})分?)?"
    r"|[:：](?P<colon_minute>\d{1,2}))"
)
MEMO_LIST_PENDING_RE = re.compile(
    r"^(?:请)?(?:显示|查看|列出|看看|告诉我|有哪些|有什么)(?:一下)?(?:所有)?"
    r"(?:未完成|待完成|待办|待处理)(?:的)?(?:备忘录|事项|事情)[？?。.]?$"
)
MEMO_LIST_TODAY_RE = re.compile(
    r"^(?:请)?(?:显示|查看|列出|看看|告诉我)(?:一下)?今天(?:的)?(?:备忘录|事项|事情)[？?。.]?$"
)
MEMO_LIST_OVERDUE_RE = re.compile(
    r"^(?:请)?(?:显示|查看|列出|看看|告诉我|有哪些|有什么)(?:一下)?(?:所有)?"
    r"(?:逾期|过期)(?:的)?(?:备忘录|事项|事情)[？?。.]?$"
)
MEMO_COMPLETE_PREFIX_RE = re.compile(
    r"^(?:请)?(?:帮我)?(?:完成|办完|标记完成|设为完成)[：:\s]*(?P<query>.+?)[。.]?$"
)
MEMO_COMPLETE_SUFFIX_RE = re.compile(
    r"^(?:请)?(?:把|将)\s*(?P<query>.+?)\s*(?:标记为|设为)?(?:已)?完成[。.]?$"
)
RENOVATION_MEMO_RE = re.compile(r"(?:装修|施工|工地|铝瓦|地暖|门窗|瓷砖|木工|油漆|水电)")
SHANGHAI = ZoneInfo("Asia/Shanghai")
CHINESE_DIGITS = {
    "零": 0,
    "〇": 0,
    "一": 1,
    "二": 2,
    "两": 2,
    "三": 3,
    "四": 4,
    "五": 5,
    "六": 6,
    "七": 7,
    "八": 8,
    "九": 9,
}


def has_explicit_media_archive_intent(text: str, attachments: list[dict[str, Any]] | None = None) -> bool:
    """Allow media writes only for an explicit positive renovation-archive request."""

    if not isinstance(text, str) or not text.strip() or not attachments:
        return False
    normalized = re.sub(r"\s+", "", text)
    if MEDIA_ARCHIVE_NEGATION_RE.search(normalized):
        return False
    return bool(
        MEDIA_ARCHIVE_ACTION_RE.search(normalized)
        and (
            MEDIA_ARCHIVE_TARGET_RE.search(normalized)
            or MEDIA_ARCHIVE_MEDIA_ACTION_RE.search(normalized)
            or QUOTE_ARCHIVE_TARGET_RE.search(normalized)
            or QUOTE_ARCHIVE_MEDIA_ACTION_RE.search(normalized)
        )
    )


def is_new_thread_command(payload: dict[str, Any]) -> bool:
    """Recognize the exact attachment-free user control command."""
    text = payload.get("text")
    attachments = payload.get("attachments")
    return isinstance(text, str) and text.strip() in NEW_THREAD_COMMANDS and attachments == []


def _chinese_number(value: str) -> int | None:
    if value.isdigit():
        return int(value)
    normalized = value.replace("两", "二").replace("〇", "零")
    if "十" in normalized:
        left, right = normalized.split("十", 1)
        tens = 1 if not left else CHINESE_DIGITS.get(left)
        ones = 0 if not right else CHINESE_DIGITS.get(right)
        if tens is None or ones is None:
            return None
        return tens * 10 + ones
    if len(normalized) == 1:
        return CHINESE_DIGITS.get(normalized)
    digits = [CHINESE_DIGITS.get(character) for character in normalized]
    if any(digit is None for digit in digits):
        return None
    return int("".join(str(digit) for digit in digits))


def direct_memo_create_arguments(payload: dict[str, Any]) -> dict[str, Any] | None:
    """Parse an explicit timed memo command without leaving tool choice to the model."""

    text = payload.get("text")
    if not isinstance(text, str) or payload.get("attachments") != []:
        return None
    prefix = MEMO_CREATE_PREFIX_RE.match(text)
    if prefix is None:
        return None
    remainder = text[prefix.end() :].strip()
    due = MEMO_DUE_RE.match(remainder)
    if due is None:
        return None
    hour = _chinese_number(due.group("hour"))
    minute_text = due.group("minute") or due.group("colon_minute")
    minute = 30 if due.group("half") else 0 if minute_text is None else _chinese_number(minute_text)
    if hour is None or minute is None or not 0 <= hour <= 23 or not 0 <= minute <= 59:
        return None
    period = due.group("period")
    if period in {"下午", "晚上"} and hour < 12:
        hour += 12
    elif period == "中午" and hour < 11:
        hour += 12
    elif period in {"凌晨", "早上", "上午"} and hour == 12:
        hour = 0
    try:
        received = datetime.fromisoformat(str(payload.get("received_at") or ""))
    except ValueError:
        return None
    if received.tzinfo is None:
        return None
    local_received = received.astimezone(SHANGHAI)
    relative = due.group("relative")
    if relative:
        day_offset = {"今天": 0, "明天": 1, "后天": 2}[relative]
        target_date = (local_received + timedelta(days=day_offset)).date()
    elif due.group("month") and due.group("day"):
        month = int(due.group("month"))
        day = int(due.group("day"))
        try:
            target_date = local_received.date().replace(month=month, day=day)
        except ValueError:
            return None
        if target_date < local_received.date():
            try:
                target_date = target_date.replace(year=target_date.year + 1)
            except ValueError:
                return None
    else:
        target_date = local_received.date()
    due_at = datetime(
        target_date.year,
        target_date.month,
        target_date.day,
        hour,
        minute,
        tzinfo=SHANGHAI,
    )
    if relative is None and due.group("month") is None and due_at <= local_received:
        due_at += timedelta(days=1)
    content = remainder[due.end() :].lstrip("，,。；;：: \t")
    content = re.sub(r"^(?:提醒我|记得)\s*", "", content).strip()
    if not content:
        return None
    arguments: dict[str, Any] = {
        "content": content,
        "due_at": due_at.isoformat(timespec="seconds"),
        "priority": "normal",
    }
    if RENOVATION_MEMO_RE.search(content):
        arguments["category"] = "装修"
    return arguments


def memo_create_result_text(result: dict[str, Any], arguments: dict[str, Any]) -> str:
    document = result.get("result") if isinstance(result.get("result"), dict) else {}
    memo = document.get("memo") if isinstance(document.get("memo"), dict) else {}
    content = str(memo.get("content") or arguments["content"])
    due_at = str(memo.get("due_at") or arguments["due_at"])
    due = datetime.fromisoformat(due_at).astimezone(SHANGHAI)
    lines = [f"已记下：{content}", f"提醒时间：{due.year}年{due.month}月{due.day}日 {due:%H:%M}"]
    if document.get("idempotent_replay") is True:
        lines.append("这条微信已处理过，没有重复创建。")
    return "\n".join(lines)


def direct_memo_list_arguments(payload: dict[str, Any]) -> dict[str, Any] | None:
    """Parse explicit bounded memo-list commands without consulting Thread history."""

    text = payload.get("text")
    if not isinstance(text, str) or payload.get("attachments") != []:
        return None
    normalized = re.sub(r"\s+", "", text.strip())
    if MEMO_LIST_PENDING_RE.fullmatch(normalized):
        return {"status": "pending", "limit": 20}
    if MEMO_LIST_TODAY_RE.fullmatch(normalized):
        return {"status": "pending", "date": "today", "limit": 20}
    if MEMO_LIST_OVERDUE_RE.fullmatch(normalized):
        return {"status": "pending", "overdue": True, "limit": 20}
    return None


def direct_memo_complete_query(payload: dict[str, Any]) -> str | None:
    """Extract an explicit completion target while rejecting questions and attachments."""

    text = payload.get("text")
    if not isinstance(text, str) or payload.get("attachments") != []:
        return None
    normalized = text.strip()
    if not normalized or normalized.endswith(("?", "？")):
        return None
    match = MEMO_COMPLETE_PREFIX_RE.fullmatch(normalized) or MEMO_COMPLETE_SUFFIX_RE.fullmatch(normalized)
    if match is None:
        return None
    query = match.group("query").strip(" ，,。.;；:：\t")
    return query or None


def _memo_records(result: dict[str, Any]) -> list[dict[str, Any]]:
    document = result.get("result")
    records = document.get("items") if isinstance(document, dict) else document
    if not isinstance(records, list) or any(not isinstance(record, dict) for record in records):
        raise ToolProxyError("memo_response_invalid", "家庭备忘录查询响应无效")
    if isinstance(document, dict) and document.get("count") not in {None, len(records)}:
        raise ToolProxyError("memo_response_invalid", "家庭备忘录查询数量无效")
    return records


def _memo_display_content(record: dict[str, Any]) -> str:
    return str(record.get("content") or record.get("title") or "未命名事项")


def _memo_due_label(record: dict[str, Any]) -> str | None:
    due_at = record.get("due_at")
    if not isinstance(due_at, str) or not due_at:
        return None
    try:
        due = datetime.fromisoformat(due_at).astimezone(SHANGHAI)
    except ValueError:
        return None
    return f"{due.month}月{due.day}日 {due:%H:%M}"


def memo_list_result_text(result: dict[str, Any]) -> str:
    records = _memo_records(result)
    if not records:
        return "没有找到符合条件的备忘录。"
    lines = [f"找到 {len(records)} 条备忘录："]
    for index, record in enumerate(records, start=1):
        content = _memo_display_content(record)
        due = _memo_due_label(record)
        lines.append(f"{index}. {content}" + (f"（{due}）" if due else ""))
    return "\n".join(lines)


def _memo_match_key(value: Any) -> str:
    return re.sub(r"[\s，,。.;；:：]", "", str(value or "")).casefold()


def matching_pending_memos(records: list[dict[str, Any]], query: str) -> list[dict[str, Any]]:
    query_key = _memo_match_key(query)
    exact = [
        record
        for record in records
        if query_key
        and query_key
        in {
            _memo_match_key(record.get("content")),
            _memo_match_key(record.get("title")),
        }
    ]
    if exact:
        return exact
    return [
        record
        for record in records
        if query_key
        and any(
            query_key in candidate or candidate in query_key
            for candidate in (
                _memo_match_key(record.get("content")),
                _memo_match_key(record.get("title")),
            )
            if candidate
        )
    ]


def memo_complete_result_text(result: dict[str, Any], fallback: dict[str, Any]) -> str:
    document = result.get("result") if isinstance(result.get("result"), dict) else {}
    memo = document.get("memo") if isinstance(document.get("memo"), dict) else {}
    return f"已完成：{_memo_display_content(memo or fallback)}"


class ControllerService:
    AUTH_MODES = {"chatgpt_device_code", "api_key"}

    def __init__(
        self,
        store: ControllerStore,
        app_server: AppServerClient,
        *,
        intake_enabled: bool,
        auth_mode: str = "chatgpt_device_code",
        api_key: str = "",
        api_base_mode: str = "official",
        codex_model_mode: str = "default",
        turn_media: TurnMediaManager | None = None,
        tool_context: Any | None = None,
        runner_manager: RunnerManagerService | None = None,
        desktop_controller: DesktopControllerService | None = None,
    ):
        if auth_mode not in self.AUTH_MODES:
            raise ValueError("Controller auth_mode 不受支持")
        if api_base_mode not in {"official", "custom"}:
            raise ValueError("Controller api_base_mode 不受支持")
        if codex_model_mode not in {"default", "custom"}:
            raise ValueError("Controller codex_model_mode 不受支持")
        self.store = store
        self.app_server = app_server
        self.configured_intake_enabled = intake_enabled
        self.configured_auth_mode = auth_mode
        self._api_key = api_key if auth_mode == "api_key" else ""
        self.api_base_mode = api_base_mode
        self.codex_model_mode = codex_model_mode
        self.turn_media = turn_media
        self.tool_context = tool_context
        self.runner_manager = runner_manager
        self.desktop_controller = desktop_controller
        self.auth_error: str | None = None
        self.pending_login: dict[str, Any] | None = None
        self.start_error: str | None = None
        self._stop = threading.Event()
        self._scheduler: threading.Thread | None = None
        self._pending_turn_events: dict[str, list[dict[str, Any]]] = {}
        self._event_lock = threading.Lock()
        self.app_server.notification_handler = self.handle_notification

    def start(self) -> None:
        self.store.recover_running()
        try:
            self.app_server.start()
            if hasattr(self.app_server, "refresh_mcp_catalog"):
                expected_tools = (
                    self.tool_context.available_tools("owner_legacy")
                    if self.tool_context is not None
                    and hasattr(self.tool_context, "available_tools")
                    else []
                )
                self.app_server.refresh_mcp_catalog(expected_tools)
        except AppServerError as exc:
            self.start_error = exc.code
        else:
            self._reconcile_initial_auth()
        self._scheduler = threading.Thread(target=self._scheduler_loop, name="codex-controller-scheduler", daemon=True)
        self._scheduler.start()
        if self.runner_manager is not None:
            self.runner_manager.start()

    def stop(self) -> None:
        self._stop.set()
        if self.runner_manager is not None:
            self.runner_manager.stop()
        self.app_server.stop()

    def submit(self, payload: dict[str, Any]) -> dict[str, Any]:
        if not self.intake_enabled:
            raise StoreError("intake_disabled", "正式任务入口尚未启用", status=409)
        return self.store.public_job(self.store.create_job(payload))

    def capabilities(self) -> dict[str, Any]:
        return {
            "capabilities": [
                "job_capability_profile_v1",
                "thread_short_v1",
                "mcp_tool_policy_v1",
                "job_artifacts_v1",
                "runner_manager_v2",
                "desktop_takeover_v1",
            ],
        }

    def tool_status(self) -> dict[str, Any]:
        if (
            self.tool_context is not None
            and getattr(self.tool_context, "store", None) is not None
            and hasattr(self.tool_context, "tool_status")
        ):
            return self.tool_context.tool_status()
        configured = (
            self.tool_context.configured_tools()
            if self.tool_context is not None and hasattr(self.tool_context, "configured_tools")
            else frozenset()
        )
        callable_names = (
            self.tool_context.route_ready_tools()
            if self.tool_context is not None and hasattr(self.tool_context, "route_ready_tools")
            else configured
        )
        return self.store.tool_control_document(configured, callable_names)

    def update_tool_policy(
        self,
        tool_name: str,
        *,
        enabled: bool,
        revision: int,
        request_id: str,
    ) -> dict[str, Any]:
        if (
            self.tool_context is not None
            and getattr(self.tool_context, "store", None) is not None
            and hasattr(self.tool_context, "update_tool_policy")
        ):
            return self.tool_context.update_tool_policy(
                tool_name,
                enabled=enabled,
                revision=revision,
                request_id=request_id,
            )
        return self.store.update_tool_policy(
            tool_name,
            enabled=enabled,
            revision=revision,
            request_id=request_id,
        )

    def begin_device_login(self) -> dict[str, Any]:
        if self.configured_auth_mode != "chatgpt_device_code":
            raise AppServerError("auth_mode_rejected", "当前 options 未选择设备码登录", definitive=True)
        login = self.app_server.start_device_login()
        self.pending_login = login
        self.auth_error = None
        return login

    def begin_api_key_login(self) -> dict[str, Any]:
        if self.configured_auth_mode != "api_key":
            raise AppServerError("auth_mode_rejected", "当前 options 未选择 API Key 登录", definitive=True)
        if not self._api_key:
            self.auth_error = "api_key_missing"
            raise AppServerError("api_key_missing", "请先在 Add-on options 中配置 API Key", definitive=True)
        try:
            result = self.app_server.start_api_key_login(self._api_key)
        except AppServerError as exc:
            self.auth_error = exc.code
            raise
        self.auth_error = None
        self.pending_login = None
        return result

    def cancel_device_login(self) -> dict[str, Any]:
        if self.pending_login is None:
            raise AppServerError("login_not_pending", "没有待完成的设备码登录", definitive=True)
        login_id = self.pending_login["loginId"]
        self.app_server.cancel_login(login_id)
        self.pending_login = None
        return {"cancelled": True}

    def logout(self) -> dict[str, Any]:
        self.app_server.logout()
        self.pending_login = None
        return {"logged_out": True}

    def status(self) -> dict[str, Any]:
        app = self.app_server.status()
        tool_status = self.tool_status()
        effective_names = [tool["name"] for tool in tool_status["tools"] if tool["callable"]]
        if self._account_matches(app):
            self.pending_login = None
        return {
            "version": "0.5.18",
            "source_identity": runtime_source_identity(),
            "codex_version": "0.146.0",
            "configured_auth_mode": self.configured_auth_mode,
            "api_key_configured": bool(self._api_key),
            "api_base_mode": self.api_base_mode,
            "api_base_configured": self.api_base_mode == "custom",
            "api_base_error": None,
            "codex_model_mode": self.codex_model_mode,
            "auth_error": self.auth_error,
            "intake_configured": self.configured_intake_enabled,
            "intake_enabled": self._intake_enabled(app),
            "ready": bool(
                self._app_server_operational(app)
                and self._account_matches(app)
                and self.start_error is None
                and self.auth_error is None
            ),
            "start_error": self.start_error,
            "app_server": app,
            "tools": {
                **tool_status["summary"],
                "count": len(effective_names),
                "names": effective_names,
                "renovation_hub": any(
                    name.startswith("ledger_") or name.startswith("renovation_")
                    for name in effective_names
                ),
                "operations": any(name.startswith("ha_operations_") for name in effective_names),
                "mcp": tool_status["mcp"],
                "policy_error": tool_status["policy_error"],
                "hub_manifest": tool_status.get("hub_manifest"),
            },
            "pending_login": self.pending_login,
            "queue": self.store.status(),
            "runner_manager": (
                self.runner_manager.status()
                if self.runner_manager is not None
                else {
                    "enabled": False,
                    "relay_configured": False,
                    "installer": {
                        "ready": False,
                        "error_code": "runner_manager_unavailable",
                        "runner_version": "0.3.6",
                    },
                    "last_error": None,
                    "summary": {
                        "total": 0,
                        "enabled": 0,
                        "online": 0,
                        "busy": 0,
                        "recovery_required": 0,
                    },
                }
            ),
            "desktop_takeover": (
                self.desktop_controller.hosts()
                if self.desktop_controller is not None
                else {
                    "hosts": [],
                    "relay_configured": False,
                    "server_time": datetime.now(SHANGHAI).isoformat(),
                }
            ),
        }

    def handle_notification(self, message: dict[str, Any], *, allow_buffer: bool = True) -> None:
        method = message.get("method")
        params = message.get("params") if isinstance(message.get("params"), dict) else {}
        if method == "error":
            turn_id = params.get("turnId")
            if isinstance(turn_id, str):
                classification = classify_turn_error(params.get("error"))
                handled = self.store.observe_turn_error(
                    turn_id,
                    error_type=classification.error_type,
                    error_code=classification.error_code,
                    upstream_http_status=classification.upstream_http_status,
                    retryable=classification.retryable,
                    will_retry=params.get("willRetry") is True,
                )
                if not handled and allow_buffer:
                    self._buffer_event(turn_id, message)
        elif method == "item/agentMessage/delta":
            turn_id = params.get("turnId")
            delta = params.get("delta")
            if isinstance(turn_id, str) and isinstance(delta, str) and delta:
                handled = self.store.observe_turn_activity(
                    turn_id,
                    output_observed=True,
                    item_type="agentMessage",
                )
                if not handled and allow_buffer:
                    self._buffer_event(turn_id, message)
        elif method in {"item/started", "item/completed"}:
            item = params.get("item") if isinstance(params.get("item"), dict) else {}
            turn_id = params.get("turnId")
            if isinstance(turn_id, str):
                output, activity, artifact, item_type = item_observations(item)
                handled = False
                relevant = output or activity or artifact
                if (
                    method == "item/completed"
                    and item.get("type") == "agentMessage"
                    and isinstance(item.get("text"), str)
                ):
                    relevant = True
                    handled = self.store.set_result_text(turn_id, item["text"], item_type="agentMessage")
                if output or activity or artifact:
                    handled = self.store.observe_turn_activity(
                        turn_id,
                        output_observed=output,
                        tool_activity_observed=activity,
                        artifact_observed=artifact,
                        item_type=item_type,
                    ) or handled
                if relevant and not handled and allow_buffer:
                    self._buffer_event(turn_id, message)
        elif method == "turn/completed":
            turn = params.get("turn") if isinstance(params.get("turn"), dict) else {}
            turn_id = turn.get("id") or params.get("turnId")
            status = turn.get("status")
            if isinstance(turn_id, str) and isinstance(status, str):
                if allow_buffer:
                    with self._event_lock:
                        pending = self._pending_turn_events.get(turn_id)
                        if pending:
                            if len(pending) < 32:
                                pending.append(message)
                            return
                if self.tool_context is not None:
                    self.tool_context.end_turn(turn_id)
                if self.turn_media is not None:
                    self.turn_media.cleanup_turn(turn_id)
                error = turn.get("error") if isinstance(turn.get("error"), dict) else {}
                classification = (
                    classify_turn_error(error)
                    if error and error.get("codexErrorInfo") is not None
                    else None
                )
                output, activity, artifact = turn_observations(turn.get("items"))
                attempt = self.store.turn_attempt(turn_id) or 1
                handled = self.store.complete_turn(
                    turn_id,
                    status,
                    error_code=None if classification is None else classification.error_code,
                    error_type=None if classification is None else classification.error_type,
                    upstream_http_status=(
                        None if classification is None else classification.upstream_http_status
                    ),
                    retryable=None if classification is None else classification.retryable,
                    retry_delay_seconds=retry_delay_seconds(attempt) if status == "failed" else None,
                    output_observed=output,
                    tool_activity_observed=activity,
                    artifact_observed=artifact,
                )
                if not handled and allow_buffer:
                    self._buffer_event(turn_id, message)
        elif method == "account/updated":
            expected = "chatgpt" if self.configured_auth_mode == "chatgpt_device_code" else "apikey"
            actual = params.get("authMode")
            if actual is None:
                self.auth_error = None
            elif actual != expected:
                self.auth_error = "auth_mode_mismatch"
            else:
                self.auth_error = None

    def _scheduler_loop(self) -> None:
        next_artifact_cleanup = 0.0
        next_desktop_sweep = 0.0
        while not self._stop.wait(0.5):
            if self.desktop_controller is not None and time.monotonic() >= next_desktop_sweep:
                try:
                    self.desktop_controller.sweep()
                except StoreError:
                    pass
                next_desktop_sweep = time.monotonic() + 5
            if time.monotonic() >= next_artifact_cleanup:
                try:
                    self.store.cleanup_artifacts()
                except (StoreError, OSError):
                    pass
                next_artifact_cleanup = time.monotonic() + 60
            if not self.intake_enabled or self.start_error is not None:
                continue
            job = self.store.claim_next()
            if job is None:
                continue
            self._dispatch(job)

    def _dispatch(self, job: dict[str, Any]) -> None:
        turn_started = False
        try:
            payload = job["input"]
            capability_profile = payload.get("capability_profile", "owner_legacy")
            media_archive_authorized = has_explicit_media_archive_intent(
                payload.get("text", ""), payload.get("attachments")
            )
            effective_tools = (
                self.tool_context.available_tools(
                    capability_profile,
                    media_archive_authorized=media_archive_authorized,
                )
                if self.tool_context is not None
                else []
            )
            if hasattr(self.app_server, "configure_developer_context"):
                if (
                    getattr(self.app_server, "supports_dynamic_tool_definitions", False)
                    and self.tool_context is not None
                    and hasattr(self.tool_context, "tool_definitions_by_name")
                ):
                    self.app_server.configure_developer_context(
                        effective_tools,
                        capability_profile,
                        self.tool_context.tool_definitions_by_name(),
                    )
                else:
                    self.app_server.configure_developer_context(effective_tools, capability_profile)
            if is_new_thread_command(payload):
                thread_id = self.app_server.start_thread()
                thread_short = self.store.short_id("TH", thread_id)
                result = f"{NEW_THREAD_RESULT}\nThread：{thread_short}"
                self.store.complete_new_thread(job["job_id"], thread_id, result)
                return
            if self.tool_context is not None:
                self.tool_context.begin_job(
                    job["job_id"],
                    payload["message_id"],
                    capability_profile,
                    media_archive_authorized=media_archive_authorized,
                )
            direct_memo = direct_memo_create_arguments(payload)
            if (
                direct_memo is not None
                and self.tool_context is not None
                and "memo_create" in effective_tools
                and capability_profile in {"owner", "owner_legacy"}
            ):
                result = self.tool_context.call("memo_create", direct_memo)
                self.store.complete_direct_result(
                    job["job_id"],
                    memo_create_result_text(result, direct_memo),
                    item_type="memoCreate",
                )
                return
            direct_memo_list = direct_memo_list_arguments(payload)
            if (
                direct_memo_list is not None
                and self.tool_context is not None
                and "memo_list" in effective_tools
            ):
                result = self.tool_context.call("memo_list", direct_memo_list)
                self.store.complete_direct_result(
                    job["job_id"],
                    memo_list_result_text(result),
                    item_type="memoList",
                )
                return
            direct_complete = direct_memo_complete_query(payload)
            if (
                direct_complete is not None
                and self.tool_context is not None
                and {"memo_list", "memo_complete"}.issubset(effective_tools)
                and capability_profile in {"owner", "owner_legacy"}
            ):
                listed = self.tool_context.call("memo_list", {"status": "pending", "limit": 100})
                matches = matching_pending_memos(_memo_records(listed), direct_complete)
                if not matches:
                    text = f"没有找到未完成的备忘录：{direct_complete}。"
                    item_type = "memoCompleteLookup"
                elif len(matches) > 1:
                    text = f"找到多条未完成备忘录与“{direct_complete}”匹配，请说得更具体。"
                    item_type = "memoCompleteDisambiguation"
                else:
                    memo = matches[0]
                    completed = self.tool_context.call("memo_complete", {"id": memo.get("id")})
                    text = memo_complete_result_text(completed, memo)
                    item_type = "memoComplete"
                self.store.complete_direct_result(job["job_id"], text, item_type=item_type)
                return
            thread_id = self.store.conversation_thread(job["conversation_key"])
            if thread_id is None:
                thread_id = self.app_server.start_thread()
                self.store.assign_thread(job["job_id"], thread_id)
            else:
                loaded_thread_id = self.app_server.resume_thread(thread_id)
                if isinstance(loaded_thread_id, str) and loaded_thread_id:
                    thread_id = loaded_thread_id
                self.store.assign_thread(job["job_id"], thread_id)
            input_items = None
            if self.turn_media is not None:
                input_items = self.turn_media.prepare(job["job_id"], payload)
            turn_id = self.app_server.start_turn(
                thread_id,
                payload["text"],
                job["message_id"],
                input_items=input_items,
            )
            if self.tool_context is not None:
                self.tool_context.bind_turn(job["job_id"], turn_id)
            turn_started = True
            if self.turn_media is not None:
                self.turn_media.bind_turn(job["job_id"], turn_id)
            self.store.assign_turn(job["job_id"], turn_id)
            self._flush_turn_events(turn_id)
        except AppServerError as exc:
            if (
                exc.code == "app_server_overloaded"
                and exc.definitive
                and self.store.retry_overloaded(
                    job["job_id"],
                    retry_delay_seconds=retry_delay_seconds(job["attempt"]),
                )
            ):
                return
            self.store.fail_claimed(job["job_id"], exc.code, uncertain=not exc.definitive)
        except StoreError as exc:
            self.store.fail_claimed(job["job_id"], exc.code, uncertain=True)
        except MediaInputError as exc:
            self.store.fail_claimed(job["job_id"], exc.code, uncertain=False)
        except ToolProxyError as exc:
            self.store.fail_claimed(job["job_id"], exc.code, uncertain=False)
        except Exception:
            self.store.fail_claimed(job["job_id"], "controller_internal_error", uncertain=True)
        finally:
            if not turn_started:
                if self.tool_context is not None:
                    self.tool_context.clear_job(job["job_id"])
                if self.turn_media is not None:
                    self.turn_media.cleanup_job(job["job_id"])

    def _buffer_event(self, turn_id: str, message: dict[str, Any]) -> None:
        with self._event_lock:
            events = self._pending_turn_events.setdefault(turn_id, [])
            if len(events) < 32:
                events.append(message)

    def _flush_turn_events(self, turn_id: str) -> None:
        with self._event_lock:
            events = self._pending_turn_events.pop(turn_id, [])
        events.sort(key=lambda message: message.get("method") == "turn/completed")
        for message in events:
            self.handle_notification(message, allow_buffer=False)

    @property
    def intake_enabled(self) -> bool:
        return self._intake_enabled()

    def watchdog_healthy(self, app_status: dict[str, Any] | None = None) -> bool:
        return bool(self.start_error is None and self._app_server_operational(app_status))

    def _intake_enabled(self, app_status: dict[str, Any] | None = None) -> bool:
        return bool(
            self.configured_intake_enabled
            and self._app_server_operational(app_status)
            and self.app_server.account_ready
            and self._account_matches(app_status)
            and self.auth_error is None
        )

    def _app_server_operational(self, app_status: dict[str, Any] | None = None) -> bool:
        app = self.app_server.status() if app_status is None else app_status
        return bool(
            app.get("running") is True
            and app.get("initialized") is True
            and app.get("protocol_error") is None
        )

    def _expected_account_type(self) -> str:
        return "chatgpt" if self.configured_auth_mode == "chatgpt_device_code" else "apiKey"

    def _account_matches(self, app_status: dict[str, Any] | None = None) -> bool:
        if app_status is None:
            actual = getattr(self.app_server, "auth_mode", None)
        else:
            account = app_status.get("account") if isinstance(app_status, dict) else None
            actual = account.get("auth_mode") if isinstance(account, dict) else None
        return actual == self._expected_account_type()

    def _reconcile_initial_auth(self) -> None:
        if self.configured_auth_mode == "chatgpt_device_code":
            if self.app_server.account_ready and not self._account_matches():
                self.auth_error = "auth_mode_mismatch"
            return
        if not self._api_key:
            self.auth_error = "api_key_missing"
            return
        if self.app_server.account_ready:
            self.auth_error = None if self._account_matches() else "auth_mode_mismatch"
            return
        try:
            self.app_server.start_api_key_login(self._api_key)
        except AppServerError as exc:
            self.auth_error = exc.code
        else:
            self.auth_error = None
