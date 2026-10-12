from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from codex_controller.app_server import AppServerClient
from codex_controller.fnos_skill import FNOS_TOOLS, FnosSkillError
from codex_controller.store import ControllerStore
from codex_controller.tool_proxy import ToolProxyError, ToolRouter


class FakeFnos:
    def __init__(self, ready=True):
        self.ready = ready
        self.calls = []
        self.error = None

    def configured(self):
        return self.ready

    def call(self, name, arguments, job_id=""):
        self.calls.append((name, arguments, job_id))
        if self.error:
            raise FnosSkillError("fnos_auth_required", "飞牛授权不可用或权限不足，请重新完成 OAuth 授权。")
        return {"query": "version", "data": {"version": "1.2.0800"}, "data_available": True}


class FnosRouterTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = ControllerStore(Path(self.temp.name) / "controller.sqlite3")
        self.client = FakeFnos()
        self.router = ToolRouter(store=self.store, fnos_client=self.client)

    def tearDown(self):
        self.temp.cleanup()

    def test_owner_gets_tools_members_and_legacy_do_not(self):
        self.assertTrue(FNOS_TOOLS <= set(self.router.available_tools("owner")))
        for profile in ("member_read_only", "owner_legacy"):
            self.assertFalse(FNOS_TOOLS & set(self.router.available_tools(profile)))
            self.router.begin_job("job-1", "message-1", profile)
            try:
                for name in FNOS_TOOLS:
                    with self.assertRaises(ToolProxyError) as caught:
                        self.router.call(name, {"query": "version"})
                    self.assertEqual(caught.exception.code, "tool_not_allowed_for_profile")
            finally:
                self.router.clear_job("job-1")
        self.assertEqual(self.client.calls, [])

    def test_missing_owner_context_and_unconfigured_block_execution(self):
        with self.assertRaises(ToolProxyError) as caught:
            self.router.call("fnos_query", {"query": "version"})
        self.assertEqual(caught.exception.code, "tool_not_allowed_for_profile")
        self.client.ready = False
        self.router.begin_job("job-1", "message-1", "owner")
        with self.assertRaises(ToolProxyError) as caught:
            self.router.call("fnos_query", {"query": "version"})
        self.assertEqual(caught.exception.code, "tool_unconfigured")
        self.assertEqual(self.client.calls, [])

    def test_disabled_policy_and_safe_errors_are_audited(self):
        self.router.begin_job("job-1", "message-1", "owner")
        revision = self.store.tool_policy_snapshot()["revision"]
        self.store.update_tool_policy("fnos_query", enabled=False, revision=revision, request_id="request-fnos-disable")
        with self.assertRaises(ToolProxyError) as caught:
            self.router.call("fnos_query", {"query": "version"})
        self.assertEqual(caught.exception.code, "tool_disabled")
        self.assertEqual(self.client.calls, [])
        revision = self.store.tool_policy_snapshot()["revision"]
        self.store.update_tool_policy("fnos_query", enabled=True, revision=revision, request_id="request-fnos-enable")
        self.client.error = True
        with self.assertRaises(ToolProxyError) as caught:
            self.router.call("fnos_query", {"query": "version"})
        self.assertEqual(caught.exception.code, "fnos_auth_required")
        with self.store._connect() as connection:
            rows = connection.execute("SELECT outcome,error_code FROM tool_invocations ORDER BY id").fetchall()
        self.assertEqual([(r["outcome"], r["error_code"]) for r in rows],
                         [("rejected", "tool_disabled"), ("failed", "fnos_auth_required")])

    def test_current_job_is_forwarded_and_catalog_instructions_match(self):
        self.router.begin_job("job-owner", "message-owner", "owner")
        result = self.router.call("fnos_query", {"query": "version"})
        self.assertTrue(result["data_available"])
        self.assertEqual(self.client.calls, [("fnos_query", {"query": "version"}, "job-owner")])
        self.assertTrue(FNOS_TOOLS <= {t["name"] for t in self.router.catalog_payload()["tools"]})
        instructions = AppServerClient.build_developer_instructions(
            self.router.available_tools("owner"), "owner", self.router.tool_definitions_by_name())
        self.assertIn("fnos_query", instructions)
        self.assertIn("unknown 不重放", instructions)
        member = AppServerClient.build_developer_instructions(
            self.router.available_tools("member_read_only"), "member_read_only", self.router.tool_definitions_by_name())
        self.assertNotIn("fnos_service_control", member)

    def test_job_switch_between_authorization_and_dispatch_cannot_execute(self):
        self.router.begin_job("job-old", "message-old", "owner")
        original = self.client.configured

        def swap_job():
            self.client.configured = original
            self.router.clear_job("job-old")
            self.router.begin_job("job-new", "message-new", "owner")
            return original()

        self.client.configured = swap_job
        with self.assertRaises(ToolProxyError) as caught:
            self.router.call("fnos_service_control", {"target_type": "container", "action": "restart", "target": "example-service"})
        self.assertEqual(caught.exception.code, "tool_context_unavailable")
        self.assertEqual(self.client.calls, [])


if __name__ == "__main__":
    unittest.main()
