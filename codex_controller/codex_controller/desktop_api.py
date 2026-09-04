"""HTTP route helpers for the Desktop takeover API."""

from __future__ import annotations

import re
from typing import Any, Mapping
from urllib.parse import parse_qs

from .desktop_service import DesktopControllerService
from .store import StoreError


THREAD_PATH_RE = re.compile(r"^/api/desktop/v1/threads/(TH-[A-Z2-7]{20,52})$")
EVENTS_PATH_RE = re.compile(r"^/api/desktop/v1/threads/(TH-[A-Z2-7]{20,52})/events$")
ACTION_PATH_RE = re.compile(
    r"^/api/desktop/v1/threads/(TH-[A-Z2-7]{20,52})/(steer|interrupt|continue|archive|unarchive)$"
)


def get_desktop_api(
    service: DesktopControllerService,
    path: str,
    query: str,
) -> dict[str, Any]:
    parameters = _query(query)
    if path == "/api/desktop/v1/hosts":
        _only(parameters, set())
        return service.hosts()
    if path == "/api/desktop/v1/projects":
        _only(parameters, {"host_ref"})
        return service.projects(host_ref=_one(parameters, "host_ref"))
    if path == "/api/desktop/v1/threads":
        _only(parameters, {"host_ref", "project_ref", "status", "cursor", "limit"})
        return service.threads(
            host_ref=_one(parameters, "host_ref"),
            project_ref=_one(parameters, "project_ref"),
            status=_one(parameters, "status"),
            after_cursor=_integer(parameters, "cursor", default=0, minimum=0, maximum=2**63 - 1),
            limit=_integer(parameters, "limit", default=100, minimum=1, maximum=200),
        )
    thread_match = THREAD_PATH_RE.fullmatch(path)
    if thread_match:
        _only(parameters, set())
        return service.thread(thread_match.group(1))
    event_match = EVENTS_PATH_RE.fullmatch(path)
    if event_match:
        _only(parameters, {"after_cursor", "limit", "wait_seconds"})
        return service.events(
            event_match.group(1),
            after_cursor=_integer(
                parameters, "after_cursor", default=0, minimum=0, maximum=2**63 - 1
            ),
            limit=_integer(parameters, "limit", default=100, minimum=1, maximum=500),
            wait_seconds=_number(parameters, "wait_seconds", default=0.0, minimum=0.0, maximum=25.0),
        )
    raise StoreError("not_found", "Desktop API 路由不存在", status=404)


def post_desktop_api(
    service: DesktopControllerService,
    path: str,
    payload: Mapping[str, Any],
) -> dict[str, Any]:
    if path == "/api/desktop/v1/threads":
        return service.create(payload)
    match = ACTION_PATH_RE.fullmatch(path)
    if match is None:
        raise StoreError("not_found", "Desktop API 路由不存在", status=404)
    return service.submit(match.group(1), match.group(2), payload)


def _query(value: str) -> dict[str, list[str]]:
    try:
        parameters = parse_qs(value, keep_blank_values=True, strict_parsing=True)
    except ValueError as exc:
        raise StoreError("desktop_query_invalid", "Desktop API query 无效", status=400) from exc
    if any(len(items) != 1 or items[0] == "" for items in parameters.values()):
        raise StoreError("desktop_query_invalid", "Desktop API query 字段重复或为空", status=400)
    return parameters


def _only(parameters: Mapping[str, list[str]], allowed: set[str]) -> None:
    if set(parameters) - allowed:
        raise StoreError("desktop_query_invalid", "Desktop API query 包含未知字段", status=400)


def _one(parameters: Mapping[str, list[str]], name: str) -> str | None:
    values = parameters.get(name)
    return None if values is None else values[0]


def _integer(
    parameters: Mapping[str, list[str]],
    name: str,
    *,
    default: int,
    minimum: int,
    maximum: int,
) -> int:
    value = _one(parameters, name)
    if value is None:
        return default
    try:
        parsed = int(value)
    except ValueError as exc:
        raise StoreError("desktop_query_invalid", f"Desktop API {name} 无效", status=400) from exc
    if str(parsed) != value or not minimum <= parsed <= maximum:
        raise StoreError("desktop_query_invalid", f"Desktop API {name} 无效", status=400)
    return parsed


def _number(
    parameters: Mapping[str, list[str]],
    name: str,
    *,
    default: float,
    minimum: float,
    maximum: float,
) -> float:
    value = _one(parameters, name)
    if value is None:
        return default
    try:
        parsed = float(value)
    except ValueError as exc:
        raise StoreError("desktop_query_invalid", f"Desktop API {name} 无效", status=400) from exc
    if not minimum <= parsed <= maximum:
        raise StoreError("desktop_query_invalid", f"Desktop API {name} 无效", status=400)
    return parsed


__all__ = [
    "ACTION_PATH_RE",
    "EVENTS_PATH_RE",
    "THREAD_PATH_RE",
    "get_desktop_api",
    "post_desktop_api",
]
