"""Pure Weixin-to-Runner-Manager v2 routing and client contracts.

This module deliberately owns no sockets, persistence, Weixin sending, or v1
runtime.  A caller may use the route decision to invoke exactly one existing
transport.  In particular, once v2 is selected, failures must not fall back to
v1 because that could execute the same task twice.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import hashlib
import json
import re
from typing import Any, Mapping, Protocol


PROTOCOL_VERSION = 2
RUNNER_MANAGER_COMMAND_PATH = "/internal/v2/runner-manager/work"
SUPPORTED_PROJECTS = frozenset({"renovation-hub"})

ROUTE_ORDINARY = "ordinary"
ROUTE_V1 = "remote_work_v1"
ROUTE_V2 = "runner_manager_v2"
ROUTE_REJECT = "reject"

MAX_INSTRUCTION_CHARS = 12_000
MAX_DOCUMENT_BYTES = 32 * 1024
MAX_SUMMARY_CHARS = 6_000
MAX_TEST_SUMMARY_CHARS = 3_000
MAX_ACTION_CHARS = 1_000
MAX_ACTIONS = 20

TASK_STATES = frozenset(
    {
        "waiting_runner",
        "leased",
        "dispatched",
        "queued",
        "running",
        "awaiting_confirmation",
        "completed",
        "failed",
        "cancelled",
        "expired",
        "recovery_required",
    }
)

_TASK_ID_RE = re.compile(r"^RW-[A-Za-z0-9][A-Za-z0-9._:-]{0,124}$")
_OPAQUE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,255}$")
_REQUEST_ID_RE = re.compile(r"^WRV2-[0-9a-f]{32}$")
_SAFE_CODE_RE = re.compile(r"^[a-z][a-z0-9_]{0,127}$")
_SAFE_VALUE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,255}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_CANDIDATE_RE = re.compile(r"^sha256:[0-9a-f]{64}$")

_FORBIDDEN_INSTRUCTION_PATTERNS = (
    (
        "work_path_forbidden",
        re.compile(
            r"(?ix)(?:"
            r"(?:^|\s)--?(?:path|cwd|worktree)(?:\s|=|:)"
            r"|(?:^|[\s,{])['\"]?(?:path|cwd|worktree)['\"]?\s*[:=]"
            r"|(?:^|\s)(?:\.\.?[/\\]|~[/\\]|[A-Za-z]:[/\\]|\\\\)"
            r"|(?:^|\s)/(?:[A-Za-z0-9._-]+/)+[A-Za-z0-9._-]+"
            r")"
        ),
    ),
    (
        "work_shell_forbidden",
        re.compile(
            r"(?ix)(?:"
            r"(?:^|\s)--?(?:shell|command|cmd)(?:\s|=|:)"
            r"|(?:^|[\s,{])['\"]?(?:shell|command|cmd)['\"]?\s*[:=]"
            r"|(?:^|\s)(?:bash|zsh|sh|fish|powershell|pwsh|cmd(?:\.exe)?)\s+-?c\b"
            r"|\$\(|`"
            r")"
        ),
    ),
    (
        "work_model_forbidden",
        re.compile(
            r"(?ix)(?:^|[\s,{])(?:--?model|['\"]?model['\"]?\s*[:=])"
        ),
    ),
    (
        "work_sandbox_forbidden",
        re.compile(
            r"(?ix)(?:^|[\s,{])(?:--?sandbox|['\"]?sandbox['\"]?\s*[:=])"
        ),
    ),
    (
        "work_git_ref_forbidden",
        re.compile(
            r"(?ix)(?:"
            r"(?:^|\s)--?(?:git[-_]?ref|ref|branch|tag)(?:\s|=|:)"
            r"|(?:^|[\s,{])['\"]?(?:git[-_]?ref|ref|branch|tag)['\"]?\s*[:=]"
            r"|\brefs/(?:heads|tags)/"
            r")"
        ),
    ),
    (
        "work_remote_forbidden",
        re.compile(
            r"(?ix)(?:"
            r"(?:^|\s)--?(?:remote|repo|repository)(?:\s|=|:)"
            r"|(?:^|[\s,{])['\"]?(?:remote|repo|repository)['\"]?\s*[:=]"
            r"|(?:https?|ssh)://"
            r"|\bgit@[^\s:]+:"
            r"|(?:^|\s)origin/"
            r")"
        ),
    ),
    (
        "production_confirmation_required",
        re.compile(r"(?ix)(?:^|\s)--?deploy(?:\s|=|:|$)"),
    ),
)


class WorkCommandError(ValueError):
    """Stable public failure for a v2 ``/work`` command."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class RunnerManagerContractError(ValueError):
    """Stable failure for a Runner Manager request or response document."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class RunnerManagerResponseError(RunnerManagerContractError):
    """Bounded error returned by the deterministic Controller API."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        status_code: int,
        request_id: str,
        retryable: bool,
    ) -> None:
        super().__init__(code, message)
        self.status_code = status_code
        self.request_id = request_id
        self.retryable = retryable


@dataclasses.dataclass(frozen=True)
class WorkCommand:
    operation: str
    project_alias: str | None = None
    task_id: str | None = None
    instruction: str | None = None


@dataclasses.dataclass(frozen=True)
class RouteDecision:
    """A single-route decision; runtime fallback is always forbidden."""

    route: str
    reason: str
    command: WorkCommand | None = None
    error_code: str | None = None
    public_message: str | None = None
    runtime_fallback_allowed: bool = False

    def __post_init__(self) -> None:
        if self.route not in {ROUTE_ORDINARY, ROUTE_V1, ROUTE_V2, ROUTE_REJECT}:
            raise ValueError("route is invalid")
        if self.runtime_fallback_allowed:
            raise ValueError("v2 runtime fallback would permit a double route")
        if self.route == ROUTE_V2 and self.command is None:
            raise ValueError("a v2 route requires a parsed command")
        if self.route != ROUTE_V2 and self.command is not None:
            raise ValueError("only a v2 route may carry a v2 command")
        if self.route == ROUTE_REJECT and (not self.error_code or not self.public_message):
            raise ValueError("a rejected route requires a public error")

    @property
    def dispatch_targets(self) -> tuple[str, ...]:
        if self.route in {ROUTE_V1, ROUTE_V2}:
            return (self.route,)
        return ()


@dataclasses.dataclass(frozen=True)
class RunnerManagerRequest:
    method: str
    path: str
    request_id: str
    body: dict[str, Any]
    body_digest: str


@dataclasses.dataclass(frozen=True)
class RunnerManagerResult:
    request_id: str
    operation: str
    task_id: str
    state: str
    updated_at: str
    stage: str | None = None
    summary: str | None = None
    candidate_id: str | None = None
    test_summary: str | None = None
    changed_path_count: int | None = None
    next_actions: tuple[str, ...] = ()
    error_code: str | None = None
    action_required: str | None = None
    queue_position: int | None = None


class RunnerManagerTransport(Protocol):
    """Injected transport boundary; this module supplies no HTTP client."""

    async def execute(self, request: RunnerManagerRequest) -> tuple[int, Mapping[str, Any]]:
        """Return ``(status_code, JSON object)`` for one request."""


def is_exact_work_command(text: object) -> bool:
    if not isinstance(text, str):
        return False
    stripped = text.strip()
    if not stripped:
        return False
    return stripped.split(None, 1)[0] == "/work"


def parse_work_command(text: str) -> WorkCommand | None:
    """Parse the exact v2 grammar; near matches remain ordinary chat."""
    if not is_exact_work_command(text):
        return None
    stripped = text.strip()
    tokens = stripped.split()
    if len(tokens) == 1:
        raise WorkCommandError("work_command_invalid", "请使用 /work renovation-hub <任务>。")

    action = tokens[1]
    if action == "deploy":
        raise WorkCommandError(
            "production_confirmation_required",
            "Runner Manager 不接受微信部署命令；生产发布需要独立受控确认。",
        )
    if action == "status":
        if len(tokens) != 3 or not _TASK_ID_RE.fullmatch(tokens[2]):
            raise WorkCommandError("work_task_id_invalid", "请使用 /work status <task_id>。")
        return WorkCommand(operation="status", task_id=tokens[2])
    if action == "cancel":
        if len(tokens) != 3 or not _TASK_ID_RE.fullmatch(tokens[2]):
            raise WorkCommandError("work_task_id_invalid", "请使用 /work cancel <task_id>。")
        return WorkCommand(operation="cancel", task_id=tokens[2])
    if action == "continue":
        if len(tokens) < 4 or not _TASK_ID_RE.fullmatch(tokens[2]):
            raise WorkCommandError("work_command_invalid", "请使用 /work continue <task_id> <补充要求>。")
        instruction = stripped.split(None, 3)[3].strip()
        _validate_instruction(instruction)
        return WorkCommand(operation="continue", task_id=tokens[2], instruction=instruction)
    if action not in SUPPORTED_PROJECTS:
        raise WorkCommandError("work_project_unknown", "当前只注册了 renovation-hub 项目别名。")
    if len(tokens) < 3:
        raise WorkCommandError("work_instruction_required", "开发任务不能为空。")
    instruction = stripped.split(None, 2)[2].strip()
    _validate_instruction(instruction)
    return WorkCommand(operation="start", project_alias=action, instruction=instruction)


def select_work_route(
    text: object,
    *,
    role: str,
    has_attachments: bool = False,
    v2_enabled: bool = False,
    v1_available: bool = True,
) -> RouteDecision:
    """Select exactly one route without executing it.

    V2 is default-off.  While it is off, every exact ``/work`` message is
    returned to the existing v1 path without v2 parsing, preserving v1 error
    ordering and disabled-mode behavior.  Once v2 is enabled it exclusively
    owns the message; parse, authorization, attachment, client, and response
    failures must not fall back to v1.
    """
    if not is_exact_work_command(text):
        return RouteDecision(route=ROUTE_ORDINARY, reason="not_exact_work_command")
    if not v2_enabled:
        if v1_available:
            return RouteDecision(route=ROUTE_V1, reason="v2_disabled_v1_selected")
        return _rejected(
            "remote_work_disabled",
            "Remote Work 当前未启用；普通微信消息不受影响。",
            reason="v2_disabled_v1_unavailable",
        )

    assert isinstance(text, str)
    try:
        command = parse_work_command(text)
    except WorkCommandError as exc:
        return _rejected(exc.code, str(exc), reason="v2_command_rejected")
    assert command is not None
    if has_attachments:
        return _rejected(
            "work_attachments_unsupported",
            "Runner Manager 命令不能携带附件；请把要求写在命令正文中。",
            reason="v2_attachment_rejected",
        )
    if role != "owner":
        return _rejected(
            "work_owner_required",
            "当前账号没有 /work 权限。",
            reason="v2_owner_required",
        )
    return RouteDecision(route=ROUTE_V2, reason="v2_selected", command=command)


def build_request_id(*, identity_id: str, message_id: str) -> str:
    """Create a stable opaque request ID from the routed message identity."""
    _validate_opaque_id(identity_id, "identity_id")
    _validate_opaque_id(message_id, "message_id")
    digest = hashlib.sha256(
        b"weixin-runner-manager-v2\x00"
        + identity_id.encode("utf-8")
        + b"\x00"
        + message_id.encode("utf-8")
    ).hexdigest()
    return f"WRV2-{digest[:32]}"


def build_runner_manager_request(
    command: WorkCommand,
    *,
    identity_id: str,
    message_id: str,
    principal_hash: str,
) -> RunnerManagerRequest:
    """Build one authenticated-transport-neutral Controller request."""
    request_id = build_request_id(identity_id=identity_id, message_id=message_id)
    body: dict[str, Any] = {
        "version": PROTOCOL_VERSION,
        "request_id": request_id,
        "operation": command.operation,
        "source": {
            "channel": "weixin",
            "principal_hash": _normalise_principal_hash(principal_hash),
            "role": "owner",
        },
    }
    if command.operation == "start":
        body["project_alias"] = command.project_alias
        body["instruction"] = command.instruction
    elif command.operation == "continue":
        body["task_id"] = command.task_id
        body["instruction"] = command.instruction
    elif command.operation in {"status", "cancel"}:
        body["task_id"] = command.task_id
    else:
        raise RunnerManagerContractError("operation_invalid", "Runner Manager operation is invalid")
    validated = validate_runner_manager_request(body)
    canonical = _canonical_json(validated)
    return RunnerManagerRequest(
        method="POST",
        path=RUNNER_MANAGER_COMMAND_PATH,
        request_id=request_id,
        body=validated,
        body_digest="sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
    )


def validate_runner_manager_request(payload: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(payload, Mapping):
        raise RunnerManagerContractError("invalid_payload", "Runner Manager request must be an object")
    operation = payload.get("operation")
    required_by_operation = {
        "start": {"version", "request_id", "operation", "source", "project_alias", "instruction"},
        "status": {"version", "request_id", "operation", "source", "task_id"},
        "continue": {"version", "request_id", "operation", "source", "task_id", "instruction"},
        "cancel": {"version", "request_id", "operation", "source", "task_id"},
    }
    required = required_by_operation.get(operation)
    if required is None:
        raise RunnerManagerContractError("operation_invalid", "Runner Manager operation is invalid")
    if set(payload) != required:
        raise RunnerManagerContractError("invalid_payload", "Runner Manager request fields are invalid")
    if payload.get("version") != PROTOCOL_VERSION:
        raise RunnerManagerContractError("version_unsupported", "Runner Manager version is unsupported")
    _validate_request_id(payload.get("request_id"))
    source = payload.get("source")
    if not isinstance(source, Mapping) or set(source) != {"channel", "principal_hash", "role"}:
        raise RunnerManagerContractError("source_invalid", "Runner Manager source is invalid")
    if source.get("channel") != "weixin" or source.get("role") != "owner":
        raise RunnerManagerContractError("source_invalid", "Runner Manager source is invalid")
    _normalise_principal_hash(source.get("principal_hash"))

    if operation == "start":
        if payload.get("project_alias") not in SUPPORTED_PROJECTS:
            raise RunnerManagerContractError("work_project_unknown", "Runner Manager project is unknown")
        _validate_instruction_value(payload.get("instruction"))
    else:
        _validate_task_id(payload.get("task_id"))
        if operation == "continue":
            _validate_instruction_value(payload.get("instruction"))
    validated = dict(payload)
    _validate_document_size(validated)
    return validated


def parse_runner_manager_response(
    status_code: int,
    payload: Mapping[str, Any],
    *,
    expected_request_id: str,
    expected_operation: str,
) -> RunnerManagerResult:
    """Validate a bounded safe response, raising for bounded API errors."""
    if not isinstance(status_code, int) or isinstance(status_code, bool) or not 100 <= status_code <= 599:
        raise RunnerManagerContractError("status_code_invalid", "HTTP status code is invalid")
    if not isinstance(payload, Mapping):
        raise RunnerManagerContractError("invalid_payload", "Runner Manager response must be an object")
    _validate_document_size(payload)
    _validate_request_id(expected_request_id)
    if expected_operation not in {"start", "status", "continue", "cancel"}:
        raise RunnerManagerContractError("operation_invalid", "expected operation is invalid")

    if not 200 <= status_code <= 299:
        _raise_response_error(status_code, payload, expected_request_id=expected_request_id)

    required = {"version", "request_id", "operation", "task_id", "state", "updated_at"}
    allowed = required | {
        "stage",
        "summary",
        "candidate_id",
        "test_summary",
        "changed_path_count",
        "next_actions",
        "error_code",
        "action_required",
        "queue_position",
    }
    if not required.issubset(payload) or not set(payload).issubset(allowed):
        raise RunnerManagerContractError("invalid_payload", "Runner Manager result fields are invalid")
    if payload.get("version") != PROTOCOL_VERSION:
        raise RunnerManagerContractError("version_unsupported", "Runner Manager version is unsupported")
    request_id = _validate_request_id(payload.get("request_id"))
    if request_id != expected_request_id:
        raise RunnerManagerContractError("request_id_mismatch", "Runner Manager request ID does not match")
    operation = payload.get("operation")
    if operation != expected_operation:
        raise RunnerManagerContractError("operation_mismatch", "Runner Manager operation does not match")
    task_id = _validate_task_id(payload.get("task_id"))
    state = payload.get("state")
    if state not in TASK_STATES:
        raise RunnerManagerContractError("state_invalid", "Runner Manager task state is invalid")
    updated_at = _parse_time(payload.get("updated_at"), "updated_at").isoformat()
    stage = _optional_safe_value(payload.get("stage"), "stage", 64)
    summary = _optional_text(payload.get("summary"), "summary", MAX_SUMMARY_CHARS)
    candidate_id = payload.get("candidate_id")
    if candidate_id is not None and (not isinstance(candidate_id, str) or not _CANDIDATE_RE.fullmatch(candidate_id)):
        raise RunnerManagerContractError("invalid_payload", "candidate_id is invalid")
    test_summary = _optional_text(payload.get("test_summary"), "test_summary", MAX_TEST_SUMMARY_CHARS)
    error_code = _optional_code(payload.get("error_code"), "error_code")
    action_required = _optional_text(payload.get("action_required"), "action_required", MAX_ACTION_CHARS)
    changed_path_count = _optional_bounded_int(
        payload.get("changed_path_count"), "changed_path_count", maximum=10_000
    )
    queue_position = _optional_bounded_int(payload.get("queue_position"), "queue_position", maximum=10_000)
    next_actions_value = payload.get("next_actions", [])
    if not isinstance(next_actions_value, list) or len(next_actions_value) > MAX_ACTIONS:
        raise RunnerManagerContractError("invalid_payload", "next_actions is invalid")
    next_actions: list[str] = []
    for value in next_actions_value:
        if not isinstance(value, str) or not value or len(value) > MAX_ACTION_CHARS or _has_forbidden_control(value):
            raise RunnerManagerContractError("invalid_payload", "next_actions is invalid")
        next_actions.append(value)
    return RunnerManagerResult(
        request_id=request_id,
        operation=operation,
        task_id=task_id,
        state=state,
        updated_at=updated_at,
        stage=stage,
        summary=summary,
        candidate_id=candidate_id,
        test_summary=test_summary,
        changed_path_count=changed_path_count,
        next_actions=tuple(next_actions),
        error_code=error_code,
        action_required=action_required,
        queue_position=queue_position,
    )


def _rejected(code: str, message: str, *, reason: str) -> RouteDecision:
    return RouteDecision(
        route=ROUTE_REJECT,
        reason=reason,
        error_code=code,
        public_message=message,
    )


def _validate_instruction(instruction: str) -> None:
    try:
        _validate_instruction_value(instruction)
    except RunnerManagerContractError as exc:
        raise WorkCommandError(exc.code, str(exc)) from exc


def _validate_instruction_value(value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise RunnerManagerContractError("work_instruction_required", "开发任务不能为空。")
    if len(value) > MAX_INSTRUCTION_CHARS:
        raise RunnerManagerContractError(
            "work_instruction_too_long",
            f"开发任务不能超过 {MAX_INSTRUCTION_CHARS} 个字符。",
        )
    if _has_forbidden_control(value):
        raise RunnerManagerContractError("work_instruction_invalid", "开发任务包含无效控制字符。")
    for code, pattern in _FORBIDDEN_INSTRUCTION_PATTERNS:
        if pattern.search(value):
            messages = {
                "work_path_forbidden": "微信任务不能指定任意路径或工作树。",
                "work_shell_forbidden": "微信任务不能指定 Shell 或任意命令。",
                "work_model_forbidden": "微信任务不能指定模型。",
                "work_sandbox_forbidden": "微信任务不能指定 sandbox。",
                "work_git_ref_forbidden": "微信任务不能指定 Git ref、分支或标签。",
                "work_remote_forbidden": "微信任务不能指定仓库 remote、URL 或网络地址。",
                "production_confirmation_required": "微信任务不能请求部署；生产发布需要独立受控确认。",
            }
            raise RunnerManagerContractError(code, messages[code])
    return value


def _validate_opaque_id(value: Any, name: str) -> str:
    if not isinstance(value, str) or not _OPAQUE_ID_RE.fullmatch(value):
        raise RunnerManagerContractError("source_id_invalid", f"{name} is invalid")
    return value


def _validate_request_id(value: Any) -> str:
    if not isinstance(value, str) or not _REQUEST_ID_RE.fullmatch(value):
        raise RunnerManagerContractError("request_id_invalid", "Runner Manager request ID is invalid")
    return value


def _validate_task_id(value: Any) -> str:
    if not isinstance(value, str) or not _TASK_ID_RE.fullmatch(value):
        raise RunnerManagerContractError("work_task_id_invalid", "Runner Manager task ID is invalid")
    return value


def _normalise_principal_hash(value: Any) -> str:
    if not isinstance(value, str):
        raise RunnerManagerContractError("source_invalid", "principal hash is invalid")
    digest = value[7:] if value.startswith("sha256:") else value
    if not _SHA256_RE.fullmatch(digest):
        raise RunnerManagerContractError("source_invalid", "principal hash is invalid")
    return f"sha256:{digest}"


def _raise_response_error(
    status_code: int,
    payload: Mapping[str, Any],
    *,
    expected_request_id: str,
) -> None:
    required = {"version", "request_id", "error_code", "message"}
    allowed = required | {"retryable"}
    if not required.issubset(payload) or not set(payload).issubset(allowed):
        raise RunnerManagerContractError("invalid_payload", "Runner Manager error fields are invalid")
    if payload.get("version") != PROTOCOL_VERSION:
        raise RunnerManagerContractError("version_unsupported", "Runner Manager version is unsupported")
    request_id = _validate_request_id(payload.get("request_id"))
    if request_id != expected_request_id:
        raise RunnerManagerContractError("request_id_mismatch", "Runner Manager request ID does not match")
    error_code = _optional_code(payload.get("error_code"), "error_code")
    message = _optional_text(payload.get("message"), "message", MAX_ACTION_CHARS)
    if error_code is None or message is None or not message:
        raise RunnerManagerContractError("invalid_payload", "Runner Manager error is invalid")
    retryable = payload.get("retryable", False)
    if not isinstance(retryable, bool):
        raise RunnerManagerContractError("invalid_payload", "Runner Manager retryable flag is invalid")
    raise RunnerManagerResponseError(
        error_code,
        message,
        status_code=status_code,
        request_id=request_id,
        retryable=retryable,
    )


def _optional_text(value: Any, name: str, maximum: int) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or len(value) > maximum or _has_forbidden_control(value):
        raise RunnerManagerContractError("invalid_payload", f"{name} is invalid")
    return value


def _optional_safe_value(value: Any, name: str, maximum: int) -> str | None:
    if value is None:
        return None
    if (
        not isinstance(value, str)
        or len(value) > maximum
        or not _SAFE_VALUE_RE.fullmatch(value)
    ):
        raise RunnerManagerContractError("invalid_payload", f"{name} is invalid")
    return value


def _optional_code(value: Any, name: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not _SAFE_CODE_RE.fullmatch(value):
        raise RunnerManagerContractError("invalid_payload", f"{name} is invalid")
    return value


def _optional_bounded_int(value: Any, name: str, *, maximum: int) -> int | None:
    if value is None:
        return None
    if not isinstance(value, int) or isinstance(value, bool) or not 0 <= value <= maximum:
        raise RunnerManagerContractError("invalid_payload", f"{name} is invalid")
    return value


def _parse_time(value: Any, name: str) -> dt.datetime:
    if not isinstance(value, str):
        raise RunnerManagerContractError("invalid_payload", f"{name} is invalid")
    try:
        parsed = dt.datetime.fromisoformat(value)
    except ValueError as exc:
        raise RunnerManagerContractError("invalid_payload", f"{name} is invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise RunnerManagerContractError("invalid_payload", f"{name} needs a timezone")
    return parsed


def _has_forbidden_control(value: str) -> bool:
    return any(ord(character) < 32 and character not in {"\n", "\r", "\t"} for character in value)


def _canonical_json(payload: Mapping[str, Any]) -> str:
    try:
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    except (TypeError, ValueError) as exc:
        raise RunnerManagerContractError("invalid_payload", "document is not JSON serializable") from exc


def _validate_document_size(payload: Mapping[str, Any]) -> None:
    if len(_canonical_json(payload).encode("utf-8")) > MAX_DOCUMENT_BYTES:
        raise RunnerManagerContractError("payload_too_large", "Runner Manager document is too large")


__all__ = [
    "PROTOCOL_VERSION",
    "ROUTE_ORDINARY",
    "ROUTE_REJECT",
    "ROUTE_V1",
    "ROUTE_V2",
    "RUNNER_MANAGER_COMMAND_PATH",
    "RunnerManagerContractError",
    "RunnerManagerRequest",
    "RunnerManagerResponseError",
    "RunnerManagerResult",
    "RunnerManagerTransport",
    "RouteDecision",
    "SUPPORTED_PROJECTS",
    "WorkCommand",
    "WorkCommandError",
    "build_request_id",
    "build_runner_manager_request",
    "is_exact_work_command",
    "parse_runner_manager_response",
    "parse_work_command",
    "select_work_route",
    "validate_runner_manager_request",
]
