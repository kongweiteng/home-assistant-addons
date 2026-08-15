from __future__ import annotations

import datetime as dt
import hashlib
from pathlib import Path
import tempfile
import unittest

from codex_controller.runner_store import RunnerStore
from codex_controller.store import StoreError


class Clock:
    def __init__(self) -> None:
        self.value = dt.datetime(2026, 8, 10, 8, 0, tzinfo=dt.timezone.utc)

    def __call__(self) -> str:
        return self.value.isoformat()

    def advance(self, seconds: int) -> None:
        self.value += dt.timedelta(seconds=seconds)


def enrollment_payload(request_id: str = "runner-create-0001") -> dict:
    return {
        "display_name": "常驻 Linux Runner",
        "os": "linux",
        "arch": "amd64",
        "labels": ["always-on", "docker"],
        "allowed_projects": ["renovation-hub"],
        "max_concurrency": 1,
        "request_id": request_id,
    }


class RunnerEnrollmentTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.clock = Clock()
        self.path = Path(self.temporary.name) / "controller.sqlite3"
        self.store = RunnerStore(self.path, clock=self.clock)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def redeem(self) -> tuple[str, str]:
        created = self.store.create_enrollment(enrollment_payload())
        runner_id = created["runner"]["runner_id"]
        token = created["enrollment"]["token"]
        redeemed = self.store.redeem_enrollment(
            {
                "token": token,
                "runner_id": runner_id,
                "protocol_version": 2,
                "agent_version": "0.1.0",
                "codex_version": "0.146.0",
                "os": "linux",
                "arch": "amd64",
                "capabilities": ["registered_projects", "worktree", "codex_exec_json"],
                "projects": ["renovation-hub"],
                "labels": ["always-on", "docker"],
                "policy_revision": 1,
                "self_check": {"ok": True, "checks": ["codex", "git", "workspace"]},
            }
        )
        return runner_id, redeemed["credential"]["secret"]

    def test_enrollment_secret_is_one_time_hashed_and_pending_requires_enable(self) -> None:
        created = self.store.create_enrollment(enrollment_payload())
        runner_id = created["runner"]["runner_id"]
        token = created["enrollment"]["token"]
        self.assertEqual(created["runner"]["admin_state"], "pending")
        self.assertNotIn(token.encode(), self.path.read_bytes())

        replay = self.store.create_enrollment(enrollment_payload())
        self.assertEqual(replay["runner"]["runner_id"], runner_id)
        self.assertFalse(replay["enrollment"]["secret_available"])
        self.assertNotIn("token", replay["enrollment"])

        redeemed = self.store.redeem_enrollment(
            {
                "token": token,
                "runner_id": runner_id,
                "protocol_version": 2,
                "agent_version": "0.1.0",
                "codex_version": "0.146.0",
                "os": "linux",
                "arch": "amd64",
                "capabilities": ["registered_projects", "worktree"],
                "projects": ["renovation-hub"],
                "labels": ["always-on", "docker"],
                "policy_revision": 1,
                "self_check": {"ok": True, "checks": ["codex", "git"]},
            }
        )
        secret = redeemed["credential"]["secret"]
        self.assertNotIn(secret.encode(), self.path.read_bytes())
        self.assertEqual(redeemed["runner"]["admin_state"], "pending")
        enabled = self.store.update_runner(
            runner_id,
            {"admin_state": "enabled", "revision": redeemed["runner"]["revision"], "request_id": "runner-enable-0001"},
        )
        self.assertEqual(enabled["admin_state"], "enabled")

        with self.assertRaises(StoreError) as context:
            self.store.redeem_enrollment(
                {
                    "token": token,
                    "runner_id": runner_id,
                    "protocol_version": 2,
                    "agent_version": "0.1.0",
                    "codex_version": "0.146.0",
                    "os": "linux",
                    "arch": "amd64",
                    "capabilities": ["codex_exec_json"],
                    "projects": ["renovation-hub"],
                    "labels": ["always-on", "docker"],
                    "policy_revision": 1,
                    "self_check": {"ok": True, "checks": ["codex"]},
                }
            )
        self.assertEqual(context.exception.code, "enrollment_replayed")

    def test_expired_enrollment_and_identity_mismatch_fail_closed(self) -> None:
        created = self.store.create_enrollment(enrollment_payload())
        self.clock.advance(901)
        with self.assertRaises(StoreError) as context:
            self.store.redeem_enrollment(
                {
                    "token": created["enrollment"]["token"],
                    "runner_id": created["runner"]["runner_id"],
                    "protocol_version": 2,
                    "agent_version": "0.1.0",
                    "codex_version": "0.146.0",
                    "os": "linux",
                    "arch": "amd64",
                    "capabilities": [],
                    "projects": ["renovation-hub"],
                    "labels": ["always-on", "docker"],
                    "policy_revision": 1,
                    "self_check": {"ok": True, "checks": []},
                }
            )
        self.assertEqual(context.exception.code, "enrollment_expired")

    def test_enrollment_rejects_labels_or_policy_outside_registry(self) -> None:
        created = self.store.create_enrollment(enrollment_payload())
        base = {
            "token": created["enrollment"]["token"],
            "runner_id": created["runner"]["runner_id"],
            "protocol_version": 2,
            "agent_version": "0.3.3",
            "codex_version": "0.146.0",
            "os": "linux",
            "arch": "amd64",
            "capabilities": [],
            "projects": ["renovation-hub"],
            "labels": ["always-on", "unregistered"],
            "policy_revision": 1,
            "self_check": {"ok": True, "checks": []},
        }
        with self.assertRaises(StoreError) as context:
            self.store.redeem_enrollment(base)
        self.assertEqual(context.exception.code, "runner_policy_rejected")
        with self.assertRaises(StoreError) as context:
            self.store.redeem_enrollment(
                {**base, "labels": ["always-on"], "policy_revision": 2}
            )
        self.assertEqual(context.exception.code, "runner_policy_rejected")

    def test_credential_rotation_replay_never_reveals_secret_and_delete_revokes(self) -> None:
        runner_id, original_secret = self.redeem()
        runner = self.store.runner(runner_id)
        enabled = self.store.update_runner(
            runner_id,
            {"admin_state": "enabled", "revision": runner["revision"], "request_id": "runner-enable-0002"},
        )
        disabled = self.store.update_runner(
            runner_id,
            {"admin_state": "disabled", "revision": enabled["revision"], "request_id": "runner-disable-0001"},
        )
        rotated = self.store.rotate_credential(
            runner_id,
            {"revision": disabled["revision"], "request_id": "runner-rotate-0001"},
        )
        new_secret = rotated["credential"]["secret"]
        self.assertNotEqual(new_secret, original_secret)
        self.assertNotIn(new_secret.encode(), self.path.read_bytes())
        replay = self.store.rotate_credential(
            runner_id,
            {"revision": disabled["revision"], "request_id": "runner-rotate-0001"},
        )
        self.assertFalse(replay["credential"]["secret_available"])
        self.assertNotIn("secret", replay["credential"])
        with self.assertRaises(StoreError):
            self.store.verify_credential(runner_id, original_secret)
        self.store.verify_credential(runner_id, new_secret)

        deleted = self.store.delete_runner(
            runner_id,
            {"revision": rotated["runner"]["revision"], "request_id": "runner-delete-0001"},
        )
        self.assertEqual(deleted["runner"]["admin_state"], "revoked")
        self.assertTrue(deleted["runner"]["archived"])
        with self.assertRaises(StoreError):
            self.store.verify_credential(runner_id, new_secret)


if __name__ == "__main__":
    unittest.main()
