"""Fixed M8 prepare-car intent and Home Assistant API boundary."""

from __future__ import annotations

import json
import re
from typing import Any, Callable, Optional
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


PREPARE_CAR_ENTITY_ID = "switch.wen_jie_m8zeng_cheng_max"
HOME_ASSISTANT_API_BASE = "http://supervisor/core/api"
PREPARE_CAR_CONFIRMATION_TTL_SECONDS = 120
PREPARE_CAR_TOOL_NAMES = frozenset(
    {
        "aito_prepare_car_status",
        "aito_prepare_car_request",
        "aito_prepare_car_execute",
    }
)

_STATUS_COMMANDS = frozenset(
    {
        "备车状态",
        "查看备车状态",
        "查询备车状态",
        "现在备车了吗",
        "备车开了吗",
    }
)
_ENABLE_REQUEST_COMMANDS = frozenset({"备车", "开始备车", "开启备车", "打开备车"})
_DISABLE_REQUEST_COMMANDS = frozenset({"停止备车", "关闭备车", "结束备车"})
_ENABLE_CONFIRM_COMMANDS = frozenset({"确认备车", "确认开始备车", "确认开启备车"})
_DISABLE_CONFIRM_COMMANDS = frozenset({"确认停止备车", "确认关闭备车", "确认结束备车"})

SAFE_ENTITY_ATTRIBUTES = (
    "command_state",
    "command_action",
    "command_requested_at",
    "command_confirmed_at",
    "command_error_code",
    "command_cooldown_remaining_seconds",
    "command_readback_attempts",
    "plan_status",
)


class HomeAssistantPrepareCarError(RuntimeError):
    """A fixed HA Core request failed without exposing credentials or internals."""

    def __init__(self, code: str, *, outcome_unknown: bool = False) -> None:
        super().__init__(code)
        self.code = code
        self.outcome_unknown = outcome_unknown


def prepare_car_intent(payload: dict[str, Any]) -> tuple[str, bool | None] | None:
    """Recognize exact, attachment-free prepare-car commands."""

    text = payload.get("text")
    if not isinstance(text, str) or payload.get("attachments") != []:
        return None
    normalized = re.sub(r"[\s，,。.!！?？;；:：]+", "", text.strip())
    for prefix in ("请帮我", "帮我", "请问", "请"):
        if normalized.startswith(prefix):
            normalized = normalized[len(prefix) :]
            break
    if normalized in _STATUS_COMMANDS:
        return ("status", None)
    if normalized in _ENABLE_REQUEST_COMMANDS:
        return ("request", True)
    if normalized in _DISABLE_REQUEST_COMMANDS:
        return ("request", False)
    if normalized in _ENABLE_CONFIRM_COMMANDS:
        return ("execute", True)
    if normalized in _DISABLE_CONFIRM_COMMANDS:
        return ("execute", False)
    return None


def action_label(target_on: bool) -> str:
    return "开始备车" if target_on else "停止备车"


def confirmation_phrase(target_on: bool) -> str:
    return "确认备车" if target_on else "确认停止备车"


def request_result_text(result: dict[str, Any], target_on: bool) -> str:
    if result.get("status") == "pending_confirmation":
        replay = "\n这条微信已处理过，没有重复创建确认。" if result.get("idempotent_replay") else ""
        return (
            f"准备{action_label(target_on)}。请在 2 分钟内回复“{confirmation_phrase(target_on)}”。\n"
            "发送任何其他消息会自动取消本次确认。"
            f"{replay}"
        )
    return f"无法创建备车确认：{result.get('error_code', 'REQUEST_FAILED')}。"


def execute_result_text(result: dict[str, Any], target_on: bool) -> str:
    status = result.get("status")
    if status == "submitted":
        replay = " 这条确认已处理过，没有重复调用车辆。" if result.get("idempotent_replay") else ""
        return (
            f"已向 Home Assistant 提交{action_label(target_on)}请求。"
            "服务受理不代表车辆已完成，请以 AITO 回读的 confirmed 状态为准。"
            f"{replay}"
        )
    if status == "already_confirmed":
        return f"当前 AITO 回读已经确认处于“{action_label(target_on)}”目标状态，没有重复发送命令。"
    if status == "unknown":
        return "备车请求结果未知，为避免重复控制车辆不会自动重试；请稍后查询备车状态。"
    messages = {
        "CONFIRMATION_MISSING": "当前没有等待确认的备车请求，请先发送“备车”或“停止备车”。",
        "CONFIRMATION_EXPIRED": "备车确认已过期，请重新发送备车请求。",
        "CONFIRMATION_ACTION_MISMATCH": "确认动作与待处理备车请求不一致，已拒绝执行。",
        "HA_ENTITY_UNAVAILABLE": "AITO 备车实体当前不可用，未发送车辆命令。",
        "HA_API_UNAVAILABLE": "Home Assistant 备车接口当前不可用，未发送车辆命令。",
    }
    code = str(result.get("error_code") or "EXECUTION_FAILED")
    return messages.get(code, f"备车请求未执行：{code}。")


def status_result_text(result: dict[str, Any]) -> str:
    if result.get("status") != "available":
        return "AITO 备车状态当前不可用。"
    entity_state = result.get("entity_state")
    state_label = {"on": "备车已开启", "off": "备车已停止"}.get(entity_state, "备车状态未知")
    command_state = result.get("command_state") or "idle"
    error_code = result.get("command_error_code")
    text = f"{state_label}；命令状态：{command_state}。"
    if error_code:
        text += f" 最近错误：{error_code}。"
    return text


def safe_entity_state(document: Any) -> dict[str, Any]:
    if not isinstance(document, dict):
        raise HomeAssistantPrepareCarError("HA_RESPONSE_INVALID")
    if document.get("entity_id") != PREPARE_CAR_ENTITY_ID:
        raise HomeAssistantPrepareCarError("HA_ENTITY_MISMATCH")
    state = document.get("state")
    if state not in {"on", "off", "unavailable", "unknown"}:
        raise HomeAssistantPrepareCarError("HA_STATE_INVALID")
    attributes = document.get("attributes")
    if not isinstance(attributes, dict):
        attributes = {}
    return {
        "status": "available" if state in {"on", "off"} else "unavailable",
        "entity_state": state,
        **{key: attributes.get(key) for key in SAFE_ENTITY_ATTRIBUTES},
    }


def request_home_assistant_json(
    method: str,
    path: str,
    token: str,
    payload: dict[str, Any] | None,
) -> Any:
    """Call one fixed HA Core endpoint using the runtime Supervisor token."""

    if not isinstance(token, str) or len(token) < 32:
        raise HomeAssistantPrepareCarError("HA_API_UNAVAILABLE")
    if path not in {
        f"/states/{PREPARE_CAR_ENTITY_ID}",
        "/services/switch/turn_on",
        "/services/switch/turn_off",
    }:
        raise HomeAssistantPrepareCarError("HA_PATH_REJECTED")
    if method == "GET":
        if payload is not None or not path.startswith("/states/"):
            raise HomeAssistantPrepareCarError("HA_REQUEST_REJECTED")
        body = None
    elif method == "POST":
        if payload != {"entity_id": PREPARE_CAR_ENTITY_ID} or not path.startswith("/services/switch/"):
            raise HomeAssistantPrepareCarError("HA_REQUEST_REJECTED")
        body = json.dumps(payload, ensure_ascii=True, separators=(",", ":")).encode("utf-8")
    else:
        raise HomeAssistantPrepareCarError("HA_METHOD_REJECTED")
    request = Request(
        f"{HOME_ASSISTANT_API_BASE}{path}",
        data=body,
        method=method,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
            "Content-Type": "application/json",
        },
    )
    try:
        with urlopen(request, timeout=15) as response:
            data = response.read(2 * 1024 * 1024 + 1)
    except HTTPError as exc:
        exc.close()
        raise HomeAssistantPrepareCarError(f"HA_HTTP_{exc.code}") from exc
    except (URLError, TimeoutError, OSError) as exc:
        raise HomeAssistantPrepareCarError(
            "HA_API_UNAVAILABLE",
            outcome_unknown=method == "POST",
        ) from exc
    if len(data) > 2 * 1024 * 1024:
        raise HomeAssistantPrepareCarError("HA_RESPONSE_TOO_LARGE")
    try:
        return json.loads(data)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise HomeAssistantPrepareCarError(
            "HA_RESPONSE_INVALID",
            outcome_unknown=method == "POST",
        ) from exc


HomeAssistantRequest = Callable[[str, str, str, Optional[dict[str, Any]]], Any]
