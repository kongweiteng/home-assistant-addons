"""Deterministic Runner Manager v2 service boundary."""

from __future__ import annotations

import threading
from typing import Any, Protocol

from .runner_store import RunnerStore
from .store import StoreError


class RunnerPublisher(Protocol):
    """Transport-neutral relay adapter used only after an atomic lease claim."""

    def publish_request(self, runner_id: str, document: dict[str, Any]) -> None:
        ...

    def publish_control(self, runner_id: str, document: dict[str, Any]) -> None:
        ...


class RunnerManagerService:
    def __init__(
        self,
        store: RunnerStore,
        *,
        enabled: bool = True,
        publisher: RunnerPublisher | None = None,
    ) -> None:
        self.store = store
        self.enabled = bool(enabled)
        self.publisher = publisher
        self.last_error: str | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if not self.enabled or self._thread is not None:
            return
        self._thread = threading.Thread(target=self._loop, name="runner-manager-v2", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=3)
            self._thread = None

    def status(self) -> dict[str, Any]:
        if not self.enabled:
            return {
                "enabled": False,
                "relay_configured": False,
                "last_error": None,
                "summary": {"total": 0, "enabled": 0, "online": 0, "busy": 0, "recovery_required": 0},
            }
        document = self.store.list_runners()
        return {
            "enabled": True,
            "relay_configured": self.publisher is not None,
            "last_error": self.last_error,
            "summary": document["summary"],
        }

    def list_runners(self) -> dict[str, Any]:
        self._require_enabled()
        return self.store.list_runners()

    def runner(self, runner_id: str) -> dict[str, Any]:
        self._require_enabled()
        return self.store.runner(runner_id)

    def create_enrollment(self, payload: dict[str, Any]) -> dict[str, Any]:
        self._require_enabled()
        return self.store.create_enrollment(payload)

    def update_runner(self, runner_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        self._require_enabled()
        return self.store.update_runner(runner_id, payload)

    def drain(self, runner_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        self._require_enabled()
        return self.store.drain(runner_id, payload)

    def emergency_disable(self, runner_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        self._require_enabled()
        result = self.store.emergency_disable(runner_id, payload)
        runner = result["runner"]
        if runner.get("current_task_id") and self.publisher is not None:
            task = self.store.work_task(str(runner["current_task_id"]))
            control = self.store.control_document(
                task["task_id"],
                action="cancel",
                control_id=f"ADMIN-{payload['request_id']}",
            )
            self.publisher.publish_control(runner_id, control)
        return result

    def rotate_credential(self, runner_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        self._require_enabled()
        return self.store.rotate_credential(runner_id, payload)

    def delete_runner(self, runner_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        self._require_enabled()
        return self.store.delete_runner(runner_id, payload)

    def request_self_check(self, runner_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        self._require_enabled()
        return self.store.request_self_check(runner_id, payload)

    def list_tasks(self) -> dict[str, Any]:
        self._require_enabled()
        return self.store.list_tasks()

    def redeem_enrollment(self, payload: dict[str, Any]) -> dict[str, Any]:
        self._require_enabled()
        return self.store.redeem_enrollment(payload)

    def heartbeat(self, payload: dict[str, Any], *, credential: str) -> dict[str, Any]:
        self._require_enabled()
        result = self.store.heartbeat(payload, credential=credential)
        self.dispatch_waiting()
        return result

    def receive_status(self, payload: dict[str, Any], *, credential: str) -> dict[str, Any]:
        self._require_enabled()
        return self.store.record_status(payload, credential=credential)

    def receive_result(self, payload: dict[str, Any], *, credential: str) -> dict[str, Any]:
        self._require_enabled()
        result = self.store.record_result(payload, credential=credential)
        self.dispatch_waiting()
        return result

    def work_command(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Handle the exact Gateway v2 command without app-server interpretation."""
        self._require_enabled()
        operation = payload.get("operation")
        request_id = payload.get("request_id")
        if operation == "start":
            task, _duplicate = self.store.create_work_task(payload)
            self.dispatch_waiting()
            task = self.store.work_task(task["task_id"])
        else:
            task, replayed = self.store.command_task(
                payload,
                control_available=self.publisher is not None,
            )
            should_publish_control = operation == "continue" or (
                operation == "cancel"
                and task.get("assigned_runner_id") is not None
                and task["state"] not in {"awaiting_confirmation", "completed", "failed", "cancelled", "expired"}
            )
            if should_publish_control and not replayed and task.get("assigned_runner_id"):
                if self.publisher is None:
                    raise StoreError("runner_relay_unavailable", "Runner Relay 当前不可用", status=503)
                control = self.store.control_document(
                    task["task_id"],
                    action=str(operation),
                    control_id=f"CTRL-{request_id}",
                    instruction=payload.get("instruction") if operation == "continue" else None,
                )
                self.publisher.publish_control(str(task["assigned_runner_id"]), control)
                task = self.store.work_task(task["task_id"])
        return self._gateway_result(task, request_id=str(request_id), operation=str(operation))

    def dispatch_waiting(self, *, limit: int = 100) -> int:
        self._require_enabled()
        if self.publisher is None:
            return 0
        dispatched = 0
        for _ in range(limit):
            assignment = self.store.claim_next()
            if assignment is None:
                break
            task = assignment["task"]
            request = assignment["request"]
            self.store.mark_dispatched(str(task["task_id"]))
            try:
                self.publisher.publish_request(str(request["runner_id"]), request)
            except Exception:
                # The lease remains authoritative. A transport exception is an
                # indeterminate publish, so it must be reconciled rather than
                # immediately reassigned or duplicated.
                self.last_error = "relay_publish_indeterminate"
                break
            dispatched += 1
        return dispatched

    def _loop(self) -> None:
        while not self._stop.wait(0.5):
            try:
                self.store.sweep()
                self.dispatch_waiting()
            except StoreError as exc:
                self.last_error = exc.code
            except Exception:
                self.last_error = "runner_manager_internal_error"

    def _require_enabled(self) -> None:
        if not self.enabled:
            raise StoreError(
                "runner_manager_disabled",
                "Runner Center v2 已由 Add-on 配置关闭；现有 Controller 与 Remote Work v1 不受影响",
                status=409,
            )

    @staticmethod
    def _gateway_result(task: dict[str, Any], *, request_id: str, operation: str) -> dict[str, Any]:
        result: dict[str, Any] = {
            "version": 2,
            "request_id": request_id,
            "operation": operation,
            "task_id": task["task_id"],
            "state": task["state"],
            "updated_at": task["updated_at"],
        }
        for name in (
            "stage",
            "summary",
            "candidate_id",
            "test_summary",
            "changed_path_count",
            "next_actions",
            "error_code",
            "action_required",
        ):
            value = task.get(name)
            if value is not None and value != []:
                result[name] = value
        return result


__all__ = ["RunnerManagerService", "RunnerPublisher"]
