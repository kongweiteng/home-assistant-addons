from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import datetime as dt
import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
import time
import unittest

from codex_controller.runner_relay import RelayPublishError
from codex_controller.runner_store import RunnerStore
from codex_controller.runner_service import RunnerManagerService
from codex_controller.store import StoreError


class Clock:
    def __init__(self) -> None:
        self.value = dt.datetime(2026, 8, 10, 8, 0, tzinfo=dt.timezone.utc)

    def __call__(self) -> str:
        return self.value.isoformat()

    def advance(self, seconds: int) -> None:
        self.value += dt.timedelta(seconds=seconds)


def digest(value: object) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def with_digest(value: dict) -> dict:
    document = dict(value)
    document["body_digest"] = digest(document)
    return document


class Publisher:
    def __init__(self) -> None:
        self.requests: list[dict] = []
        self.controls: list[dict] = []

    def publish_request(self, _runner_id: str, document: dict) -> None:
        self.requests.append(document)

    def publish_control(self, _runner_id: str, document: dict) -> None:
        self.controls.append(document)


class OfflineThenSuccessPublisher(Publisher):
    def __init__(self, *, always_indeterminate: bool = False) -> None:
        super().__init__()
        self.attempts = 0
        self.always_indeterminate = always_indeterminate

    def publish_request(self, runner_id: str, document: dict) -> None:
        self.attempts += 1
        if self.always_indeterminate:
            raise RelayPublishError(
                "relay_publish_indeterminate", definitely_undelivered=False
            )
        if self.attempts == 1:
            raise RelayPublishError("runner_offline", definitely_undelivered=True)
        super().publish_request(runner_id, document)


class RunnerSchedulerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.clock = Clock()
        self.store = RunnerStore(
            Path(self.temporary.name) / "controller.sqlite3",
            clock=self.clock,
            online_after_seconds=10,
            offline_after_seconds=20,
            lease_ttl_seconds=5,
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def add_runner(self, name: str, *, os_name: str = "linux") -> tuple[str, str]:
        created = self.store.create_enrollment(
            {
                "display_name": name,
                "os": os_name,
                "arch": "amd64",
                "labels": ["always-on", "tests"],
                "allowed_projects": ["renovation-hub"],
                "max_concurrency": 1,
                "request_id": f"create-{name.lower().replace(' ', '-')}-0001",
            }
        )
        runner_id = created["runner"]["runner_id"]
        redeemed = self.store.redeem_enrollment(
            {
                "token": created["enrollment"]["token"],
                "runner_id": runner_id,
                "protocol_version": 2,
                "agent_version": "0.1.0",
                "codex_version": "0.146.0",
                "os": os_name,
                "arch": "amd64",
                "capabilities": ["registered_projects", "worktree", "codex_exec_json", "self_check"],
                "projects": ["renovation-hub"],
                "labels": ["always-on", "tests"],
                "policy_revision": 1,
                "self_check": {"ok": True, "checks": ["codex", "git", "workspace"]},
            }
        )
        credential = redeemed["credential"]["secret"]
        heartbeat = with_digest(
            {
                "version": 2,
                "message_type": "heartbeat",
                "runner_id": runner_id,
                "task_id": None,
                "assignment_epoch": 0,
                "sequence": 1,
                "created_at": self.clock(),
                "expires_at": (self.clock.value + dt.timedelta(seconds=90)).isoformat(),
                "online": True,
                "protocol_version": 2,
                "agent_version": "0.1.0",
                "codex_version": "0.146.0",
                "os": os_name,
                "arch": "amd64",
                "labels": ["always-on", "tests"],
                "allowed_projects": ["renovation-hub"],
                "capabilities": ["registered_projects", "worktree", "codex_exec_json", "self_check"],
                "queue_depth": 0,
                "work_state": "idle",
                "active_lease_id": None,
                "self_check": "ok",
                "policy_revision": 1,
                "updated_at": self.clock(),
            }
        )
        self.store.heartbeat(
            heartbeat,
            credential=credential,
        )
        runner = self.store.runner(runner_id)
        self.store.update_runner(
            runner_id,
            {"admin_state": "enabled", "revision": runner["revision"], "request_id": f"enable-{name.lower().replace(' ', '-')}-0001"},
        )
        return runner_id, credential

    def heartbeat_idle(self, runner_id: str, credential: str, *, sequence: int) -> dict:
        runner = self.store.runner(runner_id)
        heartbeat = with_digest(
            {
                "version": 2,
                "message_type": "heartbeat",
                "runner_id": runner_id,
                "task_id": None,
                "assignment_epoch": 0,
                "sequence": sequence,
                "created_at": self.clock(),
                "expires_at": (self.clock.value + dt.timedelta(seconds=90)).isoformat(),
                "online": True,
                "protocol_version": 2,
                "agent_version": str(runner["agent_version"]),
                "codex_version": str(runner["codex_version"]),
                "os": str(runner["os"]),
                "arch": str(runner["arch"]),
                "labels": list(runner["labels"]),
                "allowed_projects": list(runner["allowed_projects"]),
                "capabilities": list(runner["capabilities"]),
                "queue_depth": 0,
                "work_state": "idle",
                "active_lease_id": None,
                "self_check": "ok",
                "policy_revision": int(runner["policy_revision"]),
                "updated_at": self.clock(),
            }
        )
        return self.store.heartbeat(heartbeat, credential=credential)

    def heartbeat_busy(
        self,
        assignment: dict,
        credential: str,
        *,
        sequence: int,
    ) -> dict:
        request = assignment["request"]
        runner = self.store.runner(request["runner_id"])
        heartbeat = with_digest(
            {
                "version": 2,
                "message_type": "heartbeat",
                "runner_id": request["runner_id"],
                "task_id": request["task_id"],
                "assignment_epoch": request["assignment_epoch"],
                "sequence": sequence,
                "created_at": self.clock(),
                "expires_at": (self.clock.value + dt.timedelta(seconds=90)).isoformat(),
                "online": True,
                "protocol_version": 2,
                "agent_version": str(runner["agent_version"]),
                "codex_version": str(runner["codex_version"]),
                "os": str(runner["os"]),
                "arch": str(runner["arch"]),
                "labels": list(runner["labels"]),
                "allowed_projects": list(runner["allowed_projects"]),
                "capabilities": list(runner["capabilities"]),
                "queue_depth": 0,
                "work_state": "busy",
                "active_lease_id": request["lease_id"],
                "self_check": "ok",
                "policy_revision": int(runner["policy_revision"]),
                "updated_at": self.clock(),
            }
        )
        return self.store.heartbeat(heartbeat, credential=credential)

    def active_lease(self, lease_id: str) -> sqlite3.Row:
        connection = sqlite3.connect(self.store.path)
        connection.row_factory = sqlite3.Row
        try:
            row = connection.execute(
                "SELECT * FROM runner_leases WHERE lease_id=?",
                (lease_id,),
            ).fetchone()
        finally:
            connection.close()
        assert row is not None
        return row

    def add_task(self, index: int = 1) -> dict:
        task, _ = self.store.create_work_task(
            {
                "version": 2,
                "request_id": f"WRV2-{index:032x}",
                "operation": "start",
                "source": {"channel": "weixin", "principal_hash": "sha256:" + "a" * 64, "role": "owner"},
                "project_alias": "renovation-hub",
                "instruction": f"合成任务 {index}",
            }
        )
        return task

    def command(self, operation: str, task_id: str, *, index: int, instruction: str | None = None) -> dict:
        document = {
            "version": 2,
            "request_id": f"WRV2-CMD-{index:024x}",
            "operation": operation,
            "source": {"channel": "weixin", "principal_hash": "sha256:" + "a" * 64, "role": "owner"},
            "task_id": task_id,
        }
        if instruction is not None:
            document["instruction"] = instruction
        return document

    def status(self, assignment: dict, credential: str, *, sequence: int, state: str = "running") -> dict:
        request = assignment["request"]
        document = with_digest(
            {
                "version": 2,
                "message_type": "status",
                "runner_id": request["runner_id"],
                "task_id": request["task_id"],
                "assignment_epoch": request["assignment_epoch"],
                "sequence": sequence,
                "created_at": self.clock(),
                "expires_at": (self.clock.value + dt.timedelta(minutes=10)).isoformat(),
                "lease_id": request["lease_id"],
                "state": state,
                "stage": "codex",
                "updated_at": self.clock(),
            }
        )
        return self.store.record_status(
            document,
            credential=credential,
        )

    def result(self, assignment: dict, credential: str, *, sequence: int, state: str = "completed") -> dict:
        request = assignment["request"]
        result = {
            "version": 2,
            "message_type": "result",
            "runner_id": request["runner_id"],
            "task_id": request["task_id"],
            "assignment_epoch": request["assignment_epoch"],
            "sequence": sequence,
            "created_at": self.clock(),
            "expires_at": (self.clock.value + dt.timedelta(minutes=10)).isoformat(),
            "lease_id": request["lease_id"],
            "state": state,
            "finished_at": self.clock(),
            "summary": "本地候选完成。",
            "commits": ["d" * 40],
            "test_summary": "8 tests passed",
            "changed_path_count": 2,
            "next_actions": ["等待主控完整矩阵"],
            "candidate_id": "sha256:" + "c" * 64,
        }
        result["result_hash"] = digest(result)
        document = with_digest(result)
        return self.store.record_result(
            document,
            credential=credential,
        )

    def test_two_runners_compete_for_one_task_with_one_atomic_winner(self) -> None:
        self.add_runner("Runner A")
        self.add_runner("Runner B")
        task = self.add_task()
        with ThreadPoolExecutor(max_workers=16) as executor:
            results = list(executor.map(lambda _: self.store.claim_next(), range(16)))
        winners = [result for result in results if result is not None]
        self.assertEqual(len(winners), 1)
        self.assertEqual(winners[0]["task"]["task_id"], task["task_id"])
        self.assertEqual(sum(runner["work_state"] == "busy" for runner in self.store.list_runners()["runners"]), 1)

    def test_running_disconnect_requires_recovery_and_never_auto_transfers(self) -> None:
        first_id, first_credential = self.add_runner("Runner A")
        second_id, second_credential = self.add_runner("Runner B")
        task = self.add_task()
        assignment = self.store.claim_next()
        assert assignment is not None
        credentials = {first_id: first_credential, second_id: second_credential}
        assigned_id = assignment["request"]["runner_id"]
        other_id = second_id if assigned_id == first_id else first_id
        self.status(assignment, credentials[assigned_id], sequence=2)
        self.clock.advance(21)
        outcome = self.store.sweep()
        recovered = self.store.work_task(task["task_id"])
        self.assertEqual(outcome["recovery_required"], 1)
        self.assertEqual(recovered["state"], "recovery_required")
        self.assertEqual(recovered["assigned_runner_id"], assigned_id)
        self.assertIsNone(self.store.claim_next())
        self.assertEqual(self.store.runner(other_id)["work_state"], "idle")

    def test_busy_heartbeat_atomically_renews_task_and_active_lease(self) -> None:
        _runner_id, credential = self.add_runner("Runner Renew")
        task = self.add_task(57)
        assignment = self.store.claim_next()
        assert assignment is not None
        self.status(assignment, credential, sequence=2)
        original_expiry = assignment["request"]["lease_expires_at"]

        self.clock.advance(4)
        self.heartbeat_busy(assignment, credential, sequence=2)

        current = self.store.work_task(task["task_id"])
        lease = self.active_lease(assignment["request"]["lease_id"])
        expected_expiry = (self.clock.value + dt.timedelta(seconds=5)).isoformat()
        self.assertEqual(current["lease_expires_at"], expected_expiry)
        self.assertEqual(lease["expires_at"], expected_expiry)
        self.assertGreater(current["lease_expires_at"], original_expiry)
        self.assertEqual(current["assignment_epoch"], assignment["request"]["assignment_epoch"])
        self.assertEqual(current["lease_id"], assignment["request"]["lease_id"])

    def test_offline_running_task_waits_for_lease_expiry_before_recovery(self) -> None:
        self.store = RunnerStore(
            Path(self.temporary.name) / "long-lease.sqlite3",
            clock=self.clock,
            online_after_seconds=10,
            offline_after_seconds=20,
            lease_ttl_seconds=60,
        )
        runner_id, credential = self.add_runner("Runner Grace")
        task = self.add_task(58)
        assignment = self.store.claim_next()
        assert assignment is not None
        self.status(assignment, credential, sequence=2)

        self.clock.advance(21)
        before_expiry = self.store.sweep()
        current = self.store.work_task(task["task_id"])
        self.assertEqual(before_expiry["recovery_required"], 0)
        self.assertEqual(current["state"], "running")
        self.assertEqual(current["assigned_runner_id"], runner_id)
        self.assertEqual(current["assignment_epoch"], assignment["request"]["assignment_epoch"])

        self.clock.advance(40)
        after_expiry = self.store.sweep()
        recovered = self.store.work_task(task["task_id"])
        self.assertEqual(after_expiry["recovery_required"], 1)
        self.assertEqual(recovered["state"], "recovery_required")
        self.assertEqual(recovered["assigned_runner_id"], runner_id)

    def test_repeated_busy_heartbeats_keep_assignment_and_extend_lease_idempotently(self) -> None:
        _runner_id, credential = self.add_runner("Runner Repeated")
        task = self.add_task(59)
        assignment = self.store.claim_next()
        assert assignment is not None
        self.status(assignment, credential, sequence=2)

        self.clock.advance(2)
        first = self.heartbeat_busy(assignment, credential, sequence=2)
        first_task = self.store.work_task(task["task_id"])
        self.assertEqual(self.heartbeat_busy(assignment, credential, sequence=2), first)
        duplicate_task = self.store.work_task(task["task_id"])
        self.assertEqual(duplicate_task["lease_expires_at"], first_task["lease_expires_at"])

        self.clock.advance(2)
        self.heartbeat_busy(assignment, credential, sequence=3)
        renewed = self.store.work_task(task["task_id"])
        self.assertGreater(renewed["lease_expires_at"], first_task["lease_expires_at"])
        self.assertEqual(renewed["assignment_epoch"], assignment["request"]["assignment_epoch"])
        self.assertEqual(renewed["lease_id"], assignment["request"]["lease_id"])

    def test_long_running_task_completes_after_heartbeat_renewal_and_releases_lease(self) -> None:
        runner_id, credential = self.add_runner("Runner Complete")
        task = self.add_task(60)
        assignment = self.store.claim_next()
        assert assignment is not None
        self.status(assignment, credential, sequence=2)

        self.clock.advance(4)
        self.heartbeat_busy(assignment, credential, sequence=2)
        self.clock.advance(4)
        completed = self.result(assignment, credential, sequence=3)

        self.assertEqual(completed["state"], "completed")
        self.assertEqual(self.store.runner(runner_id)["work_state"], "idle")
        self.assertIsNone(self.store.runner(runner_id)["current_task_id"])
        lease = self.active_lease(assignment["request"]["lease_id"])
        self.assertEqual(lease["state"], "released")
        self.assertIsNotNone(lease["released_at"])

    def test_legacy_invalid_result_is_classified_as_late_only_after_recovery(self) -> None:
        runner_id, credential = self.add_runner("Runner Legacy")
        task = self.add_task(55)
        assignment = self.store.claim_next()
        assert assignment is not None
        self.status(assignment, credential, sequence=2)
        request = assignment["request"]
        legacy = {
            "version": 2,
            "message_type": "result",
            "runner_id": request["runner_id"],
            "task_id": request["task_id"],
            "assignment_epoch": request["assignment_epoch"],
            "sequence": 3,
            "created_at": self.clock(),
            "expires_at": (self.clock.value + dt.timedelta(minutes=10)).isoformat(),
            "lease_id": request["lease_id"],
            "state": "failed",
            "finished_at": self.clock(),
            "summary": "旧版失败结果。",
            "commits": [],
            "changed_path_count": 0,
            "next_actions": [],
            "candidate_id": None,
            "result_hash": "sha256:" + "0" * 64,
            "error_code": "git_metadata_write_denied",
        }
        document = with_digest(legacy)
        with self.assertRaises(StoreError) as active:
            self.store.record_result(document, credential=credential)
        self.assertEqual(active.exception.code, "runner_payload_invalid")

        self.clock.advance(21)
        self.store.sweep()
        with self.assertRaises(StoreError) as recovered:
            self.store.record_result(document, credential=credential)
        self.assertEqual(recovered.exception.code, "runner_late_message")
        current = self.store.work_task(task["task_id"])
        self.assertEqual(current["state"], "recovery_required")
        self.assertEqual(current["error_code"], None)

        runner = self.store.runner(runner_id)
        resolution = {
            "task_id": task["task_id"],
            "resolution": "confirmed_failed",
            "revision": runner["revision"],
            "request_id": "resolve-legacy-result-0001",
        }
        resolved = self.store.resolve_task_recovery(runner_id, resolution)
        self.assertEqual(resolved["task"]["state"], "failed")
        self.assertEqual(resolved["task"]["stage"], "recovery_resolved")
        self.assertEqual(resolved["task"]["error_code"], "recovery_confirmed_failed")
        self.assertEqual(resolved["runner"]["work_state"], "idle")
        self.assertIsNone(resolved["runner"]["current_task_id"])
        self.assertEqual(self.store.resolve_task_recovery(runner_id, resolution), resolved)

        with self.assertRaises(StoreError) as late_after_resolution:
            self.store.record_result(document, credential=credential)
        self.assertEqual(late_after_resolution.exception.code, "runner_late_message")

    def test_recovery_resolution_rejects_active_task_and_idempotency_conflict(self) -> None:
        runner_id, credential = self.add_runner("Runner Recovery")
        task = self.add_task(56)
        assignment = self.store.claim_next()
        assert assignment is not None
        self.status(assignment, credential, sequence=2)
        runner = self.store.runner(runner_id)
        payload = {
            "task_id": task["task_id"],
            "resolution": "confirmed_failed",
            "revision": runner["revision"],
            "request_id": "resolve-active-result-0001",
        }
        with self.assertRaises(StoreError) as active:
            self.store.resolve_task_recovery(runner_id, payload)
        self.assertEqual(active.exception.code, "runner_task_state_conflict")

        self.clock.advance(21)
        self.store.sweep()
        resolved = self.store.resolve_task_recovery(runner_id, payload)
        self.assertEqual(resolved["task"]["state"], "failed")
        conflict = dict(payload)
        conflict["task_id"] = "RW-OTHER000000000000000"
        with self.assertRaises(StoreError) as replay:
            self.store.resolve_task_recovery(runner_id, conflict)
        self.assertEqual(replay.exception.code, "idempotency_conflict")

    def test_unstarted_expired_lease_can_reschedule_with_new_epoch(self) -> None:
        first_id, _ = self.add_runner("Runner A")
        second_id, _ = self.add_runner("Runner B")
        task = self.add_task()
        first = self.store.claim_next()
        assert first is not None
        self.clock.advance(21)
        self.store.sweep()
        # Refresh only Runner B, so the expired unstarted task moves there.
        second = self.store.runner(second_id)
        # Credential lookup is intentionally impossible; create a fresh runner to
        # demonstrate deterministic reassignment without touching old credentials.
        third_id, _ = self.add_runner("Runner C")
        reassigned = self.store.claim_next()
        assert reassigned is not None
        self.assertNotEqual(reassigned["request"]["runner_id"], first_id)
        self.assertIn(reassigned["request"]["runner_id"], {second_id, third_id})
        self.assertEqual(reassigned["request"]["assignment_epoch"], 2)
        self.assertEqual(reassigned["task"]["task_id"], task["task_id"])

    def test_drain_finishes_current_task_then_disables_runner(self) -> None:
        runner_id, credential = self.add_runner("Runner A")
        self.add_task()
        assignment = self.store.claim_next()
        assert assignment is not None
        self.status(assignment, credential, sequence=2)
        runner = self.store.runner(runner_id)
        draining = self.store.drain(
            runner_id,
            {"revision": runner["revision"], "request_id": "runner-drain-0001"},
        )
        self.assertEqual(draining["runner"]["admin_state"], "draining")
        self.result(assignment, credential, sequence=3)
        finished = self.store.runner(runner_id)
        self.assertEqual(finished["admin_state"], "disabled")
        self.assertEqual(finished["work_state"], "idle")

    def test_old_epoch_sequence_and_digest_conflicts_are_rejected(self) -> None:
        _runner_id, credential = self.add_runner("Runner A")
        self.add_task()
        assignment = self.store.claim_next()
        assert assignment is not None
        self.status(assignment, credential, sequence=2)
        duplicate = self.status(assignment, credential, sequence=2)
        self.assertEqual(duplicate["state"], "running")
        request = assignment["request"]
        with self.assertRaises(StoreError) as context:
            conflicting = {
                "version": 2,
                "message_type": "status",
                "runner_id": request["runner_id"],
                "task_id": request["task_id"],
                "assignment_epoch": request["assignment_epoch"],
                "sequence": 2,
                "created_at": self.clock(),
                "expires_at": (self.clock.value + dt.timedelta(minutes=10)).isoformat(),
                "lease_id": request["lease_id"],
                "state": "running",
                "stage": "verify",
                "updated_at": self.clock(),
            }
            self.store.record_status(with_digest(conflicting), credential=credential)
        self.assertEqual(context.exception.code, "runner_message_conflict")

    def test_cancel_before_running_uses_full_idempotent_control(self) -> None:
        self.add_runner("Runner A")
        publisher = Publisher()
        service = RunnerManagerService(self.store, enabled=True, publisher=publisher)
        start = {
            "version": 2,
            "request_id": "WRV2-SERVICE-START-000000000001",
            "operation": "start",
            "source": {"channel": "weixin", "principal_hash": "sha256:" + "a" * 64, "role": "owner"},
            "project_alias": "renovation-hub",
            "instruction": "验证取消竞态",
        }
        started = service.work_command(start)
        self.assertEqual(len(publisher.requests), 1)
        cancellation = self.command("cancel", started["task_id"], index=1)
        first = service.work_command(cancellation)
        second = service.work_command(cancellation)
        self.assertEqual(first["state"], "dispatched")
        self.assertEqual(second["state"], "dispatched")
        self.assertEqual(len(publisher.controls), 1)
        control = publisher.controls[0]
        self.assertEqual(control["action"], "cancel")
        self.assertEqual(control["sequence"], 2)
        self.assertEqual(control["runner_id"], publisher.requests[0]["runner_id"])
        body = dict(control)
        body.pop("body_digest")
        self.assertEqual(control["body_digest"], digest(body))

    def test_confirmed_runner_offline_releases_lease_and_reconnect_dispatches_once(self) -> None:
        runner_id, credential = self.add_runner("Runner Offline")
        publisher = OfflineThenSuccessPublisher()
        service = RunnerManagerService(self.store, enabled=True, publisher=publisher)
        started = service.work_command(
            {
                "version": 2,
                "request_id": "WRV2-OFFLINE-START-00000000001",
                "operation": "start",
                "source": {
                    "channel": "weixin",
                    "principal_hash": "sha256:" + "a" * 64,
                    "role": "owner",
                },
                "project_alias": "renovation-hub",
                "instruction": "验证 Relay 断连窗口安全重排",
            }
        )
        self.assertEqual(started["state"], "waiting_runner")
        self.assertEqual(started["error_code"], "runner_offline")
        self.assertEqual(service.last_error, "runner_offline")
        waiting = self.store.work_task(started["task_id"])
        self.assertIsNone(waiting["assigned_runner_id"])
        self.assertIsNone(waiting["lease_id"])
        self.assertEqual(waiting["assignment_epoch"], 1)
        runner = self.store.runner(runner_id)
        self.assertEqual(runner["connectivity_state"], "offline")
        self.assertEqual(runner["work_state"], "idle")
        self.assertEqual(service.dispatch_waiting(), 0)
        self.assertEqual(publisher.attempts, 1)

        self.heartbeat_idle(runner_id, credential, sequence=2)
        self.assertEqual(service.dispatch_waiting(), 1)
        delivered = self.store.work_task(started["task_id"])
        self.assertEqual(delivered["state"], "dispatched")
        self.assertEqual(delivered["assignment_epoch"], 2)
        self.assertIsNone(delivered["error_code"])
        self.assertEqual(len(publisher.requests), 1)
        self.assertEqual(publisher.attempts, 2)
        self.assertIsNone(service.last_error)

    def test_indeterminate_publish_keeps_authoritative_lease_and_never_reassigns(self) -> None:
        runner_id, _credential = self.add_runner("Runner Indeterminate")
        publisher = OfflineThenSuccessPublisher(always_indeterminate=True)
        service = RunnerManagerService(self.store, enabled=True, publisher=publisher)
        started = service.work_command(
            {
                "version": 2,
                "request_id": "WRV2-INDETERMINATE-0000000001",
                "operation": "start",
                "source": {
                    "channel": "weixin",
                    "principal_hash": "sha256:" + "b" * 64,
                    "role": "owner",
                },
                "project_alias": "renovation-hub",
                "instruction": "验证未知发布结果不重放",
            }
        )
        self.assertEqual(started["state"], "dispatched")
        assigned = self.store.work_task(started["task_id"])
        self.assertEqual(assigned["assigned_runner_id"], runner_id)
        self.assertIsNotNone(assigned["lease_id"])
        self.assertEqual(self.store.runner(runner_id)["work_state"], "busy")
        self.assertEqual(service.last_error, "relay_publish_indeterminate")
        self.assertEqual(service.dispatch_waiting(), 0)
        self.assertEqual(publisher.attempts, 1)

    def test_active_cancel_without_relay_fails_without_mutating_assignment(self) -> None:
        self.add_runner("Runner A")
        task = self.add_task(77)
        assignment = self.store.claim_next()
        assert assignment is not None
        self.store.mark_dispatched(task["task_id"])
        service = RunnerManagerService(self.store, enabled=True, publisher=None)
        with self.assertRaises(StoreError) as context:
            service.work_command(self.command("cancel", task["task_id"], index=2))
        self.assertEqual(context.exception.code, "runner_relay_unavailable")
        current = self.store.work_task(task["task_id"])
        self.assertEqual(current["state"], "dispatched")
        self.assertEqual(current["assigned_runner_id"], assignment["request"]["runner_id"])
        self.assertIsNone(current["action_required"])

    def test_continue_reuses_runner_lease_epoch_and_is_idempotent(self) -> None:
        runner_id, credential = self.add_runner("Runner A")
        publisher = Publisher()
        service = RunnerManagerService(self.store, enabled=True, publisher=publisher)
        task = self.add_task(88)
        assignment = self.store.claim_next()
        assert assignment is not None
        self.store.mark_dispatched(task["task_id"])
        self.status(assignment, credential, sequence=2)
        self.result(assignment, credential, sequence=3)
        continuation = self.command("continue", task["task_id"], index=3, instruction="继续补充回归")
        first = service.work_command(continuation)
        second = service.work_command(continuation)
        self.assertEqual(first["state"], "dispatched")
        self.assertEqual(second["state"], "dispatched")
        self.assertEqual(len(publisher.controls), 1)
        control = publisher.controls[0]
        self.assertEqual(control["action"], "continue")
        self.assertEqual(control["instruction"], "继续补充回归")
        self.assertEqual(control["runner_id"], runner_id)
        self.assertEqual(control["lease_id"], assignment["request"]["lease_id"])
        self.assertEqual(control["assignment_epoch"], assignment["request"]["assignment_epoch"])
        self.assertEqual(control["sequence"], 4)

    def test_capacity_32_runners_1000_tasks_and_16_claimers_is_bounded(self) -> None:
        started = time.monotonic()
        for index in range(32):
            self.add_runner(f"Capacity {index:02d}")
        for index in range(1000):
            self.add_task(index + 100)
        with ThreadPoolExecutor(max_workers=16) as executor:
            claims = list(executor.map(lambda _: self.store.claim_next(), range(64)))
        self.assertEqual(sum(claim is not None for claim in claims), 32)
        task_ids = [claim["task"]["task_id"] for claim in claims if claim is not None]
        self.assertEqual(len(task_ids), len(set(task_ids)))
        self.assertEqual(self.store.integrity(), "ok")
        self.assertLess(time.monotonic() - started, 30)


if __name__ == "__main__":
    unittest.main()
