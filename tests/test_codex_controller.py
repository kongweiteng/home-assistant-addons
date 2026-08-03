from __future__ import annotations

import base64
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest

from codex_controller.api import DASHBOARD_HTML, DASHBOARD_JS
from codex_controller.app_server import AppServerClient, AppServerError
from codex_controller.main import read_api_key_from_fd
from codex_controller.service import ControllerService
from codex_controller.store import ControllerStore, StoreError
from codex_controller.tool_proxy import ToolProxyError, ToolRouter, validate_base_url


def fixture_job(message_id: str = "fixture-message-1", text: str = "查询装修支出") -> dict:
    return {
        "version": 1,
        "message_id": message_id,
        "conversation_key": "sha256:" + hashlib.sha256(b"weixin:fixture-owner").hexdigest(),
        "received_at": "2026-08-03T12:00:00+08:00",
        "text": text,
        "attachments": [],
        "reply_capabilities": ["text", "image"],
    }


FAKE_APP_SERVER = r'''
import json, sys
thread_id = "thread-fixture"
account_type = "chatgpt"
for line in sys.stdin:
    message = json.loads(line)
    if "id" not in message:
        continue
    method = message.get("method")
    request_id = message["id"]
    if method == "initialize":
        result = {"userAgent":"codex-test","codexHome":"/data/codex-home","platformFamily":"unix","platformOs":"linux"}
    elif method == "account/read":
        account = None if account_type is None else {"type":account_type,"email":None}
        if account_type == "chatgpt":
            account["planType"] = "plus"
        result = {"account":account,"requiresOpenaiAuth":True}
    elif method == "account/login/start":
        params = message.get("params")
        if params == {"type":"chatgptDeviceCode"}:
            result = {"type":"chatgptDeviceCode","loginId":"login-fixture","verificationUrl":"https://auth.openai.com/codex/device","userCode":"ABCD-1234"}
        elif params == {"type":"apiKey","apiKey":"fixture-api-key-value"}:
            account_type = "apiKey"
            result = {"type":"apiKey"}
        else:
            print(json.dumps({"id":request_id,"error":{"code":-32602,"message":"wrong auth"}}), flush=True)
            continue
    elif method == "account/login/cancel":
        result = {}
    elif method == "account/logout":
        account_type = None
        result = {}
    elif method in ("thread/start", "thread/resume"):
        result = {"thread":{"id":thread_id,"turns":[]}}
    elif method == "turn/start":
        result = {"turn":{"id":"turn-fixture","status":"inProgress","items":[]}}
    else:
        print(json.dumps({"id":request_id,"error":{"code":-32601,"message":"unknown"}}), flush=True)
        continue
    print(json.dumps({"id":request_id,"result":result}), flush=True)
    if method == "turn/start":
        print(json.dumps({"method":"item/completed","params":{"threadId":thread_id,"turnId":"turn-fixture","item":{"type":"agentMessage","id":"item-fixture","text":"查询完成。"}}}), flush=True)
        print(json.dumps({"method":"turn/completed","params":{"threadId":thread_id,"turn":{"id":"turn-fixture","status":"completed","items":[]}}}), flush=True)
'''


class ControllerStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.store = ControllerStore(self.root / "controller.sqlite3", max_queue=3)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_message_id_is_idempotent_and_conflicting_reuse_fails(self) -> None:
        first = self.store.create_job(fixture_job())
        second = self.store.create_job(fixture_job())
        self.assertEqual(first["job_id"], second["job_id"])
        with self.assertRaises(StoreError) as context:
            self.store.create_job(fixture_job(text="不同内容"))
        self.assertEqual(context.exception.code, "idempotency_conflict")

    def test_only_one_job_can_be_running(self) -> None:
        self.store.create_job(fixture_job("fixture-message-1"))
        self.store.create_job(fixture_job("fixture-message-2"))
        first = self.store.claim_next()
        self.assertIsNotNone(first)
        self.assertEqual(first["state"], "running")
        self.assertIsNone(self.store.claim_next())
        self.store.fail_claimed(first["job_id"], "fixture", uncertain=False)
        second = self.store.claim_next()
        self.assertIsNotNone(second)
        self.assertNotEqual(first["job_id"], second["job_id"])

    def test_restart_marks_running_job_recovery_required(self) -> None:
        self.store.create_job(fixture_job())
        running = self.store.claim_next()
        self.assertEqual(self.store.recover_running(), 1)
        recovered = self.store.get_job(running["job_id"])
        self.assertEqual(recovered["state"], "recovery_required")
        self.assertEqual(recovered["error_code"], "turn_state_unknown")


class AppServerClientTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.fake = self.root / "fake_app_server.py"
        self.fake.write_text(FAKE_APP_SERVER)
        self.notifications: list[dict] = []
        self.event = threading.Event()

        def notification(message: dict) -> None:
            self.notifications.append(message)
            if message.get("method") == "turn/completed":
                self.event.set()

        self.client = AppServerClient(
            [sys.executable, str(self.fake)],
            codex_home=self.root / "codex-home",
            workspace=self.root / "workspace",
            notification_handler=notification,
            request_timeout=3,
        )

    def tearDown(self) -> None:
        self.client.stop()
        self.temporary.cleanup()

    def test_device_login_is_exact_and_chatgpt_account_is_required(self) -> None:
        self.client.start()
        self.assertTrue(self.client.account_ready)
        self.assertEqual(self.client.auth_mode, "chatgpt")
        login = self.client.start_device_login()
        self.assertEqual(login["type"], "chatgptDeviceCode")
        self.assertEqual(login["userCode"], "ABCD-1234")

    def test_thread_turn_and_notifications_use_stable_protocol(self) -> None:
        self.client.start()
        thread_id = self.client.start_thread()
        turn_id = self.client.start_turn(thread_id, "查询装修支出", "fixture-message-1")
        self.assertEqual(thread_id, "thread-fixture")
        self.assertEqual(turn_id, "turn-fixture")
        self.assertTrue(self.event.wait(2))
        self.assertIn("item/completed", [message.get("method") for message in self.notifications])

    def test_api_key_login_is_exact_and_normalized(self) -> None:
        self.client.start()
        result = self.client.start_api_key_login("fixture-api-key-value")
        self.assertEqual(result, {"type": "apiKey", "ready": True})
        self.assertEqual(self.client.auth_mode, "apiKey")
        self.assertTrue(self.client.account_ready)

    def test_api_key_rejection_does_not_echo_secret(self) -> None:
        self.client.start()
        secret = "fixture-rejected-api-key"
        with self.assertRaises(AppServerError) as context:
            self.client.start_api_key_login(secret)
        self.assertEqual(context.exception.code, "app_server_request_failed")
        self.assertNotIn(secret, str(context.exception))

    def test_child_environment_excludes_internal_bearers(self) -> None:
        environment = self.client.build_child_env(
            {
                "PATH": "/usr/bin",
                "CONTROLLER_LEDGER_API_TOKEN": "secret-ledger",
                "CONTROLLER_OPERATIONS_API_TOKEN": "secret-broker",
                "CONTROLLER_OPENAI_API_KEY": "secret-openai",
                "CONTROLLER_MCP_SOCKET": "/secret/path",
            }
        )
        self.assertEqual(environment["PATH"], "/usr/bin")
        self.assertNotIn("CONTROLLER_LEDGER_API_TOKEN", environment)
        self.assertNotIn("CONTROLLER_OPERATIONS_API_TOKEN", environment)
        self.assertNotIn("CONTROLLER_OPENAI_API_KEY", environment)
        self.assertNotIn("CONTROLLER_MCP_SOCKET", environment)


class ControllerAuthenticationTests(unittest.TestCase):
    class StubApp:
        def __init__(self, *, auth_mode: str | None = None) -> None:
            self.auth_mode = auth_mode
            self.account_ready = auth_mode is not None
            self.notification_handler = None
            self.api_key_calls: list[str] = []

        def start(self) -> None:
            return None

        def stop(self) -> None:
            return None

        def status(self) -> dict:
            return {
                "running": True,
                "initialized": True,
                "protocol_error": None,
                "account": {"auth_mode": self.auth_mode, "plan_type": None, "ready": self.account_ready},
            }

        def start_api_key_login(self, api_key: str) -> dict:
            self.api_key_calls.append(api_key)
            self.auth_mode = "apiKey"
            self.account_ready = True
            return {"type": "apiKey", "ready": True}

    def make_service(self, app: object, **kwargs: object) -> tuple[tempfile.TemporaryDirectory, ControllerService]:
        temporary = tempfile.TemporaryDirectory()
        store = ControllerStore(Path(temporary.name) / "controller.sqlite3")
        service = ControllerService(store, app, intake_enabled=True, **kwargs)  # type: ignore[arg-type]
        return temporary, service

    def test_missing_api_key_fails_closed_without_secret_state(self) -> None:
        app = self.StubApp()
        temporary, service = self.make_service(app, auth_mode="api_key", api_key="")
        try:
            service.start()
            status = service.status()
            self.assertEqual(status["auth_error"], "api_key_missing")
            self.assertFalse(status["api_key_configured"])
            self.assertFalse(status["intake_enabled"])
        finally:
            service.stop()
            temporary.cleanup()

    def test_unlogged_api_key_mode_applies_key_without_exposing_it(self) -> None:
        secret = "fixture-api-key-value"
        app = self.StubApp()
        temporary, service = self.make_service(app, auth_mode="api_key", api_key=secret)
        try:
            service.start()
            status = service.status()
            self.assertEqual(app.api_key_calls, [secret])
            self.assertTrue(status["ready"])
            self.assertTrue(status["intake_enabled"])
            self.assertNotIn(secret, json.dumps(status, ensure_ascii=False))
            self.assertNotIn(secret.encode(), (Path(temporary.name) / "controller.sqlite3").read_bytes())
        finally:
            service.stop()
            temporary.cleanup()

    def test_existing_wrong_account_fails_closed_until_explicit_retry(self) -> None:
        secret = "fixture-api-key-value"
        app = self.StubApp(auth_mode="chatgpt")
        temporary, service = self.make_service(app, auth_mode="api_key", api_key=secret)
        try:
            service.start()
            self.assertEqual(service.status()["auth_error"], "auth_mode_mismatch")
            self.assertFalse(service.intake_enabled)
            self.assertEqual(app.api_key_calls, [])
            service.begin_api_key_login()
            self.assertEqual(app.api_key_calls, [secret])
            self.assertTrue(service.intake_enabled)
        finally:
            service.stop()
            temporary.cleanup()

    def test_modes_cannot_call_each_others_login_entry(self) -> None:
        app = self.StubApp()
        temporary, service = self.make_service(app, auth_mode="api_key", api_key="fixture-api-key-value")
        try:
            with self.assertRaisesRegex(AppServerError, "未选择设备码"):
                service.begin_device_login()
        finally:
            service.stop()
            temporary.cleanup()

    def test_api_key_is_read_from_fd_and_not_named_environment_value(self) -> None:
        read_fd, write_fd = os.pipe()
        secret = b"fixture-api-key-value"
        os.write(write_fd, secret)
        os.close(write_fd)
        original = os.environ.get("CONTROLLER_OPENAI_API_KEY_FD")
        os.environ["CONTROLLER_OPENAI_API_KEY_FD"] = str(read_fd)
        try:
            self.assertEqual(read_api_key_from_fd(), secret.decode())
            self.assertNotIn("CONTROLLER_OPENAI_API_KEY", os.environ)
        finally:
            if original is None:
                os.environ.pop("CONTROLLER_OPENAI_API_KEY_FD", None)
            else:
                os.environ["CONTROLLER_OPENAI_API_KEY_FD"] = original

    def test_ingress_supports_both_modes_without_key_input(self) -> None:
        self.assertIn("api/auth/device/start", DASHBOARD_JS)
        self.assertIn("api/auth/api-key/retry", DASHBOARD_JS)
        self.assertIn("页面不会显示 Key 内容", DASHBOARD_JS)
        self.assertNotIn("<input", DASHBOARD_HTML.lower())
        self.assertNotIn("openai_api_key", DASHBOARD_HTML + DASHBOARD_JS)

    def test_addon_config_and_run_script_keep_key_out_of_environment(self) -> None:
        root = Path(__file__).resolve().parents[1] / "codex_controller"
        config = (root / "config.yaml").read_text(encoding="utf-8")
        run_script = (root / "run.sh").read_text(encoding="utf-8")
        self.assertIn('auth_mode: "list(chatgpt_device_code|api_key)"', config)
        self.assertIn("openai_api_key: password", config)
        self.assertIn("CONTROLLER_OPENAI_API_KEY_FD", run_script)
        self.assertNotIn("export CONTROLLER_OPENAI_API_KEY=", run_script)


class ToolRouterTests(unittest.TestCase):
    def test_internal_url_rejects_ip_paths_and_credentials(self) -> None:
        self.assertEqual(validate_base_url("http://renovation-hub:8101"), "http://renovation-hub:8101")
        for value in ("https://service", "http://127.0.0.1:8101", "http://user@service", "http://service/private"):
            with self.assertRaises(ToolProxyError):
                validate_base_url(value)

    def test_router_only_calls_fixed_tools_and_keeps_token_in_transport(self) -> None:
        observed: list[tuple] = []

        def fake_request(method: str, url: str, token: str, payload: dict | None) -> dict:
            observed.append((method, url, token, payload))
            return {"version": 1, "result": {"ok": True}}

        token = "x" * 32
        router = ToolRouter(ledger_base_url="http://renovation-hub:8101", ledger_token=token, request_json=fake_request)
        self.assertIn("renovation_dashboard", router.available_tools())
        result = router.call("ledger_summary", {})
        self.assertTrue(result["result"]["ok"])
        self.assertEqual(observed[0][2], token)
        with self.assertRaises(ToolProxyError) as context:
            router.call("execute_shell", {})
        self.assertEqual(context.exception.code, "unknown_tool")

    def test_attachment_ref_is_consumed_from_gateway_and_forwarded_only_as_content(self) -> None:
        content = "合成附件".encode("utf-8")
        digest = hashlib.sha256(content).hexdigest()
        gateway_token = "g" * 32

        class GatewayHandler(BaseHTTPRequestHandler):
            consumed = False

            def log_message(self, _format: str, *_args: object) -> None:
                return None

            def do_GET(self) -> None:  # noqa: N802
                if self.headers.get("Authorization") != f"Bearer {gateway_token}" or self.consumed:
                    self.send_response(404)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                type(self).consumed = True
                self.send_response(200)
                self.send_header("Content-Type", "text/plain")
                self.send_header("Content-Length", str(len(content)))
                self.send_header("X-Attachment-Filename", base64.urlsafe_b64encode("收据.txt".encode("utf-8")).decode("ascii"))
                self.send_header("X-Attachment-Sha256", f"sha256:{digest}")
                self.end_headers()
                self.wfile.write(content)

        observed: list[tuple] = []

        def fake_request(method: str, url: str, token: str, payload: dict | None) -> dict:
            observed.append((method, url, token, payload))
            return {"version": 1, "result": {"ok": True}}

        server = ThreadingHTTPServer(("127.0.0.1", 0), GatewayHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            router = ToolRouter(
                ledger_base_url="http://renovation-hub:8101",
                ledger_token="l" * 32,
                gateway_base_url=f"http://localhost:{server.server_port}",
                gateway_token=gateway_token,
                request_json=fake_request,
            )
            self.assertIn("ledger_attach", router.available_tools())
            result = router.call(
                "ledger_attach",
                {
                    "idempotency_key": "fixture-attach-" + "0" * 24,
                    "transaction_id": "fixture-payment",
                    "attachment_ref": "a" * 43,
                },
            )
            self.assertTrue(result["result"]["ok"])
            forwarded = observed[0][3]["arguments"]
            self.assertNotIn("attachment_ref", forwarded)
            self.assertEqual(forwarded["original_filename"], "收据.txt")
            self.assertEqual(forwarded["mime_type"], "text/plain")
            self.assertEqual(base64.b64decode(forwarded["content_base64"]), content)
            self.assertNotIn(gateway_token, json.dumps(observed, ensure_ascii=False))
            with self.assertRaises(ToolProxyError) as context:
                router.call(
                    "ledger_attach",
                    {
                        "idempotency_key": "fixture-attach-" + "1" * 24,
                        "transaction_id": "fixture-payment",
                        "attachment_ref": "a" * 43,
                    },
                )
            self.assertEqual(context.exception.code, "attachment_unavailable")
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def test_attachment_tool_is_hidden_without_gateway_credentials(self) -> None:
        router = ToolRouter(ledger_base_url="http://renovation-hub:8101", ledger_token="l" * 32)
        self.assertNotIn("ledger_attach", router.available_tools())
        with self.assertRaises(ToolProxyError) as context:
            router.call(
                "ledger_attach",
                {"idempotency_key": "fixture", "transaction_id": "fixture", "attachment_ref": "a" * 43},
            )
        self.assertEqual(context.exception.code, "gateway_unavailable")

    def test_media_tool_streams_reference_without_base64_arguments(self) -> None:
        observed: list[tuple] = []

        def fake_stream(*args: object) -> dict:
            observed.append(args)
            return {"version": 1, "result": {"media": {"id": "fixture-media"}}}

        router = ToolRouter(
            ledger_base_url="http://renovation-hub:8101",
            ledger_token="l" * 32,
            gateway_base_url="http://weixin-gateway:8103",
            gateway_token="g" * 32,
            stream_media=fake_stream,
        )
        result = router.call(
            "renovation_media_ingest",
            {
                "idempotency_key": "fixture-media-" + "0" * 24,
                "attachment_ref": "a" * 43,
                "project_id": "fixture-project",
            },
        )
        self.assertEqual(result["result"]["media"]["id"], "fixture-media")
        forwarded = observed[0][5]
        self.assertNotIn("attachment_ref", forwarded)
        self.assertNotIn("content_base64", forwarded)


class ControllerServiceRaceTests(unittest.TestCase):
    def test_fast_notifications_are_replayed_after_turn_binding(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            store = ControllerStore(root / "controller.sqlite3")
            job = store.create_job(fixture_job())
            running = store.claim_next()

            class StubApp:
                notification_handler = None

            service = ControllerService(store, StubApp(), intake_enabled=True)  # type: ignore[arg-type]
            service.handle_notification(
                {"method": "item/completed", "params": {"turnId": "turn-fast", "item": {"type": "agentMessage", "text": "完成"}}}
            )
            service.handle_notification(
                {"method": "turn/completed", "params": {"turn": {"id": "turn-fast", "status": "completed"}}}
            )
            store.assign_thread(running["job_id"], "thread-fast")
            store.assign_turn(running["job_id"], "turn-fast")
            service._flush_turn_events("turn-fast")
            completed = store.get_job(job["job_id"])
            self.assertEqual(completed["state"], "completed")
            self.assertEqual(completed["result"], "完成")


if __name__ == "__main__":
    unittest.main()
