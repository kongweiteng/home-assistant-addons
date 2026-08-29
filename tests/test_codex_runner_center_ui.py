from __future__ import annotations

from http.client import HTTPConnection
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest import mock

from codex_controller.api import DASHBOARD_HTML, DASHBOARD_JS, create_server
from codex_controller.runner_service import RunnerManagerService
from codex_controller.runner_store import RunnerStore
from codex_controller.service import ControllerService
from codex_controller.store import ControllerStore


class App:
    auth_mode = "apiKey"
    account_ready = True
    notification_handler = None

    @staticmethod
    def status() -> dict:
        return {
            "running": True,
            "initialized": True,
            "protocol_error": None,
            "account": {"auth_mode": "apiKey", "plan_type": None, "ready": True},
        }

    @staticmethod
    def stop() -> None:
        return None


class Installer:
    def __init__(self) -> None:
        self.tokens: dict[str, str] = {}

    @staticmethod
    def status() -> dict:
        return {
            "ready": True,
            "error_code": None,
            "runner_version": "0.3.18",
            "codex_version": "0.146.0",
            "python_version": "3.11.13",
        }

    @staticmethod
    def manifest() -> dict:
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
        if manifest != {"fixture": True}:
            raise AssertionError(f"unexpected fixture manifest: {manifest!r}")
        self.tokens[runner_id] = enrollment_token
        link = f"https://runner.example.com/install/{enrollment_token}"
        return {
            "link": link,
            "command": f"curl -fsSL {link} -o /tmp/install-runner && sh /tmp/install-runner",
            "runner_version": "0.3.18",
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
            "relay_url": "wss://runner.example.com/v1/runner",
            "os": os_name,
            "arch": arch,
            "projects": projects,
            "labels": labels,
            "policy_revision": policy_revision,
            "asset_url": f"https://downloads.example.com/codex-runner-0.3.18-{os_name}-{arch}.tar.gz",
            "asset_sha256": "a" * 64,
            "asset_size": 123456,
            "installer_url": "https://downloads.example.com/codex-runner-installer-2.sh",
            "installer_sha256": "b" * 64,
            "installer_size": 4567,
            "runner_version": "0.3.18",
            "codex_version": "0.146.0",
            "python_version": "3.11.13",
            "self_contained": True,
        }

class RunnerCenterUiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        path = Path(self.temporary.name) / "controller.sqlite3"
        self.controller_store = ControllerStore(path)
        self.runner_store = RunnerStore(path)
        self.installer = Installer()
        self.manager = RunnerManagerService(self.runner_store, installer=self.installer)
        self.service = ControllerService(
            self.controller_store,
            App(),  # type: ignore[arg-type]
            intake_enabled=False,
            auth_mode="api_key",
            api_key="fixture-api-key",
            runner_manager=self.manager,
        )
        self.token = "t" * 32
        self.relay_publish_token = "p" * 32
        self.relay_controller_token = "c" * 32
        self.server = create_server(
            "127.0.0.1",
            0,
            service=self.service,
            api_token=self.token,
            runner_relay_controller_api_token=self.relay_controller_token,
            max_request_bytes=1024 * 1024,
        )
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)
        self.temporary.cleanup()

    def request(
        self,
        method: str,
        path: str,
        payload: dict | None = None,
        headers: dict[str, str] | None = None,
        *,
        server: object | None = None,
    ) -> tuple[int, dict]:
        target = self.server if server is None else server
        connection = HTTPConnection("127.0.0.1", target.server_port, timeout=3)
        body = None if payload is None else json.dumps(payload).encode()
        request_headers = dict(headers or {})
        if body is not None:
            request_headers.setdefault("Content-Type", "application/json")
        try:
            connection.request(method, path, body=body, headers=request_headers)
            response = connection.getresponse()
            return response.status, json.loads(response.read())
        finally:
            connection.close()

    def csrf(self) -> str:
        status, document = self.request("GET", "/api/status")
        self.assertEqual(status, 200)
        self.assertTrue(document["runner_manager"]["enabled"])
        return document["csrf_token"]

    def create_runner(self, csrf: str) -> tuple[dict, str]:
        status, document = self.request(
            "POST",
            "/api/runner-enrollments",
            {
                "display_name": "页面 Linux Runner",
                "os": "linux",
                "arch": "amd64",
                "labels": ["always-on"],
                "allowed_projects": ["renovation-hub"],
                "max_concurrency": 1,
                "request_id": "ui-create-runner-0001",
            },
            {"X-CSRF-Token": csrf},
        )
        self.assertEqual(status, 201)
        result = document["result"]
        self.assertNotIn("token", result["enrollment"])
        self.assertFalse(result["enrollment"]["secret_available"])
        self.assertTrue(result["installation"]["command_available"])
        self.assertIn("/install/", result["installation"]["link"])
        self.assertIn("install-runner", result["installation"]["command"])
        runner = result["runner"]
        return runner, self.installer.tokens[runner["runner_id"]]

    def test_page_contains_complete_runner_management_without_terminal(self) -> None:
        combined = DASHBOARD_HTML + DASHBOARD_JS
        for text in (
            "Runner Center",
            "新增 Runner",
            "启用",
            "排空停用",
            "紧急停用",
            "轮换凭据",
            "删除",
            "self-check",
            "credential-rotation",
            "runnerStateFilter",
            "runnerPlatformFilter",
            "runnerRelayMissing",
            "runnerInstallerMissing",
            "管理功能已启用，任务执行 Relay 尚未接入",
            "Runner Center v2 已由 Add-on 配置关闭",
            "navigator.clipboard",
            "document.execCommand('copy')",
            "installationState",
            "runnerInstallCountdown",
            "runnerInstallLink",
            "copyRunnerLink",
            "openRunnerLink",
            "复制安装链接",
            "打开安装链接",
            "安装包已内置固定 Python、Runner 与 Codex",
            "enrollment-revocation",
            "enrollment-regeneration",
            "注册已过期",
            "@media(max-width:700px)",
        ):
            self.assertIn(text, combined)
        self.assertNotIn("innerHTML", DASHBOARD_JS)
        self.assertNotIn("showRunnerSecret", DASHBOARD_JS)
        self.assertNotIn('type="password"', DASHBOARD_HTML.lower())
        self.assertNotIn("xterm", combined.lower())

    def test_controller_version_is_consistent_across_runtime_surfaces(self) -> None:
        root = Path(__file__).resolve().parents[1] / "codex_controller"
        expected = "0.5.24"
        self.assertIn(f'version: "{expected}"', (root / "config.yaml").read_text(encoding="utf-8"))
        for relative in (
            "codex_controller/__init__.py",
            "codex_controller/api.py",
            "codex_controller/service.py",
            "codex_controller/app_server.py",
            "codex_controller/mcp_proxy.py",
            "codex_controller/runner_relay.py",
            "README.md",
            "DOCS.md",
            "CHANGELOG.md",
        ):
            with self.subTest(relative=relative):
                self.assertIn(expected, (root / relative).read_text(encoding="utf-8"))

    def test_default_enabled_without_relay_and_explicit_false_fail_closed(self) -> None:
        status, document = self.request("GET", "/api/status")
        self.assertEqual(status, 200)
        self.assertEqual(
            document["runner_manager"],
            {
                "enabled": True,
                "relay_configured": False,
                "installer": {
                    "ready": True,
                    "error_code": None,
                    "runner_version": "0.3.18",
                    "codex_version": "0.146.0",
                    "python_version": "3.11.13",
                },
                "last_error": None,
                "summary": {
                    "total": 0,
                    "enabled": 0,
                    "online": 0,
                    "busy": 0,
                    "recovery_required": 0,
                },
            },
        )
        runners_status, runners = self.request("GET", "/api/runners")
        self.assertEqual(runners_status, 200)
        self.assertEqual(runners["result"]["summary"]["total"], 0)
        self.assertEqual(document["version"], "0.5.24")
        self.assertEqual(document["source_identity"]["schema_version"], 1)
        self.assertEqual(document["source_identity"]["algorithm"], "sha256")
        self.assertRegex(document["source_identity"]["digest"], r"^[0-9a-f]{64}$")
        self.assertGreater(document["source_identity"]["file_count"], 0)

        disabled_manager = RunnerManagerService(self.runner_store, enabled=False)
        disabled_service = ControllerService(
            self.controller_store,
            App(),  # type: ignore[arg-type]
            intake_enabled=False,
            auth_mode="api_key",
            api_key="fixture-api-key",
            runner_manager=disabled_manager,
        )
        disabled_server = create_server(
            "127.0.0.1",
            0,
            service=disabled_service,
            api_token=self.token,
            max_request_bytes=1024 * 1024,
        )
        disabled_thread = threading.Thread(target=disabled_server.serve_forever, daemon=True)
        disabled_thread.start()
        try:
            disabled_status, disabled = self.request(
                "GET", "/api/status", server=disabled_server
            )
            self.assertEqual(disabled_status, 200)
            self.assertFalse(disabled["runner_manager"]["enabled"])
            blocked_status, blocked = self.request(
                "GET", "/api/runners", server=disabled_server
            )
            self.assertEqual(
                (blocked_status, blocked["error"]["code"]),
                (409, "runner_manager_disabled"),
            )
        finally:
            disabled_server.shutdown()
            disabled_server.server_close()
            disabled_thread.join(timeout=3)

    def test_runner_api_requires_csrf_and_supports_create_list_enable_drain_delete(self) -> None:
        missing_status, missing = self.request(
            "POST",
            "/api/runner-enrollments",
            {
                "display_name": "blocked",
                "os": "linux",
                "arch": "amd64",
                "labels": ["always-on"],
                "allowed_projects": ["renovation-hub"],
                "max_concurrency": 1,
                "request_id": "ui-create-blocked-0001",
            },
        )
        self.assertEqual((missing_status, missing["error"]["code"]), (403, "csrf_required"))

        csrf = self.csrf()
        runner, token = self.create_runner(csrf)
        list_status, listed = self.request("GET", "/api/runners")
        self.assertEqual(list_status, 200)
        self.assertEqual(listed["result"]["summary"]["total"], 1)
        self.assertNotIn(token, json.dumps(listed))

        redeemed = self.manager.redeem_enrollment(
            {
                "token": token,
                "runner_id": runner["runner_id"],
                "protocol_version": 2,
                "agent_version": "0.1.0",
                "codex_version": "0.146.0",
                "os": "linux",
                "arch": "amd64",
                "capabilities": ["registered_projects", "worktree"],
                "projects": ["renovation-hub"],
                "labels": [],
                "policy_revision": 1,
                "self_check": {"ok": True, "checks": ["codex", "git"]},
            }
        )
        enabled_status, enabled = self.request(
            "PATCH",
            f"/api/runners/{runner['runner_id']}",
            {
                "admin_state": "enabled",
                "revision": redeemed["runner"]["revision"],
                "request_id": "ui-enable-runner-0001",
            },
            {"X-CSRF-Token": csrf},
        )
        self.assertEqual(enabled_status, 200)
        draining_status, draining = self.request(
            "POST",
            f"/api/runners/{runner['runner_id']}/drain",
            {
                "revision": enabled["result"]["revision"],
                "request_id": "ui-drain-runner-0001",
            },
            {"X-CSRF-Token": csrf},
        )
        self.assertEqual(draining_status, 200)
        self.assertEqual(draining["result"]["runner"]["admin_state"], "disabled")
        deleted_status, deleted = self.request(
            "DELETE",
            f"/api/runners/{runner['runner_id']}",
            {
                "revision": draining["result"]["runner"]["revision"],
                "request_id": "ui-delete-runner-0001",
            },
            {"X-CSRF-Token": csrf},
        )
        self.assertEqual(deleted_status, 200)
        self.assertEqual(deleted["result"]["runner"]["admin_state"], "revoked")

    def test_runner_recovery_resolution_requires_csrf_and_routes_exact_payload(self) -> None:
        csrf = self.csrf()
        runner, _token = self.create_runner(csrf)
        payload = {
            "task_id": "RW-RECOVERY000000000001",
            "resolution": "confirmed_failed",
            "revision": runner["revision"],
            "request_id": "ui-resolve-recovery-0001",
        }
        blocked_status, blocked = self.request(
            "POST",
            f"/api/runners/{runner['runner_id']}/recovery-resolution",
            payload,
        )
        self.assertEqual((blocked_status, blocked["error"]["code"]), (403, "csrf_required"))

        expected = {"task": {"state": "failed"}, "resolution": "confirmed_failed"}
        with mock.patch.object(
            self.manager,
            "resolve_task_recovery",
            return_value=expected,
        ) as resolve:
            status, document = self.request(
                "POST",
                f"/api/runners/{runner['runner_id']}/recovery-resolution",
                payload,
                {"X-CSRF-Token": csrf},
            )
        self.assertEqual(status, 200)
        self.assertEqual(document["result"], expected)
        resolve.assert_called_once_with(runner["runner_id"], payload)

    def test_enrollment_revoke_regenerate_and_internal_relay_auth_are_fail_closed(self) -> None:
        csrf = self.csrf()
        runner, old_token = self.create_runner(csrf)
        revoke_status, revoked = self.request(
            "POST",
            f"/api/runners/{runner['runner_id']}/enrollment-revocation",
            {"revision": runner["revision"], "request_id": "ui-revoke-enrollment-0001"},
            {"X-CSRF-Token": csrf},
        )
        self.assertEqual(revoke_status, 200)
        self.assertEqual(revoked["result"]["runner"]["enrollment"]["state"], "revoked")

        regenerate_payload = {
            "revision": revoked["result"]["runner"]["revision"],
            "request_id": "ui-regenerate-enrollment-0001",
        }
        regenerate_status, regenerated = self.request(
            "POST",
            f"/api/runners/{runner['runner_id']}/enrollment-regeneration",
            regenerate_payload,
            {"X-CSRF-Token": csrf},
        )
        self.assertEqual(regenerate_status, 200)
        self.assertNotIn("token", regenerated["result"]["enrollment"])
        self.assertIn("installation", regenerated["result"])
        new_token = self.installer.tokens[runner["runner_id"]]
        self.assertNotEqual(old_token, new_token)

        bootstrap_status, bootstrap = self.request(
            "POST",
            "/internal/v2/runner-relay/install-bootstrap",
            {"ticket": new_token},
            {"Authorization": f"Bearer {self.relay_controller_token}"},
        )
        self.assertEqual(bootstrap_status, 200)
        self.assertEqual(bootstrap["result"]["runner_id"], runner["runner_id"])
        self.assertEqual(bootstrap["result"]["enrollment_token"], new_token)
        self.assertEqual(bootstrap["result"]["runner_version"], "0.3.18")
        self.assertEqual(bootstrap["result"]["labels"], ["always-on"])
        self.assertEqual(bootstrap["result"]["policy_revision"], 1)

        blocked_bootstrap_status, blocked_bootstrap = self.request(
            "POST",
            "/internal/v2/runner-relay/install-bootstrap",
            {"ticket": new_token},
        )
        self.assertEqual(
            (blocked_bootstrap_status, blocked_bootstrap["error"]["code"]),
            (401, "not_authorized"),
        )

        replay_status, replay = self.request(
            "POST",
            f"/api/runners/{runner['runner_id']}/enrollment-regeneration",
            regenerate_payload,
            {"X-CSRF-Token": csrf},
        )
        self.assertEqual(replay_status, 200)
        self.assertNotIn("installation", replay["result"])

        unauthorized_status, unauthorized = self.request(
            "POST",
            "/internal/v2/runner-relay/enroll",
            {
                "token": new_token,
                "runner_id": runner["runner_id"],
                "protocol_version": 2,
                "agent_version": "0.3.6",
                "codex_version": "0.146.0",
                "os": "linux",
                "arch": "amd64",
                "capabilities": ["registered_projects", "worktree"],
                "projects": ["renovation-hub"],
                "labels": ["always-on"],
                "policy_revision": 1,
                "self_check": {"ok": True, "checks": ["codex", "git"]},
            },
        )
        self.assertEqual((unauthorized_status, unauthorized["error"]["code"]), (401, "not_authorized"))

        publisher_identity_status, publisher_identity = self.request(
            "POST",
            "/internal/v2/runner-relay/enroll",
            {
                "token": new_token,
                "runner_id": runner["runner_id"],
                "protocol_version": 2,
                "agent_version": "0.3.6",
                "codex_version": "0.146.0",
                "os": "linux",
                "arch": "amd64",
                "capabilities": ["registered_projects", "worktree"],
                "projects": ["renovation-hub"],
                "labels": ["always-on"],
                "policy_revision": 1,
                "self_check": {"ok": True, "checks": ["codex", "git"]},
            },
            {"Authorization": f"Bearer {self.relay_publish_token}"},
        )
        self.assertEqual(
            (publisher_identity_status, publisher_identity["error"]["code"]),
            (401, "not_authorized"),
        )

        enroll_status, enrolled = self.request(
            "POST",
            "/internal/v2/runner-relay/enroll",
            {
                "token": new_token,
                "runner_id": runner["runner_id"],
                "protocol_version": 2,
                "agent_version": "0.3.6",
                "codex_version": "0.146.0",
                "os": "linux",
                "arch": "amd64",
                "capabilities": ["registered_projects", "worktree"],
                "projects": ["renovation-hub"],
                "labels": ["always-on"],
                "policy_revision": 1,
                "self_check": {"ok": True, "checks": ["codex", "git"]},
            },
            {"Authorization": f"Bearer {self.relay_controller_token}"},
        )
        self.assertEqual(enroll_status, 200)
        credential = enrolled["result"]["credential"]["secret"]
        auth_status, authenticated = self.request(
            "POST",
            "/internal/v2/runner-relay/authenticate",
            {"runner_id": runner["runner_id"], "credential": credential},
            {"Authorization": f"Bearer {self.relay_controller_token}"},
        )
        self.assertEqual(auth_status, 200)
        self.assertEqual(authenticated["result"]["authenticated"], True)

        unconfigured_server = create_server(
            "127.0.0.1",
            0,
            service=self.service,
            api_token=self.token,
            max_request_bytes=1024 * 1024,
        )
        unconfigured_thread = threading.Thread(
            target=unconfigured_server.serve_forever, daemon=True
        )
        unconfigured_thread.start()
        try:
            unconfigured_status, unconfigured = self.request(
                "POST",
                "/internal/v2/runner-relay/authenticate",
                {"runner_id": runner["runner_id"], "credential": credential},
                {"Authorization": f"Bearer {self.relay_controller_token}"},
                server=unconfigured_server,
            )
            self.assertEqual(
                (unconfigured_status, unconfigured["error"]["code"]),
                (503, "runner_relay_not_configured"),
            )
        finally:
            unconfigured_server.shutdown()
            unconfigured_server.server_close()
            unconfigured_thread.join(timeout=3)

    def test_revision_conflict_and_internal_v2_auth_are_fail_closed(self) -> None:
        csrf = self.csrf()
        runner, _ = self.create_runner(csrf)
        conflict_status, conflict = self.request(
            "PATCH",
            f"/api/runners/{runner['runner_id']}",
            {"display_name": "冲突", "revision": 999, "request_id": "ui-conflict-0001"},
            {"X-CSRF-Token": csrf},
        )
        self.assertEqual((conflict_status, conflict["error"]["code"]), (409, "revision_conflict"))

        work = {
            "version": 2,
            "request_id": "WRV2-" + "1" * 32,
            "operation": "start",
            "source": {"channel": "weixin", "principal_hash": "sha256:" + "a" * 64, "role": "owner"},
            "project_alias": "renovation-hub",
            "instruction": "增加页面回归测试",
        }
        unauth_status, unauth = self.request("POST", "/internal/v2/runner-manager/work", work)
        self.assertEqual((unauth_status, unauth["error"]["code"]), (401, "not_authorized"))
        accepted_status, accepted = self.request(
            "POST",
            "/internal/v2/runner-manager/work",
            work,
            {"Authorization": f"Bearer {self.token}"},
        )
        self.assertEqual(accepted_status, 200)
        self.assertEqual(accepted["version"], 2)
        self.assertEqual(accepted["operation"], "start")
        self.assertEqual(accepted["state"], "waiting_runner")
        self.assertNotIn("instruction", accepted)


if __name__ == "__main__":
    unittest.main()
