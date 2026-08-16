"""Path router for Runner Center v2 APIs.

Authentication, CSRF, request-size limits, and response headers remain owned by
the existing Controller HTTP server. This module only maps deterministic paths
to the Runner Manager service.
"""

from __future__ import annotations

import re
from typing import Any

from .runner_service import RunnerManagerService


RUNNER_PATH_RE = re.compile(r"^/api/runners/(RN-[A-Z2-7]{20,32})$")
RUNNER_ACTION_RE = re.compile(
    r"^/api/runners/(RN-[A-Z2-7]{20,32})/(self-check|drain|emergency-disable|credential-rotation|enrollment-revocation|enrollment-regeneration|recovery-resolution)$"
)


def get_runner_api(service: RunnerManagerService, path: str) -> dict[str, Any] | None:
    if path == "/api/runners":
        return service.list_runners()
    if path == "/api/runner-tasks":
        return service.list_tasks()
    match = RUNNER_PATH_RE.fullmatch(path)
    if match:
        return service.runner(match.group(1))
    return None


def post_runner_api(
    service: RunnerManagerService,
    path: str,
    payload: dict[str, Any],
) -> dict[str, Any] | None:
    if path == "/api/runner-enrollments":
        return service.create_enrollment(payload)
    match = RUNNER_ACTION_RE.fullmatch(path)
    if match is None:
        return None
    runner_id, action = match.groups()
    if action == "self-check":
        return service.request_self_check(runner_id, payload)
    if action == "drain":
        return service.drain(runner_id, payload)
    if action == "emergency-disable":
        return service.emergency_disable(runner_id, payload)
    if action == "credential-rotation":
        return service.rotate_credential(runner_id, payload)
    if action == "recovery-resolution":
        return service.resolve_task_recovery(runner_id, payload)
    if action == "enrollment-revocation":
        return service.revoke_enrollment(runner_id, payload)
    if action == "enrollment-regeneration":
        return service.regenerate_enrollment(runner_id, payload)
    raise AssertionError(action)


def patch_runner_api(
    service: RunnerManagerService,
    path: str,
    payload: dict[str, Any],
) -> dict[str, Any] | None:
    match = RUNNER_PATH_RE.fullmatch(path)
    if match is None:
        return None
    return service.update_runner(match.group(1), payload)


def delete_runner_api(
    service: RunnerManagerService,
    path: str,
    payload: dict[str, Any],
) -> dict[str, Any] | None:
    match = RUNNER_PATH_RE.fullmatch(path)
    if match is None:
        return None
    return service.delete_runner(match.group(1), payload)


__all__ = [
    "RUNNER_ACTION_RE",
    "RUNNER_PATH_RE",
    "delete_runner_api",
    "get_runner_api",
    "patch_runner_api",
    "post_runner_api",
]
