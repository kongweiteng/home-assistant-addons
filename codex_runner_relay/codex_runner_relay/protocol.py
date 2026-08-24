"""Strict public WebSocket and internal publish contracts."""

from __future__ import annotations

import json
import re
from typing import Any, Mapping


RUNNER_ID_RE = re.compile(r"^RN-[A-Z2-7]{20,32}$")
CREDENTIAL_RE = re.compile(r"^[A-Za-z0-9_-]{32,512}$")
EVENT_TYPES = frozenset(
    {
        "heartbeat",
        "status",
        "result",
        "desktop_snapshot",
        "desktop_event",
        "desktop_receipt",
    }
)
PUBLISH_TYPES = frozenset({"request", "control", "desktop_command"})


class RelayProtocolError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def validate_runner_id(value: Any) -> str:
    if not isinstance(value, str) or not RUNNER_ID_RE.fullmatch(value):
        raise RelayProtocolError("runner_id_invalid", "Runner ID 无效")
    return value


def validate_credential(value: Any, *, name: str) -> str:
    if not isinstance(value, str) or not CREDENTIAL_RE.fullmatch(value):
        raise RelayProtocolError("credential_invalid", f"{name} 无效")
    return value


def validate_first_message(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise RelayProtocolError("message_invalid", "首帧必须是 JSON object")
    message = dict(value)
    message_type = message.get("type")
    if message_type == "authenticate":
        if set(message) != {"type", "runner_id", "credential"}:
            raise RelayProtocolError("message_fields_invalid", "认证首帧字段无效")
        return {
            "type": "authenticate",
            "runner_id": validate_runner_id(message.get("runner_id")),
            "credential": validate_credential(message.get("credential"), name="Runner credential"),
        }
    if message_type == "enroll":
        if set(message) != {"type", "runner_id", "token", "payload"}:
            raise RelayProtocolError("message_fields_invalid", "注册首帧字段无效")
        runner_id = validate_runner_id(message.get("runner_id"))
        token = validate_credential(message.get("token"), name="Enrollment token")
        payload = message.get("payload")
        if not isinstance(payload, Mapping) or payload.get("runner_id") != runner_id:
            raise RelayProtocolError("enrollment_payload_invalid", "注册 payload 与 Runner 不匹配")
        return {"type": "enroll", "runner_id": runner_id, "token": token, "payload": dict(payload)}
    raise RelayProtocolError("first_message_required", "首帧必须完成注册或认证")


def validate_event_message(value: Any, *, runner_id: str) -> tuple[str, dict[str, Any]]:
    if not isinstance(value, Mapping) or set(value) != {"type", "event_type", "document"}:
        raise RelayProtocolError("message_fields_invalid", "Runner event 字段无效")
    if value.get("type") != "event" or value.get("event_type") not in EVENT_TYPES:
        raise RelayProtocolError("event_type_invalid", "Runner event 类型无效")
    document = value.get("document")
    if not isinstance(document, Mapping):
        raise RelayProtocolError("event_document_invalid", "Runner event document 无效")
    document_copy = dict(document)
    if document_copy.get("runner_id") != runner_id or document_copy.get("message_type") != value["event_type"]:
        raise RelayProtocolError("runner_binding_mismatch", "Runner event 越过连接绑定")
    return str(value["event_type"]), document_copy


def validate_publish(kind: Any, runner_id: Any, value: Any) -> dict[str, Any]:
    bound = validate_runner_id(runner_id)
    if kind not in PUBLISH_TYPES:
        raise RelayProtocolError("publish_type_invalid", "发布类型无效")
    if not isinstance(value, Mapping) or set(value) != {"document"} or not isinstance(value.get("document"), Mapping):
        raise RelayProtocolError("publish_document_invalid", "发布 document 无效")
    document = dict(value["document"])
    if document.get("runner_id") != bound or document.get("message_type") != kind:
        raise RelayProtocolError("runner_binding_mismatch", "Controller 发布越过 Runner 绑定")
    return document


def json_size(value: Any) -> int:
    return len(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8"))


__all__ = [
    "EVENT_TYPES",
    "PUBLISH_TYPES",
    "RelayProtocolError",
    "json_size",
    "validate_event_message",
    "validate_first_message",
    "validate_publish",
    "validate_runner_id",
]
