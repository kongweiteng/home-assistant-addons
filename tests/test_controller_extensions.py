from __future__ import annotations

import hashlib
import json
import os
import sys
import time
from pathlib import Path
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer

from codex_controller.app_server import AppServerClient
from codex_controller.extensions import ExtensionError, ExtensionRegistry, request_extension, base_url
from codex_controller.extension_schema import SchemaError, validate_schema, validate_value
from codex_controller.skills import SkillCatalog, SkillError, private_text
from codex_controller.store import ControllerStore
from codex_job_fixture import persist_tool_job
from codex_controller.tool_proxy import ToolProxyError, ToolRouter, ToolProxyServer
from codex_controller.main import codex_app_server_command, write_codex_config

EMPTY = {"type": "object", "properties": {}, "additionalProperties": False}
OUTPUT = {"type": "object", "properties": {"status": {"type": "string", "maxLength": 40}}, "required": ["status"], "additionalProperties": False}


class ExtensionsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.ext = self.root / "extensions"
        self.secrets = self.root / "extension-secrets"
        self.ext.mkdir()
        self.secrets.mkdir(mode=0o700)
        self.calls = []
        self.response = {"status": "ok"}
        self.store = ControllerStore(self.root / "db.sqlite3")
        self.registry = ExtensionRegistry(self.ext, self.secrets, self.request)
        self.skills = SkillCatalog(self.root / "skills")
        self.install()
        self.router = ToolRouter(store=self.store, extension_registry=self.registry, skill_catalog=self.skills)

    def request(self, url, token, payload):
        self.calls.append((url, payload))
        if isinstance(self.response, Exception):
            raise self.response
        return self.response

    def install(self, extension_id="demo", write=False, tools=None):
        folder = self.ext / extension_id
        folder.mkdir(exist_ok=True)
        manifest = {"version": 1, "tools": tools or [{"name": "lookup", "display_name": "查询示例", "description": "查询当前示例状态", "risk_type": "write" if write else "read_only", "inputSchema": EMPTY, "outputSchema": OUTPUT}]}
        raw = json.dumps(manifest, ensure_ascii=False)
        (folder / "manifest.json").write_text(raw)
        path = self.secrets / f"{extension_id}.json"
        path.write_text(json.dumps({"bearer": "fixture-extension-bearer-" + extension_id + "-" * 32}))
        path.chmod(0o600)
        config = {"id": extension_id, "enabled": True, "base_url": f"http://local-{extension_id}:8129", "manifest_sha256": "sha256:" + hashlib.sha256(raw.encode()).hexdigest(), "allow_write": write}
        registry_path = self.ext / "registry.json"
        registry = json.loads(registry_path.read_text()) if registry_path.exists() else {"version": 1, "extensions": []}
        registry["extensions"] = [item for item in registry["extensions"] if item["id"] != extension_id] + [config]
        registry_path.write_text(json.dumps(registry))
        return manifest

    def enable(self, name="ext_demo_lookup"):
        self.router.update_tool_policy(name, enabled=True, revision=self.store.tool_catalog_revision(), request_id="enable-" + name)

    def begin(self, profile="owner", message="message-1"):
        message = message + "-" + profile
        self.current_job_id = persist_tool_job(self.store, message, profile)
        self.router.begin_job(self.current_job_id, message, profile)
        self.router.bind_turn(self.current_job_id, "turn-1")

    def code(self, code, call):
        with self.assertRaises(ToolProxyError) as caught:
            call()
        self.assertEqual(caught.exception.code, code)

    def test_new_tools_are_visible_but_disabled_and_owner_only(self):
        self.assertIn("ext_demo_lookup", self.router.configured_tools())
        self.assertNotIn("ext_demo_lookup", self.router.available_tools("owner"))
        self.enable()
        self.assertIn("ext_demo_lookup", self.router.available_tools("owner"))
        self.assertNotIn("ext_demo_lookup", self.router.available_tools("owner_legacy"))
        self.assertNotIn("ext_demo_lookup", self.router.available_tools("member_read_only"))
        self.code("tool_not_allowed_for_profile", lambda: self.router.call("ext_demo_lookup", {}))
        self.begin("member_read_only")
        self.code("tool_not_allowed_for_profile", lambda: self.router.call("ext_demo_lookup", {}))
        self.assertEqual(self.calls, [])

    def test_two_providers_load_without_changing_router_and_policies_persist(self):
        self.enable()
        self.install("second")
        self.router.sync_extensions()
        self.enable("ext_second_lookup")
        self.begin()
        self.assertEqual(self.router.call("ext_demo_lookup", {}), {"status": "ok"})
        self.assertEqual(self.router.call("ext_second_lookup", {}), {"status": "ok"})
        self.assertEqual([url for url, _ in self.calls], ["http://local-demo:8129", "http://local-second:8129"])
        self.assertNotIn("job-1", json.dumps(self.calls))
        self.assertIn("ext_second_lookup", ControllerStore(self.root / "db.sqlite3").tool_policy_snapshot()["enabled"])

    def test_bad_schema_arguments_never_reach_provider(self):
        self.enable(); self.begin()
        self.code("extension_arguments_invalid", lambda: self.router.call("ext_demo_lookup", {"shell": "bad"}))
        self.assertEqual(self.calls, [])

    def test_removed_and_disabled_config_revokes_cached_tool(self):
        self.enable(); self.begin()
        path = self.ext / "registry.json"
        config = json.loads(path.read_text())
        config["extensions"][0]["enabled"] = False
        path.write_text(json.dumps(config))
        self.code("unknown_tool", lambda: self.router.call("ext_demo_lookup", {}))
        self.assertEqual(self.calls, [])
        self.assertIn("ext_demo_lookup", self.store.historical_tool_names())
        self.assertNotIn("ext_demo_lookup", self.store.active_tool_names())

    def test_schema_or_endpoint_change_requires_reenable(self):
        self.enable()
        config_path = self.ext / "registry.json"
        config = json.loads(config_path.read_text())
        config["extensions"][0]["base_url"] = "http://local-other:8129"
        config_path.write_text(json.dumps(config))
        self.router.sync_extensions()
        self.assertNotIn("ext_demo_lookup", self.router.available_tools("owner"))

    def test_pin_mismatch_removes_tools_but_leaves_builtin_tools(self):
        self.enable()
        (self.ext / "demo/manifest.json").write_text('{"version":1,"tools":[]}')
        self.router.sync_extensions()
        self.assertNotIn("ext_demo_lookup", self.router.configured_tools())
        self.assertEqual(self.registry.errors[0]["error_code"], "extension_digest_mismatch")
        self.assertIn("ledger_query", self.router.tool_definitions_by_name())

    def test_invalid_registry_does_not_keep_last_good_privileges(self):
        self.enable()
        (self.ext / "registry.json").write_text("invalid")
        self.router.sync_extensions()
        self.assertEqual(self.registry.tools, {})

    def test_unsafe_result_and_secret_never_return(self):
        self.enable(); self.begin()
        self.response = {"status": "fixture-extension-bearer-demo" + "-" * 32}
        self.code("extension_response_invalid", lambda: self.router.call("ext_demo_lookup", {}))
        self.response = {"status": "Bearer hidden-secret"}
        self.code("extension_response_unsafe", lambda: self.router.call("ext_demo_lookup", {}))
        self.response = {"token": "hidden-secret"}
        self.code("extension_response_invalid", lambda: self.router.call("ext_demo_lookup", {}))

    def test_confirmed_write_reuses_result_and_unknown_does_not_replay_after_restart(self):
        self.install(write=True); self.router.sync_extensions(); self.enable(); self.begin()
        self.router.call("ext_demo_lookup", {})
        self.router.call("ext_demo_lookup", {})
        self.assertEqual(len(self.calls), 1)
        self.router.clear_job(self.current_job_id); self.begin(message="message-2")
        self.response = ExtensionError("extension_upstream_unavailable")
        self.code("extension_write_unknown", lambda: self.router.call("ext_demo_lookup", {}))
        self.assertEqual(len(self.calls), 2)
        restarted = ToolRouter(store=ControllerStore(self.root / "db.sqlite3"), extension_registry=self.registry)
        job_id = persist_tool_job(self.store, "message-2-owner")
        restarted.begin_job(job_id, "message-2-owner", "owner"); restarted.bind_turn(job_id, "turn-2")
        self.response = {"status": "ok"}
        self.code("extension_write_unknown", lambda: restarted.call("ext_demo_lookup", {}))
        self.assertEqual(len(self.calls), 2)

    def test_catalog_change_cannot_replay_unknown_write(self):
        self.install(write=True); self.router.sync_extensions(); self.enable(); self.begin()
        self.response = ExtensionError("extension_upstream_unavailable")
        self.code("extension_write_unknown", lambda: self.router.call("ext_demo_lookup", {}))
        path = self.ext / "registry.json"
        config = json.loads(path.read_text()); config["extensions"][0]["base_url"] = "http://local-new:8129"
        path.write_text(json.dumps(config)); self.router.sync_extensions()
        self.router.update_tool_policy("ext_demo_lookup", enabled=True,
            revision=self.store.tool_catalog_revision(), request_id="reenable-demo")
        self.response = {"status": "ok"}
        self.code("extension_write_unknown", lambda: self.router.call("ext_demo_lookup", {}))
        self.assertEqual(len(self.calls), 1)


    def test_provider_failure_is_audited_without_response_body(self):
        self.enable(); self.begin()
        self.response = ExtensionError("extension_upstream_rejected")
        self.code("extension_upstream_rejected", lambda: self.router.call("ext_demo_lookup", {}))
        tool = next(t for t in self.router.tool_status()["tools"] if t["name"] == "ext_demo_lookup")
        self.assertEqual(tool["last_invocation"]["error_code"], "extension_upstream_rejected")

    def test_credentials_must_have_private_permissions(self):
        (self.secrets / "demo.json").chmod(0o644)
        self.router.sync_extensions()
        self.assertEqual(self.registry.tools, {})

    def test_duplicate_or_unbounded_tool_schema_fails_closed(self):
        tool = self.install()["tools"][0]
        self.install(tools=[tool, tool]); self.router.sync_extensions()
        self.assertEqual(self.registry.tools, {})
        tool["inputSchema"] = {"type": "object", "additionalProperties": True}
        self.install(tools=[tool]); self.router.sync_extensions()
        self.assertEqual(self.registry.tools, {})

    def test_skill_discovery_prompt_and_references_without_native_shell(self):
        skill = self.root / "skills/trip-review"
        (skill / "references").mkdir(parents=True)
        (skill / "SKILL.md").write_text('---\nname: trip-review\ndescription: >-\n  分析行程报告，\n  检查耗电。\n---\n读取参考说明并分析数据。')
        (skill / "references/check.md").write_text("核对数据来源。")
        self.router.sync_extensions(); self.begin()
        self.assertIn("controller_skill_read", self.router.available_tools("owner"))
        text = AppServerClient.build_developer_instructions(self.router.available_tools("owner"), "owner", self.router.tool_definitions_by_name(), self.router.skill_context("owner"))
        self.assertIn("trip-review", text)
        self.assertEqual(self.router.call("controller_skill_read", {"name": "trip-review", "resource": "references/check.md"})["content"], "核对数据来源。")
        self.code("skill_path_invalid", lambda: self.router.call("controller_skill_read", {"name": "trip-review", "resource": "references/../../demo.json"}))
        self.code("skill_path_invalid", lambda: self.router.call("controller_skill_read", {"name": "trip-review", "resource": "scripts/run.py"}))
        self.router.clear_job(self.current_job_id); self.begin("member_read_only")
        self.code("tool_not_allowed_for_profile", lambda: self.router.call("controller_skill_read", {"name": "trip-review"}))

    def test_symlink_skill_and_reference_rejected(self):
        folder = self.root / "skills/demo"
        (folder / "references").mkdir(parents=True)
        target = self.root / "private.txt"; target.write_text("private")
        (folder / "SKILL.md").write_text("---\nname: demo\ndescription: demo\n---\nDemo")
        (folder / "references/private.txt").symlink_to(target)
        self.skills.refresh()
        with self.assertRaises(SkillError):
            self.skills.call("controller_skill_read", {"name": "demo", "resource": "references/private.txt"})
        (folder / "SKILL.md").unlink(); (folder / "SKILL.md").symlink_to(target)
        self.skills.refresh(); self.assertEqual(self.skills.skills, {})

    def test_duplicate_skills_do_not_choose_arbitrary_winner(self):
        for folder in ("one", "two"):
            path = self.root / "skills" / folder; path.mkdir(parents=True)
            (path / "SKILL.md").write_text("---\nname: same\ndescription: same\n---\nworkflow")
        self.skills.refresh(); self.assertEqual(self.skills.skills, {})

    def test_skill_removal_takes_effect_in_old_thread(self):
        folder = self.root / "skills/demo"; folder.mkdir(parents=True)
        (folder / "SKILL.md").write_text("---\nname: demo\ndescription: demo\n---\nDemo")
        self.router.sync_extensions(); self.begin()
        (folder / "SKILL.md").unlink()
        self.code("unknown_tool", lambda: self.router.call("controller_skill_read", {"name": "demo"}))

    def test_calls_are_frozen_against_job_switch(self):
        self.enable(); self.begin()
        entered = threading.Event(); release = threading.Event(); finished = threading.Event()
        def request(*args):
            entered.set(); release.wait(2); return {"status": "ok"}
        self.registry.request = request
        caller = threading.Thread(target=lambda: self.router.call("ext_demo_lookup", {}))
        def clear():
            self.router.clear_job(self.current_job_id); finished.set()
        caller.start(); self.assertTrue(entered.wait(1))
        clearing = threading.Thread(target=clear); clearing.start()
        self.assertFalse(finished.wait(.05))
        release.set(); caller.join(2); clearing.join(2)
        self.assertTrue(finished.is_set())

    def test_official_app_server_observes_extension_enable_and_disable_without_restart(self):
        binary = os.environ.get("CODEX_REAL_BINARY")
        if not binary:
            self.skipTest("CODEX_REAL_BINARY not configured")
        proxy = ToolProxyServer(self.root / "proxy.sock", self.router)
        proxy.start()
        codex_home = self.root / "codex-home"
        write_codex_config(codex_home, self.root / "proxy.sock", mcp_python=sys.executable,
                           mcp_pythonpath=str(Path(__file__).resolve().parents[1] / "codex_controller"))
        client = AppServerClient(codex_app_server_command(binary), codex_home=codex_home,
                                 workspace=self.root / "workspace", request_timeout=20)
        def published():
            servers = client.request("mcpServerStatus/list", {"detail": "full"}).get("data", [])
            return next(server["tools"] for server in servers if server.get("name") == "home_assistant_tools")
        def await_presence(expected):
            deadline = time.monotonic() + 12
            while time.monotonic() < deadline:
                if ("ext_demo_lookup" in published()) == expected:
                    return
                time.sleep(.1)
            self.fail("official MCP catalog did not refresh")
        try:
            client.start()
            self.assertTrue(client.status()["initialized"])
            await_presence(False)
            self.enable()
            await_presence(True)
            self.router.update_tool_policy("ext_demo_lookup", enabled=False,
                revision=self.store.tool_catalog_revision(), request_id="disable-demo")
            await_presence(False)
        finally:
            client.stop(); proxy.stop()

    def test_pinned_binary_requests_no_native_execution_tools(self):
        binary = os.environ.get("CODEX_REAL_BINARY")
        if not binary:
            self.skipTest("CODEX_REAL_BINARY not configured")
        captured = []
        arrived = threading.Event()
        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                data = self.rfile.read(int(self.headers.get("Content-Length", "0")))
                document = json.loads(data)
                captured.append(document)
                arrived.set()
                response = {"id": "resp-fixture", "object": "response", "status": "completed", "output": [],
                            "usage": {"input_tokens": 1, "output_tokens": 0, "total_tokens": 1}}
                body = ("data: " + json.dumps({"type": "response.created", "response": {**response, "status": "in_progress"}}) + "\n\n" +
                        "data: " + json.dumps({"type": "response.completed", "response": response}) + "\n\n").encode()
                self.send_response(200); self.send_header("Content-Type", "text/event-stream")
                self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)
            def log_message(self, *args): pass
        http = HTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=http.serve_forever, daemon=True); thread.start()
        codex_home = self.root / "native-profile"
        write_codex_config(codex_home, self.root / "unused.sock",
                           openai_base_url=f"http://127.0.0.1:{http.server_port}")
        client = AppServerClient(codex_app_server_command(binary), codex_home=codex_home,
                                 workspace=self.root / "native-workspace", request_timeout=20)
        try:
            client.start()
            client.request("account/login/start", {"type": "apiKey", "apiKey": "fixture-key-for-loopback-only"})
            thread_id = client.start_thread()
            client.start_turn(thread_id, "Explain a supplied report", "native-fixture")
            self.assertTrue(arrived.wait(10), "no local mock Responses request")
            tools = captured[0].get("tools", [])
            names = {tool.get("name", tool.get("function", {}).get("name")) for tool in tools}
            self.assertFalse(names & {"exec_command", "write_stdin", "shell", "shell_command", "apply_patch", "code_mode"})
        finally:
            client.stop(); http.shutdown(); http.server_close(); thread.join()


    def test_installer_directories_are_discovered_and_parent_symlinks_rejected(self):
        install_root = self.root / "codex-home/skills/demo"
        install_root.mkdir(parents=True)
        (install_root / "SKILL.md").write_text("---\nname: demo\ndescription: installed\n---\nworkflow")
        catalog = SkillCatalog(self.root / "skills", additional_roots=(install_root.parent,), boundary=self.root)
        catalog.refresh(); self.assertIn("demo", catalog.skills)
        outside = self.root / "outside/skills/secret"
        outside.mkdir(parents=True)
        (outside / "SKILL.md").write_text("---\nname: secret\ndescription: secret\n---\nprivate")
        (self.root / "codex-home/.agents").symlink_to(self.root / "outside", target_is_directory=True)
        catalog = SkillCatalog(self.root / "codex-home/.agents/skills", boundary=self.root)
        catalog.refresh(); self.assertEqual(catalog.skills, {})

    def test_valid_schema_cannot_expose_actual_provider_bearer(self):
        tool = self.install()["tools"][0]
        tool["outputSchema"] = {"type": "object", "properties": {"status": {"type": "string", "maxLength": 4096}}, "required": ["status"], "additionalProperties": False}
        self.install(tools=[tool]); self.router.sync_extensions(); self.enable(); self.begin()
        self.response = {"status": "fixture-extension-bearer-demo" + "-" * 32}
        self.code("extension_response_unsafe", lambda: self.router.call("ext_demo_lookup", {}))


    def test_no_turn_cannot_call_external_service(self):
        self.enable(); self.router.begin_job("job-1", "message-1", "owner")
        self.code("tool_context_unavailable", lambda: self.router.call("ext_demo_lookup", {}))
        self.assertEqual(self.calls, [])


class SchemaAndTransportTests(unittest.TestCase):
    def test_schema_nested_validation_and_type_boundaries(self):
        schema = {"type": "object", "properties": {"items": {"type": "array", "maxItems": 2, "items": {"type": "integer", "minimum": 1}}}, "required": ["items"], "additionalProperties": False}
        validate_schema(schema); validate_value({"items": [1, 2]}, schema)
        for value in ({"items": [True]}, {"items": [0]}, {"items": [1,2,3]}, {}, {"items": [], "x": 1}):
            with self.assertRaises(SchemaError): validate_value(value, schema)
        with self.assertRaises(SchemaError): validate_schema({"type": "string", "pattern": "a+"})

    def test_only_internal_named_services_and_explicit_ports(self):
        self.assertEqual(base_url("http://local-demo:8129"), "http://local-demo:8129")
        for value in ("https://example.com", "http://1.2.3.4:80", "http://supervisor:80", "http://local-demo", "http://local-demo:80/path", "http://user:pass@local-demo:80"):
            with self.assertRaises(ExtensionError): base_url(value)

    def test_transport_does_not_follow_redirects_or_expose_error_body(self):
        class Handler(BaseHTTPRequestHandler):
            calls = 0
            def do_POST(self):
                Handler.calls += 1
                self.send_response(302); self.send_header("Location", "/leak"); self.end_headers(); self.wfile.write(b"private-error")
            def log_message(self, *args): pass
        server = HTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
        try:
            with self.assertRaises(ExtensionError) as caught:
                request_extension(f"http://127.0.0.1:{server.server_port}", "fixture-token", {"version": 1})
            self.assertEqual(str(caught.exception), "extension_upstream_rejected")
            self.assertEqual(Handler.calls, 1)
        finally:
            server.shutdown(); server.server_close(); thread.join()


if __name__ == "__main__": unittest.main()
