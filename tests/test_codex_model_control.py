from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

from codex_controller.model_control import (
    DEFAULT_CODEX_MODEL, MAX_CATALOG_BYTES, ModelControlError, ModelManager, backend_catalog_provider,
)
from codex_controller.tool_catalog import TOOL_BY_NAME


class ModelControlTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.path = Path(self.temporary.name) / "controller.sqlite3"
        self.app = Mock()
        self.catalog = Mock(return_value={"data": [{"id": "gpt-6.1-sol"}, {"id": "gpt-5.6-sol"}]})
        self.manager = ModelManager(self.path, self.app, catalog_provider=self.catalog)
        self.context = {"capability_profile": "owner", "job_id": "job-1", "message_id": "message-1", "conversation_key": "fixture"}

    def call(self, model="gpt-6.1-sol", **context):
        return self.manager.call("controller_model_set_default", {"model": model}, {**self.context, **context})

    def assert_code(self, code, function):
        with self.assertRaises(ModelControlError) as caught:
            function()
        self.assertEqual(caught.exception.code, code)

    def test_initial_default_and_explicit_config_precedence(self):
        self.assertEqual(self.manager.get_default(), DEFAULT_CODEX_MODEL)
        configured = ModelManager(self.path, self.app, configured_model="gpt-5.6-sol")
        self.assertEqual(configured.get_default(), "gpt-5.6-sol")
        self.call()
        self.assertEqual(configured.get_default(), "gpt-6.1-sol")

    def test_owner_only_and_required_message_context(self):
        for profile in ("owner_legacy", "member_read_only", "", None):
            self.assert_code("tool_not_allowed_for_profile", lambda: self.call(capability_profile=profile))
            self.assert_code("tool_not_allowed_for_profile", lambda: self.manager.call("controller_model_list", {}, {"capability_profile": profile}))
        self.assert_code("tool_context_unavailable", lambda: self.call(message_id=""))
        self.catalog.assert_not_called()

    def test_exact_catalog_validation_and_no_arbitrary_configuration(self):
        self.assert_code("model_not_available", lambda: self.call("gpt-6.1"))
        for value in ("https://example.invalid/v1", "gpt-6.1-sol\n", "gpt-6.1-sol/provider", "", None):
            self.assert_code("model_invalid", lambda: self.call(value))
        self.assert_code("model_invalid", lambda: self.manager.call("controller_model_set_default", {"model": "gpt-6.1-sol", "provider": "other"}, self.context))
        with sqlite3.connect(self.path) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM controller_model_changes").fetchone()[0], 0)

    def test_custom_backend_unavailable_never_falls_back_to_runtime(self):
        self.catalog.side_effect = RuntimeError("private backend address and secret")
        self.assert_code("model_catalog_unavailable", self.call)
        self.app.request.assert_not_called()
        self.assertEqual(self.manager.get_default(), DEFAULT_CODEX_MODEL)
        try:
            self.call()
        except ModelControlError as error:
            self.assertNotIn("private", str(error))
            self.assertIsNone(error.__cause__)

    def test_persistent_idempotent_audited_change_and_replay_after_newer_change(self):
        receipt = self.call("gpt-5.6-sol")
        self.assertEqual(receipt["effective"], "next_turn")
        self.assertTrue(receipt["changed_at"].endswith("+08:00"))
        restarted = ModelManager(self.path, self.app, catalog_provider=self.catalog)
        self.assertEqual(restarted.get_default(), "gpt-5.6-sol")
        self.call(message_id="message-2")
        self.catalog.side_effect = RuntimeError("offline")
        replay = restarted.call("controller_model_set_default", {"model": "gpt-5.6-sol"}, self.context)
        self.assertTrue(replay["replayed"])
        self.assertEqual(replay["selected_model"], "gpt-5.6-sol")
        self.assertEqual(replay["default_model"], "gpt-6.1-sol")
        self.assert_code("model_request_conflict", self.call)
        with sqlite3.connect(self.path) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM controller_model_changes").fetchone()[0], 2)

    def test_simultaneous_duplicate_requests_change_only_once(self):
        with ThreadPoolExecutor(max_workers=4) as executor:
            results = list(executor.map(lambda _: self.call("gpt-5.6-sol"), range(8)))
        self.assertEqual(sum(not row["replayed"] for row in results), 1)
        self.assertEqual(self.manager.get_default(), "gpt-5.6-sol")

    def test_official_runtime_paginates_and_uses_actual_model_slug(self):
        self.app.request.side_effect = [
            {"data": [{"id": "catalog-row", "model": "gpt-6.1-sol"}], "nextCursor": "page-2"},
            {"data": [{"id": "gpt-5.6-sol"}], "nextCursor": None},
        ]
        manager = ModelManager(self.path, self.app)
        result = manager.call("controller_model_list", {}, self.context)
        self.assertEqual(result["models"], ["gpt-5.6-sol", "gpt-6.1-sol"])
        self.assertEqual(result["catalog_source"], "codex_runtime_model_list")
        self.assertEqual(self.app.request.call_args_list[1].args[1]["cursor"], "page-2")

    def test_corrupt_or_incomplete_catalog_fails_closed(self):
        for document in ({}, {"data": []}, {"data": ["gpt-6.1-sol"]}, {"data": [{"id": "https://bad.invalid"}]}):
            self.catalog.return_value = document
            self.assert_code("model_catalog_unavailable", self.call)
        self.app.request.return_value = {"data": [{"model": "gpt-6.1-sol"}], "nextCursor": "same"}
        self.assert_code("model_catalog_unavailable", lambda: ModelManager(self.path, self.app).list_models())

    def test_backend_request_uses_fixed_endpoint_and_hides_transport_errors(self):
        response = Mock()
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        response.read.return_value = json.dumps({"data": [{"id": "gpt-6.1-sol"}]}).encode()
        with patch("codex_controller.model_control.build_opener") as opener:
            opener.return_value.open.return_value = response
            fetch = backend_catalog_provider("https://api.example.invalid/v1/", "example-only-key")
            self.assertEqual(fetch()["data"][0]["id"], "gpt-6.1-sol")
            request = opener.return_value.open.call_args.args[0]
            self.assertEqual(request.full_url, "https://api.example.invalid/v1/models")
            self.assertEqual(request.get_header("Authorization"), "Bearer example-only-key")
            opener.return_value.open.side_effect = RuntimeError("example-only-key https://api.example.invalid")
            with self.assertRaises(ModelControlError) as error:
                fetch()
            self.assertNotIn("example-only-key", str(error.exception))
            self.assertNotIn("example.invalid", str(error.exception))
            self.assertIsNone(error.exception.__cause__)

    def test_backend_response_size_and_credential_id_are_rejected(self):
        response = Mock()
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        fetch = backend_catalog_provider("https://api.example.invalid/v1", "example-only-key")
        with patch("codex_controller.model_control.build_opener") as opener:
            opener.return_value.open.return_value = response
            response.read.return_value = b" " * (MAX_CATALOG_BYTES + 1)
            self.assert_code("model_catalog_unavailable", fetch)
            response.read.assert_called_once_with(MAX_CATALOG_BYTES + 1)
            response.read.return_value = json.dumps({"data": [{"id": "example-only-key"}]}).encode()
            self.assert_code("model_catalog_unavailable", fetch)

    def test_backend_metadata_never_reaches_model_catalog_result(self):
        self.catalog.return_value = {"data": [{"id": "gpt-6.1-sol", "api_key": "example-only-key", "description": "Bearer example-only-key"}],
                                     "provider_url": "https://api.example.invalid/v1"}
        serialized = json.dumps(self.manager.list_models())
        self.assertNotIn("example-only-key", serialized)
        self.assertNotIn("example.invalid", serialized)

    def test_real_http_redirect_is_rejected_without_forwarding_credentials(self):
        seen = []

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                seen.append(self.path)
                self.send_response(302)
                self.send_header("Location", "/credential-target")
                self.end_headers()

            def log_message(self, *_):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            fetch = backend_catalog_provider(f"http://127.0.0.1:{server.server_port}/v1", "example-only-key")
            self.assert_code("model_catalog_unavailable", fetch)
            self.assertEqual(seen, ["/v1/models"])
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def test_model_tool_description_does_not_authorize_discussion(self):
        description = TOOL_BY_NAME["controller_model_set_default"].description
        for word in ("明确要求", "讨论", "假设", "举例", "下一轮", "当前活动轮"):
            self.assertIn(word, description)
        self.assertTrue(TOOL_BY_NAME["controller_model_set_default"].idempotent_write)

    def test_runtime_catalog_rpc_does_not_hold_lifecycle_lock_and_revocation_blocks_write(self):
        lifecycle_lock = threading.RLock()
        live = {"current": True}

        def runtime_request(*_):
            def terminal_notification():
                with lifecycle_lock:
                    live["current"] = False

            reader = threading.Thread(target=terminal_notification, daemon=True)
            reader.start()
            reader.join(timeout=1)
            self.assertFalse(reader.is_alive(), "model/list must not block the terminal notification reader")
            return {"data": [{"model": "gpt-5.6-sol"}], "nextCursor": None}

        self.app.request.side_effect = runtime_request
        manager = ModelManager(self.path, self.app)
        self.assert_code("tool_context_unavailable", lambda: manager.call(
            "controller_model_set_default", {"model": "gpt-5.6-sol"}, self.context,
            authorization_lock=lifecycle_lock, context_is_current=lambda: live["current"],
        ))
        self.assertEqual(manager.get_default(), DEFAULT_CODEX_MODEL)
        with sqlite3.connect(self.path) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM controller_model_changes").fetchone()[0], 0)

    def test_catalog_query_rechecks_current_context_after_rpc(self):
        live = {"current": True}

        def fetch():
            live["current"] = False
            return {"data": [{"id": "gpt-5.6-sol"}]}

        self.manager.catalog_provider = fetch
        self.assert_code("tool_context_unavailable", lambda: self.manager.call(
            "controller_model_list", {}, self.context, authorization_lock=threading.RLock(),
            context_is_current=lambda: live["current"],
        ))

    def router_fixture(self):
        from codex_controller.store import ControllerStore
        from codex_controller.tool_proxy import ToolRouter

        store = ControllerStore(self.path)
        job = store.create_job({
            "version": 1, "message_id": "router-message-1", "conversation_key": "sha256:" + "a" * 64,
            "received_at": "2026-10-17T16:00:00+08:00", "text": "切换默认模型", "attachments": [],
            "reply_capabilities": ["text"], "capability_profile": "owner",
        })
        router = ToolRouter(store=store)
        router.model_manager = self.manager
        return store, router, job

    def test_router_enforces_owner_and_admin_revocation_at_call_time(self):
        from codex_controller.tool_proxy import ToolProxyError

        store, router, job = self.router_fixture()
        self.assertIn("controller_model_set_default", router.available_tools("owner"))
        for profile in ("owner_legacy", "member_read_only"):
            self.assertNotIn("controller_model_set_default", router.available_tools(profile))
            router.begin_job("job-1", "message-1", profile, conversation_key="fixture")
            with self.assertRaises(ToolProxyError) as error:
                router.call("controller_model_set_default", {"model": "gpt-5.6-sol"})
            self.assertEqual(error.exception.code, "tool_not_allowed_for_profile")
            router.clear_job("job-1")
        router.begin_job(job["job_id"], "router-message-1", "owner", conversation_key="sha256:" + "a" * 64)
        receipt = router.call("controller_model_set_default", {"model": "gpt-5.6-sol"})
        self.assertEqual(receipt["default_model"], "gpt-5.6-sol")
        store.update_tool_policy("controller_model_set_default", enabled=False,
                                 revision=store.tool_policy_snapshot()["revision"], request_id="request-disable-1")
        with self.assertRaises(ToolProxyError) as error:
            router.call("controller_model_set_default", {"model": "gpt-6.1-sol"})
        self.assertEqual(error.exception.code, "tool_disabled")
        self.assertEqual(self.manager.get_default(), "gpt-5.6-sol")

    def test_router_terminal_reader_is_free_during_model_catalog_rpc(self):
        from codex_controller.tool_proxy import ToolProxyError

        _, router, job = self.router_fixture()
        router.begin_job(job["job_id"], "router-message-1", "owner", conversation_key="sha256:" + "a" * 64)

        def fetch():
            reader = threading.Thread(target=lambda: router.clear_job(job["job_id"]), daemon=True)
            reader.start()
            reader.join(timeout=1)
            self.assertFalse(reader.is_alive(), "catalog RPC must release the context lock for the reader")
            return {"data": [{"id": "gpt-5.6-sol"}]}

        self.manager.catalog_provider = fetch
        with self.assertRaises(ToolProxyError) as error:
            router.call("controller_model_set_default", {"model": "gpt-5.6-sol"})
        self.assertEqual(error.exception.code, "tool_context_unavailable")
        self.assertEqual(self.manager.get_default(), DEFAULT_CODEX_MODEL)

    def test_router_admin_revocation_during_catalog_rpc_prevents_model_change(self):
        from codex_controller.tool_proxy import ToolProxyError

        store, router, job = self.router_fixture()
        router.begin_job(job["job_id"], "router-message-1", "owner", conversation_key="sha256:" + "a" * 64)

        def fetch():
            store.update_tool_policy("controller_model_set_default", enabled=False,
                                     revision=store.tool_policy_snapshot()["revision"], request_id="disable-during-rpc")
            return {"data": [{"id": "gpt-5.6-sol"}]}

        self.manager.catalog_provider = fetch
        with self.assertRaises(ToolProxyError) as error:
            router.call("controller_model_set_default", {"model": "gpt-5.6-sol"})
        self.assertEqual(error.exception.code, "tool_disabled")
        self.assertEqual(self.manager.get_default(), DEFAULT_CODEX_MODEL)


if __name__ == "__main__":
    unittest.main()
