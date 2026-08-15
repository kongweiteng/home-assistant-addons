from __future__ import annotations

import datetime as dt
from pathlib import Path
import tempfile
import unittest

from codex_controller.runner_service import RunnerManagerService
from codex_controller.runner_store import RunnerStore
from codex_controller.store import StoreError


class Clock:
    def __init__(self) -> None:
        self.value = dt.datetime(2026, 8, 11, 1, 0, tzinfo=dt.timezone.utc)

    def __call__(self) -> str:
        return self.value.isoformat()

    def advance(self, seconds: int) -> None:
        self.value += dt.timedelta(seconds=seconds)


class Installer:
    def __init__(self, *, error: StoreError | None = None) -> None:
        self.error = error
        self.tokens: dict[str, str] = {}
        self.manifest_calls = 0

    def status(self) -> dict:
        if self.error is not None:
            return {"ready": False, "error_code": self.error.code, "runner_version": "0.3.2"}
        return {
            "ready": True,
            "error_code": None,
            "runner_version": "0.3.2",
            "codex_version": "0.146.0",
            "python_version": "3.11.13",
        }

    def manifest(self) -> dict:
        self.manifest_calls += 1
        if self.error is not None:
            raise self.error
        return {"fixture": True}

    def command(
        self,
        *,
        runner_id: str,
        enrollment_token: str,
        os_name: str,
        arch: str,
        projects: list[str],
        manifest: dict,
    ) -> dict:
        self.assert_manifest(manifest)
        self.tokens[runner_id] = enrollment_token
        link = f"https://runner.example.com/install/{enrollment_token}"
        return {
            "link": link,
            "command": f"curl -fsSL {link} -o /tmp/install-runner && sh /tmp/install-runner",
            "runner_version": "0.3.2",
            "codex_version": "0.146.0",
            "python_version": "3.11.13",
            "platform": os_name,
            "arch": arch,
            "self_contained": True,
        }

    def bootstrap(
        self,
        *,
        runner_id: str,
        enrollment_token: str,
        os_name: str,
        arch: str,
        projects: list[str],
        labels: list[str],
        policy_revision: int,
    ) -> dict:
        return {
            "runner_id": runner_id,
            "enrollment_token": enrollment_token,
            "os": os_name,
            "arch": arch,
            "projects": projects,
            "labels": labels,
            "policy_revision": policy_revision,
            "runner_version": "0.3.2",
        }

    @staticmethod
    def assert_manifest(manifest: dict) -> None:
        if manifest != {"fixture": True}:
            raise AssertionError("unexpected fixture manifest")


def enrollment_payload(request_id: str) -> dict:
    return {
        "display_name": "常驻 Linux Runner",
        "os": "linux",
        "arch": "amd64",
        "labels": ["always-on", "tests"],
        "allowed_projects": ["renovation-hub"],
        "max_concurrency": 1,
        "request_id": request_id,
    }


def redeem_payload(runner_id: str, token: str) -> dict:
    return {
        "token": token,
        "runner_id": runner_id,
        "protocol_version": 2,
        "agent_version": "0.3.2",
        "codex_version": "0.146.0",
        "os": "linux",
        "arch": "amd64",
        "capabilities": ["registered_projects", "worktree", "codex_exec_json"],
        "projects": ["renovation-hub"],
        "labels": ["always-on", "tests"],
        "policy_revision": 1,
        "self_check": {"ok": True, "checks": ["codex", "git", "workspace"]},
    }


class RunnerManagerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.path = Path(self.temporary.name) / "controller.sqlite3"
        self.clock = Clock()
        self.store = RunnerStore(self.path, clock=self.clock)
        self.installer = Installer()
        self.service = RunnerManagerService(self.store, installer=self.installer)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_create_returns_full_command_without_separate_token_and_status_is_ready(self) -> None:
        result = self.service.create_enrollment(enrollment_payload("manager-create-0001"))
        runner_id = result["runner"]["runner_id"]
        token = self.installer.tokens[runner_id]
        self.assertEqual(result["enrollment"]["state"], "pending")
        self.assertFalse(result["enrollment"]["secret_available"])
        self.assertNotIn("token", result["enrollment"])
        self.assertTrue(result["installation"]["command_available"])
        self.assertIn(token, result["installation"]["link"])
        self.assertIn(result["installation"]["link"], result["installation"]["command"])
        self.assertEqual(result["installation"]["expires_at"], result["enrollment"]["expires_at"])
        self.assertNotIn(token.encode(), self.path.read_bytes())
        self.assertEqual(self.service.status()["installer"]["ready"], True)
        bootstrap = self.service.install_bootstrap({"ticket": token})
        self.assertEqual(bootstrap["runner_id"], runner_id)
        self.assertEqual(bootstrap["enrollment_token"], token)
        self.assertEqual(bootstrap["labels"], ["always-on", "tests"])
        self.assertEqual(bootstrap["policy_revision"], 1)

        replay = self.service.create_enrollment(enrollment_payload("manager-create-0001"))
        self.assertNotIn("installation", replay)
        self.assertFalse(replay["enrollment"]["secret_available"])
        self.assertEqual(replay["enrollment"]["state"], "pending")

    def test_manifest_failure_happens_before_registry_mutation(self) -> None:
        service = RunnerManagerService(
            self.store,
            installer=Installer(
                error=StoreError(
                    "installer_manifest_digest_mismatch",
                    "manifest mismatch",
                    status=503,
                )
            ),
        )
        with self.assertRaises(StoreError) as context:
            service.create_enrollment(enrollment_payload("manager-create-0002"))
        self.assertEqual(context.exception.code, "installer_manifest_digest_mismatch")
        self.assertEqual(self.store.list_runners()["summary"]["total"], 0)

    def test_revoke_regenerate_and_replay_never_restore_old_command(self) -> None:
        created = self.service.create_enrollment(enrollment_payload("manager-create-0003"))
        runner_id = created["runner"]["runner_id"]
        old_token = self.installer.tokens[runner_id]
        revoked = self.service.revoke_enrollment(
            runner_id,
            {"revision": created["runner"]["revision"], "request_id": "manager-revoke-0001"},
        )
        self.assertEqual(revoked["runner"]["enrollment"]["state"], "revoked")
        with self.assertRaises(StoreError) as context:
            self.store.redeem_enrollment(redeem_payload(runner_id, old_token))
        self.assertEqual(context.exception.code, "enrollment_revoked")

        request = {
            "revision": revoked["runner"]["revision"],
            "request_id": "manager-regenerate-0001",
        }
        regenerated = self.service.regenerate_enrollment(runner_id, request)
        new_token = self.installer.tokens[runner_id]
        self.assertNotEqual(old_token, new_token)
        self.assertIn(new_token, regenerated["installation"]["link"])
        self.assertEqual(regenerated["runner"]["enrollment"]["state"], "pending")

        replay = self.service.regenerate_enrollment(runner_id, request)
        self.assertNotIn("installation", replay)
        self.assertNotIn("token", replay["enrollment"])
        self.assertFalse(replay["enrollment"]["secret_available"])

    def test_claimed_runner_authenticates_and_cannot_regenerate(self) -> None:
        created = self.service.create_enrollment(enrollment_payload("manager-create-0004"))
        runner_id = created["runner"]["runner_id"]
        redeemed = self.service.redeem_enrollment(
            redeem_payload(runner_id, self.installer.tokens[runner_id])
        )
        credential = redeemed["credential"]["secret"]
        self.assertEqual(redeemed["runner"]["enrollment"]["state"], "claimed")
        self.assertEqual(
            self.service.authenticate_runner({"runner_id": runner_id, "credential": credential}),
            {"authenticated": True, "runner_id": runner_id},
        )
        with self.assertRaises(StoreError) as context:
            self.service.regenerate_enrollment(
                runner_id,
                {
                    "revision": redeemed["runner"]["revision"],
                    "request_id": "manager-regenerate-0002",
                },
            )
        self.assertEqual(context.exception.code, "enrollment_state_conflict")

    def test_expired_enrollment_is_visible_and_cannot_be_revoked(self) -> None:
        created = self.service.create_enrollment(enrollment_payload("manager-create-0005"))
        self.clock.advance(901)
        runner = self.store.runner(created["runner"]["runner_id"])
        self.assertEqual(runner["enrollment"]["state"], "expired")
        with self.assertRaises(StoreError) as context:
            self.service.revoke_enrollment(
                runner["runner_id"],
                {"revision": runner["revision"], "request_id": "manager-revoke-0002"},
            )
        self.assertEqual(context.exception.code, "enrollment_state_conflict")


if __name__ == "__main__":
    unittest.main()
