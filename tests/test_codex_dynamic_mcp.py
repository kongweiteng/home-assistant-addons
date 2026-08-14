from __future__ import annotations

from copy import deepcopy
import json
import os
from pathlib import Path
import select
import sqlite3
import subprocess
import sys
import tempfile
import time
import tomllib
import unittest

from codex_controller.app_server import AppServerClient
from codex_controller.hub_manifest import (
    BOOTSTRAP_MANIFEST,
    HubManifestError,
    manifest_digest,
    validate_hub_manifest,
)
from codex_controller.main import write_codex_config
from codex_controller.store import ControllerStore, StoreError
from codex_controller.tool_catalog import MEMBER_READ_ONLY_TOOL_NAMES
from codex_controller.tool_proxy import (
    ToolProxyError,
    ToolProxyServer,
    ToolRouter,
)
from renovation_hub.business_tools import business_manifest


FUTURE_READ = "renovation_future_lookup"
FUTURE_WRITE = "renovation_future_update"


def signed_manifest(
    *,
    include_future_read: bool = False,
    include_future_write: bool = False,
    catalog_revision: int = 1,
) -> dict:
    document = deepcopy(business_manifest())
    document["catalog_revision"] = catalog_revision
    if include_future_read:
        source = next(tool for tool in document["tools"] if tool["name"] == "renovation_search")
        future = deepcopy(source)
        future.update(
            {
                "name": FUTURE_READ,
                "display_name": "未来只读工具",
                "description": "用于验证 Controller 无需静态目录即可发布和调用未来只读工具。",
                "inputSchema": {
                    "type": "object",
                    "properties": {"query": {"type": "string", "maxLength": 120}},
                    "required": ["query"],
                    "additionalProperties": False,
                },
            }
        )
        document["tools"].append(future)
    if include_future_write:
        source = next(
            tool for tool in document["tools"] if tool["name"] == "renovation_project_update"
        )
        future = deepcopy(source)
        future.update(
            {
                "name": FUTURE_WRITE,
                "display_name": "未来写入工具",
                "description": "用于验证 Controller 为未来 JSON 写工具生成稳定幂等键。",
                "inputSchema": {
                    "type": "object",
                    "properties": {"value": {"type": "string", "maxLength": 120}},
                    "required": ["value"],
                    "additionalProperties": False,
                },
            }
        )
        document["tools"].append(future)
    document["tools"] = sorted(document["tools"], key=lambda tool: tool["name"])
    document["catalog_digest"] = manifest_digest(document)
    return document


class ManifestEndpoint:
    def __init__(self, manifest: dict):
        self.manifest: dict | BaseException = manifest
        self.calls: list[tuple[str, str, dict | None]] = []
        self.tool_payloads: list[dict] = []

    def __call__(self, method: str, url: str, _token: str, payload: dict | None) -> dict:
        self.calls.append((method, url, deepcopy(payload)))
        if method == "GET" and url.endswith("/internal/v1/mcp/manifest"):
            if isinstance(self.manifest, BaseException):
                raise self.manifest
            return deepcopy(self.manifest)
        if method == "POST" and url.endswith("/internal/v1/tools/call"):
            assert payload is not None
            self.tool_payloads.append(deepcopy(payload))
            return {"version": 1, "result": {"tool": payload["name"], "ok": True}}
        raise AssertionError(f"unexpected request: {method} {url}")


class DynamicMcpTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.store = ControllerStore(self.root / "controller.sqlite3")

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def router(self, endpoint: ManifestEndpoint, *, gateway: bool = False) -> ToolRouter:
        return ToolRouter(
            ledger_base_url="http://renovation-hub:8101",
            ledger_token="l" * 32,
            gateway_base_url="http://weixin-gateway:8103" if gateway else "",
            gateway_token="g" * 32 if gateway else "",
            request_json=endpoint,
            store=self.store,
        )

    def test_bootstrap_is_available_without_hub_sync(self) -> None:
        endpoint = ManifestEndpoint(signed_manifest())
        router = self.router(endpoint)

        self.assertEqual(len(BOOTSTRAP_MANIFEST.definitions), 27)
        self.assertEqual(len(router.tool_definitions()), 37)
        self.assertIn("ledger_summary", router.available_tools())
        self.assertNotIn("ledger_attach", router.available_tools())
        self.assertNotIn("renovation_search", router.available_tools())
        self.assertEqual(router.tool_status()["hub_manifest"]["source"], "bootstrap")
        self.assertEqual(endpoint.calls, [])

    def test_current_hub_manifest_and_non_monotonic_revisions_are_accepted(self) -> None:
        current = validate_hub_manifest(business_manifest())
        self.assertEqual(len(current.definitions), 31)
        self.assertIn("renovation_mutate", {definition.name for definition in current.definitions})

        endpoint = ManifestEndpoint(signed_manifest(catalog_revision=9))
        router = self.router(endpoint)
        first = router.sync_hub_manifest()
        self.assertTrue(first["changed"])
        self.assertEqual(first["revision"], 2)
        self.assertIn("renovation_search", router.available_tools())

        endpoint.manifest = signed_manifest(catalog_revision=2)
        second = router.sync_hub_manifest()
        self.assertTrue(second["changed"])
        self.assertEqual(second["revision"], 3)
        self.assertEqual(router.tool_status()["hub_manifest"]["hub_revision"], 2)

        unchanged = router.sync_hub_manifest()
        self.assertFalse(unchanged["changed"])
        self.assertEqual(unchanged["revision"], 3)

    def test_manifest_rejects_digest_namespace_schema_transport_exposure_and_annotations(self) -> None:
        cases: dict[str, tuple[callable, str]] = {
            "digest": (
                lambda document: document.update({"catalog_digest": "sha256:" + "0" * 64}),
                "manifest_digest_mismatch",
            ),
            "namespace": (
                lambda document: document["tools"][0].update({"name": "ha_future_tool"}),
                "manifest_tool_name_invalid",
            ),
            "schema": (
                lambda document: document["tools"][0]["inputSchema"].update(
                    {"additionalProperties": True}
                ),
                "manifest_schema_invalid",
            ),
            "transport": (
                lambda document: document["tools"][0].update({"transport": "arbitrary_http"}),
                "manifest_transport_invalid",
            ),
            "exposure": (
                lambda document: document["tools"][0].update({"exposure": "internal"}),
                "manifest_exposure_invalid",
            ),
            "annotations": (
                lambda document: document["tools"][0]["annotations"].update(
                    {"openWorldHint": True}
                ),
                "manifest_annotations_invalid",
            ),
            "second_media_stream": (
                lambda document: document["tools"][0].update(
                    {
                        "name": "renovation_future_stream",
                        "risk_type": "write",
                        "transport": "gateway_media_stream",
                        "requires_job_context": True,
                        "idempotent_write": True,
                        "annotations": {
                            "readOnlyHint": False,
                            "destructiveHint": False,
                            "idempotentHint": True,
                            "openWorldHint": False,
                        },
                    }
                ),
                "manifest_transport_invalid",
            ),
        }
        for name, (mutate, expected_code) in cases.items():
            with self.subTest(name=name):
                document = signed_manifest()
                mutate(document)
                if name != "digest":
                    document["catalog_digest"] = manifest_digest(document)
                with self.assertRaises(HubManifestError) as context:
                    validate_hub_manifest(document)
                self.assertEqual(context.exception.code, expected_code)

    def test_last_good_survives_restart_unreachable_and_invalid_remote_manifest(self) -> None:
        manifest = signed_manifest(include_future_read=True, catalog_revision=7)
        endpoint = ManifestEndpoint(manifest)
        router = self.router(endpoint)
        router.sync_hub_manifest()
        self.assertIn(FUTURE_READ, router.available_tools())

        unavailable = ManifestEndpoint(
            manifest
        )
        unavailable.manifest = ToolProxyError("upstream_unavailable", "fixture unavailable")
        restarted = ToolRouter(
            ledger_base_url="http://renovation-hub:8101",
            ledger_token="l" * 32,
            request_json=unavailable,
            store=ControllerStore(self.root / "controller.sqlite3"),
        )
        self.assertIn(FUTURE_READ, restarted.available_tools())
        with self.assertRaises(ToolProxyError):
            restarted.sync_hub_manifest()
        self.assertIn(FUTURE_READ, restarted.available_tools())
        status = restarted.tool_status()["hub_manifest"]
        self.assertEqual(status["source"], "last_good")
        self.assertEqual(status["error_code"], "hub_manifest_unavailable")

        invalid = deepcopy(manifest)
        invalid["catalog_digest"] = "sha256:" + "f" * 64
        unavailable.manifest = invalid
        with self.assertRaises(ToolProxyError) as context:
            restarted.sync_hub_manifest()
        self.assertEqual(context.exception.code, "manifest_digest_mismatch")
        self.assertIn(FUTURE_READ, restarted.available_tools())

    def test_future_json_tools_are_owner_callable_and_member_remains_fixed(self) -> None:
        endpoint = ManifestEndpoint(
            signed_manifest(
                include_future_read=True,
                include_future_write=True,
                catalog_revision=4,
            )
        )
        router = self.router(endpoint)
        router.sync_hub_manifest()

        self.assertIn(FUTURE_READ, router.available_tools("owner"))
        self.assertIn(FUTURE_WRITE, router.available_tools("owner_legacy"))
        self.assertEqual(
            set(router.available_tools("member_read_only")),
            set(MEMBER_READ_ONLY_TOOL_NAMES),
        )

        router.begin_job("owner-job", "owner-message", "owner")
        self.assertTrue(router.call(FUTURE_READ, {"query": "水电"})["result"]["ok"])
        first = router.call(
            FUTURE_WRITE,
            {"value": "完成", "idempotency_key": "model-supplied-value"},
        )
        second = router.call(
            FUTURE_WRITE,
            {"value": "完成", "idempotency_key": "another-model-value"},
        )
        self.assertTrue(first["result"]["ok"] and second["result"]["ok"])
        write_payloads = [
            payload for payload in endpoint.tool_payloads if payload["name"] == FUTURE_WRITE
        ]
        self.assertEqual(len(write_payloads), 2)
        first_key = write_payloads[0]["arguments"]["idempotency_key"]
        second_key = write_payloads[1]["arguments"]["idempotency_key"]
        self.assertEqual(first_key, second_key)
        self.assertRegex(first_key, r"^sha256:[a-f0-9]{64}$")
        router.clear_job("owner-job")

        router.begin_job("legacy-job", "legacy-message", "owner_legacy")
        self.assertTrue(router.call(FUTURE_READ, {"query": "木工"})["result"]["ok"])
        router.clear_job("legacy-job")

        calls_before_member = len(endpoint.tool_payloads)
        router.begin_job("member-job", "member-message", "member_read_only")
        with self.assertRaises(ToolProxyError) as context:
            router.call(FUTURE_READ, {"query": "油漆"})
        self.assertEqual(context.exception.code, "tool_not_allowed_for_profile")
        self.assertEqual(len(endpoint.tool_payloads), calls_before_member)
        router.clear_job("member-job")

        instructions = AppServerClient.build_developer_instructions(
            router.available_tools("owner"),
            "owner",
            router.tool_definitions_by_name(),
        )
        self.assertIn(FUTURE_READ, instructions)
        self.assertIn(FUTURE_WRITE, instructions)

    def test_disabled_tool_is_rejected_at_call_time_before_upstream(self) -> None:
        endpoint = ManifestEndpoint(signed_manifest(include_future_read=True))
        router = self.router(endpoint)
        router.sync_hub_manifest()
        router.begin_job("owner-job", "owner-message", "owner")
        revision = self.store.tool_catalog_revision()
        router.update_tool_policy(
            FUTURE_READ,
            enabled=False,
            revision=revision,
            request_id="disable-future-read-0001",
        )
        calls_before = len(endpoint.tool_payloads)
        with self.assertRaises(ToolProxyError) as context:
            router.call(FUTURE_READ, {"query": "fixture"})
        self.assertEqual(context.exception.code, "tool_disabled")
        self.assertEqual(len(endpoint.tool_payloads), calls_before)

    def test_retired_tool_is_not_published_and_disabled_policy_is_preserved(self) -> None:
        future_manifest = signed_manifest(include_future_read=True, catalog_revision=5)
        endpoint = ManifestEndpoint(future_manifest)
        router = self.router(endpoint)
        router.sync_hub_manifest()
        router.update_tool_policy(
            FUTURE_READ,
            enabled=False,
            revision=self.store.tool_catalog_revision(),
            request_id="disable-future-retire-0001",
        )

        endpoint.manifest = signed_manifest(catalog_revision=2)
        router.sync_hub_manifest()
        self.assertNotIn(FUTURE_READ, router.available_tools("owner"))
        self.assertNotIn(
            FUTURE_READ,
            {tool["name"] for tool in router.catalog_payload()["tools"]},
        )
        with sqlite3.connect(self.store.database_path) as connection:
            row = connection.execute(
                "SELECT enabled FROM tool_policies WHERE tool_name=?",
                (FUTURE_READ,),
            ).fetchone()
        self.assertEqual(row, (0,))
        with self.assertRaises(StoreError) as retired:
            router.update_tool_policy(
                FUTURE_READ,
                enabled=True,
                revision=self.store.tool_catalog_revision(),
                request_id="enable-retired-tool-0001",
            )
        self.assertEqual(retired.exception.code, "tool_not_found")

        endpoint.manifest = future_manifest
        router.sync_hub_manifest()
        self.assertNotIn(FUTURE_READ, router.available_tools("owner"))

    def test_catalog_payload_contains_complete_dynamic_schema(self) -> None:
        endpoint = ManifestEndpoint(
            signed_manifest(include_future_read=True, include_future_write=True)
        )
        router = self.router(endpoint)
        router.sync_hub_manifest()
        documents = {tool["name"]: tool for tool in router.catalog_payload()["tools"]}
        self.assertEqual(
            documents[FUTURE_READ]["inputSchema"],
            next(
                tool["inputSchema"]
                for tool in endpoint.manifest["tools"]
                if tool["name"] == FUTURE_READ
            ),
        )
        self.assertTrue(documents[FUTURE_READ]["annotations"]["readOnlyHint"])
        self.assertFalse(documents[FUTURE_WRITE]["annotations"]["readOnlyHint"])

    def test_mcp_publishes_dynamic_schema_and_emits_list_changed(self) -> None:
        endpoint = ManifestEndpoint(signed_manifest(include_future_read=True))
        router = self.router(endpoint)
        socket_path = self.root / "runtime" / "tool-proxy.sock"
        proxy = ToolProxyServer(socket_path, router)
        proxy.start()
        project_root = Path(__file__).resolve().parents[1]
        environment = {
            **os.environ,
            "PYTHONPATH": str(project_root / "codex_controller"),
            "CONTROLLER_MCP_SOCKET": str(socket_path),
        }
        process = subprocess.Popen(
            [sys.executable, "-m", "codex_controller.mcp_proxy"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            env=environment,
        )

        def send(message: dict) -> None:
            assert process.stdin is not None
            process.stdin.write(json.dumps(message, ensure_ascii=False) + "\n")
            process.stdin.flush()

        def receive(timeout: float = 5.0) -> dict:
            assert process.stdout is not None
            ready, _, _ = select.select([process.stdout], [], [], timeout)
            self.assertTrue(ready, "MCP 子进程未按时返回消息")
            line = process.stdout.readline()
            self.assertTrue(line)
            return json.loads(line)

        try:
            send({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
            self.assertEqual(receive()["id"], 1)
            send({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
            initial = receive()
            self.assertEqual(initial["id"], 2)
            self.assertNotIn(
                FUTURE_READ,
                {tool["name"] for tool in initial["result"]["tools"]},
            )

            router.sync_hub_manifest()
            deadline = time.monotonic() + 5
            notification = None
            while time.monotonic() < deadline:
                message = receive(max(0.1, deadline - time.monotonic()))
                if message.get("method") == "notifications/tools/list_changed":
                    notification = message
                    break
            self.assertIsNotNone(notification)

            send({"jsonrpc": "2.0", "id": 3, "method": "tools/list", "params": {}})
            refreshed = receive()
            self.assertEqual(refreshed["id"], 3)
            documents = {tool["name"]: tool for tool in refreshed["result"]["tools"]}
            self.assertIn(FUTURE_READ, documents)
            self.assertEqual(documents[FUTURE_READ]["inputSchema"]["required"], ["query"])
            self.assertTrue(documents[FUTURE_READ]["annotations"]["readOnlyHint"])
        finally:
            if process.poll() is None:
                process.terminate()
                process.wait(timeout=5)
            for stream in (process.stdin, process.stdout, process.stderr):
                if stream is not None and not stream.closed:
                    stream.close()
            proxy.stop()

    def test_codex_config_has_server_level_default_approval(self) -> None:
        codex_home = self.root / "codex-home"
        write_codex_config(
            codex_home,
            self.root / "runtime" / "tool-proxy.sock",
            mcp_python=sys.executable,
            mcp_pythonpath=str(Path(__file__).resolve().parents[1] / "codex_controller"),
        )
        parsed = tomllib.loads((codex_home / "config.toml").read_text(encoding="utf-8"))
        server = parsed["mcp_servers"]["home_assistant_tools"]
        self.assertEqual(server["default_tools_approval_mode"], "approve")
        self.assertEqual(server["tools"]["ledger_summary"]["approval_mode"], "approve")

    def test_official_app_server_refreshes_future_manifest_tool_without_restart(self) -> None:
        codex_binary = os.environ.get("CODEX_BINARY", "")
        if not codex_binary:
            self.skipTest("CODEX_BINARY 未配置")
        codex_binary_path = Path(codex_binary).resolve(strict=True)
        endpoint = ManifestEndpoint(signed_manifest(include_future_read=True))
        router = self.router(endpoint)
        socket_path = self.root / "official-runtime" / "tool-proxy.sock"
        proxy = ToolProxyServer(socket_path, router)
        proxy.start()
        codex_home = self.root / "official-codex-home"
        write_codex_config(
            codex_home,
            socket_path,
            mcp_python=sys.executable,
            mcp_pythonpath=str(Path(__file__).resolve().parents[1] / "codex_controller"),
        )
        client = AppServerClient(
            [str(codex_binary_path), "app-server", "--stdio"],
            codex_home=codex_home,
            workspace=self.root / "official-workspace",
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
            self.assertNotIn(FUTURE_READ, server.get("tools", {}))

            router.sync_hub_manifest()
            deadline = time.monotonic() + 12
            after_tools = server.get("tools", {})
            while time.monotonic() < deadline:
                current = client.request("mcpServerStatus/list", {"detail": "full"})
                current_server = next(
                    item
                    for item in current.get("data", [])
                    if item.get("name") == "home_assistant_tools"
                )
                after_tools = current_server.get("tools", {})
                if FUTURE_READ in after_tools:
                    break
                time.sleep(0.5)
            self.assertIn(FUTURE_READ, after_tools)
        finally:
            client.stop()
            proxy.stop()


if __name__ == "__main__":
    unittest.main()
