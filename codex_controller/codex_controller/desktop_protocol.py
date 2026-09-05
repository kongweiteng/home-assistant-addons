"""Strict ref-only protocol for Codex Desktop takeover messages."""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import re
from typing import Any, Mapping
from zoneinfo import ZoneInfo


DESKTOP_PROTOCOL_VERSION = 1
MAX_DOCUMENT_BYTES = 256 * 1024
SHANGHAI = ZoneInfo("Asia/Shanghai")
RUNNER_RE = re.compile(r"^RN-[A-Z2-7]{20,32}$")
REQUEST_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
REF_RE = re.compile(r"^(HS|PJ|TH|TR|QS)-[A-Z2-7]{20,52}$")
DIGEST_RE = re.compile(r"^sha256:[a-f0-9]{64}$")
MODEL_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,63}$")
UUID_RE = re.compile(
    r"(?i)\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b"
)
PRIVATE_PATH_RE = re.compile(
    r"(?i)(?:^|[\s\"'])"
    r"(?:/(?:Users|private|var|etc|opt|Applications)/[^\s\"']+|[A-Z]:\\Users\\[^\s\"']+)"
)
SECRET_RE = re.compile(
    r"(?i)\b(?:token|password|passwd|secret|authorization|cookie|api[_-]?key)\s*[:=]\s*[^\s,;]+"
)
FORBIDDEN_PUBLIC_KEYS = frozenset(
    {
        "thread_id",
        "turn_id",
        "conversation_id",
        "conversationId",
        "expectedTurnId",
        "cwd",
        "path",
        "absolute_path",
        "socket",
        "credential",
        "token",
        "cookie",
        "private_key",
        "reasoning",
        "prompt",
        "system_prompt",
        "developer_instructions",
    }
)
EVENT_KINDS = frozenset(
    {
        "thread.discovered",
        "thread.updated",
        "thread.archived",
        "turn.started",
        "turn.completed",
        "turn.interrupted",
        "turn.failed",
        "user.message",
        "assistant.delta",
        "assistant.completed",
        "plan.updated",
        "command.started",
        "command.output",
        "command.completed",
        "file.changed",
        "file.patch",
        "reasoning.summary",
        "awaiting.input",
        "recovery.required",
        "protocol.degraded",
    }
)
THREAD_STATUSES = frozenset(
    {
        "active",
        "idle",
        "notLoaded",
        "archived",
        "failed",
        "recovery_required",
        "protocol_degraded",
    }
)
RECEIPT_STATES = frozenset(
    {"accepted", "confirmed", "conflict", "expired", "failed", "unknown", "recovery_required"}
)
ACTIONS = frozenset(
    {
        "read",
        "load",
        "steer",
        "interrupt",
        "continue",
        "archive",
        "unarchive",
        "create",
        "queue_add",
        "queue_update",
        "queue_delete",
        "queue_reorder",
        "queue_start",
    }
)


class DesktopProtocolError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def canonical_json(value: Mapping[str, Any]) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    except (TypeError, ValueError) as exc:
        raise DesktopProtocolError("desktop_payload_invalid", "Desktop 文档不是有效 JSON") from exc


def body_digest(value: Mapping[str, Any]) -> str:
    document = dict(value)
    document.pop("body_digest", None)
    return "sha256:" + hashlib.sha256(canonical_json(document).encode("utf-8")).hexdigest()


def intent_digest(value: Mapping[str, Any]) -> str:
    return "sha256:" + hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def validate_desktop_document(message_type: str, value: Mapping[str, Any]) -> dict[str, Any]:
    if message_type == "desktop_snapshot":
        return _validate_snapshot(value)
    if message_type == "desktop_event":
        return _validate_event(value)
    if message_type == "desktop_receipt":
        return _validate_receipt(value)
    raise DesktopProtocolError("desktop_message_type_invalid", "Desktop 消息类型无效")


def build_desktop_command(
    *,
    runner_id: str,
    request_id: str,
    host_ref: str,
    thread_ref: str | None,
    expected_thread_revision: int | None,
    expected_control_revision: int | None,
    action: str,
    now: dt.datetime,
    expected_turn_ref: str | None = None,
    input_text: str | None = None,
    mode: str | None = None,
    model: str | None = None,
    effort: str | None = None,
    queue_ref: str | None = None,
    queue_refs: list[str] | None = None,
    project_ref: str | None = None,
    ttl_seconds: int = 120,
) -> dict[str, Any]:
    _runner(runner_id)
    _request(request_id)
    _ref(host_ref, "HS")
    if action == "create":
        _ref(project_ref, "PJ")
        if thread_ref is not None or expected_thread_revision is not None or expected_control_revision is not None:
            raise DesktopProtocolError("desktop_create_target_invalid", "Desktop 新任务不得携带既有 Thread 前提")
    else:
        _ref(thread_ref, "TH")
        _revision(expected_thread_revision)
        if project_ref is not None:
            raise DesktopProtocolError("desktop_project_invalid", "既有 Desktop Thread 命令不得携带项目")
    _nullable_revision(expected_control_revision)
    if action not in ACTIONS:
        raise DesktopProtocolError("desktop_action_invalid", "Desktop 动作无效")
    if not 5 <= ttl_seconds <= 600:
        raise DesktopProtocolError("desktop_expiry_invalid", "Desktop 命令有效期无效")
    current = _aware(now, "now").astimezone(SHANGHAI)
    document: dict[str, Any] = {
        "version": DESKTOP_PROTOCOL_VERSION,
        "message_type": "desktop_command",
        "runner_id": runner_id,
        "request_id": request_id,
        "host_ref": host_ref,
        "thread_ref": thread_ref,
        "expected_thread_revision": expected_thread_revision,
        "expected_control_revision": expected_control_revision,
        "action": action,
        "created_at": current.isoformat(),
        "expires_at": (current + dt.timedelta(seconds=ttl_seconds)).isoformat(),
    }
    if project_ref is not None:
        document["project_ref"] = project_ref
    if expected_turn_ref is not None:
        document["expected_turn_ref"] = expected_turn_ref
    if input_text is not None:
        document["input"] = input_text
    if mode is not None:
        document["mode"] = mode
    if model is not None:
        document["model"] = model
    if effort is not None:
        document["effort"] = effort
    if queue_ref is not None:
        document["queue_ref"] = queue_ref
    if queue_refs is not None:
        document["queue_refs"] = list(queue_refs)
    document["body_digest"] = body_digest(document)
    return validate_desktop_command(document, now=current)


def validate_desktop_command(value: Mapping[str, Any], *, now: dt.datetime) -> dict[str, Any]:
    required = {
        "version",
        "message_type",
        "runner_id",
        "request_id",
        "host_ref",
        "thread_ref",
        "expected_thread_revision",
        "expected_control_revision",
        "action",
        "created_at",
        "expires_at",
        "body_digest",
    }
    optional = {
        "project_ref",
        "expected_turn_ref",
        "input",
        "mode",
        "model",
        "effort",
        "queue_ref",
        "queue_refs",
    }
    document = _exact_mapping(value, required, optional)
    _base(document, "desktop_command")
    _request(document["request_id"])
    _ref(document["host_ref"], "HS")
    action = document["action"]
    if action not in ACTIONS:
        raise DesktopProtocolError("desktop_action_invalid", "Desktop 动作无效")
    project_ref = document.get("project_ref")
    if action == "create":
        _ref(project_ref, "PJ")
        if (
            document["thread_ref"] is not None
            or document["expected_thread_revision"] is not None
            or document["expected_control_revision"] is not None
        ):
            raise DesktopProtocolError("desktop_create_target_invalid", "Desktop 新任务不得携带既有 Thread 前提")
    else:
        _ref(document["thread_ref"], "TH")
        _revision(document["expected_thread_revision"])
        _nullable_revision(document["expected_control_revision"])
        if project_ref is not None:
            raise DesktopProtocolError("desktop_project_invalid", "既有 Desktop Thread 命令不得携带项目")
    created_at = _shanghai_time(document["created_at"], "created_at")
    expires_at = _shanghai_time(document["expires_at"], "expires_at")
    current = _aware(now, "now")
    if expires_at <= current:
        raise DesktopProtocolError("desktop_request_expired", "Desktop 命令已过期")
    if expires_at <= created_at or expires_at - created_at > dt.timedelta(minutes=10):
        raise DesktopProtocolError("desktop_expiry_invalid", "Desktop 命令有效期无效")
    expected_turn = document.get("expected_turn_ref")
    if expected_turn is not None:
        _ref(expected_turn, "TR")
    input_text = document.get("input")
    if action in {"steer", "continue", "create", "queue_add", "queue_update"}:
        _safe_input(input_text)
    elif input_text is not None:
        raise DesktopProtocolError("desktop_input_invalid", "该 Desktop 动作不允许输入文本")
    if action in {"steer", "interrupt"} and expected_turn is None:
        raise DesktopProtocolError("desktop_expected_turn_required", "Desktop 动作缺少 expected Turn")
    if action not in {"steer", "interrupt"} and expected_turn is not None:
        raise DesktopProtocolError("desktop_expected_turn_invalid", "该 Desktop 动作不允许 expected Turn")
    mode = document.get("mode")
    if action == "steer":
        if mode not in {"safe", "native"}:
            raise DesktopProtocolError("desktop_mode_invalid", "Desktop steer 模式无效")
    elif mode is not None:
        raise DesktopProtocolError("desktop_mode_invalid", "该 Desktop 动作不允许 mode")
    model = document.get("model")
    if model is not None:
        _model_id(model)
        if action in {"continue", "create"}:
            pass
        elif action == "steer" and mode == "safe":
            pass
        else:
            raise DesktopProtocolError("desktop_model_invalid", "该 Desktop 动作不允许 model")
    effort = document.get("effort")
    if effort is not None:
        _effort(effort)
        if action in {"continue", "create"}:
            pass
        elif action == "steer" and mode == "safe":
            pass
        else:
            raise DesktopProtocolError("desktop_effort_invalid", "该 Desktop 动作不允许推理强度")
    queue_ref = document.get("queue_ref")
    queue_refs = document.get("queue_refs")
    if action in {"queue_update", "queue_delete", "queue_start"}:
        _ref(queue_ref, "QS")
    elif queue_ref is not None:
        raise DesktopProtocolError("desktop_ref_invalid", "该 Desktop 动作不允许 queue ref")
    if action == "queue_reorder":
        if (
            not isinstance(queue_refs, list)
            or not queue_refs
            or len(queue_refs) > 100
            or len(set(queue_refs)) != len(queue_refs)
        ):
            raise DesktopProtocolError("desktop_ref_invalid", "Desktop 排队顺序无效")
        for item in queue_refs:
            _ref(item, "QS")
    elif queue_refs is not None:
        raise DesktopProtocolError("desktop_ref_invalid", "该 Desktop 动作不允许 queue refs")
    _digest(document)
    return document


def validate_public_input(value: Any) -> None:
    _public(value)


def _validate_snapshot(value: Mapping[str, Any]) -> dict[str, Any]:
    required = {
        "version",
        "message_type",
        "runner_id",
        "created_at",
        "host_ref",
        "project_ref",
        "thread_ref",
        "thread_revision",
        "snapshot",
        "body_digest",
    }
    document = _exact_mapping(value, required, {"host", "snapshot_sequence"})
    _base(document, "desktop_snapshot")
    _ref(document["host_ref"], "HS")
    _ref(document["project_ref"], "PJ")
    _ref(document["thread_ref"], "TH")
    _revision(document["thread_revision"])
    snapshot_sequence = document.get("snapshot_sequence")
    if "snapshot_sequence" in document and (
        not isinstance(snapshot_sequence, int)
        or isinstance(snapshot_sequence, bool)
        or snapshot_sequence < 1
        or snapshot_sequence > (1 << 63) - 1
    ):
        raise DesktopProtocolError(
            "desktop_sequence_invalid",
            "Desktop snapshot sequence 无效",
        )
    _shanghai_time(document["created_at"], "created_at")
    snapshot = document["snapshot"]
    if not isinstance(snapshot, Mapping):
        raise DesktopProtocolError("desktop_snapshot_invalid", "Desktop snapshot 无效")
    if snapshot.get("project_ref") != document["project_ref"] or snapshot.get("thread_ref") != document["thread_ref"]:
        raise DesktopProtocolError("desktop_identity_mismatch", "Desktop snapshot ref 不一致")
    if snapshot.get("thread_revision") != document["thread_revision"]:
        raise DesktopProtocolError("desktop_revision_mismatch", "Desktop snapshot revision 不一致")
    if "control_revision" in snapshot:
        _nullable_revision(snapshot.get("control_revision"))
    alias = snapshot.get("project_alias")
    if not isinstance(alias, str) or not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,63}", alias):
        raise DesktopProtocolError("desktop_project_invalid", "Desktop 项目别名无效")
    if snapshot.get("status") not in THREAD_STATUSES:
        raise DesktopProtocolError("desktop_status_invalid", "Desktop Thread 状态无效")
    if not isinstance(snapshot.get("title"), str) or len(snapshot["title"]) > 500:
        raise DesktopProtocolError("desktop_snapshot_invalid", "Desktop Thread title 无效")
    if not isinstance(snapshot.get("preview"), str) or len(snapshot["preview"]) > 1200:
        raise DesktopProtocolError("desktop_snapshot_invalid", "Desktop Thread preview 无效")
    if not isinstance(snapshot.get("control_state"), str) or len(snapshot["control_state"]) > 64:
        raise DesktopProtocolError("desktop_snapshot_invalid", "Desktop control_state 无效")
    if not isinstance(snapshot.get("history_incomplete"), bool):
        raise DesktopProtocolError("desktop_snapshot_invalid", "Desktop history_incomplete 无效")
    for field in ("created_at", "updated_at"):
        if snapshot.get(field) is not None:
            _shanghai_time(snapshot[field], f"snapshot.{field}")
    active_turn = snapshot.get("active_turn_ref")
    if active_turn is not None:
        _ref(active_turn, "TR")
    turns = snapshot.get("turns")
    if not isinstance(turns, list) or len(turns) > 100:
        raise DesktopProtocolError("desktop_snapshot_invalid", "Desktop Turn 列表无效")
    model = snapshot.get("model")
    if "model" in snapshot and model is not None:
        _model_id(model)
    reasoning_effort = snapshot.get("reasoning_effort")
    if "reasoning_effort" in snapshot and reasoning_effort is not None:
        _effort(reasoning_effort)
    queue_is_present = "queued_submissions" in snapshot
    if queue_is_present:
        _queued_submissions(snapshot.get("queued_submissions"))
    _public(snapshot)
    host = document.get("host")
    if host is not None:
        if not isinstance(host, Mapping) or host.get("host_ref") != document["host_ref"]:
            raise DesktopProtocolError("desktop_host_invalid", "Desktop host 文档无效")
        _shanghai_time(host.get("synced_at"), "host.synced_at")
        capabilities = host.get("capabilities")
        if not isinstance(capabilities, list) or any(not isinstance(item, str) for item in capabilities):
            raise DesktopProtocolError("desktop_host_invalid", "Desktop host capabilities 无效")
        if host.get("state") not in {"normal", "unavailable", "protocol_degraded"}:
            raise DesktopProtocolError("desktop_host_invalid", "Desktop host state 无效")
        if not isinstance(host.get("control_enabled"), bool):
            raise DesktopProtocolError("desktop_host_invalid", "Desktop host control flag 无效")
        if host.get("control_enabled") is True and host.get("state") != "normal":
            raise DesktopProtocolError("desktop_host_invalid", "Desktop host control/state 不一致")
        models = host.get("models", [])
        complete_reasoning_catalog = _model_catalog(models)
        if "model_override_v1" in capabilities and (
            host.get("control_enabled") is not True or not models
        ):
            raise DesktopProtocolError(
                "desktop_host_invalid",
                "Desktop host model capability 与目录不一致",
            )
        if "reasoning_effort_v1" in capabilities and (
            host.get("control_enabled") is not True
            or not complete_reasoning_catalog
            or not any(item.get("supported_reasoning_efforts") for item in models)
        ):
            raise DesktopProtocolError(
                "desktop_host_invalid",
                "Desktop host 推理强度能力与目录不一致",
            )
        if "thread_queue_v1" in capabilities and (
            not queue_is_present or snapshot_sequence is None
        ):
            raise DesktopProtocolError(
                "desktop_host_invalid",
                "Desktop host 排队能力与快照序号不一致",
            )
        listener_count = host.get("tcp_listener_count")
        if not isinstance(listener_count, int) or isinstance(listener_count, bool) or listener_count < -1:
            raise DesktopProtocolError("desktop_host_invalid", "Desktop host listener count 无效")
        for field in ("app_version", "app_build", "cli_version", "schema_digest", "socket_mode"):
            field_value = host.get(field)
            if not isinstance(field_value, str) or not field_value or len(field_value) > 128:
                raise DesktopProtocolError("desktop_host_invalid", f"Desktop host {field} 无效")
        _public(host)
    _digest(document)
    return document


def _validate_event(value: Mapping[str, Any]) -> dict[str, Any]:
    required = {
        "version",
        "message_type",
        "runner_id",
        "created_at",
        "host_ref",
        "project_ref",
        "thread_ref",
        "turn_ref",
        "thread_revision",
        "event_sequence",
        "event_kind",
        "source",
        "payload",
        "body_digest",
    }
    document = _exact_mapping(value, required)
    _base(document, "desktop_event")
    _ref(document["host_ref"], "HS")
    _ref(document["project_ref"], "PJ")
    _ref(document["thread_ref"], "TH")
    if document["turn_ref"] is not None:
        _ref(document["turn_ref"], "TR")
    _revision(document["thread_revision"])
    sequence = document["event_sequence"]
    if not isinstance(sequence, int) or isinstance(sequence, bool) or sequence < 1:
        raise DesktopProtocolError("desktop_sequence_invalid", "Desktop event sequence 无效")
    if document["event_kind"] not in EVENT_KINDS or document["source"] not in {"desktop", "mobile", "app"}:
        raise DesktopProtocolError("desktop_event_invalid", "Desktop event 类型或来源无效")
    if not isinstance(document["payload"], Mapping):
        raise DesktopProtocolError("desktop_event_invalid", "Desktop event payload 无效")
    _shanghai_time(document["created_at"], "created_at")
    _public(document["payload"])
    _digest(document)
    return document


def _validate_receipt(value: Mapping[str, Any]) -> dict[str, Any]:
    required = {
        "version",
        "message_type",
        "runner_id",
        "created_at",
        "request_id",
        "host_ref",
        "thread_ref",
        "turn_ref",
        "action",
        "state",
        "thread_revision",
        "body_digest",
    }
    document = _exact_mapping(value, required, {"error_code", "project_ref", "queue_ref"})
    _base(document, "desktop_receipt")
    _request(document["request_id"])
    _ref(document["host_ref"], "HS")
    action = document["action"]
    state = document["state"]
    thread_ref = document["thread_ref"]
    revision = document["thread_revision"]
    project_ref = document.get("project_ref")
    if action == "create":
        _ref(project_ref, "PJ")
        if thread_ref is not None:
            _ref(thread_ref, "TH")
        if revision is not None:
            _revision(revision)
        if state == "confirmed" and (thread_ref is None or revision is None or document["turn_ref"] is None):
            raise DesktopProtocolError("desktop_receipt_invalid", "Desktop 新任务 confirmed 收据不完整")
    else:
        if project_ref is not None:
            raise DesktopProtocolError("desktop_receipt_invalid", "既有 Desktop Thread 收据不得携带项目")
        _ref(thread_ref, "TH")
        _revision(revision)
    if document["turn_ref"] is not None:
        _ref(document["turn_ref"], "TR")
    if action not in ACTIONS or state not in RECEIPT_STATES:
        raise DesktopProtocolError("desktop_receipt_invalid", "Desktop receipt 动作或状态无效")
    queue_ref = document.get("queue_ref")
    if queue_ref is not None:
        _ref(queue_ref, "QS")
    if action in {"queue_update", "queue_delete", "queue_start"} and queue_ref is None:
        raise DesktopProtocolError("desktop_receipt_invalid", "Desktop 排队收据缺少 queue ref")
    if action == "queue_add" and state == "confirmed" and queue_ref is None:
        raise DesktopProtocolError("desktop_receipt_invalid", "Desktop 添加排队消息收据不完整")
    if action not in {"queue_add", "queue_update", "queue_delete", "queue_start"} and queue_ref is not None:
        raise DesktopProtocolError("desktop_receipt_invalid", "该 Desktop 收据不允许 queue ref")
    error_code = document.get("error_code")
    if error_code is not None:
        _request(error_code)
    _shanghai_time(document["created_at"], "created_at")
    _digest(document)
    return document


def _base(document: Mapping[str, Any], message_type: str) -> None:
    if document.get("version") != DESKTOP_PROTOCOL_VERSION or document.get("message_type") != message_type:
        raise DesktopProtocolError("desktop_version_invalid", "Desktop 协议版本或消息类型无效")
    _runner(document.get("runner_id"))
    if len(canonical_json(document).encode("utf-8")) > MAX_DOCUMENT_BYTES:
        raise DesktopProtocolError("desktop_payload_too_large", "Desktop 文档过大")


def _exact_mapping(
    value: Mapping[str, Any],
    required: set[str],
    optional: set[str] | None = None,
) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise DesktopProtocolError("desktop_payload_invalid", "Desktop 文档必须是 object")
    allowed = required | (optional or set())
    if required - set(value) or set(value) - allowed:
        raise DesktopProtocolError("desktop_fields_invalid", "Desktop 文档字段无效")
    return dict(value)


def _digest(document: Mapping[str, Any]) -> None:
    value = document.get("body_digest")
    if not isinstance(value, str) or not DIGEST_RE.fullmatch(value) or value != body_digest(document):
        raise DesktopProtocolError("desktop_digest_invalid", "Desktop 文档摘要无效")


def _runner(value: Any) -> None:
    if not isinstance(value, str) or not RUNNER_RE.fullmatch(value):
        raise DesktopProtocolError("desktop_runner_invalid", "Desktop Runner ID 无效")


def _request(value: Any) -> None:
    if not isinstance(value, str) or not REQUEST_RE.fullmatch(value):
        raise DesktopProtocolError("desktop_request_id_invalid", "Desktop request ID 无效")


def _ref(value: Any, prefix: str) -> None:
    if not isinstance(value, str) or not REF_RE.fullmatch(value) or not value.startswith(prefix + "-"):
        raise DesktopProtocolError("desktop_ref_invalid", f"Desktop {prefix} ref 无效")


def _model_id(value: Any) -> None:
    if not isinstance(value, str) or not MODEL_ID_RE.fullmatch(value):
        raise DesktopProtocolError("desktop_model_invalid", "Desktop model 无效")


def _model_catalog(value: Any) -> bool:
    if not isinstance(value, list) or len(value) > 32:
        raise DesktopProtocolError("desktop_host_invalid", "Desktop host models 无效")
    seen: set[str] = set()
    default_count = 0
    complete_catalog = True
    for item in value:
        if not isinstance(item, Mapping) or frozenset(item) not in {
            frozenset({"id", "display_name", "is_default"}),
            frozenset(
                {
                    "id",
                    "display_name",
                    "is_default",
                    "default_reasoning_effort",
                    "supported_reasoning_efforts",
                }
            ),
        }:
            raise DesktopProtocolError("desktop_host_invalid", "Desktop host model entry 无效")
        complete_entry = "supported_reasoning_efforts" in item
        complete_catalog = complete_catalog and complete_entry
        model_id = item.get("id")
        _model_id(model_id)
        display_name = item.get("display_name")
        is_default = item.get("is_default")
        default_effort = item.get("default_reasoning_effort")
        efforts = item.get("supported_reasoning_efforts")
        if (
            model_id in seen
            or not isinstance(display_name, str)
            or not display_name
            or display_name != display_name.strip()
            or len(display_name) > 100
            or any(ord(character) < 32 for character in display_name)
            or not isinstance(is_default, bool)
            or (complete_entry and default_effort is not None and not isinstance(default_effort, str))
            or (complete_entry and not isinstance(efforts, list))
            or (complete_entry and len(efforts) > 16)
        ):
            raise DesktopProtocolError("desktop_host_invalid", "Desktop host model entry 无效")
        seen.add(model_id)
        default_count += int(is_default)
        if default_count > 1:
            raise DesktopProtocolError("desktop_host_invalid", "Desktop host default model 无效")
        effort_ids: set[str] = set()
        for effort in efforts or []:
            if not isinstance(effort, Mapping) or set(effort) != {"id", "description"}:
                raise DesktopProtocolError("desktop_host_invalid", "Desktop reasoning effort entry 无效")
            effort_id = effort.get("id")
            description = effort.get("description")
            _effort(effort_id)
            if (
                effort_id in effort_ids
                or not isinstance(description, str)
                or len(description) > 300
                or any(ord(character) < 32 and character not in "\n\t" for character in description)
            ):
                raise DesktopProtocolError("desktop_host_invalid", "Desktop reasoning effort entry 无效")
            effort_ids.add(effort_id)
        if default_effort is not None:
            _effort(default_effort)
            if default_effort not in effort_ids:
                raise DesktopProtocolError("desktop_host_invalid", "Desktop default reasoning effort 无效")
    return complete_catalog


def _queued_submissions(value: Any) -> None:
    if not isinstance(value, list) or len(value) > 100:
        raise DesktopProtocolError("desktop_snapshot_invalid", "Desktop 排队消息列表无效")
    seen: set[str] = set()
    for position, item in enumerate(value):
        if not isinstance(item, Mapping) or set(item) != {
            "queue_ref",
            "position",
            "text",
            "editable",
            "input_kind",
        }:
            raise DesktopProtocolError("desktop_snapshot_invalid", "Desktop 排队消息条目无效")
        queue_ref = item.get("queue_ref")
        _ref(queue_ref, "QS")
        text = item.get("text")
        editable = item.get("editable")
        input_kind = item.get("input_kind")
        if (
            queue_ref in seen
            or item.get("position") != position
            or not isinstance(text, str)
            or not text
            or len(text) > 12000
            or not isinstance(editable, bool)
            or input_kind not in {"text", "non_text"}
            or editable != (input_kind == "text")
        ):
            raise DesktopProtocolError("desktop_snapshot_invalid", "Desktop 排队消息条目无效")
        seen.add(queue_ref)


def _effort(value: Any) -> None:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,31}", value):
        raise DesktopProtocolError("desktop_effort_invalid", "Desktop reasoning effort 无效")


def _revision(value: Any) -> None:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise DesktopProtocolError("desktop_revision_invalid", "Desktop revision 无效")


def _nullable_revision(value: Any) -> None:
    if value is not None:
        _revision(value)


def _shanghai_time(value: Any, name: str) -> dt.datetime:
    if not isinstance(value, str):
        raise DesktopProtocolError("desktop_time_invalid", f"Desktop {name} 时间无效")
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise DesktopProtocolError("desktop_time_invalid", f"Desktop {name} 时间无效") from exc
    parsed = _aware(parsed, name)
    if parsed.utcoffset() != dt.timedelta(hours=8):
        raise DesktopProtocolError("desktop_time_zone_invalid", f"Desktop {name} 必须使用 +08:00")
    return parsed


def _aware(value: dt.datetime, name: str) -> dt.datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise DesktopProtocolError("desktop_time_invalid", f"Desktop {name} 必须包含时区")
    return value


def _safe_input(value: Any) -> None:
    if not isinstance(value, str) or not value.strip() or len(value) > 12000:
        raise DesktopProtocolError("desktop_input_invalid", "Desktop 输入文本无效")
    _public(value)


def _public(value: Any) -> None:
    if isinstance(value, Mapping):
        for key, child in value.items():
            if key in FORBIDDEN_PUBLIC_KEYS:
                raise DesktopProtocolError("desktop_privacy_rejected", f"Desktop 公开字段 {key} 被禁止")
            _public(child)
        return
    if isinstance(value, list):
        for child in value:
            _public(child)
        return
    if isinstance(value, str) and (
        UUID_RE.search(value) or PRIVATE_PATH_RE.search(value) or SECRET_RE.search(value)
    ):
        raise DesktopProtocolError("desktop_privacy_rejected", "Desktop 公开文本包含禁止内容")


__all__ = [
    "ACTIONS",
    "DESKTOP_PROTOCOL_VERSION",
    "DesktopProtocolError",
    "REF_RE",
    "REQUEST_RE",
    "SHANGHAI",
    "THREAD_STATUSES",
    "body_digest",
    "build_desktop_command",
    "canonical_json",
    "intent_digest",
    "validate_desktop_command",
    "validate_desktop_document",
    "validate_public_input",
]
