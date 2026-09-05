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
    "response_stream_disconnected": ("回复连接中断", "warn", True),
    "context_window_exceeded": ("对话内容过长", "warn", False),
    "codex_unauthorized": ("Codex 认证不可用", "bad", False),
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
}
_GATEWAY_DETAIL_TEXTS: dict[str, tuple[str, str]] = {
    "response_stream_disconnected": (
        "上游响应流中断，自动重试后仍未完成。请稍后再试。",
        "检查网络和 Runner 在线状态，恢复后重试。",
    ),
    "context_window_exceeded": (
        "这次对话内容过长，已停止处理。请新开一个对话或缩短问题后重试。",
        "新建任务，或缩短输入和历史上下文后重试。",
    ),
    "codex_unauthorized": (
        "Codex 登录已失效，任务未执行完成。请在 Controller 页面重新登录后再试。",
        "在 Codex Controller 中恢复登录后重试。",
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
        return {"version": 1, "error_revision": self._revision(items), "summary": {"total": len(items), "components": sum(i["scope"] == "component" for i in items), "tasks": sum(i["scope"] == "task" for i in items), "retryable": sum(bool(i["error"]["retryable"]) for i in items)}, "page": {"cursor": cursor, "limit": limit, "next_cursor": next_cursor if next_cursor < len(items) else None, "has_more": next_cursor < len(items)}, "items": visible}

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
        return sorted(gateway + self._component_items(sync_error) + self._task_items(), key=lambda i: (i.get("occurred_at") is not None, i.get("occurred_at") or "", {"bad": 2, "warn": 1}.get(i["error"]["severity"], 0), i["reference"]), reverse=True)

    def _gateway_item(self, value: Any) -> dict[str, Any] | None:
        if not isinstance(value, Mapping) or not isinstance(value.get("failure_short"), str) or not FAILURE_REF_RE.fullmatch(value["failure_short"]) or value.get("source") not in {"controller", "gateway"}: return None
        error = value.get("error")
        fallback = "controller_task_failed" if value["source"] == "controller" else "gateway_delivery_failed"
        public_error = _error(error.get("code") if isinstance(error, Mapping) else None, fallback=fallback)
        result = {"scope": "component", "origin": "wechat", "reference": value["failure_short"], "title": "微信任务未完成" if value["source"] == "controller" else "微信消息处理异常", "occurred_at": _safe_time(value.get("occurred_at")), "error": public_error}
        result.update(_safe_gateway_detail(error, public_error["code"]))
        if isinstance(value.get("job_short"), str) and JOB_REF_RE.fullmatch(value["job_short"]): result["task_reference"] = value["job_short"]
        return result

    def _component_items(self, sync_error: bool) -> list[dict[str, Any]]:
        try: status = self.component_status()
        except Exception: status = {"controller_start_error": "controller_start_failed"}
        result = []
        for key, title, fallback in (("controller_start_error", "Controller", "controller_start_failed"), ("controller_auth_error", "Codex 认证", "app_server_unavailable"), ("app_server_error", "Codex 服务", "app_server_unavailable"), ("runner_error", "Runner", "runner_manager_internal_error")):
            if status.get(key): result.append({"scope": "component", "origin": "controller", "reference": f"CP-{key}", "title": title, "occurred_at": None, "error": _error(status[key], fallback=fallback)})
        if sync_error: result.append({"scope": "component", "origin": "wechat", "reference": "CP-gateway-sync", "title": "微信机器人", "occurred_at": None, "error": _error("gateway_failure_sync_unavailable")})
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
            if isinstance(reference, str) and THREAD_REF_RE.fullmatch(reference) and code: result.append({"scope": "task", "origin": "desktop", "reference": reference, "task_href": f"desktop/?thread_ref={reference}", "title": "Mac 任务需要处理", "occurred_at": _safe_time(thread.get("updated_at")), "error": _error(code)})
        return result

    def _frame(self, event: str, cursor: int, document: Mapping[str, Any]) -> dict[str, Any]: return {"event": event, "cursor": cursor, "data": {"version": 1, "cursor": cursor, "error_revision": document["error_revision"], "summary": document["summary"], "server_time": self._now().isoformat()}}
    @staticmethod
    def _revision(items: list[dict[str, Any]]) -> str: return hashlib.sha256(json.dumps(items, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()[:24]
    def _now(self) -> dt.datetime:
        value = self.now(); return value.astimezone(SHANGHAI) if value.tzinfo is not None and value.utcoffset() is not None else dt.datetime.now(SHANGHAI)

__all__ = ["ErrorCenter"]
