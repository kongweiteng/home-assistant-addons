"""Controller service for Desktop snapshots, cursors, and fail-closed commands."""

from __future__ import annotations

import datetime as dt
import re
import threading
import time
from typing import Any, Callable, Mapping, Protocol
from zoneinfo import ZoneInfo

from .desktop_protocol import (
    DesktopProtocolError,
    REF_RE,
    REQUEST_RE,
    THREAD_STATUSES,
    build_desktop_command,
    intent_digest,
    validate_desktop_document,
    validate_public_input,
)
from .desktop_store import DesktopStore
from .runner_relay import RelayPublishError
from .store import StoreError


SHANGHAI = ZoneInfo("Asia/Shanghai")


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
        self._command_lock = threading.Lock()

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
        else:
            result = self.store.ingest_receipt(document)
        with self._event_condition:
            self._event_condition.notify_all()
        return {"accepted": True, **result}

    def sweep(self) -> int:
        return self.store.sweep_commands(now=self._now().isoformat())

    def hosts(self) -> dict[str, Any]:
        current = self._now()
        hosts = self.store.list_hosts()
        for host in hosts:
            online = False
            runner_id = self.store.host_runner_id(str(host["host_ref"]))
            if self.runner_status_provider is not None:
                try:
                    runner = self.runner_status_provider(runner_id)
                except StoreError:
                    runner = {}
                online = runner.get("connectivity_state") == "online"
            else:
                synced_at = self._parse_time(host.get("synced_at"))
                online = synced_at is not None and current - synced_at <= dt.timedelta(
                    seconds=self.host_stale_seconds
                )
            authorized = bool(
                self.runner_authorizer is not None and self.runner_authorizer(runner_id)
            )
            host["online"] = online
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
    ) -> dict[str, Any]:
        if host_ref is not None:
            self._ref(host_ref, "HS")
        if project_ref is not None:
            self._ref(project_ref, "PJ")
        if status is not None and status not in THREAD_STATUSES:
            raise StoreError("desktop_status_invalid", "Desktop Thread 状态筛选无效", status=400)
        if after_cursor < 0 or not 1 <= limit <= 200:
            raise StoreError("desktop_cursor_invalid", "Desktop Thread cursor 或 limit 无效", status=400)
        return self.store.list_threads(
            host_ref=host_ref,
            project_ref=project_ref,
            status=status,
            after_cursor=after_cursor,
            limit=limit,
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
        while True:
            with self._event_condition:
                result = self.store.events(thread_ref, after_cursor=after_cursor, limit=limit)
                if result["events"] or wait_seconds == 0:
                    return result
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return result
                self._event_condition.wait(timeout=remaining)

    def submit(self, thread_ref: str, action: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        with self._command_lock:
            return self._submit_locked(thread_ref, action, payload)

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
            expected_control_revision=thread.get("control_revision"),
            action=action,
            expected_turn_ref=normalized.get("expected_turn_ref"),
            input_text=normalized.get("input"),
            mode=normalized.get("mode"),
            model=normalized.get("model"),
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
        }
        if control_state not in allowed_control_states.get(action, set()):
            raise StoreError(
                "desktop_snapshot_refresh_required",
                "Desktop Thread 必须先刷新最新 App 快照",
                status=409,
            )
        if action in {"steer", "interrupt"} or (action == "continue" and control_state == "ready"):
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
        else:
            raise StoreError("desktop_action_invalid", "Desktop API 动作无效", status=400)

    def _normalize_action(self, action: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        if not isinstance(payload, Mapping):
            raise StoreError("desktop_payload_invalid", "Desktop API payload 无效", status=400)
        fields: dict[str, set[str]] = {
            "steer": {"request_id", "expected_turn_ref", "thread_revision", "input"},
            "interrupt": {"request_id", "expected_turn_ref", "thread_revision"},
            "continue": {"request_id", "thread_revision", "input"},
            "archive": {"request_id", "thread_revision"},
            "unarchive": {"request_id", "thread_revision"},
        }
        required = fields.get(action)
        if required is None:
            raise StoreError("desktop_action_invalid", "Desktop API 动作无效", status=400)
        allowed = required | ({"mode", "model"} if action == "steer" else {"model"} if action == "continue" else set())
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
        if action in {"steer", "continue"}:
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
        return result

    def _runner_id(self, thread_ref: str) -> str:
        # runner_id remains internal and is intentionally absent from the public Thread DTO.
        return self.store.runner_id(thread_ref)

    def _now(self) -> dt.datetime:
        value = self.now()
        if value.tzinfo is None or value.utcoffset() is None:
            raise StoreError("desktop_clock_invalid", "Desktop Controller 时钟必须包含时区", status=500)
        return value.astimezone(SHANGHAI)

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


__all__ = ["DesktopControllerService", "DesktopPublisher"]
