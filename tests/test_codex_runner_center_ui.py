from __future__ import annotations

from http.client import HTTPConnection
import json
from pathlib import Path
import tempfile
import threading
import unittest

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


class RunnerCenterUiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        path = Path(self.temporary.name) / "controller.sqlite3"
        self.controller_store = ControllerStore(path)
        self.runner_store = RunnerStore(path)
        self.manager = RunnerManagerService(self.runner_store)
        self.service = ControllerService(
            self.controller_store,
            App(),  # type: ignore[arg-type]
            intake_enabled=False,
            auth_mode="api_key",
            api_key="fixture-api-key",
            runner_manager=self.manager,
        )
        self.token = "t" * 32
        self.server = create_server(
            "127.0.0.1",
            0,
            service=self.service,
            api_token=self.token,
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
        return document["result"]["runner"], document["result"]["enrollment"]["token"]

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
            "管理功能已启用，任务执行 Relay 尚未接入",
            "Runner Center v2 已由 Add-on 配置关闭",
            "@media(max-width:700px)",
        ):
            self.assertIn(text, combined)
        self.assertNotIn("innerHTML", DASHBOARD_JS)
        self.assertNotIn('type="password"', DASHBOARD_HTML.lower())
        self.assertNotIn("xterm", combined.lower())

    def test_default_enabled_without_relay_and_explicit_false_fail_closed(self) -> None:
        status, document = self.request("GET", "/api/status")
        self.assertEqual(status, 200)
        self.assertEqual(
            document["runner_manager"],
            {
                "enabled": True,
                "relay_configured": False,
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
                "labels": [],
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
