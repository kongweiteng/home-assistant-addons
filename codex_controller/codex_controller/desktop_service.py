"""Controller service for Desktop snapshots, cursors, and fail-closed commands."""

from __future__ import annotations

from contextlib import contextmanager
import datetime as dt
import json
import re
import sqlite3
import threading
import time
from typing import Any, Callable, Iterator, Mapping, Protocol
from zoneinfo import ZoneInfo

from .desktop_protocol import (
    DesktopProtocolError,
    REF_RE,
    REQUEST_RE,
    THREAD_STATUSES,
    build_desktop_command,
    canonical_json,
    intent_digest,
    validate_desktop_document,
    validate_public_input,
)
from .desktop_store import DesktopStore
from .runner_relay import RelayPublishError
from .store import StoreError


SHANGHAI = ZoneInfo("Asia/Shanghai")
SSE_EVENT_BATCH_LIMIT = 100
SSE_HEARTBEAT_SECONDS = 12.0
DATA_FRESH_SECONDS = 15
DATA_DELAYED_SECONDS = 30
QUEUE_ACTIONS = frozenset(
    {"queue_add", "queue_update", "queue_delete", "queue_reorder", "queue_start"}
)


class DesktopPublisher(Protocol):
    def publish_desktop_command(self, runner_id: str, document: dict[str, Any]) -> None:
        ...


class DesktopControllerService:
    def __init__(
        self,
        store: DesktopStore,
        *,
        publisher: DesktopPublisher | None,
        now: Callable[[], dt.datetime] | None = None,
        host_stale_seconds: int = 90,
        runner_authorizer: Callable[[str], bool] | None = None,
        runner_status_provider: Callable[[str], Mapping[str, Any]] | None = None,
    ) -> None:
        if not 10 <= host_stale_seconds <= 900:
            raise ValueError("Desktop host stale policy 无效")
        self.store = store
        self.publisher = publisher
        self.now = now or (lambda: dt.datetime.now(dt.timezone.utc))
        self.host_stale_seconds = host_stale_seconds
        self.runner_authorizer = runner_authorizer
        self.runner_status_provider = runner_status_provider
        self._event_condition = threading.Condition()
        self._change_sequence = 0
        self._broadcast_sequence = 0
        self._host_change_sequences: dict[str, int] = {}
        self._thread_change_sequences: dict[str, int] = {}
        self._command_lock = threading.Lock()
        self._create_journal = _DesktopCreateJournal(store.database_path)

    def receive(self, event_type: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        try:
            document = validate_desktop_document(event_type, payload)
        except DesktopProtocolError as exc:
            raise StoreError(exc.code, str(exc), status=409) from exc
        current = self._now()
        created_at = self._parse_time(document.get("created_at"))
        host = document.get("host")
        host_synced_at = self._parse_time(host.get("synced_at")) if isinstance(host, Mapping) else None
        if created_at is None or created_at > current + dt.timedelta(minutes=5):
            raise StoreError("desktop_clock_skew", "Desktop 消息时间超出允许范围", status=409)
        if host_synced_at is not None and host_synced_at > current + dt.timedelta(minutes=5):
            raise StoreError("desktop_clock_skew", "Desktop host 同步时间超出允许范围", status=409)
        if event_type == "desktop_snapshot":
            result = self.store.ingest_snapshot(document, observed_at=current.isoformat())
        elif event_type == "desktop_event":
            result = self.store.ingest_event(document)
        elif document.get("action") == "create":
            result = self._create_journal.ingest_receipt(document)
        else:
            result = self.store.ingest_receipt(document)
        self._notify_change(
            host_ref=str(document.get("host_ref") or "") or None,
            thread_ref=str(document.get("thread_ref") or "") or None,
        )
        return {"accepted": True, **result}

    def sweep(self) -> int:
        current = self._now().isoformat()
        changed = self.store.sweep_commands(now=current) + self._create_journal.sweep(now=current)
        if changed:
            self._notify_change()
        return changed

    def hosts(self) -> dict[str, Any]:
        current = self._now()
        hosts = self.store.list_hosts()
        for host in hosts:
            online = False
            connection_observed_at: str | None = None
            runner_id = self.store.host_runner_id(str(host["host_ref"]))
            if self.runner_status_provider is not None:
                try:
                    runner = self.runner_status_provider(runner_id)
                except StoreError:
                    runner = {}
                online = runner.get("connectivity_state") == "online"
                heartbeat_at = runner.get("last_heartbeat_at")
                parsed_heartbeat = self._parse_time(heartbeat_at)
                if parsed_heartbeat is not None:
                    connection_observed_at = parsed_heartbeat.isoformat()
            else:
                synced_at = self._parse_time(host.get("synced_at"))
                online = synced_at is not None and current - synced_at <= dt.timedelta(
                    seconds=self.host_stale_seconds
                )
                if synced_at is not None:
                    connection_observed_at = str(host.get("synced_at"))
            authorized = bool(
                self.runner_authorizer is not None and self.runner_authorizer(runner_id)
            )
            host["online"] = online
            host["connection_observed_at"] = connection_observed_at
            host["data_synced_at"] = host.get("synced_at")
            data_synced_at = self._parse_time(host.get("synced_at"))
            if data_synced_at is None:
                host["data_age_seconds"] = None
                host["data_freshness_state"] = "unknown"
            else:
                data_age_seconds = max(0, int((current - data_synced_at).total_seconds()))
                host["data_age_seconds"] = data_age_seconds
                if data_age_seconds <= DATA_FRESH_SECONDS:
                    host["data_freshness_state"] = "fresh"
                elif data_age_seconds <= DATA_DELAYED_SECONDS:
                    host["data_freshness_state"] = "delayed"
                else:
                    host["data_freshness_state"] = "stale"
            host["write_available"] = bool(
                online
                and authorized
                and host.get("control_enabled") is True
                and self.publisher is not None
            )
        return {
            "hosts": hosts,
            "relay_configured": self.publisher is not None,
            "server_time": current.isoformat(),
        }

    def projects(self, *, host_ref: str | None = None) -> dict[str, Any]:
        if host_ref is not None:
            self._ref(host_ref, "HS")
        return {"projects": self.store.list_projects(host_ref=host_ref)}

    def threads(
        self,
        *,
        host_ref: str | None,
        project_ref: str | None,
        status: str | None,
        after_cursor: int,
        limit: int,
        order: str | None = None,
    ) -> dict[str, Any]:
        if host_ref is not None:
            self._ref(host_ref, "HS")
        if project_ref is not None:
            self._ref(project_ref, "PJ")
        if status is not None and status not in THREAD_STATUSES:
            raise StoreError("desktop_status_invalid", "Desktop Thread 状态筛选无效", status=400)
        if after_cursor < 0 or not 1 <= limit <= 200 or order not in {None, "recent"}:
            raise StoreError("desktop_cursor_invalid", "Desktop Thread cursor 或 limit 无效", status=400)
        if order == "recent":
            return self._recent_threads(
                host_ref=host_ref,
                project_ref=project_ref,
                status=status,
                offset=after_cursor,
                limit=limit,
            )
        return self.store.list_threads(
            host_ref=host_ref,
            project_ref=project_ref,
            status=status,
            after_cursor=after_cursor,
            limit=limit,
        )

    def host_stream(
        self,
        host_ref: str,
        *,
        after_cursor: int | None,
        heartbeat_seconds: float = SSE_HEARTBEAT_SECONDS,
    ) -> Iterator[dict[str, Any]]:
        """Yield bounded host event deltas and observable SSE heartbeats."""
        self._ref(host_ref, "HS")
        self.store.host_runner_id(host_ref)
        yield from self._stream(
            scope_kind="host",
            scope_ref=host_ref,
            after_cursor=after_cursor,
            heartbeat_seconds=heartbeat_seconds,
        )

    def thread_stream(
        self,
        thread_ref: str,
        *,
        after_cursor: int | None,
        heartbeat_seconds: float = SSE_HEARTBEAT_SECONDS,
    ) -> Iterator[dict[str, Any]]:
        """Yield bounded Thread event deltas and observable SSE heartbeats."""
        self._ref(thread_ref, "TH")
        self.store.runner_id(thread_ref)
        yield from self._stream(
            scope_kind="thread",
            scope_ref=thread_ref,
            after_cursor=after_cursor,
            heartbeat_seconds=heartbeat_seconds,
        )

    def thread(self, thread_ref: str) -> dict[str, Any]:
        self._ref(thread_ref, "TH")
        return self.store.thread(thread_ref)

    def events(
        self,
        thread_ref: str,
        *,
        after_cursor: int,
        limit: int,
        wait_seconds: float,
    ) -> dict[str, Any]:
        self._ref(thread_ref, "TH")
        if after_cursor < 0 or not 1 <= limit <= 500 or not 0 <= wait_seconds <= 25:
            raise StoreError("desktop_cursor_invalid", "Desktop event cursor、limit 或 wait 无效", status=400)
        deadline = time.monotonic() + wait_seconds
        with self._event_condition:
            observed_change = self._change_sequence
            while True:
                result = self.store.events(thread_ref, after_cursor=after_cursor, limit=limit)
                if result["events"] or wait_seconds == 0:
                    return {**result, "changed": False}
                if self._change_sequence != observed_change:
                    return {**result, "changed": True}
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return {**result, "changed": False}
                self._event_condition.wait(timeout=remaining)

    def host_events(
        self,
        host_ref: str,
        *,
        after_cursor: int,
        limit: int,
        wait_seconds: float,
    ) -> dict[str, Any]:
        self._ref(host_ref, "HS")
        if after_cursor < 0 or not 1 <= limit <= 500 or not 0 <= wait_seconds <= 25:
            raise StoreError("desktop_cursor_invalid", "Desktop event cursor、limit 或 wait 无效", status=400)
        deadline = time.monotonic() + wait_seconds
        with self._event_condition:
            observed_change = self._change_sequence
            while True:
                result = self.store.host_events(host_ref, after_cursor=after_cursor, limit=limit)
                if result["events"] or wait_seconds == 0:
                    return {**result, "changed": False}
                if self._change_sequence != observed_change:
                    return {**result, "changed": True}
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return {**result, "changed": False}
                self._event_condition.wait(timeout=remaining)

    def submit(self, thread_ref: str, action: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        with self._command_lock:
            result = self._submit_locked(thread_ref, action, payload)
        detail = self.store.thread(thread_ref)
        self._notify_change(host_ref=str(detail["host_ref"]), thread_ref=thread_ref)
        return result

    def create(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        with self._command_lock:
            result = self._create_locked(payload)
        self._notify_change(host_ref=str(payload.get("host_ref") or "") or None)
        return result

    def _create_locked(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        normalized = self._normalize_create(payload)
        host_ref = str(normalized["host_ref"])
        project_ref = str(normalized["project_ref"])
        runner_id = self.store.host_runner_id(host_ref)
        intent = intent_digest(
            {
                "runner_id": runner_id,
                "host_ref": host_ref,
                "project_ref": project_ref,
                "action": "create",
                "input": normalized["input"],
                "model": normalized.get("model"),
                "effort": normalized.get("effort"),
            }
        )
        replay = self._create_journal.replay(str(normalized["request_id"]), intent_digest=intent)
        if replay is not None:
            return replay
        self._create_preconditions(
            runner_id=runner_id,
            host_ref=host_ref,
            project_ref=project_ref,
            model=normalized.get("model"),
            effort=normalized.get("effort"),
        )
        current = self._now()
        command = build_desktop_command(
            runner_id=runner_id,
            request_id=str(normalized["request_id"]),
            host_ref=host_ref,
            project_ref=project_ref,
            thread_ref=None,
            expected_thread_revision=None,
            expected_control_revision=None,
            action="create",
            input_text=str(normalized["input"]),
            model=normalized.get("model"),
            effort=normalized.get("effort"),
            now=current,
        )
        stored, created = self._create_journal.prepare(command=command, intent_digest=intent)
        if not created:
            return stored
        if self.publisher is None:
            return self._create_journal.mark(
                str(command["request_id"]),
                state="failed",
                error_code="desktop_relay_unavailable",
                updated_at=self._now().isoformat(),
            )
        try:
            self.publisher.publish_desktop_command(runner_id, command)
        except RelayPublishError as exc:
            state = "failed" if exc.definitely_undelivered else "unknown"
            error = exc.code if exc.definitely_undelivered else "relay_publish_indeterminate"
            result = self._create_journal.mark(
                str(command["request_id"]),
                state=state,
                error_code=error,
                updated_at=self._now().isoformat(),
            )
            if exc.definitely_undelivered:
                raise StoreError(error, "Desktop Runner 当前离线，新任务未创建", status=503) from exc
            return result
        except Exception:
            return self._create_journal.mark(
                str(command["request_id"]),
                state="unknown",
                error_code="relay_publish_indeterminate",
                updated_at=self._now().isoformat(),
            )
        return self._create_journal.mark(
            str(command["request_id"]),
            state="submitted",
            error_code=None,
            updated_at=self._now().isoformat(),
        )

    def _submit_locked(
        self,
        thread_ref: str,
        action: str,
        payload: Mapping[str, Any],
    ) -> dict[str, Any]:
        self._ref(thread_ref, "TH")
        normalized = self._normalize_action(action, payload)
        thread = self.store.thread(thread_ref)
        runner_id = self._runner_id(thread_ref)
        intent = intent_digest(
            {
                "runner_id": runner_id,
                "host_ref": thread["host_ref"],
                "thread_ref": thread_ref,
                "expected_thread_revision": normalized["thread_revision"],
                "expected_turn_ref": normalized.get("expected_turn_ref"),
                "action": action,
                "input": normalized.get("input"),
                "mode": normalized.get("mode"),
                "model": normalized.get("model"),
                "effort": normalized.get("effort"),
                "queue_ref": normalized.get("queue_ref"),
                "queue_refs": normalized.get("queue_refs"),
            }
        )
        replay = self.store.replay_command(
            str(normalized["request_id"]),
            intent_digest=intent,
        )
        if replay is not None:
            return replay
        inflight = self.store.active_command(thread_ref)
        if inflight is not None:
            code = (
                "desktop_command_recovery_required"
                if inflight["state"] == "unknown"
                else "desktop_command_inflight"
            )
            raise StoreError(
                code,
                "Desktop Thread 已有未完成或结果未知的控制命令",
                status=409,
            )
        self._preconditions(thread, action, normalized)
        if self.runner_authorizer is None or not self.runner_authorizer(runner_id):
            raise StoreError(
                "desktop_runner_not_authorized",
                "Desktop Runner 未启用或缺少独立 Desktop capability",
                status=403,
            )
        if self.publisher is None:
            raise StoreError("desktop_relay_unavailable", "Desktop Relay 尚未配置", status=503)
        current = self._now()
        command = build_desktop_command(
            runner_id=runner_id,
            request_id=normalized["request_id"],
            host_ref=str(thread["host_ref"]),
            thread_ref=thread_ref,
            expected_thread_revision=int(normalized["thread_revision"]),
            expected_control_revision=(
                None if action in QUEUE_ACTIONS else thread.get("control_revision")
            ),
            action=action,
            expected_turn_ref=normalized.get("expected_turn_ref"),
            input_text=normalized.get("input"),
            mode=normalized.get("mode"),
            model=normalized.get("model"),
            effort=normalized.get("effort"),
            queue_ref=normalized.get("queue_ref"),
            queue_refs=normalized.get("queue_refs"),
            now=current,
        )
        stored, created = self.store.prepare_command(command=command, intent_digest=intent)
        if not created:
            return stored
        try:
            self.publisher.publish_desktop_command(str(command["runner_id"]), command)
        except RelayPublishError as exc:
            state = "failed" if exc.definitely_undelivered else "unknown"
            error = exc.code if exc.definitely_undelivered else "relay_publish_indeterminate"
            result = self.store.mark_command(
                str(command["request_id"]),
                state=state,
                error_code=error,
                updated_at=self._now().isoformat(),
            )
            if exc.definitely_undelivered:
                raise StoreError(error, "Desktop Runner 当前离线，命令未发送", status=503) from exc
            return result
        except Exception:
            return self.store.mark_command(
                str(command["request_id"]),
                state="unknown",
                error_code="relay_publish_indeterminate",
                updated_at=self._now().isoformat(),
            )
        return self.store.mark_command(
            str(command["request_id"]),
            state="submitted",
            error_code=None,
            updated_at=self._now().isoformat(),
        )

    def _preconditions(
        self,
        thread: Mapping[str, Any],
        action: str,
        payload: Mapping[str, Any],
    ) -> None:
        expected_revision = payload["thread_revision"]
        if thread["thread_revision"] != expected_revision:
            raise StoreError("desktop_thread_revision_stale", "Desktop Thread revision 已过期", status=409)
        hosts = {host["host_ref"]: host for host in self.hosts()["hosts"]}
        host = hosts.get(thread["host_ref"])
        if host is None or not host["online"]:
            raise StoreError("desktop_host_stale", "Desktop host 状态已过期", status=409)
        if host.get("control_enabled") is not True:
            raise StoreError("desktop_protocol_degraded", "Desktop host 当前只读", status=409)
        capabilities = set(host.get("capabilities") or [])
        model = payload.get("model")
        effort = payload.get("effort")
        required_capabilities = {
            "steer": (
                {"native_steer_racy"}
                if payload.get("mode") == "native"
                else {"interrupt_expected_turn", "continue_same_thread"}
            ),
            "interrupt": {"interrupt_expected_turn"},
            "continue": {"continue_same_thread"},
            "archive": {"archive_control_v1"},
            "unarchive": {"archive_control_v1"},
            "queue_add": {"thread_queue_v1"},
            "queue_update": {"thread_queue_v1"},
            "queue_delete": {"thread_queue_v1"},
            "queue_reorder": {"thread_queue_v1"},
            "queue_start": {"thread_queue_v1"},
        }
        if not required_capabilities.get(action, set()).issubset(capabilities):
            raise StoreError(
                "desktop_capability_unavailable",
                "Desktop host 缺少该控制动作所需能力",
                status=409,
            )
        if model is not None:
            if "model_override_v1" not in capabilities:
                raise StoreError(
                    "desktop_capability_unavailable",
                    "Desktop host 不支持运行模型覆盖",
                    status=409,
                )
            available_models = {
                item.get("id")
                for item in host.get("models") or []
                if isinstance(item, Mapping)
            }
            if model not in available_models:
                raise StoreError("desktop_model_unavailable", "所选模型已不在当前 App 目录", status=409)
        if effort is not None:
            if "reasoning_effort_v1" not in capabilities:
                raise StoreError("desktop_capability_unavailable", "Desktop host 不支持推理强度覆盖", status=409)
            snapshot_model = thread.get("snapshot", {}).get("model")
            selected_model = next(
                (
                    item
                    for item in host.get("models") or []
                    if isinstance(item, Mapping)
                    and (
                        item.get("id") == model
                        or (
                            model is None
                            and isinstance(snapshot_model, str)
                            and item.get("id") == snapshot_model
                        )
                        or (
                            model is None
                            and not isinstance(snapshot_model, str)
                            and item.get("is_default") is True
                        )
                    )
                ),
                None,
            )
            available_efforts = {
                item.get("id")
                for item in (selected_model or {}).get("supported_reasoning_efforts", [])
                if isinstance(item, Mapping)
            }
            if effort not in available_efforts:
                raise StoreError("desktop_effort_unavailable", "所选推理强度不适用于当前模型", status=409)
        status = thread["status"]
        active_turn = thread["active_turn_ref"]
        expected_turn = payload.get("expected_turn_ref")
        control_state = thread.get("control_state")
        control_revision = thread.get("control_revision")
        allowed_control_states = {
            "steer": {"ready"},
            "interrupt": {"ready"},
            "continue": {"ready", "load_required"},
            "archive": {"ready"},
            "unarchive": {"read_only"},
            "queue_add": {"ready"},
            "queue_update": {"ready"},
            "queue_delete": {"ready"},
            "queue_reorder": {"ready"},
            "queue_start": {"ready"},
        }
        if control_state not in allowed_control_states.get(action, set()):
            raise StoreError(
                "desktop_snapshot_refresh_required",
                "Desktop Thread 必须先刷新最新 App 快照",
                status=409,
            )
        if (
            action in {"steer", "interrupt"}
            or (action == "continue" and control_state == "ready")
        ):
            if not isinstance(control_revision, int) or isinstance(control_revision, bool):
                raise StoreError(
                    "desktop_snapshot_refresh_required",
                    "Desktop Thread 缺少当前控制 revision",
                    status=409,
                )
        if action in {"steer", "interrupt"}:
            if status != "active" or active_turn is None or active_turn != expected_turn:
                raise StoreError("desktop_turn_conflict", "Desktop active Turn 与请求不一致", status=409)
        elif action == "continue":
            if active_turn is not None or status not in {"idle", "notLoaded", "failed"}:
                raise StoreError("desktop_continue_conflict", "Desktop Thread 当前不能继续", status=409)
        elif action == "archive":
            if active_turn is not None or status != "idle":
                raise StoreError("desktop_archive_conflict", "Desktop Thread 当前不能归档", status=409)
        elif action == "unarchive":
            if active_turn is not None or status != "archived":
                raise StoreError("desktop_unarchive_conflict", "Desktop Thread 当前不能恢复归档", status=409)
        elif action in QUEUE_ACTIONS:
            queued = thread.get("snapshot", {}).get("queued_submissions", [])
            if not isinstance(queued, list):
                raise StoreError(
                    "desktop_snapshot_refresh_required",
                    "Desktop Thread 缺少最新排队消息快照",
                    status=409,
                )
            current_refs = [
                item.get("queue_ref")
                for item in queued
                if isinstance(item, Mapping) and isinstance(item.get("queue_ref"), str)
            ]
            if action == "queue_add":
                if status != "active" or active_turn is None:
                    raise StoreError(
                        "desktop_queue_add_conflict",
                        "Desktop Thread 仅在运行中允许加入排队消息",
                        status=409,
                    )
            elif action == "queue_reorder":
                requested_refs = payload.get("queue_refs")
                if (
                    not current_refs
                    or not isinstance(requested_refs, list)
                    or len(requested_refs) != len(current_refs)
                    or set(requested_refs) != set(current_refs)
                ):
                    raise StoreError(
                        "desktop_queue_conflict",
                        "Desktop 排队顺序已变化，请刷新后重试",
                        status=409,
                    )
            elif payload.get("queue_ref") not in current_refs:
                raise StoreError(
                    "desktop_queue_conflict",
                    "Desktop 排队消息已变化，请刷新后重试",
                    status=409,
                )
            elif action == "queue_update":
                target = next(
                    (
                        item
                        for item in queued
                        if isinstance(item, Mapping)
                        and item.get("queue_ref") == payload.get("queue_ref")
                    ),
                    None,
                )
                if not isinstance(target, Mapping) or target.get("editable") is not True:
                    raise StoreError(
                        "desktop_queue_not_editable",
                        "包含非文本内容的排队消息不能在手机端编辑",
                        status=409,
                    )
        else:
            raise StoreError("desktop_action_invalid", "Desktop API 动作无效", status=400)

    def _create_preconditions(
        self,
        *,
        runner_id: str,
        host_ref: str,
        project_ref: str,
        model: Any,
        effort: Any = None,
    ) -> None:
        projects = self.store.list_projects(host_ref=host_ref)
        if not any(project.get("project_ref") == project_ref for project in projects):
            raise StoreError(
                "desktop_project_not_found",
                "Desktop 项目不存在或未由 Runner 白名单发布",
                status=404,
            )
        hosts = {host["host_ref"]: host for host in self.hosts()["hosts"]}
        host = hosts.get(host_ref)
        if host is None or not host["online"]:
            raise StoreError("desktop_host_stale", "Desktop host 状态已过期", status=409)
        if host.get("control_enabled") is not True:
            raise StoreError("desktop_protocol_degraded", "Desktop host 当前只读", status=409)
        capabilities = set(host.get("capabilities") or [])
        if "create_thread_v1" not in capabilities:
            raise StoreError(
                "desktop_capability_unavailable",
                "Desktop host 不支持创建 App 原生任务",
                status=409,
            )
        if model is not None:
            if "model_override_v1" not in capabilities:
                raise StoreError(
                    "desktop_capability_unavailable",
                    "Desktop host 不支持运行模型覆盖",
                    status=409,
                )
            available = {
                item.get("id")
                for item in host.get("models") or []
                if isinstance(item, Mapping)
            }
            if model not in available:
                raise StoreError("desktop_model_unavailable", "所选模型已不在当前 App 目录", status=409)
        if effort is not None:
            if "reasoning_effort_v1" not in capabilities:
                raise StoreError("desktop_capability_unavailable", "Desktop host 不支持推理强度覆盖", status=409)
            selected_model = next(
                (
                    item
                    for item in host.get("models") or []
                    if isinstance(item, Mapping)
                    and (item.get("id") == model or (model is None and item.get("is_default") is True))
                ),
                None,
            )
            supported = {
                item.get("id")
                for item in (selected_model or {}).get("supported_reasoning_efforts", [])
                if isinstance(item, Mapping)
            }
            if effort not in supported:
                raise StoreError("desktop_effort_unavailable", "所选推理强度不适用于当前模型", status=409)
        if self.runner_authorizer is None or not self.runner_authorizer(runner_id):
            raise StoreError(
                "desktop_runner_not_authorized",
                "Desktop Runner 未启用或缺少独立 Desktop capability",
                status=403,
            )
        if self.publisher is None:
            raise StoreError("desktop_relay_unavailable", "Desktop Relay 尚未配置", status=503)

    def _normalize_action(self, action: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        if not isinstance(payload, Mapping):
            raise StoreError("desktop_payload_invalid", "Desktop API payload 无效", status=400)
        fields: dict[str, set[str]] = {
            "steer": {"request_id", "expected_turn_ref", "thread_revision", "input"},
            "interrupt": {"request_id", "expected_turn_ref", "thread_revision"},
            "continue": {"request_id", "thread_revision", "input"},
            "archive": {"request_id", "thread_revision"},
            "unarchive": {"request_id", "thread_revision"},
            "queue_add": {"request_id", "thread_revision", "input"},
            "queue_update": {"request_id", "thread_revision", "queue_ref", "input"},
            "queue_delete": {"request_id", "thread_revision", "queue_ref"},
            "queue_reorder": {"request_id", "thread_revision", "queue_refs"},
            "queue_start": {"request_id", "thread_revision", "queue_ref"},
        }
        required = fields.get(action)
        if required is None:
            raise StoreError("desktop_action_invalid", "Desktop API 动作无效", status=400)
        allowed = required | ({"mode", "model", "effort"} if action == "steer" else {"model", "effort"} if action == "continue" else set())
        if required - set(payload) or set(payload) - allowed:
            raise StoreError("desktop_fields_invalid", "Desktop API 字段无效", status=400)
        result = dict(payload)
        request_id = result.get("request_id")
        if not isinstance(request_id, str) or not REQUEST_RE.fullmatch(request_id):
            raise StoreError("desktop_request_id_invalid", "Desktop request_id 无效", status=400)
        revision = result.get("thread_revision")
        if not isinstance(revision, int) or isinstance(revision, bool) or revision < 0:
            raise StoreError("desktop_revision_invalid", "Desktop thread_revision 无效", status=400)
        expected_turn = result.get("expected_turn_ref")
        if expected_turn is not None:
            self._ref(expected_turn, "TR")
        if action in {"steer", "continue", "queue_add", "queue_update"}:
            try:
                validate_public_input(result.get("input"))
            except DesktopProtocolError as exc:
                raise StoreError(exc.code, str(exc), status=400) from exc
            if not isinstance(result.get("input"), str) or not result["input"].strip() or len(result["input"]) > 12000:
                raise StoreError("desktop_input_invalid", "Desktop 输入文本无效", status=400)
        if action == "steer":
            mode = result.get("mode", "safe")
            if mode not in {"safe", "native"}:
                raise StoreError("desktop_mode_invalid", "Desktop steer mode 无效", status=400)
            result["mode"] = mode
        model = result.get("model")
        if model is not None:
            if not isinstance(model, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,63}", model):
                raise StoreError("desktop_model_invalid", "Desktop model 无效", status=400)
            if action == "steer" and result.get("mode") != "safe":
                raise StoreError("desktop_model_invalid", "原生快速调整不允许覆盖模型", status=400)
        effort = result.get("effort")
        if effort is not None:
            if not isinstance(effort, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,31}", effort):
                raise StoreError("desktop_effort_invalid", "Desktop 推理强度无效", status=400)
            if action == "steer" and result.get("mode") != "safe":
                raise StoreError("desktop_effort_invalid", "原生快速调整不允许覆盖推理强度", status=400)
        queue_ref = result.get("queue_ref")
        if queue_ref is not None:
            self._ref(queue_ref, "QS")
        queue_refs = result.get("queue_refs")
        if queue_refs is not None:
            if (
                not isinstance(queue_refs, list)
                or not queue_refs
                or len(queue_refs) > 100
                or len(set(queue_refs)) != len(queue_refs)
            ):
                raise StoreError("desktop_queue_invalid", "Desktop 排队顺序无效", status=400)
            for item in queue_refs:
                self._ref(item, "QS")
        return result

    def _normalize_create(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        if not isinstance(payload, Mapping):
            raise StoreError("desktop_payload_invalid", "Desktop API payload 无效", status=400)
        required = {"request_id", "host_ref", "project_ref", "input"}
        allowed = required | {"model", "effort"}
        if required - set(payload) or set(payload) - allowed:
            raise StoreError("desktop_fields_invalid", "Desktop 新任务字段无效", status=400)
        result = dict(payload)
        request_id = result.get("request_id")
        if not isinstance(request_id, str) or not REQUEST_RE.fullmatch(request_id):
            raise StoreError("desktop_request_id_invalid", "Desktop request_id 无效", status=400)
        self._ref(result.get("host_ref"), "HS")
        self._ref(result.get("project_ref"), "PJ")
        input_text = result.get("input")
        try:
            validate_public_input(input_text)
        except DesktopProtocolError as exc:
            raise StoreError(exc.code, str(exc), status=400) from exc
        if not isinstance(input_text, str) or not input_text.strip() or len(input_text) > 12000:
            raise StoreError("desktop_input_invalid", "Desktop 输入文本无效", status=400)
        model = result.get("model")
        if model is not None and (
            not isinstance(model, str)
            or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,63}", model)
        ):
            raise StoreError("desktop_model_invalid", "Desktop model 无效", status=400)
        effort = result.get("effort")
        if effort is not None and (
            not isinstance(effort, str)
            or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,31}", effort)
        ):
            raise StoreError("desktop_effort_invalid", "Desktop 推理强度无效", status=400)
        return result

    def _runner_id(self, thread_ref: str) -> str:
        # runner_id remains internal and is intentionally absent from the public Thread DTO.
        return self.store.runner_id(thread_ref)

    def _now(self) -> dt.datetime:
        value = self.now()
        if value.tzinfo is None or value.utcoffset() is None:
            raise StoreError("desktop_clock_invalid", "Desktop Controller 时钟必须包含时区", status=500)
        return value.astimezone(SHANGHAI)

    def _recent_threads(
        self,
        *,
        host_ref: str | None,
        project_ref: str | None,
        status: str | None,
        offset: int,
        limit: int,
    ) -> dict[str, Any]:
        conditions: list[str] = []
        parameters: list[Any] = []
        for column, value in (
            ("host_ref", host_ref),
            ("project_ref", project_ref),
            ("status", status),
        ):
            if value is not None:
                conditions.append(f"{column}=?")
                parameters.append(value)
        where = "" if not conditions else "WHERE " + " AND ".join(conditions)
        parameters.extend((limit + 1, offset))
        # DesktopStore owns the schema and public DTO conversion. This query only adds
        # the alternate, offset-based order required for bounded initial rendering.
        with self.store._connect() as connection:  # noqa: SLF001 - package-local read model
            rows = connection.execute(
                "SELECT * FROM desktop_threads "
                f"{where} ORDER BY source_updated_at DESC,id DESC LIMIT ? OFFSET ?",
                parameters,
            ).fetchall()
        has_more = len(rows) > limit
        rows = rows[:limit]
        return {
            "threads": [
                self.store._thread_public(row)  # noqa: SLF001 - package-local DTO contract
                for row in rows
            ],
            "next_cursor": offset + len(rows),
            "has_more": has_more,
        }

    def _stream(
        self,
        *,
        scope_kind: str,
        scope_ref: str,
        after_cursor: int | None,
        heartbeat_seconds: float,
    ) -> Iterator[dict[str, Any]]:
        if after_cursor is not None and (
            isinstance(after_cursor, bool)
            or not isinstance(after_cursor, int)
            or after_cursor < 0
            or after_cursor > 2**63 - 1
        ):
            raise StoreError("desktop_cursor_invalid", "Desktop SSE cursor 无效", status=400)
        if not 0.01 <= heartbeat_seconds <= 15:
            raise StoreError("desktop_stream_invalid", "Desktop SSE heartbeat 无效", status=500)

        with self._event_condition:
            tail = self._event_tail(scope_kind=scope_kind, scope_ref=scope_ref)
            cursor = tail if after_cursor is None else after_cursor
            pruned_through = self.store.event_pruned_through(
                scope_kind=scope_kind,
                scope_ref=scope_ref,
            )
            resync_required = bool(
                after_cursor is not None
                and (
                    after_cursor > tail
                    or (pruned_through > 0 and after_cursor <= pruned_through)
                )
            )
            if resync_required:
                cursor = tail
            observed_scope = self._scope_change_sequence(scope_kind, scope_ref)
            observed_broadcast = self._broadcast_sequence
        yield self._stream_frame(
            "ready",
            scope_kind=scope_kind,
            scope_ref=scope_ref,
            cursor=cursor,
            events=[],
            changed=False,
            include_host=scope_kind == "host",
            resync_required=resync_required,
        )

        heartbeat_at = time.monotonic() + heartbeat_seconds
        while True:
            frame: dict[str, Any] | None = None
            with self._event_condition:
                while frame is None:
                    current_scope = self._scope_change_sequence(scope_kind, scope_ref)
                    current_broadcast = self._broadcast_sequence
                    if scope_kind == "host":
                        result = self.store.host_events(
                            scope_ref,
                            after_cursor=cursor,
                            limit=SSE_EVENT_BATCH_LIMIT,
                        )
                    else:
                        result = self.store.events(
                            scope_ref,
                            after_cursor=cursor,
                            limit=SSE_EVENT_BATCH_LIMIT,
                        )
                    if result["events"]:
                        cursor = int(result["next_cursor"])
                        observed_scope = current_scope
                        observed_broadcast = current_broadcast
                        frame = self._stream_frame(
                            "desktop",
                            scope_kind=scope_kind,
                            scope_ref=scope_ref,
                            cursor=cursor,
                            events=list(result["events"]),
                            changed=False,
                            has_more=bool(result["has_more"]),
                        )
                        continue
                    if current_scope != observed_scope or current_broadcast != observed_broadcast:
                        observed_scope = current_scope
                        observed_broadcast = current_broadcast
                        frame = self._stream_frame(
                            "desktop",
                            scope_kind=scope_kind,
                            scope_ref=scope_ref,
                            cursor=cursor,
                            events=[],
                            changed=True,
                            has_more=False,
                        )
                        continue
                    remaining = heartbeat_at - time.monotonic()
                    if remaining <= 0:
                        frame = self._stream_frame(
                            "heartbeat",
                            scope_kind=scope_kind,
                            scope_ref=scope_ref,
                            cursor=cursor,
                            events=[],
                            changed=False,
                            include_host=scope_kind == "host",
                        )
                        heartbeat_at = time.monotonic() + heartbeat_seconds
                        continue
                    self._event_condition.wait(timeout=remaining)
            yield frame

    def _event_tail(self, *, scope_kind: str, scope_ref: str) -> int:
        column = "host_ref" if scope_kind == "host" else "thread_ref"
        with sqlite3.connect(self.store.database_path) as connection:
            row = connection.execute(
                f"SELECT MAX(cursor) FROM desktop_events WHERE {column}=?",
                (scope_ref,),
            ).fetchone()
        return int(row[0]) if row is not None and row[0] is not None else 0

    def _scope_change_sequence(self, scope_kind: str, scope_ref: str) -> int:
        if scope_kind == "host":
            return self._host_change_sequences.get(scope_ref, 0)
        return self._thread_change_sequences.get(scope_ref, 0)

    def _stream_frame(
        self,
        event: str,
        *,
        scope_kind: str,
        scope_ref: str,
        cursor: int,
        events: list[dict[str, Any]],
        changed: bool,
        has_more: bool = False,
        include_host: bool = False,
        resync_required: bool = False,
    ) -> dict[str, Any]:
        data: dict[str, Any] = {
            "version": 1,
            "scope": {"type": scope_kind, "ref": scope_ref},
            "cursor": cursor,
            "server_time": self._now().isoformat(),
            "events": events,
        }
        if event == "desktop":
            data["changed"] = changed
            data["has_more"] = has_more
        if event == "ready":
            data["resync_required"] = resync_required
        if include_host:
            data["host"] = self._host_document(scope_ref)
        return {"event": event, "cursor": cursor, "data": data}

    def _host_document(self, host_ref: str) -> dict[str, Any]:
        for host in self.hosts()["hosts"]:
            if host.get("host_ref") == host_ref:
                return host
        raise StoreError("desktop_host_not_found", "Desktop host 不存在", status=404)

    def _notify_change(
        self,
        *,
        host_ref: str | None = None,
        thread_ref: str | None = None,
    ) -> None:
        with self._event_condition:
            self._change_sequence += 1
            if host_ref is None and thread_ref is None:
                self._broadcast_sequence += 1
            if host_ref is not None:
                self._host_change_sequences[host_ref] = (
                    self._host_change_sequences.get(host_ref, 0) + 1
                )
            if thread_ref is not None:
                self._thread_change_sequences[thread_ref] = (
                    self._thread_change_sequences.get(thread_ref, 0) + 1
                )
            self._event_condition.notify_all()

    @staticmethod
    def _parse_time(value: Any) -> dt.datetime | None:
        if not isinstance(value, str):
            return None
        try:
            parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            return None
        return parsed.astimezone(SHANGHAI)

    @staticmethod
    def _ref(value: Any, prefix: str) -> None:
        if not isinstance(value, str) or not REF_RE.fullmatch(value) or not value.startswith(prefix + "-"):
            raise StoreError("desktop_ref_invalid", f"Desktop {prefix} ref 无效", status=400)


class _DesktopCreateJournal:
    """Durable create-request receipts without inventing a Controller Thread row."""

    _STATES = frozenset(
        {
            "pending",
            "submitted",
            "accepted",
            "confirmed",
            "conflict",
            "expired",
            "failed",
            "unknown",
            "recovery_required",
        }
    )

    def __init__(self, database_path: str) -> None:
        self.database_path = database_path
        self._lock = threading.RLock()
        with self._connect() as connection:
            connection.execute(
                "CREATE TABLE IF NOT EXISTS desktop_thread_create_commands("
                "request_id TEXT PRIMARY KEY,intent_digest TEXT NOT NULL,command_body_digest TEXT NOT NULL UNIQUE,"
                "runner_id TEXT NOT NULL,host_ref TEXT NOT NULL,project_ref TEXT NOT NULL,state TEXT NOT NULL,"
                "error_code TEXT,command_json TEXT,receipt_json TEXT,created_at TEXT NOT NULL,updated_at TEXT NOT NULL,"
                "orphan INTEGER NOT NULL DEFAULT 0,relay_delivered_at TEXT,runner_received_at TEXT,mac_confirmed_at TEXT)"
            )
            columns = {
                str(row["name"])
                for row in connection.execute(
                    "PRAGMA table_info(desktop_thread_create_commands)"
                ).fetchall()
            }
            for name in ("relay_delivered_at", "runner_received_at", "mac_confirmed_at"):
                if name not in columns:
                    connection.execute(
                        f"ALTER TABLE desktop_thread_create_commands ADD COLUMN {name} TEXT"
                    )
            current = dt.datetime.now(SHANGHAI).isoformat()
            connection.execute(
                "UPDATE desktop_thread_create_commands SET state='unknown',"
                "error_code='controller_restarted_before_delivery',updated_at=? WHERE state='pending'",
                (current,),
            )

    def prepare(
        self,
        *,
        command: Mapping[str, Any],
        intent_digest: str,
    ) -> tuple[dict[str, Any], bool]:
        request_id = str(command["request_id"])
        with self._lock, self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM desktop_thread_create_commands WHERE request_id=?",
                (request_id,),
            ).fetchone()
            if row is not None:
                self._same_intent(row, intent_digest)
                return self._row(row), False
            connection.execute(
                "INSERT INTO desktop_thread_create_commands("
                "request_id,intent_digest,command_body_digest,runner_id,host_ref,project_ref,state,error_code,"
                "command_json,receipt_json,created_at,updated_at,orphan) VALUES(?,?,?,?,?,?,'pending',NULL,?,NULL,?,?,0)",
                (
                    request_id,
                    intent_digest,
                    command["body_digest"],
                    command["runner_id"],
                    command["host_ref"],
                    command["project_ref"],
                    canonical_json(command),
                    command["created_at"],
                    command["created_at"],
                ),
            )
            row = connection.execute(
                "SELECT * FROM desktop_thread_create_commands WHERE request_id=?",
                (request_id,),
            ).fetchone()
            assert row is not None
            return self._row(row), True

    def replay(self, request_id: str, *, intent_digest: str) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM desktop_thread_create_commands WHERE request_id=?",
                (request_id,),
            ).fetchone()
        if row is None:
            return None
        self._same_intent(row, intent_digest)
        return self._row(row)

    def mark(
        self,
        request_id: str,
        *,
        state: str,
        error_code: str | None,
        updated_at: str,
    ) -> dict[str, Any]:
        if state not in self._STATES:
            raise StoreError("desktop_command_state_invalid", "Desktop 新任务状态无效", status=500)
        with self._lock, self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM desktop_thread_create_commands WHERE request_id=?",
                (request_id,),
            ).fetchone()
            if row is None:
                raise StoreError("desktop_command_unknown", "Desktop 新任务请求不存在", status=404)
            if row["state"] == "pending":
                connection.execute(
                    "UPDATE desktop_thread_create_commands SET state=?,error_code=?,updated_at=?,"
                    "relay_delivered_at=CASE WHEN ?='submitted' THEN COALESCE(relay_delivered_at,?) ELSE relay_delivered_at END "
                    "WHERE request_id=?",
                    (state, error_code, updated_at, state, updated_at, request_id),
                )
            elif state == "submitted":
                connection.execute(
                    "UPDATE desktop_thread_create_commands SET relay_delivered_at=COALESCE(relay_delivered_at,?) "
                    "WHERE request_id=?",
                    (updated_at, request_id),
                )
            row = connection.execute(
                "SELECT * FROM desktop_thread_create_commands WHERE request_id=?",
                (request_id,),
            ).fetchone()
            assert row is not None
            return self._row(row)

    def ingest_receipt(self, document: Mapping[str, Any]) -> dict[str, Any]:
        request_id = str(document["request_id"])
        encoded_receipt = canonical_json(document)
        with self._lock, self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM desktop_thread_create_commands WHERE request_id=?",
                (request_id,),
            ).fetchone()
            if row is None:
                connection.execute(
                    "INSERT INTO desktop_thread_create_commands("
                    "request_id,intent_digest,command_body_digest,runner_id,host_ref,project_ref,state,error_code,"
                    "command_json,receipt_json,created_at,updated_at,orphan) VALUES(?,?,?,?,?,?,?,?,NULL,?,?,?,?,1)",
                    (
                        request_id,
                        "orphan:" + str(document["body_digest"]),
                        str(document["body_digest"]),
                        document["runner_id"],
                        document["host_ref"],
                        document["project_ref"],
                        "recovery_required",
                        "desktop_command_unknown",
                        encoded_receipt,
                        document["created_at"],
                        document["created_at"],
                    ),
                )
                created = connection.execute(
                    "SELECT * FROM desktop_thread_create_commands WHERE request_id=?",
                    (request_id,),
                ).fetchone()
                assert created is not None
                return {"status": "stored", "orphan": True, "command": self._row(created)}
            if (
                row["runner_id"] != document["runner_id"]
                or row["host_ref"] != document["host_ref"]
                or row["project_ref"] != document["project_ref"]
            ):
                raise StoreError(
                    "desktop_receipt_binding_conflict",
                    "Desktop 新任务 receipt 与请求绑定不一致",
                    status=409,
                )
            if row["receipt_json"] is not None:
                if row["receipt_json"] == encoded_receipt:
                    return {"status": "duplicate", "orphan": bool(row["orphan"]), "command": self._row(row)}
                previous = json.loads(str(row["receipt_json"]))
                previous_state = previous.get("state")
                current_state = document["state"]
                if previous_state != "accepted" and current_state == "accepted":
                    return {"status": "stale", "orphan": bool(row["orphan"]), "command": self._row(row)}
                if previous_state != "accepted" or current_state == "accepted":
                    raise StoreError(
                        "desktop_receipt_conflict",
                        "同一 Desktop 新任务 request_id 收据阶段冲突",
                        status=409,
                    )
            connection.execute(
                "UPDATE desktop_thread_create_commands SET state=?,error_code=?,receipt_json=?,updated_at=?,"
                "runner_received_at=CASE WHEN ?='accepted' THEN COALESCE(runner_received_at,?) ELSE runner_received_at END,"
                "mac_confirmed_at=CASE WHEN ?='confirmed' THEN COALESCE(mac_confirmed_at,?) ELSE mac_confirmed_at END "
                "WHERE request_id=?",
                (
                    document["state"],
                    document.get("error_code"),
                    encoded_receipt,
                    document["created_at"],
                    document["state"],
                    document["created_at"],
                    document["state"],
                    document["created_at"],
                    request_id,
                ),
            )
            updated = connection.execute(
                "SELECT * FROM desktop_thread_create_commands WHERE request_id=?",
                (request_id,),
            ).fetchone()
            assert updated is not None
            return {"status": "stored", "orphan": False, "command": self._row(updated)}

    def sweep(self, *, now: str) -> int:
        current = dt.datetime.fromisoformat(now.replace("Z", "+00:00"))
        expired: list[str] = []
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                "SELECT request_id,command_json FROM desktop_thread_create_commands "
                "WHERE state IN ('submitted','accepted')"
            ).fetchall()
            for row in rows:
                try:
                    command = json.loads(row["command_json"])
                    expires_at = dt.datetime.fromisoformat(str(command["expires_at"]).replace("Z", "+00:00"))
                except (KeyError, TypeError, ValueError, json.JSONDecodeError):
                    expires_at = current
                if expires_at.tzinfo is None or expires_at.utcoffset() is None or expires_at <= current:
                    expired.append(str(row["request_id"]))
            if expired:
                connection.executemany(
                    "UPDATE desktop_thread_create_commands SET state='unknown',"
                    "error_code='desktop_receipt_timeout',updated_at=? WHERE request_id=? "
                    "AND state IN ('submitted','accepted')",
                    ((now, request_id) for request_id in expired),
                )
        return len(expired)

    @staticmethod
    def _same_intent(row: sqlite3.Row, digest: str) -> None:
        if row["intent_digest"] != digest:
            raise StoreError(
                "desktop_request_conflict",
                "同一 Desktop 新任务 request_id 使用了不同正文",
                status=409,
            )

    @staticmethod
    def _row(row: sqlite3.Row) -> dict[str, Any]:
        command = json.loads(row["command_json"]) if row["command_json"] else {}
        receipt = json.loads(row["receipt_json"]) if row["receipt_json"] else None
        if isinstance(receipt, dict):
            receipt.pop("runner_id", None)
        return {
            "request_id": row["request_id"],
            "host_ref": row["host_ref"],
            "project_ref": row["project_ref"],
            "action": "create",
            "model": command.get("model"),
            "effort": command.get("effort"),
            "state": row["state"],
            "error_code": row["error_code"],
            "thread_ref": receipt.get("thread_ref") if isinstance(receipt, dict) else None,
            "turn_ref": receipt.get("turn_ref") if isinstance(receipt, dict) else None,
            "thread_revision": receipt.get("thread_revision") if isinstance(receipt, dict) else None,
            "created_at": row["created_at"],
            "expires_at": command.get("expires_at"),
            "updated_at": row["updated_at"],
            "receipt": receipt,
            "delivery_stage": _create_delivery_stage(row),
            "stage_timestamps": {
                "controller_received": row["created_at"],
                "relay_delivered": row["relay_delivered_at"],
                "runner_received": row["runner_received_at"],
                "mac_confirmed": row["mac_confirmed_at"],
            },
            "recovery_required": row["state"] in {"unknown", "recovery_required"},
        }

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.database_path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout=10000")
        connection.execute("PRAGMA journal_mode=WAL")
        try:
            with connection:
                yield connection
        finally:
            connection.close()


def _create_delivery_stage(row: sqlite3.Row) -> str:
    if row["mac_confirmed_at"] is not None:
        return "mac_confirmed"
    if row["runner_received_at"] is not None:
        return "runner_received"
    if row["relay_delivered_at"] is not None:
        return "relay_delivered"
    return "controller_received"


__all__ = ["DesktopControllerService", "DesktopPublisher"]
