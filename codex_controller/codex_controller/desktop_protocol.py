"""Strict ref-only protocol for Codex Desktop takeover messages."""

from __future__ import annotations

import datetime as dt
import base64
import hashlib
import json
from pathlib import PurePosixPath
import re
from typing import Any, Mapping
from zoneinfo import ZoneInfo
from .desktop_images import validate_images


DESKTOP_PROTOCOL_VERSION = 1
MAX_DOCUMENT_BYTES = 256 * 1024
SHANGHAI = ZoneInfo("Asia/Shanghai")
RUNNER_RE = re.compile(r"^RN-[A-Z2-7]{20,32}$")
REQUEST_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
REF_RE = re.compile(r"^(HS|PJ|TH|TR|QS|HC|SC|RQ|QU|FL|AR|FD|DF|DC)-[A-Z2-7]{20,52}$")
DIFF_REF_RE = re.compile(r"^(DF|DC)-[A-Z2-7]{26}$")
DIGEST_RE = re.compile(r"^sha256:[a-f0-9]{64}$")
MODEL_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,63}$")
COLLABORATION_MODE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,63}$")
PERMISSION_PROFILE_ORDER = {
    ":read-only": 0,
    ":workspace": 1,
    ":danger-full-access": 2,
}
PERMISSION_PROFILE_LABELS = {
    ":read-only": "只读",
    ":workspace": "工作区访问",
    ":danger-full-access": "完全访问",
}
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
        "settings",
        "requests",
        "method",
        "params",
        "command",
        "permissions",
        "owner_client_id",
        "client_id",
        "message_id",
        "question_id",
        "raw_request_id",
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
        "history.page",
        "history.search",
        "history.error",
        "file.list",
        "file.opened",
        "file.chunk",
        "file.error",
        "diagnostic.result",
        "diagnostic.error",
        "diff.summary",
        "diff.chunk",
        "diff.error",
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
        "rename",
        "pin",
        "fork",
        "review",
        "collaboration_mode_update",
        "create",
        "queue_add",
        "queue_update",
        "queue_delete",
        "queue_reorder",
        "queue_start",
        "history_page",
        "history_search",
        "respond_request",
        "file_list",
        "file_open",
        "file_read",
        "diagnostic_run",
        "diff_summary",
        "diff_read",
    }
)

READ_ONLY_ACTIONS = frozenset(
    {
        "history_page",
        "history_search",
        "file_list",
        "file_open",
        "file_read",
        "diagnostic_run",
        "diff_summary",
        "diff_read",
    }
)
DIAGNOSTIC_IDS = frozenset(
    {"git_status", "git_diff_stat", "git_diff_check", "git_recent_commits"}
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
    value = _normalize_legacy_collaboration_catalog(message_type, value)
    if message_type == "desktop_host":
        return _validate_host(value)
    if message_type == "desktop_snapshot":
        return _validate_snapshot(value)
    if message_type == "desktop_event":
        return _validate_event(value)
    if message_type == "desktop_receipt":
        return _validate_receipt(value)
    raise DesktopProtocolError("desktop_message_type_invalid", "Desktop 消息类型无效")


def _normalize_legacy_collaboration_catalog(
    message_type: str,
    value: Mapping[str, Any],
) -> Mapping[str, Any]:
    """Accept only the previous five-field catalog, then validate as current."""

    if message_type not in {"desktop_host", "desktop_snapshot"}:
        return value
    if not isinstance(value, Mapping):
        return value
    host = value.get("host")
    if not isinstance(host, Mapping):
        return value
    modes = host.get("collaboration_modes")
    legacy_fields = {"id", "label", "mode", "model", "reasoning_effort"}
    if (
        not isinstance(modes, list)
        or not modes
        or any(not isinstance(item, Mapping) or set(item) != legacy_fields for item in modes)
    ):
        return value

    # Verify the immutable Runner envelope before changing its public shape.
    # The normalized copy then goes through the complete current validator,
    # including every nested Host and snapshot contract.
    _base(value, message_type)
    _digest(value)
    _public(value)
    for item in modes:
        model = item.get("model")
        effort = item.get("reasoning_effort")
        if model is not None:
            _model_id(model)
        if effort is not None:
            _effort(effort)

    normalized = dict(value)
    normalized_host = dict(host)
    normalized_host["collaboration_modes"] = [
        {"id": item["id"], "label": item["label"], "mode": item["mode"]}
        for item in modes
    ]
    normalized["host"] = normalized_host
    normalized["body_digest"] = body_digest(normalized)
    return normalized


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
    images: list[dict[str, str]] | None = None,
    mode: str | None = None,
    model: str | None = None,
    effort: str | None = None,
    permission_profile_id: str | None = None,
    collaboration_mode_id: str | None = None,
    queue_ref: str | None = None,
    queue_refs: list[str] | None = None,
    project_ref: str | None = None,
    cursor: str | None = None,
    limit: int | None = None,
    query: str | None = None,
    title: str | None = None,
    pinned: bool | None = None,
    request_ref: str | None = None,
    decision: str | None = None,
    answers: list[dict[str, Any]] | None = None,
    relative_path: str | None = None,
    file_kind: str | None = None,
    file_ref: str | None = None,
    offset: int | None = None,
    diagnostic_id: str | None = None,
    review_target: Mapping[str, Any] | None = None,
    diff_ref: str | None = None,
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
        if action in READ_ONLY_ACTIONS or action == "collaboration_mode_update":
            _ref(project_ref, "PJ")
        elif project_ref is not None:
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
    if cursor is not None:
        document["cursor"] = cursor
    if limit is not None:
        document["limit"] = limit
    if query is not None:
        document["query"] = query
    if title is not None:
        document["title"] = title
    if pinned is not None:
        document["pinned"] = pinned
    if request_ref is not None:
        document["request_ref"] = request_ref
    if decision is not None:
        document["decision"] = decision
    if answers is not None:
        document["answers"] = answers
    if relative_path is not None:
        document["relative_path"] = relative_path
    if file_kind is not None:
        document["file_kind"] = file_kind
    if file_ref is not None:
        document["file_ref"] = file_ref
    if offset is not None:
        document["offset"] = offset
    if diagnostic_id is not None:
        document["diagnostic_id"] = diagnostic_id
    if review_target is not None:
        document["review_target"] = dict(review_target)
    if diff_ref is not None:
        document["diff_ref"] = diff_ref
    if expected_turn_ref is not None:
        document["expected_turn_ref"] = expected_turn_ref
    if input_text is not None:
        document["input"] = input_text
    if images:
        document["images"] = images
    if mode is not None:
        document["mode"] = mode
    if model is not None:
        document["model"] = model
    if effort is not None:
        document["effort"] = effort
    if permission_profile_id is not None:
        document["permission_profile_id"] = permission_profile_id
    if collaboration_mode_id is not None:
        document["collaboration_mode_id"] = collaboration_mode_id
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
        "images",
        "project_ref",
        "expected_turn_ref",
        "input",
        "mode",
        "model",
        "effort",
        "permission_profile_id",
        "collaboration_mode_id",
        "queue_ref",
        "queue_refs",
        "cursor",
        "limit",
        "query",
        "title",
        "pinned",
        "request_ref",
        "decision",
        "answers",
        "relative_path",
        "file_kind",
        "file_ref",
        "offset",
        "diagnostic_id",
        "review_target",
        "diff_ref",
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
        if action in READ_ONLY_ACTIONS or action == "collaboration_mode_update":
            _ref(project_ref, "PJ")
        elif project_ref is not None:
            raise DesktopProtocolError("desktop_project_invalid", "既有 Desktop Thread 命令不得携带项目")
    if action in READ_ONLY_ACTIONS and document["expected_control_revision"] is not None:
        raise DesktopProtocolError("desktop_revision_invalid", "Desktop 只读请求不得携带控制 revision")
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
    if "images" in document:
        if action not in {"continue", "create"} and not (action == "steer" and document.get("mode") == "safe"):
            raise DesktopProtocolError("desktop_image_action_invalid", "该动作不支持图片")
        try:
            validate_images(document["images"])
        except (ValueError, TypeError) as exc:
            raise DesktopProtocolError("desktop_image_invalid", "图片内容无效") from exc
    if action in {"steer", "continue", "create", "queue_add", "queue_update"}:
        _safe_input(input_text)
    elif input_text is not None:
        raise DesktopProtocolError("desktop_input_invalid", "该 Desktop 动作不允许输入文本")
    if action in {"steer", "interrupt", "respond_request"} and expected_turn is None:
        raise DesktopProtocolError("desktop_expected_turn_required", "Desktop 动作缺少 expected Turn")
    if action not in {"steer", "interrupt", "respond_request"} and expected_turn is not None:
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
    permission_profile_id = document.get("permission_profile_id")
    if permission_profile_id is not None:
        _permission_profile_id(permission_profile_id)
        if action in {"continue", "create"}:
            pass
        elif action == "steer" and mode == "safe":
            pass
        else:
            raise DesktopProtocolError(
                "desktop_permission_profile_invalid",
                "该 Desktop 动作不允许权限档位",
            )
    collaboration_mode_id = document.get("collaboration_mode_id")
    if collaboration_mode_id is not None:
        _collaboration_mode_id(collaboration_mode_id)
        if action in {"continue", "create", "collaboration_mode_update"}:
            pass
        elif action == "steer" and mode == "safe":
            pass
        else:
            raise DesktopProtocolError(
                "desktop_collaboration_mode_invalid",
                "该 Desktop 动作不允许计划模式",
            )
    if action == "collaboration_mode_update":
        if collaboration_mode_id is None:
            raise DesktopProtocolError(
                "desktop_collaboration_mode_invalid",
                "Desktop 任务设置缺少计划模式",
            )
        if document.get("expected_control_revision") is None:
            raise DesktopProtocolError(
                "desktop_revision_invalid",
                "Desktop 任务设置缺少控制 revision",
            )
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
    cursor = document.get("cursor")
    if action == "history_page":
        if cursor is not None:
            _ref(cursor, "HC")
    elif action == "history_search":
        if cursor is not None:
            _ref(cursor, "SC")
    elif action == "file_list":
        if cursor is not None:
            _ref(cursor, "FD")
    elif action == "diff_read":
        if cursor is not None:
            _diff_ref(cursor, "DC")
    elif cursor is not None:
        raise DesktopProtocolError("desktop_ref_invalid", "该 Desktop 动作不允许历史游标")
    limit = document.get("limit")
    if action.startswith("history_"):
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 20:
            raise DesktopProtocolError("desktop_history_limit_invalid", "Desktop 历史分页大小无效")
    elif action == "file_list":
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 100:
            raise DesktopProtocolError("desktop_file_limit_invalid", "Desktop 目录分页大小无效")
    elif action == "file_read":
        resource_limit = 32 * 1024 if str(document.get("file_ref") or "").startswith("FL-") else 64 * 1024
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= resource_limit:
            raise DesktopProtocolError("desktop_file_limit_invalid", "Desktop 文件分块大小无效")
    elif action == "diff_summary":
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 100:
            raise DesktopProtocolError("desktop_diff_limit_invalid", "Desktop Diff 摘要大小无效")
    elif action == "diff_read":
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 32 * 1024:
            raise DesktopProtocolError("desktop_diff_limit_invalid", "Desktop Diff 分块大小无效")
    elif limit is not None:
        raise DesktopProtocolError("desktop_history_limit_invalid", "该 Desktop 动作不允许分页大小")
    query = document.get("query")
    if action == "history_search":
        if (
            not isinstance(query, str)
            or query != query.strip()
            or not 1 <= len(query) <= 120
            or any(ord(character) < 32 for character in query)
        ):
            raise DesktopProtocolError("desktop_history_query_invalid", "Desktop 历史搜索词无效")
    elif query is not None:
        raise DesktopProtocolError("desktop_history_query_invalid", "该 Desktop 动作不允许搜索词")
    relative_path = document.get("relative_path")
    if action in {"file_list", "file_open"}:
        _relative_path(relative_path, allow_empty=action == "file_list")
    elif relative_path is not None:
        raise DesktopProtocolError("desktop_file_path_invalid", "该 Desktop 动作不允许相对路径")
    file_kind = document.get("file_kind")
    if action == "file_open":
        if file_kind not in {"file", "artifact"}:
            raise DesktopProtocolError("desktop_file_kind_invalid", "Desktop 文件类型无效")
    elif file_kind is not None:
        raise DesktopProtocolError("desktop_file_kind_invalid", "该 Desktop 动作不允许文件类型")
    file_ref = document.get("file_ref")
    if action == "file_read":
        if not isinstance(file_ref, str) or not file_ref.startswith(("FL-", "AR-")):
            raise DesktopProtocolError("desktop_file_ref_invalid", "Desktop 文件引用无效")
        _ref(file_ref, file_ref[:2])
    elif file_ref is not None:
        raise DesktopProtocolError("desktop_file_ref_invalid", "该 Desktop 动作不允许文件引用")
    offset = document.get("offset")
    if action == "file_read":
        if not isinstance(offset, int) or isinstance(offset, bool) or offset < 0:
            raise DesktopProtocolError("desktop_file_range_invalid", "Desktop 文件偏移无效")
    elif offset is not None:
        raise DesktopProtocolError("desktop_file_range_invalid", "该 Desktop 动作不允许文件偏移")
    diagnostic_id = document.get("diagnostic_id")
    if action == "diagnostic_run":
        if diagnostic_id not in DIAGNOSTIC_IDS:
            raise DesktopProtocolError("desktop_diagnostic_not_allowed", "Desktop 诊断未在允许列表")
    elif diagnostic_id is not None:
        raise DesktopProtocolError("desktop_diagnostic_not_allowed", "该 Desktop 动作不允许诊断")
    diff_ref = document.get("diff_ref")
    if action == "diff_read":
        _diff_ref(diff_ref, "DF")
    elif diff_ref is not None:
        raise DesktopProtocolError("desktop_diff_ref_invalid", "该 Desktop 动作不允许 Diff 引用")
    title = document.get("title")
    if action == "rename":
        if (
            not isinstance(title, str)
            or title != title.strip()
            or not 1 <= len(title) <= 80
            or any(not character.isprintable() for character in title)
        ):
            raise DesktopProtocolError("desktop_title_invalid", "Desktop 任务标题无效")
    elif title is not None:
        raise DesktopProtocolError("desktop_title_invalid", "该 Desktop 动作不允许任务标题")
    pinned = document.get("pinned")
    if action == "pin":
        if not isinstance(pinned, bool):
            raise DesktopProtocolError("desktop_pinned_invalid", "Desktop 置顶状态无效")
    elif pinned is not None:
        raise DesktopProtocolError("desktop_pinned_invalid", "该 Desktop 动作不允许置顶状态")
    review_target = document.get("review_target")
    if action == "review":
        _review_target(review_target)
    elif review_target is not None:
        raise DesktopProtocolError("desktop_review_target_invalid", "该 Desktop 动作不允许审查目标")
    request_ref = document.get("request_ref")
    decision = document.get("decision")
    answers = document.get("answers")
    if action == "respond_request":
        _ref(request_ref, "RQ")
        if decision not in {"accept", "decline", "cancel", "submit"}:
            raise DesktopProtocolError("desktop_request_decision_invalid", "Desktop 请求响应决定无效")
        validate_request_answers(answers, required=decision == "submit")
    elif request_ref is not None or decision is not None or answers is not None:
        raise DesktopProtocolError("desktop_request_response_invalid", "该 Desktop 动作不允许请求响应")
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
    document = _exact_mapping(
        value,
        required,
        {"host", "host_sequence", "snapshot_sequence"},
    )
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
    host_sequence = document.get("host_sequence")
    if "host_sequence" in document and (
        not isinstance(host_sequence, int)
        or isinstance(host_sequence, bool)
        or host_sequence < 1
        or host_sequence > (1 << 63) - 1
    ):
        raise DesktopProtocolError(
            "desktop_sequence_invalid",
            "Desktop host sequence 无效",
        )
    if host_sequence is not None and "host" not in document:
        raise DesktopProtocolError(
            "desktop_host_invalid",
            "Desktop host sequence 缺少 host 文档",
        )
    if "host" in document:
        host_capabilities = document["host"].get("capabilities") if isinstance(
            document["host"], Mapping
        ) else None
        declares_host_v1 = (
            isinstance(host_capabilities, list)
            and "desktop_host_v1" in host_capabilities
        )
        if declares_host_v1 != (host_sequence is not None):
            raise DesktopProtocolError(
                "desktop_sequence_invalid",
                "Desktop host sequence 与 capability 声明不一致",
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
    if "pinned" in snapshot and not isinstance(snapshot.get("pinned"), bool):
        raise DesktopProtocolError("desktop_snapshot_invalid", "Desktop Thread pinned 无效")
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
    permission_profile_present = "permission_profile" in snapshot
    if permission_profile_present:
        _permission_profile(snapshot.get("permission_profile"))
    collaboration_mode_present = "collaboration_mode" in snapshot
    if collaboration_mode_present:
        _collaboration_mode_state(snapshot.get("collaboration_mode"))
    queue_is_present = "queued_submissions" in snapshot
    if queue_is_present:
        _queued_submissions(snapshot.get("queued_submissions"))
    pending_requests_present = "pending_requests" in snapshot
    if pending_requests_present:
        _pending_requests(snapshot.get("pending_requests"))
    _public(snapshot)
    host = document.get("host")
    if host is not None:
        _validate_host_projection(
            host,
            host_ref=str(document["host_ref"]),
            standalone=False,
            permission_profile_present=permission_profile_present,
            collaboration_mode_present=collaboration_mode_present,
            queue_is_present=queue_is_present,
            pending_requests_present=pending_requests_present,
            snapshot_sequence=snapshot_sequence,
            snapshot=snapshot,
        )
    _digest(document)
    return document


def _validate_host(value: Mapping[str, Any]) -> dict[str, Any]:
    document = _exact_mapping(
        value,
        {
            "version",
            "message_type",
            "runner_id",
            "created_at",
            "host_ref",
            "host_sequence",
            "host",
            "body_digest",
        },
    )
    _base(document, "desktop_host")
    _ref(document["host_ref"], "HS")
    sequence = document["host_sequence"]
    if (
        not isinstance(sequence, int)
        or isinstance(sequence, bool)
        or sequence < 1
        or sequence > (1 << 63) - 1
    ):
        raise DesktopProtocolError("desktop_sequence_invalid", "Desktop host sequence 无效")
    _shanghai_time(document["created_at"], "created_at")
    _validate_host_projection(
        document["host"],
        host_ref=str(document["host_ref"]),
        standalone=True,
    )
    _digest(document)
    return document


def _validate_host_projection(
    value: Any,
    *,
    host_ref: str,
    standalone: bool,
    permission_profile_present: bool = False,
    collaboration_mode_present: bool = False,
    queue_is_present: bool = False,
    pending_requests_present: bool = False,
    snapshot_sequence: Any = None,
    snapshot: Mapping[str, Any] | None = None,
) -> None:
    required = {
        "host_ref",
        "state",
        "app_version",
        "app_build",
        "cli_version",
        "schema_digest",
        "socket_mode",
        "tcp_listener_count",
        "capabilities",
        "control_enabled",
        "synced_at",
    }
    host = _exact_mapping(
        value,
        required,
        {
            "models",
            "settings_catalog",
            "permission_profiles",
            "collaboration_modes",
            "sync_health",
            "app_bridge",
            "data_synced_at",
        },
    )
    if host.get("host_ref") != host_ref:
        raise DesktopProtocolError("desktop_host_invalid", "Desktop host 文档无效")
    _shanghai_time(host.get("synced_at"), "host.synced_at")
    if host.get("data_synced_at") is not None:
        _shanghai_time(host["data_synced_at"], "host.data_synced_at")
    capabilities = host.get("capabilities")
    if (
        not isinstance(capabilities, list)
        or len(capabilities) > 64
        or any(
            not isinstance(item, str)
            or not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", item)
            for item in capabilities
        )
        or len(set(capabilities)) != len(capabilities)
    ):
        raise DesktopProtocolError("desktop_host_invalid", "Desktop host capabilities 无效")
    if host.get("state") not in {"normal", "unavailable", "protocol_degraded"}:
        raise DesktopProtocolError("desktop_host_invalid", "Desktop host state 无效")
    if not isinstance(host.get("control_enabled"), bool):
        raise DesktopProtocolError("desktop_host_invalid", "Desktop host control flag 无效")
    if host.get("control_enabled") is True and host.get("state") != "normal":
        raise DesktopProtocolError("desktop_host_invalid", "Desktop host control/state 不一致")
    models = host.get("models", [])
    complete_reasoning_catalog = _model_catalog(models)
    settings_catalog = host.get("settings_catalog")
    permission_profiles = host.get("permission_profiles")
    collaboration_modes = host.get("collaboration_modes")
    if settings_catalog is not None:
        _settings_catalog(settings_catalog)
    if permission_profiles is not None:
        _permission_profiles(permission_profiles)
    if collaboration_modes is not None:
        _collaboration_mode_catalog(collaboration_modes)
    if "settings_catalog_v1" in capabilities and settings_catalog is None:
        raise DesktopProtocolError("desktop_host_invalid", "Desktop host 设置能力与目录不一致")
    if "permission_profile_selection_v1" in capabilities and (
        host.get("control_enabled") is not True
        or "owner_request_response_v1" not in capabilities
        or not permission_profiles
        or (not standalone and not permission_profile_present)
    ):
        raise DesktopProtocolError("desktop_host_invalid", "Desktop host 权限能力与目录不一致")
    if not standalone and "permission_profile_selection_v1" in capabilities:
        assert snapshot is not None
        allowed_profiles = {item["id"] for item in permission_profiles}
        current_options = {item["id"] for item in snapshot["permission_profile"]["options"]}
        if not current_options.issubset(allowed_profiles):
            raise DesktopProtocolError(
                "desktop_host_invalid",
                "Desktop Thread 权限选项超出 Runner 目录",
            )
    if "model_override_v1" in capabilities and (
        host.get("control_enabled") is not True or not models
    ):
        raise DesktopProtocolError("desktop_host_invalid", "Desktop host model capability 与目录不一致")
    if "reasoning_effort_v1" in capabilities and (
        host.get("control_enabled") is not True
        or not complete_reasoning_catalog
        or not any(item.get("supported_reasoning_efforts") for item in models)
    ):
        raise DesktopProtocolError("desktop_host_invalid", "Desktop host 推理强度能力与目录不一致")
    if not standalone and "thread_queue_v1" in capabilities and (
        not queue_is_present or snapshot_sequence is None
    ):
        raise DesktopProtocolError("desktop_host_invalid", "Desktop host 排队能力与快照序号不一致")
    if not standalone and "owner_request_response_v1" in capabilities and (
        not pending_requests_present or snapshot_sequence is None
    ):
        raise DesktopProtocolError("desktop_host_invalid", "Desktop host 请求响应能力与快照序号不一致")
    catalog_capability = "collaboration_mode_catalog_v1" in capabilities
    turn_capability = "collaboration_mode_turn_v1" in capabilities
    update_capability = "thread_collaboration_mode_update_v1" in capabilities
    if catalog_capability != (collaboration_modes is not None):
        raise DesktopProtocolError("desktop_host_invalid", "Desktop host 计划模式目录能力不一致")
    if not standalone and catalog_capability and not collaboration_mode_present:
        raise DesktopProtocolError("desktop_host_invalid", "Desktop host 计划模式目录缺少当前状态")
    if (turn_capability or update_capability) and not catalog_capability:
        raise DesktopProtocolError("desktop_host_invalid", "Desktop host 计划模式能力缺少目录")
    if not standalone and collaboration_mode_present and not catalog_capability:
        raise DesktopProtocolError("desktop_host_invalid", "Desktop host 计划模式能力缺少目录")
    if (turn_capability or update_capability) and host.get("control_enabled") is not True:
        raise DesktopProtocolError("desktop_host_invalid", "Desktop host 计划模式写入能力与控制状态不一致")
    if not standalone and collaboration_mode_present:
        assert snapshot is not None
        current_mode_id = snapshot["collaboration_mode"].get("id")
        allowed_mode_ids = {item["id"] for item in collaboration_modes or []}
        if current_mode_id is not None and current_mode_id not in allowed_mode_ids:
            raise DesktopProtocolError(
                "desktop_host_invalid",
                "Desktop Thread 当前计划模式不在 Runner 目录",
            )
    listener_count = host.get("tcp_listener_count")
    if not isinstance(listener_count, int) or isinstance(listener_count, bool) or listener_count < -1:
        raise DesktopProtocolError("desktop_host_invalid", "Desktop host listener count 无效")
    for field in ("app_version", "app_build", "cli_version", "schema_digest", "socket_mode"):
        field_value = host.get(field)
        if not isinstance(field_value, str) or not field_value or len(field_value) > 128:
            raise DesktopProtocolError("desktop_host_invalid", f"Desktop host {field} 无效")
    if host.get("sync_health") is not None:
        _sync_health(host["sync_health"])
    if host.get("app_bridge") is not None:
        _app_bridge(host["app_bridge"])
    if standalone and (
        "desktop_host_v1" not in capabilities
        or host.get("sync_health") is None
        or host.get("app_bridge") is None
    ):
        raise DesktopProtocolError("desktop_host_invalid", "Desktop host 独立状态能力不完整")
    _public(host)


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
    if str(document["event_kind"]).startswith("history."):
        _validate_history_event_payload(
            str(document["event_kind"]),
            document["payload"],
        )
        if len(canonical_json(document).encode("utf-8")) > 64 * 1024:
            raise DesktopProtocolError("desktop_payload_too_large", "Desktop 历史页超过 64KiB")
    if str(document["event_kind"]).startswith(("file.", "diagnostic.", "diff.")):
        _validate_resource_event_payload(
            str(document["event_kind"]),
            document["payload"],
            project_ref=str(document["project_ref"]),
        )
    _shanghai_time(document["created_at"], "created_at")
    _public(document["payload"])
    _digest(document)
    return document


def _validate_history_event_payload(event_kind: str, value: Mapping[str, Any]) -> None:
    if event_kind == "history.error":
        payload = _exact_mapping(value, {"request_id", "action", "error_code"})
        _request(payload["request_id"])
        _request(payload["error_code"])
        if payload["action"] not in {"history_page", "history_search"}:
            raise DesktopProtocolError("desktop_event_invalid", "Desktop 历史错误动作无效")
        return
    required = {"request_id", "next_cursor", "has_more"}
    content_key = "turns" if event_kind == "history.page" else "occurrences"
    payload = _exact_mapping(value, required | {content_key})
    _request(payload["request_id"])
    if not isinstance(payload["has_more"], bool):
        raise DesktopProtocolError("desktop_event_invalid", "Desktop 历史分页状态无效")
    cursor = payload["next_cursor"]
    if cursor is not None:
        _ref(cursor, "HC" if event_kind == "history.page" else "SC")
    if payload["has_more"] is not (cursor is not None):
        raise DesktopProtocolError("desktop_event_invalid", "Desktop 历史游标与分页状态不一致")
    items = payload[content_key]
    if not isinstance(items, list) or len(items) > 20:
        raise DesktopProtocolError("desktop_event_invalid", "Desktop 历史页条目数无效")
    if event_kind == "history.page":
        for turn in items:
            _validate_history_turn(turn)
    else:
        for occurrence in items:
            if set(occurrence) != {"turn_ref", "snippet"}:
                raise DesktopProtocolError("desktop_event_invalid", "Desktop 历史搜索结果无效")
            _ref(occurrence.get("turn_ref"), "TR")
            snippet = occurrence.get("snippet")
            if not isinstance(snippet, str) or len(snippet.encode("utf-8")) > 2000:
                raise DesktopProtocolError("desktop_event_invalid", "Desktop 历史搜索摘要无效")


def _validate_history_turn(value: Any) -> None:
    turn = _exact_mapping(
        value,
        {
            "turn_ref",
            "status",
            "started_at",
            "completed_at",
            "duration_ms",
            "items_incomplete",
            "items",
        },
    )
    _ref(turn["turn_ref"], "TR")
    if turn["status"] not in {"completed", "interrupted", "failed", "inProgress"}:
        raise DesktopProtocolError("desktop_event_invalid", "Desktop 历史 Turn 状态无效")
    for field in ("started_at", "completed_at"):
        if turn[field] is not None:
            _shanghai_time(turn[field], f"history.{field}")
    duration = turn["duration_ms"]
    if duration is not None and (
        not isinstance(duration, int)
        or isinstance(duration, bool)
        or not 0 <= duration <= 86_400_000
    ):
        raise DesktopProtocolError("desktop_event_invalid", "Desktop 历史 Turn 时长无效")
    if not isinstance(turn["items_incomplete"], bool):
        raise DesktopProtocolError("desktop_event_invalid", "Desktop 历史 Turn 完整性无效")
    if not isinstance(turn["items"], list) or len(turn["items"]) > 500:
        raise DesktopProtocolError("desktop_event_invalid", "Desktop 历史 Turn 条目无效")


def _validate_resource_event_payload(
    event_kind: str,
    value: Mapping[str, Any],
    *,
    project_ref: str,
) -> None:
    if event_kind in {"file.error", "diagnostic.error", "diff.error"}:
        payload = _exact_mapping(value, {"request_id", "action", "error_code"})
        _request(payload["request_id"])
        _request(payload["error_code"])
        allowed_actions = (
            {"file_list", "file_open", "file_read"}
            if event_kind == "file.error"
            else {"diff_summary", "diff_read"}
            if event_kind == "diff.error"
            else {"diagnostic_run"}
        )
        if payload["action"] not in allowed_actions:
            raise DesktopProtocolError("desktop_event_invalid", "Desktop 资源错误动作无效")
        return
    if event_kind == "file.list":
        payload = _exact_mapping(
            value,
            {"request_id", "project_ref", "relative_path", "entries", "next_cursor", "has_more"},
        )
        _request(payload["request_id"])
        _resource_project(payload["project_ref"], project_ref)
        _relative_path(payload["relative_path"], allow_empty=True)
        entries = payload["entries"]
        if not isinstance(entries, list) or len(entries) > 100:
            raise DesktopProtocolError("desktop_event_invalid", "Desktop 目录页条目数无效")
        for entry in entries:
            _validate_directory_entry(entry)
        cursor = payload["next_cursor"]
        if cursor is not None:
            _ref(cursor, "FD")
        if not isinstance(payload["has_more"], bool) or payload["has_more"] is not (cursor is not None):
            raise DesktopProtocolError("desktop_event_invalid", "Desktop 目录游标与分页状态不一致")
        return
    if event_kind == "file.opened":
        payload = _exact_mapping(
            value,
            {"request_id", "ref", "kind", "project_ref", "relative_path", "mime_type", "size", "expires_at_ms"},
        )
        _request(payload["request_id"])
        _resource_project(payload["project_ref"], project_ref)
        kind = payload["kind"]
        if kind not in {"file", "artifact"}:
            raise DesktopProtocolError("desktop_event_invalid", "Desktop 文件类型无效")
        _ref(payload["ref"], "FL" if kind == "file" else "AR")
        _relative_path(payload["relative_path"], allow_empty=False)
        _resource_mime(payload["mime_type"])
        _bounded_integer(payload["size"], maximum=512 * 1024 * 1024, name="文件大小")
        _bounded_integer(payload["expires_at_ms"], maximum=(1 << 63) - 1, name="文件到期时间", minimum=1)
        return
    if event_kind == "file.chunk":
        base = {"request_id", "ref", "mime_type", "offset", "chunk_bytes", "next_offset", "eof"}
        if not isinstance(value, Mapping):
            raise DesktopProtocolError("desktop_event_invalid", "Desktop 文件分块无效")
        is_text = "text" in value or "encoding" in value
        payload = _exact_mapping(
            value,
            base | ({"encoding", "text"} if is_text else {"data_base64"}),
        )
        _request(payload["request_id"])
        ref = payload["ref"]
        if not isinstance(ref, str) or not ref.startswith(("FL-", "AR-")):
            raise DesktopProtocolError("desktop_event_invalid", "Desktop 文件引用无效")
        _ref(ref, ref[:2])
        _resource_mime(payload["mime_type"])
        offset = _bounded_integer(payload["offset"], maximum=(1 << 63) - 1, name="文件偏移")
        maximum = 32 * 1024 if ref.startswith("FL-") else 64 * 1024
        chunk_bytes = _bounded_integer(payload["chunk_bytes"], maximum=maximum, name="文件分块大小")
        next_offset = payload["next_offset"]
        if next_offset is not None:
            next_offset = _bounded_integer(next_offset, maximum=(1 << 63) - 1, name="下一文件偏移")
            if next_offset != offset + chunk_bytes:
                raise DesktopProtocolError("desktop_event_invalid", "Desktop 文件下一偏移无效")
        if not isinstance(payload["eof"], bool) or payload["eof"] is not (next_offset is None):
            raise DesktopProtocolError("desktop_event_invalid", "Desktop 文件结束状态无效")
        if is_text:
            if ref.startswith("AR-") or payload["encoding"] != "utf-8" or not isinstance(payload["text"], str):
                raise DesktopProtocolError("desktop_event_invalid", "Desktop 文本分块无效")
            if len(payload["text"].encode("utf-8")) > 32 * 1024:
                raise DesktopProtocolError("desktop_payload_too_large", "Desktop 文本分块超过 32KiB")
        else:
            encoded = payload["data_base64"]
            if ref.startswith("FL-") or not isinstance(encoded, str):
                raise DesktopProtocolError("desktop_event_invalid", "Desktop 产物分块无效")
            try:
                decoded = base64.b64decode(encoded, validate=True)
            except (ValueError, TypeError) as exc:
                raise DesktopProtocolError("desktop_event_invalid", "Desktop 产物分块编码无效") from exc
            if len(decoded) != chunk_bytes or len(decoded) > 64 * 1024:
                raise DesktopProtocolError("desktop_payload_too_large", "Desktop 产物分块超过 64KiB")
        return
    if event_kind == "diagnostic.result":
        payload = _exact_mapping(
            value,
            {
                "request_id", "diagnostic_id", "project_ref", "title", "action", "status",
                "exit_code", "duration_ms", "stdout", "stderr", "output_truncated",
            },
        )
        _request(payload["request_id"])
        _resource_project(payload["project_ref"], project_ref)
        if payload["diagnostic_id"] not in DIAGNOSTIC_IDS or payload["action"] != payload["diagnostic_id"]:
            raise DesktopProtocolError("desktop_event_invalid", "Desktop 诊断类型无效")
        _visible_text(payload["title"], minimum=1, maximum=128)
        if payload["status"] not in {"completed", "failed", "timed_out", "output_limited"}:
            raise DesktopProtocolError("desktop_event_invalid", "Desktop 诊断状态无效")
        exit_code = payload["exit_code"]
        if exit_code is not None and (not isinstance(exit_code, int) or isinstance(exit_code, bool) or not -(1 << 31) <= exit_code < (1 << 31)):
            raise DesktopProtocolError("desktop_event_invalid", "Desktop 诊断退出码无效")
        _bounded_integer(payload["duration_ms"], maximum=60_000, name="诊断耗时")
        if not isinstance(payload["stdout"], str) or not isinstance(payload["stderr"], str):
            raise DesktopProtocolError("desktop_event_invalid", "Desktop 诊断输出无效")
        if len((payload["stdout"] + payload["stderr"]).encode("utf-8")) > 32 * 1024:
            raise DesktopProtocolError("desktop_payload_too_large", "Desktop 诊断输出超过 32KiB")
        if not isinstance(payload["output_truncated"], bool):
            raise DesktopProtocolError("desktop_event_invalid", "Desktop 诊断截断状态无效")
        return
    if event_kind == "diff.summary":
        payload = _exact_mapping(
            value,
            {
                "request_id", "project_ref", "workspace_digest", "files",
                "file_count", "has_more", "limit",
            },
        )
        _request(payload["request_id"])
        _resource_project(payload["project_ref"], project_ref)
        if not isinstance(payload["workspace_digest"], str) or not DIGEST_RE.fullmatch(payload["workspace_digest"]):
            raise DesktopProtocolError("desktop_event_invalid", "Desktop Diff 工作区摘要无效")
        limit = _bounded_integer(payload["limit"], maximum=100, minimum=1, name="Diff 摘要大小")
        files = payload["files"]
        if not isinstance(files, list) or len(files) > limit:
            raise DesktopProtocolError("desktop_event_invalid", "Desktop Diff 文件列表无效")
        for entry in files:
            _validate_diff_entry(entry)
        file_count = _bounded_integer(payload["file_count"], maximum=1_000_000, name="Diff 文件数量")
        if not isinstance(payload["has_more"], bool):
            raise DesktopProtocolError("desktop_event_invalid", "Desktop Diff 分页状态无效")
        if payload["has_more"] is not (file_count > len(files)):
            raise DesktopProtocolError("desktop_event_invalid", "Desktop Diff 文件数量与分页状态不一致")
        return
    if event_kind == "diff.chunk":
        payload = _exact_mapping(
            value,
            {
                "request_id", "project_ref", "diff_ref", "relative_path", "status",
                "diff_digest", "content", "next_cursor", "complete",
            },
        )
        _request(payload["request_id"])
        _resource_project(payload["project_ref"], project_ref)
        _diff_ref(payload["diff_ref"], "DF")
        _relative_path(payload["relative_path"], allow_empty=False)
        if payload["status"] not in {"untracked", "conflict", "deleted", "renamed", "added", "modified"}:
            raise DesktopProtocolError("desktop_event_invalid", "Desktop Diff 文件状态无效")
        if not isinstance(payload["diff_digest"], str) or not DIGEST_RE.fullmatch(payload["diff_digest"]):
            raise DesktopProtocolError("desktop_event_invalid", "Desktop Diff 内容摘要无效")
        if not isinstance(payload["content"], str) or len(payload["content"].encode("utf-8")) > 32 * 1024:
            raise DesktopProtocolError("desktop_payload_too_large", "Desktop Diff 分块超过 32KiB")
        cursor = payload["next_cursor"]
        if cursor is not None:
            _diff_ref(cursor, "DC")
        if not isinstance(payload["complete"], bool) or payload["complete"] is not (cursor is None):
            raise DesktopProtocolError("desktop_event_invalid", "Desktop Diff 游标与完成状态不一致")
        return
    raise DesktopProtocolError("desktop_event_invalid", "Desktop 资源事件类型无效")


def _validate_directory_entry(value: Any) -> None:
    if not isinstance(value, Mapping):
        raise DesktopProtocolError("desktop_event_invalid", "Desktop 目录条目无效")
    kind = value.get("kind")
    basic = {"name", "kind", "readable"}
    if kind in {"directory", "symlink", "unavailable", "other"}:
        entry = _exact_mapping(value, basic)
        if entry["readable"] is not (kind == "directory"):
            raise DesktopProtocolError("desktop_event_invalid", "Desktop 目录条目可读状态无效")
    elif kind == "file":
        readable = value.get("readable")
        fields = basic | {"size", "modified_at_ms"}
        if readable is True:
            fields |= {"ref", "mime_type", "ref_kind"}
        entry = _exact_mapping(value, fields)
        if not isinstance(readable, bool):
            raise DesktopProtocolError("desktop_event_invalid", "Desktop 文件可读状态无效")
        _bounded_integer(entry["size"], maximum=512 * 1024 * 1024, name="目录文件大小")
        _bounded_integer(entry["modified_at_ms"], maximum=(1 << 63) - 1, name="文件修改时间", minimum=1)
        if readable:
            if entry["ref_kind"] not in {"file", "artifact"}:
                raise DesktopProtocolError("desktop_event_invalid", "Desktop 文件引用类型无效")
            _ref(entry["ref"], "FL" if entry["ref_kind"] == "file" else "AR")
            _resource_mime(entry["mime_type"])
    else:
        raise DesktopProtocolError("desktop_event_invalid", "Desktop 目录条目类型无效")
    _visible_text(entry["name"], minimum=1, maximum=255)


def _validate_diff_entry(value: Any) -> None:
    entry = _exact_mapping(
        value,
        {"diff_ref", "relative_path", "status", "added_lines", "deleted_lines", "binary"},
    )
    _diff_ref(entry["diff_ref"], "DF")
    _relative_path(entry["relative_path"], allow_empty=False)
    if entry["status"] not in {"untracked", "conflict", "deleted", "renamed", "added", "modified"}:
        raise DesktopProtocolError("desktop_event_invalid", "Desktop Diff 文件状态无效")
    for name in ("added_lines", "deleted_lines"):
        value = entry[name]
        if value is not None:
            _bounded_integer(value, maximum=1_000_000_000, name="Diff 行数")
    if not isinstance(entry["binary"], bool):
        raise DesktopProtocolError("desktop_event_invalid", "Desktop Diff 二进制状态无效")


def _resource_project(value: Any, expected: str) -> None:
    _ref(value, "PJ")
    if value != expected:
        raise DesktopProtocolError("desktop_identity_mismatch", "Desktop 资源项目引用不一致")


def _resource_mime(value: Any) -> None:
    if not isinstance(value, str) or not re.fullmatch(r"[a-z0-9][a-z0-9.+-]{0,63}/[a-z0-9][a-z0-9.+-]{0,127}", value):
        raise DesktopProtocolError("desktop_event_invalid", "Desktop 资源 MIME 无效")


def _bounded_integer(value: Any, *, maximum: int, name: str, minimum: int = 0) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or not minimum <= value <= maximum:
        raise DesktopProtocolError("desktop_event_invalid", f"Desktop {name}无效")
    return value


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
    document = _exact_mapping(
        value,
        required,
        {
            "error_code",
            "project_ref",
            "queue_ref",
            "request_ref",
            "created_thread_ref",
            "collaboration_mode_id",
        },
    )
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
    elif action == "collaboration_mode_update":
        _ref(project_ref, "PJ")
        _ref(thread_ref, "TH")
        _revision(revision)
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
    request_ref = document.get("request_ref")
    if action == "respond_request":
        _ref(request_ref, "RQ")
    elif request_ref is not None:
        raise DesktopProtocolError("desktop_receipt_invalid", "该 Desktop 收据不允许 request ref")
    created_thread_ref = document.get("created_thread_ref")
    if action == "fork":
        if state == "confirmed":
            _ref(created_thread_ref, "TH")
        elif created_thread_ref is not None:
            _ref(created_thread_ref, "TH")
    elif created_thread_ref is not None:
        raise DesktopProtocolError("desktop_receipt_invalid", "该 Desktop 收据不允许新任务引用")
    collaboration_mode_id = document.get("collaboration_mode_id")
    if collaboration_mode_id is not None:
        _collaboration_mode_id(collaboration_mode_id)
    if action not in {"create", "continue", "steer", "collaboration_mode_update"} and collaboration_mode_id is not None:
        raise DesktopProtocolError("desktop_receipt_invalid", "该 Desktop 收据不允许计划模式")
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
    maximum = 512 * 1024 if message_type == "desktop_command" else MAX_DOCUMENT_BYTES
    if len(canonical_json(document).encode("utf-8")) > maximum:
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


def _diff_ref(value: Any, prefix: str) -> None:
    if not isinstance(value, str) or not DIFF_REF_RE.fullmatch(value) or not value.startswith(prefix + "-"):
        raise DesktopProtocolError("desktop_diff_ref_invalid", f"Desktop {prefix} ref 无效")


def _review_target(value: Any) -> None:
    if not isinstance(value, Mapping):
        raise DesktopProtocolError("desktop_review_target_invalid", "Desktop 审查目标无效")
    kind = value.get("type")
    if kind == "uncommittedChanges" and set(value) == {"type"}:
        return
    branch = value.get("branch")
    if (
        kind != "baseBranch"
        or set(value) != {"type", "branch"}
        or not isinstance(branch, str)
        or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,254}", branch)
        or branch.startswith(("/", "."))
        or branch.endswith(("/", ".", ".lock"))
        or ".." in branch
        or "//" in branch
        or "@{" in branch
    ):
        raise DesktopProtocolError("desktop_review_target_invalid", "Desktop 审查目标无效")


def _relative_path(value: Any, *, allow_empty: bool) -> None:
    if not isinstance(value, str) or "\x00" in value or "\\" in value:
        raise DesktopProtocolError("desktop_file_path_invalid", "Desktop 相对路径无效")
    if value in {"", "."}:
        if allow_empty and value == "":
            return
        raise DesktopProtocolError("desktop_file_path_invalid", "Desktop 相对路径无效")
    path = PurePosixPath(value)
    if (
        path.is_absolute()
        or path.as_posix() != value
        or not path.parts
        or len(path.parts) > 128
        or len(value.encode("utf-8")) > 4096
        or any(part in {"", ".", ".."} for part in path.parts)
    ):
        raise DesktopProtocolError("desktop_file_path_invalid", "Desktop 相对路径无效")


def _model_id(value: Any) -> None:
    if not isinstance(value, str) or not MODEL_ID_RE.fullmatch(value):
        raise DesktopProtocolError("desktop_model_invalid", "Desktop model 无效")


def _collaboration_mode_id(value: Any) -> None:
    if not isinstance(value, str) or not COLLABORATION_MODE_ID_RE.fullmatch(value):
        raise DesktopProtocolError("desktop_collaboration_mode_invalid", "Desktop 计划模式 ID 无效")


def _collaboration_mode_catalog(value: Any) -> None:
    if not isinstance(value, list) or not 1 <= len(value) <= 32:
        raise DesktopProtocolError("desktop_host_invalid", "Desktop 计划模式目录无效")
    seen: set[str] = set()
    for raw in value:
        entry = _exact_mapping(raw, {"id", "label", "mode"})
        mode_id = entry.get("id")
        _collaboration_mode_id(mode_id)
        if mode_id in seen or entry.get("mode") not in {"plan", "default"}:
            raise DesktopProtocolError("desktop_host_invalid", "Desktop 计划模式目录项无效")
        _visible_text(entry.get("label"), minimum=1, maximum=80)
        seen.add(mode_id)


def _collaboration_mode_state(value: Any) -> None:
    state = _exact_mapping(value, {"id", "label", "mode", "editable", "reason"})
    mode_id = state.get("id")
    label = state.get("label")
    mode = state.get("mode")
    reason = state.get("reason")
    if mode_id is not None:
        _collaboration_mode_id(mode_id)
    if label is not None:
        _visible_text(label, minimum=1, maximum=80)
    if mode is not None and mode not in {"plan", "default"}:
        raise DesktopProtocolError("desktop_snapshot_invalid", "Desktop 当前计划模式无效")
    if not isinstance(state.get("editable"), bool):
        raise DesktopProtocolError("desktop_snapshot_invalid", "Desktop 计划模式可编辑状态无效")
    if reason is not None:
        if not isinstance(reason, str) or not REQUEST_RE.fullmatch(reason):
            raise DesktopProtocolError("desktop_snapshot_invalid", "Desktop 计划模式原因无效")
    if mode_id is None and (label is not None or mode is not None or reason is None):
        raise DesktopProtocolError("desktop_snapshot_invalid", "Desktop 未知计划模式状态无效")
    if mode_id is not None and (label is None or mode is None):
        raise DesktopProtocolError("desktop_snapshot_invalid", "Desktop 已知计划模式状态不完整")


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


def validate_request_answers(value: Any, *, required: bool) -> None:
    if not required:
        if value is not None:
            raise DesktopProtocolError("desktop_request_answers_invalid", "非提交决定不得携带答案")
        return
    if not isinstance(value, list) or not 1 <= len(value) <= 16:
        raise DesktopProtocolError("desktop_request_answers_invalid", "Desktop 请求答案无效")
    seen: set[str] = set()
    total_bytes = 0
    for item in value:
        answer = _exact_mapping(item, {"question_ref", "answers"})
        question_ref = answer.get("question_ref")
        _ref(question_ref, "QU")
        values = answer.get("answers")
        if question_ref in seen or not isinstance(values, list) or not 1 <= len(values) <= 8:
            raise DesktopProtocolError("desktop_request_answers_invalid", "Desktop 请求答案无效")
        seen.add(question_ref)
        for text in values:
            if (
                not isinstance(text, str)
                or not text
                or len(text) > 1000
                or any(ord(character) < 32 and character not in "\n\t" for character in text)
            ):
                raise DesktopProtocolError("desktop_request_answers_invalid", "Desktop 请求答案内容无效")
            total_bytes += len(text.encode("utf-8"))
    if total_bytes > 12_000:
        raise DesktopProtocolError("desktop_request_answers_invalid", "Desktop 请求答案过大")
    _public(value)


def _pending_requests(value: Any) -> None:
    if not isinstance(value, list) or len(value) > 32:
        raise DesktopProtocolError("desktop_snapshot_invalid", "Desktop 待处理请求列表无效")
    seen: set[str] = set()
    base = {"request_ref", "turn_ref", "blocking", "kind", "title", "decisions"}
    for item in value:
        if not isinstance(item, Mapping):
            raise DesktopProtocolError("desktop_snapshot_invalid", "Desktop 待处理请求无效")
        kind = item.get("kind")
        required = set(base)
        if kind in {"command_approval", "file_approval"}:
            required.add("summary")
        elif kind == "permissions_approval":
            required |= {"summary", "permission_summary"}
        elif kind == "user_input":
            required.add("questions")
        elif kind == "mcp_elicitation":
            required |= {"summary", "server", "mode", "questions"}
        else:
            raise DesktopProtocolError("desktop_snapshot_invalid", "Desktop 待处理请求类型无效")
        request = _exact_mapping(item, required)
        request_ref = request.get("request_ref")
        _ref(request_ref, "RQ")
        _ref(request.get("turn_ref"), "TR")
        if request_ref in seen or not isinstance(request.get("blocking"), bool):
            raise DesktopProtocolError("desktop_snapshot_invalid", "Desktop 待处理请求绑定无效")
        seen.add(request_ref)
        _visible_text(request.get("title"), minimum=1, maximum=128)
        decisions = request.get("decisions")
        expected_decisions = {
            "command_approval": ["accept", "decline", "cancel"],
            "file_approval": ["accept", "decline", "cancel"],
            "permissions_approval": ["accept", "decline"],
        }.get(kind)
        if kind in {"command_approval", "file_approval", "permissions_approval"}:
            if decisions != expected_decisions:
                raise DesktopProtocolError("desktop_snapshot_invalid", "Desktop 待处理请求决定无效")
        if "summary" in request:
            _visible_text(request.get("summary"), minimum=1, maximum=4000 if kind == "command_approval" else 2000)
        if kind == "permissions_approval":
            summary = _exact_mapping(request.get("permission_summary"), {"file_entry_count", "network_requested"})
            count = summary.get("file_entry_count")
            if (
                not isinstance(count, int)
                or isinstance(count, bool)
                or not 0 <= count <= 1000
                or not isinstance(summary.get("network_requested"), bool)
            ):
                raise DesktopProtocolError("desktop_snapshot_invalid", "Desktop 权限请求摘要无效")
        if kind in {"user_input", "mcp_elicitation"}:
            questions = request.get("questions")
            _request_questions(questions, kind=kind)
            has_secret = any(question.get("secret") is True for question in questions)
            if kind == "user_input":
                if decisions != (["cancel"] if has_secret else ["submit", "cancel"]):
                    raise DesktopProtocolError("desktop_snapshot_invalid", "Desktop 提问决定无效")
            else:
                mode = request.get("mode")
                server = request.get("server")
                if mode not in {"form", "url"}:
                    raise DesktopProtocolError("desktop_snapshot_invalid", "Desktop 工具提问模式无效")
                _visible_text(server, minimum=0, maximum=128)
                if has_secret or (mode == "url" and questions):
                    raise DesktopProtocolError("desktop_snapshot_invalid", "Desktop 工具提问字段无效")
                expected = ["decline", "cancel"] if mode == "url" else ["submit", "decline", "cancel"]
                if decisions != expected:
                    raise DesktopProtocolError("desktop_snapshot_invalid", "Desktop 工具提问决定无效")


def _request_questions(value: Any, *, kind: str) -> None:
    minimum = 0 if kind == "mcp_elicitation" else 1
    if not isinstance(value, list) or not minimum <= len(value) <= 16:
        raise DesktopProtocolError("desktop_snapshot_invalid", "Desktop 问题列表无效")
    seen: set[str] = set()
    fields = {
        "question_ref",
        "header",
        "question",
        "options",
        "allows_other",
        "secret",
        "value_type",
        "required",
    }
    for item in value:
        question = _exact_mapping(item, fields)
        question_ref = question.get("question_ref")
        _ref(question_ref, "QU")
        if question_ref in seen:
            raise DesktopProtocolError("desktop_snapshot_invalid", "Desktop 问题引用重复")
        seen.add(question_ref)
        _visible_text(question.get("header"), minimum=1, maximum=128)
        _visible_text(question.get("question"), minimum=0, maximum=2000)
        options = question.get("options")
        if not isinstance(options, list) or len(options) > 16:
            raise DesktopProtocolError("desktop_snapshot_invalid", "Desktop 问题选项无效")
        for item_option in options:
            option = _exact_mapping(item_option, {"label", "description"})
            _visible_text(option.get("label"), minimum=1, maximum=256)
            _visible_text(option.get("description"), minimum=0, maximum=1000)
        if not isinstance(question.get("allows_other"), bool) or not isinstance(question.get("secret"), bool):
            raise DesktopProtocolError("desktop_snapshot_invalid", "Desktop 问题标记无效")
        if question.get("value_type") not in {"string", "number", "integer", "boolean", "array"}:
            raise DesktopProtocolError("desktop_snapshot_invalid", "Desktop 问题值类型无效")
        if not isinstance(question.get("required"), bool):
            raise DesktopProtocolError("desktop_snapshot_invalid", "Desktop 问题必填标记无效")
        if kind == "user_input" and (question.get("value_type") != "string" or question.get("required") is not True):
            raise DesktopProtocolError("desktop_snapshot_invalid", "Desktop 用户提问类型无效")


def _visible_text(value: Any, *, minimum: int, maximum: int) -> None:
    if (
        not isinstance(value, str)
        or not minimum <= len(value) <= maximum
        or any(ord(character) < 32 and character not in "\n\t" for character in value)
    ):
        raise DesktopProtocolError("desktop_snapshot_invalid", "Desktop 公共文本无效")


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


def _settings_catalog(value: Any) -> None:
    catalog = _exact_mapping(
        value,
        {"model", "reasoning_effort", "personality", "service_tier", "web_search"},
    )
    for field, item in catalog.items():
        if item is not None and (
            not isinstance(item, str)
            or item != item.strip()
            or not item
            or len(item) > 128
            or any(ord(character) < 32 for character in item)
        ):
            raise DesktopProtocolError("desktop_host_invalid", f"Desktop host {field} 设置无效")


def _sync_health(value: Any) -> None:
    health = _exact_mapping(
        value,
        {"lane", "timings_ms", "last_success", "data_age_ms", "consecutive_failures"},
    )
    lane = health.get("lane")
    if not isinstance(lane, str) or not re.fullmatch(r"[a-z][a-z0-9_]{0,31}", lane):
        raise DesktopProtocolError("desktop_host_invalid", "Desktop host 同步通道无效")
    timings = health.get("timings_ms")
    if (
        not isinstance(timings, Mapping)
        or len(timings) > 16
        or any(
            not isinstance(key, str)
            or not re.fullmatch(r"[a-z][a-z0-9_]{0,31}", key)
            or not isinstance(item, int)
            or isinstance(item, bool)
            or not 0 <= item <= 300_000
            for key, item in timings.items()
        )
    ):
        raise DesktopProtocolError("desktop_host_invalid", "Desktop host 同步耗时无效")
    _shanghai_time(health.get("last_success"), "host.sync_health.last_success")
    for field, maximum in (("data_age_ms", 86_400_000), ("consecutive_failures", 1_000_000)):
        item = health.get(field)
        if not isinstance(item, int) or isinstance(item, bool) or not 0 <= item <= maximum:
            raise DesktopProtocolError("desktop_host_invalid", f"Desktop host {field} 无效")


def _app_bridge(value: Any) -> None:
    bridge = _exact_mapping(
        value,
        {"ready", "last_success", "last_error_code"},
        {
            "sidecar_running",
            "restart_count",
            "retry_seconds",
            "supervisor_error_code",
            "recovery_attempt_count",
            "last_attempt",
            "last_activation_success",
        },
    )
    if not isinstance(bridge.get("ready"), bool):
        raise DesktopProtocolError("desktop_host_invalid", "Desktop App Bridge ready 状态无效")
    last_success = bridge.get("last_success")
    if last_success is not None:
        _shanghai_time(last_success, "host.app_bridge.last_success")
    for field in ("last_attempt", "last_activation_success"):
        timestamp = bridge.get(field)
        if timestamp is not None:
            _shanghai_time(timestamp, f"host.app_bridge.{field}")
    for field in ("last_error_code", "supervisor_error_code"):
        error_code = bridge.get(field)
        if error_code is not None and (
            not isinstance(error_code, str)
            or not re.fullmatch(r"bridge_[a-z0-9_]{1,56}", error_code)
        ):
            raise DesktopProtocolError("desktop_host_invalid", f"Desktop App Bridge {field} 无效")
    sidecar_running = bridge.get("sidecar_running")
    if sidecar_running is not None and not isinstance(sidecar_running, bool):
        raise DesktopProtocolError("desktop_host_invalid", "Desktop App Bridge sidecar 状态无效")
    for field, maximum in (
        ("restart_count", 1_000_000),
        ("recovery_attempt_count", 1_000_000),
        ("retry_seconds", 30),
    ):
        item = bridge.get(field)
        if item is not None and (
            not isinstance(item, int) or isinstance(item, bool) or not 0 <= item <= maximum
        ):
            raise DesktopProtocolError("desktop_host_invalid", f"Desktop App Bridge {field} 无效")


def _permission_profiles(value: Any) -> None:
    if not isinstance(value, list) or len(value) > len(PERMISSION_PROFILE_ORDER):
        raise DesktopProtocolError("desktop_host_invalid", "Desktop host 权限目录无效")
    seen: set[str] = set()
    previous_order = -1
    for item in value:
        profile = _exact_mapping(item, {"id", "label"})
        profile_id = profile.get("id")
        label = profile.get("label")
        if (
            not isinstance(profile_id, str)
            or profile_id not in PERMISSION_PROFILE_ORDER
            or profile_id in seen
            or label != PERMISSION_PROFILE_LABELS[profile_id]
            or PERMISSION_PROFILE_ORDER[profile_id] <= previous_order
        ):
            raise DesktopProtocolError("desktop_host_invalid", "Desktop host 权限条目无效")
        seen.add(profile_id)
        previous_order = PERMISSION_PROFILE_ORDER[profile_id]


def _permission_profile(value: Any) -> None:
    profile = _exact_mapping(
        value,
        {"id", "label", "editable", "options"},
        {"reason"},
    )
    profile_id = profile.get("id")
    label = profile.get("label")
    editable = profile.get("editable")
    options = profile.get("options")
    reason = profile.get("reason")
    if not isinstance(label, str) or not label or label != label.strip() or len(label) > 100:
        raise DesktopProtocolError("desktop_snapshot_invalid", "Desktop 权限档位无效")
    if not isinstance(editable, bool):
        raise DesktopProtocolError("desktop_snapshot_invalid", "Desktop 权限编辑状态无效")
    _permission_profiles(options)
    option_ids = [item["id"] for item in options]
    if profile_id is None:
        if editable or option_ids or not isinstance(reason, str) or not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", reason):
            raise DesktopProtocolError("desktop_snapshot_invalid", "Desktop 权限档位不可用状态无效")
        return
    _permission_profile_id(profile_id)
    if label != PERMISSION_PROFILE_LABELS[profile_id]:
        raise DesktopProtocolError("desktop_snapshot_invalid", "Desktop 权限档位标签无效")
    if editable != bool(option_ids):
        raise DesktopProtocolError("desktop_snapshot_invalid", "Desktop 权限编辑状态与选项不一致")
    if any(PERMISSION_PROFILE_ORDER[item] > PERMISSION_PROFILE_ORDER[profile_id] for item in option_ids):
        raise DesktopProtocolError("desktop_snapshot_invalid", "Desktop 权限选项不得扩大当前权限")
    if editable and reason is not None:
        raise DesktopProtocolError("desktop_snapshot_invalid", "Desktop 权限可编辑状态不应包含原因")
    if not editable and (
        not isinstance(reason, str) or not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", reason)
    ):
        raise DesktopProtocolError("desktop_snapshot_invalid", "Desktop 权限不可编辑原因无效")


def _permission_profile_id(value: Any) -> None:
    if not isinstance(value, str) or value not in PERMISSION_PROFILE_ORDER:
        raise DesktopProtocolError("desktop_permission_profile_invalid", "Desktop 权限档位无效")


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
    "COLLABORATION_MODE_ID_RE",
    "DIAGNOSTIC_IDS",
    "DIFF_REF_RE",
    "DESKTOP_PROTOCOL_VERSION",
    "DesktopProtocolError",
    "REF_RE",
    "REQUEST_RE",
    "READ_ONLY_ACTIONS",
    "SHANGHAI",
    "THREAD_STATUSES",
    "body_digest",
    "build_desktop_command",
    "canonical_json",
    "intent_digest",
    "validate_desktop_command",
    "validate_desktop_document",
    "validate_public_input",
    "validate_request_answers",
]
