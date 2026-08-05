from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest

from codex_controller.app_server import AppServerClient
from codex_controller.main import write_codex_config
from codex_controller.store import ControllerStore
from codex_controller.tool_proxy import ToolProxyServer, ToolRouter


class AppServerSchemaCompatibilityTests(unittest.TestCase):
    def setUp(self) -> None:
        schema_value = os.environ.get("CODEX_SCHEMA_DIR", "")
        if not schema_value:
            self.skipTest("CODEX_SCHEMA_DIR 未配置")
        self.schema_dir = Path(schema_value).resolve(strict=True)

    def load(self, relative: str) -> dict:
        document = json.loads((self.schema_dir / relative).read_text(encoding="utf-8"))
        self.assertIsInstance(document, dict)
        return document

    def test_controller_methods_and_fields_match_official_0146_schema(self) -> None:
        initialize = self.load("v1/InitializeParams.json")
        self.assertIn("clientInfo", initialize["required"])

        login = self.load("v2/LoginAccountParams.json")
        login_types = {
            variant["properties"]["type"]["enum"][0]
            for variant in login["oneOf"]
            if variant.get("properties", {}).get("type", {}).get("enum")
        }
        self.assertIn("chatgptDeviceCode", login_types)
        self.assertIn("apiKey", login_types)
        api_key_login = next(
            variant for variant in login["oneOf"]
            if variant.get("properties", {}).get("type", {}).get("enum") == ["apiKey"]
        )
        self.assertIn("apiKey", api_key_login["required"])

        login_response = self.load("v2/LoginAccountResponse.json")
        response_types = {
            variant["properties"]["type"]["enum"][0]
            for variant in login_response["oneOf"]
            if variant.get("properties", {}).get("type", {}).get("enum")
        }
        self.assertIn("apiKey", response_types)

        account = self.load("v2/GetAccountResponse.json")
        account_types = {
            variant["properties"]["type"]["enum"][0]
            for variant in account["definitions"]["Account"]["oneOf"]
        }
        self.assertIn("chatgpt", account_types)
        self.assertIn("apiKey", account_types)

        thread_start_document = self.load("v2/ThreadStartParams.json")
        thread_start = thread_start_document["properties"]
        for field in ("cwd", "sandbox", "approvalPolicy", "developerInstructions"):
            self.assertIn(field, thread_start)
        self.assertIn("read-only", thread_start_document["definitions"]["SandboxMode"]["enum"])

        thread_resume_document = self.load("v2/ThreadResumeParams.json")
        self.assertIn("threadId", thread_resume_document["required"])
        thread_resume = thread_resume_document["properties"]
        for field in ("cwd", "sandbox", "approvalPolicy", "developerInstructions"):
            self.assertIn(field, thread_resume)
        self.assertIn("read-only", thread_resume_document["definitions"]["SandboxMode"]["enum"])

        thread_fork_document = self.load("v2/ThreadForkParams.json")
        self.assertIn("threadId", thread_fork_document["required"])
        thread_fork = thread_fork_document["properties"]
        for field in ("cwd", "sandbox", "approvalPolicy", "developerInstructions"):
            self.assertIn(field, thread_fork)

        turn_start = self.load("v2/TurnStartParams.json")
        self.assertEqual(set(turn_start["required"]), {"input", "threadId"})
        for field in ("clientUserMessageId", "approvalPolicy"):
            self.assertIn(field, turn_start["properties"])
        turn_start_serialized = json.dumps(turn_start, ensure_ascii=False)
        for field in ('"localImage"', '"path"', '"detail"'):
            self.assertIn(field, turn_start_serialized)

        item_completed = self.load("v2/ItemCompletedNotification.json")
        self.assertTrue({"threadId", "turnId", "item"}.issubset(item_completed["required"]))
        turn_completed = self.load("v2/TurnCompletedNotification.json")
        self.assertTrue({"threadId", "turn"}.issubset(turn_completed["required"]))


class AppServerMcpRuntimeCompatibilityTests(unittest.TestCase):
    def test_official_app_server_replaces_empty_loaded_thread_when_context_changes(self) -> None:
        codex_binary = os.environ.get("CODEX_BINARY", "")
        if not codex_binary:
            self.skipTest("CODEX_BINARY 未配置")
        codex_binary_path = Path(codex_binary).resolve(strict=True)

        with tempfile.TemporaryDirectory() as raw_directory:
            root = Path(raw_directory)
            client = AppServerClient(
                [str(codex_binary_path), "app-server", "--stdio"],
                codex_home=root / "codex-home",
                workspace=root / "workspace",
                request_timeout=20,
            )
            try:
                client.start()
                client.configure_developer_context(["ledger_show"], "owner")
                original_thread = client.start_thread()
                client.configure_developer_context(["ledger_show"], "member_read_only")
                replacement_thread = client.resume_thread(original_thread)
                self.assertNotEqual(replacement_thread, original_thread)
                self.assertEqual(client.resume_thread(replacement_thread), replacement_thread)
            finally:
                client.stop()

    def test_official_app_server_forks_persisted_thread_with_updated_context(self) -> None:
        codex_binary = os.environ.get("CODEX_BINARY", "")
        if not codex_binary:
            self.skipTest("CODEX_BINARY 未配置")
        codex_binary_path = Path(codex_binary).resolve(strict=True)

        with tempfile.TemporaryDirectory() as raw_directory:
            root = Path(raw_directory)
            codex_home = root / "codex-home"
            workspace = root / "workspace"
            user_message_persisted = threading.Event()

            def observe(message: dict) -> None:
                if message.get("method") != "item/completed":
                    return
                params = message.get("params")
                item = params.get("item") if isinstance(params, dict) else None
                if isinstance(item, dict) and item.get("type") == "userMessage":
                    user_message_persisted.set()

            first = AppServerClient(
                [str(codex_binary_path), "app-server", "--stdio"],
                codex_home=codex_home,
                workspace=workspace,
                notification_handler=observe,
                request_timeout=20,
            )
            try:
                first.start()
                first.configure_developer_context(["ledger_show"], "owner")
                original_thread = first.start_thread()
                first.start_turn(original_thread, "fixture persistence turn", "fixture-persist-turn")
                self.assertTrue(user_message_persisted.wait(5), "app-server 未持久化合成用户消息")
            finally:
                first.stop()

            second = AppServerClient(
                [str(codex_binary_path), "app-server", "--stdio"],
                codex_home=codex_home,
                workspace=workspace,
                request_timeout=20,
            )
            try:
                second.start()
                second.configure_developer_context(["ledger_show"], "member_read_only")
                result = second.request(
                    "thread/fork",
                    {
                        "threadId": original_thread,
                        "cwd": str(workspace),
                        "sandbox": "read-only",
                        "approvalPolicy": "never",
                        "developerInstructions": second.current_developer_instructions(),
                    },
                )
                thread = result.get("thread") if isinstance(result, dict) else None
                forked_thread = thread.get("id") if isinstance(thread, dict) else None
                self.assertIsInstance(forked_thread, str)
                self.assertNotEqual(forked_thread, original_thread)
            finally:
                second.stop()

    def test_official_app_server_refreshes_dynamic_mcp_catalog(self) -> None:
        codex_binary = os.environ.get("CODEX_BINARY", "")
        if not codex_binary:
            self.skipTest("CODEX_BINARY 未配置")
        codex_binary_path = Path(codex_binary).resolve(strict=True)
        project_root = Path(__file__).resolve().parents[1]

        with tempfile.TemporaryDirectory() as raw_directory:
            root = Path(raw_directory)
            codex_home = root / "codex-home"
            workspace = root / "workspace"
            socket_path = root / "runtime" / "tool-proxy.sock"
            store = ControllerStore(root / "controller.sqlite3")
            router = ToolRouter(
                ledger_base_url="http://renovation-hub.invalid",
                ledger_token="l" * 32,
                gateway_base_url="http://weixin-gateway.invalid",
                gateway_token="g" * 32,
                operations_base_url="http://operations.invalid",
                operations_token="o" * 32,
                store=store,
            )
            proxy = ToolProxyServer(socket_path, router)
            proxy.start()
            write_codex_config(
                codex_home,
                socket_path,
                mcp_pythonpath=str(project_root / "codex_controller"),
            )
            client = AppServerClient(
                [str(codex_binary_path), "app-server", "--stdio"],
                codex_home=codex_home,
                workspace=workspace,
                request_timeout=20,
            )
            try:
                client.start()
                before = client.request("mcpServerStatus/list", {"detail": "full"})
                server = next(
                    item
                    for item in before.get("data", [])
                    if item.get("name") == "home_assistant_tools"
                )
                before_tools = set(server.get("tools", {}))
                self.assertEqual(len(before_tools), 32)
                self.assertIn("ledger_summary", before_tools)

                revision = store.tool_catalog_revision()
                store.update_tool_policy(
                    "ledger_summary",
                    enabled=False,
                    revision=revision,
                    request_id="runtime-list-change-0001",
                )

                deadline = time.monotonic() + 12
                after_tools = before_tools
                while time.monotonic() < deadline:
                    current = client.request("mcpServerStatus/list", {"detail": "full"})
                    current_server = next(
                        item
                        for item in current.get("data", [])
                        if item.get("name") == "home_assistant_tools"
                    )
                    after_tools = set(current_server.get("tools", {}))
                    if "ledger_summary" not in after_tools:
                        break
                    time.sleep(0.5)

                self.assertEqual(len(after_tools), 31)
                self.assertNotIn("ledger_summary", after_tools)
                control = store.tool_control_document(
                    router.configured_tools(),
                    router.route_ready_tools(),
                )
                self.assertTrue(control["mcp"]["current"])
                self.assertEqual(control["mcp"]["observed_revision"], revision + 1)
                summary = next(tool for tool in control["tools"] if tool["name"] == "ledger_summary")
                self.assertFalse(summary["enabled"])
                self.assertFalse(summary["mcp_published"])
                self.assertFalse(summary["callable"])
            finally:
                client.stop()
                proxy.stop()


if __name__ == "__main__":
    unittest.main()
