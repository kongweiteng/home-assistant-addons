from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import datetime as dt
import hashlib
import json
from pathlib import Path
import tempfile
import time
import unittest

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
