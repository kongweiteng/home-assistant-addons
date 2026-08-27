"""Safe Codex turn error classification and retry evidence helpers."""

from __future__ import annotations

from dataclasses import dataclass
import secrets
from typing import Any


RETRYABLE_HTTP_STATUSES = frozenset({408, 429})
BENIGN_ITEM_TYPES = frozenset(
    {
        "userMessage",
        "hookPrompt",
        "agentMessage",
        "plan",
        "reasoning",
        "contextCompaction",
    }
)
ARTIFACT_ITEM_TYPES = frozenset({"imageGeneration"})


@dataclass(frozen=True)
class TurnErrorClassification:
    """Only bounded, non-sensitive fields that may be persisted."""

    error_type: str
    error_code: str
    upstream_http_status: int | None
    retryable: bool


STRING_ERROR_TYPES: dict[str, tuple[str, str, bool]] = {
    "contextWindowExceeded": ("context_window_exceeded", "context_window_exceeded", False),
    "sessionBudgetExceeded": ("session_budget_exceeded", "session_budget_exceeded", False),
    "usageLimitExceeded": ("usage_limit_exceeded", "usage_limit_exceeded", False),
    "serverOverloaded": ("server_overloaded", "app_server_overloaded", True),
    "cyberPolicy": ("cyber_policy", "cyber_policy_rejected", False),
    "internalServerError": ("internal_server_error", "upstream_internal_server_error", True),
    "unauthorized": ("unauthorized", "codex_unauthorized", False),
    "badRequest": ("bad_request", "codex_bad_request", False),
    "threadRollbackFailed": ("thread_rollback_failed", "thread_rollback_failed", False),
    "sandboxError": ("sandbox_error", "sandbox_error", False),
    "other": ("other", "turn_failed", False),
}

OBJECT_ERROR_TYPES: dict[str, tuple[str, str, bool]] = {
    "httpConnectionFailed": ("http_connection_failed", "upstream_http_connection_failed", True),
    "responseStreamConnectionFailed": (
        "response_stream_connection_failed",
        "response_stream_connection_failed",
        True,
    ),
    "responseStreamDisconnected": (
        "response_stream_disconnected",
        "response_stream_disconnected",
        True,
    ),
    "responseTooManyFailedAttempts": (
        "response_too_many_failed_attempts",
        "response_too_many_failed_attempts",
        True,
    ),
    "activeTurnNotSteerable": (
        "active_turn_not_steerable",
        "active_turn_not_steerable",
        False,
    ),
}


def _http_status(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or not 100 <= value <= 599:
        return None
    return value


def _http_retryable(status: int | None) -> bool:
    return status is None or status in RETRYABLE_HTTP_STATUSES or 500 <= status <= 599


def classify_turn_error(error: Any) -> TurnErrorClassification:
    """Classify a v2 TurnError without retaining message or additionalDetails."""

    info = error.get("codexErrorInfo") if isinstance(error, dict) else None
    if isinstance(info, str):
        mapped = STRING_ERROR_TYPES.get(info)
        if mapped is not None:
            error_type, error_code, retryable = mapped
            return TurnErrorClassification(error_type, error_code, None, retryable)
        return TurnErrorClassification("unknown", "turn_failed", None, False)
    if isinstance(info, dict) and len(info) == 1:
        variant, details = next(iter(info.items()))
        mapped = OBJECT_ERROR_TYPES.get(variant)
        if mapped is not None:
            error_type, error_code, retryable_by_type = mapped
            status = _http_status(details.get("httpStatusCode")) if isinstance(details, dict) else None
            retryable = retryable_by_type
            if variant != "activeTurnNotSteerable":
                retryable = retryable_by_type and _http_retryable(status)
            return TurnErrorClassification(error_type, error_code, status, retryable)
    return TurnErrorClassification("unknown", "turn_failed", None, False)


def item_observations(item: Any) -> tuple[bool, bool, bool, str | None]:
    """Return output, tool/activity, artifact and safe item type observations."""

    if not isinstance(item, dict):
        return False, False, False, None
    item_type = item.get("type")
    if not isinstance(item_type, str) or not item_type:
        return False, False, False, None
    output_observed = False
    if item_type == "agentMessage":
        output_observed = isinstance(item.get("text"), str) and bool(item["text"])
    elif item_type == "plan":
        output_observed = isinstance(item.get("text"), str) and bool(item["text"])
    elif item_type == "reasoning":
        output_observed = bool(item.get("summary")) or bool(item.get("content"))
    tool_activity_observed = item_type not in BENIGN_ITEM_TYPES
    artifact_observed = item_type in ARTIFACT_ITEM_TYPES
    return output_observed, tool_activity_observed, artifact_observed, item_type[:64]


def turn_observations(items: Any) -> tuple[bool, bool, bool]:
    output_observed = False
    tool_activity_observed = False
    artifact_observed = False
    if not isinstance(items, list):
        return output_observed, tool_activity_observed, artifact_observed
    for item in items:
        output, activity, artifact, _item_type = item_observations(item)
        output_observed = output_observed or output
        tool_activity_observed = tool_activity_observed or activity
        artifact_observed = artifact_observed or artifact
    return output_observed, tool_activity_observed, artifact_observed


def retry_delay_seconds(attempt: int) -> float:
    """Exponential delay with bounded cryptographic jitter for attempts 1 and 2."""

    base = min(2 ** max(1, int(attempt)), 8)
    return base + secrets.randbelow(1001) / 1000
