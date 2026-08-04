from __future__ import annotations

import base64
import hashlib
from http.client import HTTPConnection
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import threading
import time
import tomllib
import unittest

from codex_controller.api import DASHBOARD_HTML, DASHBOARD_JS, create_server
from codex_controller.app_server import AppServerClient, AppServerError
from codex_controller.main import normalize_codex_model, normalize_openai_base_url, read_api_key_from_fd, write_codex_config
from codex_controller.mcp_proxy import socket_call, tool_catalog
from codex_controller.media_input import TurnMediaManager
from codex_controller.service import ControllerService, NEW_THREAD_RESULT, is_new_thread_command
from codex_controller.store import ControllerStore, StoreError
from codex_controller.tool_proxy import ToolProxyError, ToolProxyServer, ToolRouter, validate_base_url


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


def controller_request(
    service: object,
    method: str,
    path: str,
    *,
    payload: dict | None = None,
    authorized: bool = False,
) -> tuple[int, dict]:
    token = "t" * 32
    server = create_server(
        "127.0.0.1",
        0,
        service=service,  # type: ignore[arg-type]
        api_token=token,
        max_request_bytes=1024 * 1024,
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    connection = HTTPConnection("127.0.0.1", server.server_port, timeout=3)
    body = None if payload is None else json.dumps(payload).encode("utf-8")
    headers = {"Content-Type": "application/json"} if body is not None else {}
    if authorized:
        headers["Authorization"] = f"Bearer {token}"
    try:
        connection.request(method, path, body=body, headers=headers)
        response = connection.getresponse()
        document = json.loads(response.read())
        return response.status, document
    finally:
        connection.close()
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


FAKE_APP_SERVER = r'''
import json, sys
thread_id = "thread-fixture"
account_type = "chatgpt"
thread_loaded = False
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
    elif method == "thread/start":
        thread_loaded = True
        result = {"thread":{"id":thread_id,"turns":[]}}
    elif method == "thread/resume":
        if thread_loaded:
            print(json.dumps({"id":request_id,"error":{"code":-32602,"message":"thread already loaded"}}), flush=True)
            continue
        thread_loaded = True
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

    def test_recovery_required_blocks_queue_until_explicit_audited_resolution(self) -> None:
        self.store.create_job(fixture_job("fixture-message-1"))
        self.store.create_job(fixture_job("fixture-message-2"))
        running = self.store.claim_next()
        assert running is not None
        self.store.recover_running()

        self.assertIsNone(self.store.claim_next())
        with self.assertRaises(StoreError) as invalid:
            self.store.resolve_recovery(running["job_id"], "retry")
        self.assertEqual(invalid.exception.code, "invalid_recovery_resolution")

        resolved = self.store.resolve_recovery(running["job_id"], "confirmed_failed")
        self.assertEqual(resolved["state"], "failed")
        self.assertEqual(resolved["error_code"], "recovery_review_failed")
        with self.store._connect() as connection:
            event = connection.execute(
                "SELECT event_type,item_type,error_code FROM job_events WHERE job_id=? ORDER BY id DESC LIMIT 1",
                (running["job_id"],),
            ).fetchone()
        self.assertEqual(dict(event), {
            "event_type": "recovery_resolved",
            "item_type": "confirmed_failed",
            "error_code": "recovery_review_failed",
        })
        next_job = self.store.claim_next()
        self.assertIsNotNone(next_job)
        self.assertEqual(next_job["message_id"], "fixture-message-2")

    def test_recovery_resolution_api_requires_explicit_bearer_action(self) -> None:
        self.store.create_job(fixture_job())
        running = self.store.claim_next()
        assert running is not None
        self.store.recover_running()

        class RecoveryService:
            def __init__(inner_self, store: ControllerStore):
                inner_self.store = store

        path = f"/internal/v1/jobs/{running['job_id']}/recovery-resolution"
        unauthorized_status, _ = controller_request(
            RecoveryService(self.store),
            "POST",
            path,
            payload={"resolution": "confirmed_completed"},
        )
        self.assertEqual(unauthorized_status, 401)
        status, document = controller_request(
            RecoveryService(self.store),
            "POST",
            path,
            payload={"resolution": "confirmed_completed"},
            authorized=True,
        )
        self.assertEqual(status, 200)
        self.assertEqual(document["result"]["state"], "completed")


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
            available_tools=[
                "ledger_summary",
                "ledger_query",
                "renovation_dashboard",
            ],
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

    def test_thread_instructions_keep_weixin_as_general_codex_entry(self) -> None:
        observed: list[tuple[str, dict]] = []

        def fake_request(method: str, params: dict) -> dict:
            observed.append((method, params))
            return {"thread": {"id": "thread-general", "turns": []}}

        self.client.request = fake_request  # type: ignore[method-assign]
        self.assertEqual(self.client.start_thread(), "thread-general")
        self.assertEqual(observed[0][0], "thread/start")
        instructions = observed[0][1]["developerInstructions"]
        self.assertIn("通用 Codex 助手", instructions)
        self.assertIn("不得把所有消息默认解释为装修事项", instructions)
        self.assertIn("只有用户意图确实需要装修账本或 Home Assistant 操作时", instructions)
        self.assertIn("普通问答", instructions)
        self.assertIn("当前会话已配置 Renovation Hub", instructions)
        self.assertIn("必须先调用 renovation_dashboard", instructions)
        self.assertIn("无副作用的只读工具", instructions)
        self.assertIn("不需要 Passkey、写入确认或额外征求授权", instructions)
        self.assertIn("不得转入 Home Assistant Operations 授权流程", instructions)
        self.assertIn("不得回复‘未连接账本’", instructions)
        self.assertIn("不得沿用历史对话中的旧 Mac 代理", instructions)
        self.assertIn("不得使用 Shell", instructions)

    def test_thread_resume_refreshes_current_instructions_and_safety_policy(self) -> None:
        observed: list[tuple[str, dict]] = []

        def fake_request(method: str, params: dict) -> dict:
            observed.append((method, params))
            return {"thread": {"id": "thread-existing", "turns": []}}

        self.client.request = fake_request  # type: ignore[method-assign]
        self.client.resume_thread("thread-existing")
        self.assertEqual(observed[0][0], "thread/resume")
        params = observed[0][1]
        self.assertEqual(params["threadId"], "thread-existing")
        self.assertEqual(params["cwd"], str(self.root / "workspace"))
        self.assertEqual(params["sandbox"], "read-only")
        self.assertEqual(params["approvalPolicy"], "never")
        self.assertEqual(params["developerInstructions"], self.client.developer_instructions)
        self.assertIn("Renovation Hub", params["developerInstructions"])

    def test_new_thread_is_not_resumed_again_in_the_same_process(self) -> None:
        observed: list[str] = []

        def fake_request(method: str, _params: dict) -> dict:
            observed.append(method)
            return {"thread": {"id": "thread-new", "turns": []}}

        self.client.request = fake_request  # type: ignore[method-assign]
        self.assertEqual(self.client.start_thread(), "thread-new")
        self.client.resume_thread("thread-new")
        self.assertEqual(observed, ["thread/start"])

    def test_stop_clears_loaded_threads_and_requires_resume_after_restart(self) -> None:
        observed: list[str] = []

        def fake_request(method: str, _params: dict) -> dict:
            observed.append(method)
            return {"thread": {"id": "thread-restart", "turns": []}}

        self.client.request = fake_request  # type: ignore[method-assign]
        self.assertEqual(self.client.start_thread(), "thread-restart")
        self.client.stop()
        self.client.resume_thread("thread-restart")
        self.assertEqual(observed, ["thread/start", "thread/resume"])

    def test_concurrent_resume_of_unknown_thread_sends_only_one_rpc(self) -> None:
        observed: list[str] = []
        entered = threading.Event()
        release = threading.Event()
        failures: list[BaseException] = []

        def fake_request(method: str, _params: dict) -> dict:
            observed.append(method)
            entered.set()
            self.assertTrue(release.wait(2))
            return {"thread": {"id": "thread-concurrent", "turns": []}}

        def resume() -> None:
            try:
                self.client.resume_thread("thread-concurrent")
            except BaseException as exc:  # pragma: no cover - surfaced below
                failures.append(exc)

        self.client.request = fake_request  # type: ignore[method-assign]
        first = threading.Thread(target=resume)
        second = threading.Thread(target=resume)
        first.start()
        self.assertTrue(entered.wait(2))
        second.start()
        release.set()
        first.join(timeout=2)
        second.join(timeout=2)
        self.assertFalse(first.is_alive())
        self.assertFalse(second.is_alive())
        self.assertEqual(failures, [])
        self.assertEqual(observed, ["thread/resume"])

    def test_new_command_then_next_job_reaches_turn_without_duplicate_resume(self) -> None:
        self.client.start()
        with tempfile.TemporaryDirectory() as temporary:
            store = ControllerStore(Path(temporary) / "controller.sqlite3")
            service = ControllerService(store, self.client, intake_enabled=False)

            reset = store.create_job(fixture_job("fixture-new-command", text="/new"))
            reset_running = store.claim_next()
            assert reset_running is not None
            service._dispatch(reset_running)
            self.assertEqual(store.get_job(reset["job_id"])["state"], "completed")

            query = store.create_job(
                fixture_job("fixture-query-after-new", text="当前连接装修账本了吗")
            )
            query_running = store.claim_next()
            assert query_running is not None
            service._dispatch(query_running)

            deadline = time.monotonic() + 2
            current = store.get_job(query["job_id"])
            while current["state"] == "running" and time.monotonic() < deadline:
                time.sleep(0.01)
                current = store.get_job(query["job_id"])
            self.assertEqual(current["state"], "completed")
            self.assertEqual(current["thread_id"], "thread-fixture")
            self.assertEqual(current["result"], "查询完成。")

    def test_current_instructions_describe_only_the_configured_tool_families(self) -> None:
        operations_only = AppServerClient.build_developer_instructions(
            ["ha_operations_propose_restart"]
        )
        self.assertIn("当前会话未配置 Renovation Hub", operations_only)
        self.assertIn("当前会话已配置受控 Home Assistant Operations", operations_only)

        no_tools = AppServerClient.build_developer_instructions([])
        self.assertIn("当前会话未配置 Renovation Hub", no_tools)
        self.assertNotIn("当前会话已配置受控 Home Assistant Operations", no_tools)

    def test_turn_accepts_official_local_image_input(self) -> None:
        observed: list[tuple[str, dict]] = []

        def fake_request(method: str, params: dict) -> dict:
            observed.append((method, params))
            return {"turn": {"id": "turn-local-image", "status": "inProgress", "items": []}}

        self.client.request = fake_request  # type: ignore[method-assign]
        items = [
            {"type": "text", "text": "识别这张图片"},
            {"type": "localImage", "path": "/data/turn-media/job/image.jpg", "detail": "auto"},
        ]
        turn_id = self.client.start_turn(
            "thread-fixture",
            "识别这张图片",
            "fixture-message-image",
            input_items=items,
        )
        self.assertEqual(turn_id, "turn-local-image")
        self.assertEqual(observed[0][0], "turn/start")
        self.assertEqual(observed[0][1]["input"], items)

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
                "CONTROLLER_OPENAI_BASE_URL": "https://private.example.test/v1",
                "CONTROLLER_CODEX_MODEL": "gpt-fixture",
                "CONTROLLER_MCP_SOCKET": "/secret/path",
            }
        )
        self.assertEqual(environment["PATH"], "/usr/bin")
        self.assertNotIn("CONTROLLER_LEDGER_API_TOKEN", environment)
        self.assertNotIn("CONTROLLER_OPERATIONS_API_TOKEN", environment)
        self.assertNotIn("CONTROLLER_OPENAI_API_KEY", environment)
        self.assertNotIn("CONTROLLER_OPENAI_BASE_URL", environment)
        self.assertNotIn("CONTROLLER_CODEX_MODEL", environment)
        self.assertNotIn("CONTROLLER_MCP_SOCKET", environment)


class ControllerApiBaseUrlTests(unittest.TestCase):
    @staticmethod
    def public_resolver(host: str, port: int, **_kwargs: object) -> list[tuple]:
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", port))]

    def test_empty_url_keeps_official_endpoint_for_both_auth_modes(self) -> None:
        self.assertEqual(normalize_openai_base_url("", auth_mode="api_key"), "")
        self.assertEqual(normalize_openai_base_url("", auth_mode="chatgpt_device_code"), "")

    def test_custom_url_is_normalized_for_api_key_mode(self) -> None:
        result = normalize_openai_base_url(
            "https://API.Example.Test:8443/openai/v1/",
            auth_mode="api_key",
            resolver=self.public_resolver,
        )
        self.assertEqual(result, "https://api.example.test:8443/openai/v1")

    def test_optional_model_is_restricted_to_api_key_and_safe_identifier(self) -> None:
        self.assertEqual(normalize_codex_model("", auth_mode="chatgpt_device_code"), "")
        self.assertEqual(normalize_codex_model("gpt-5.6-sol", auth_mode="api_key"), "gpt-5.6-sol")
        for value in (" gpt-5.6-sol", "gpt model", "gpt\nmodel", "../model", "x" * 129):
            with self.subTest(value=value), self.assertRaises(ValueError):
                normalize_codex_model(value, auth_mode="api_key")
        with self.assertRaisesRegex(ValueError, "API Key"):
            normalize_codex_model("gpt-5.6-sol", auth_mode="chatgpt_device_code")

    def test_custom_url_rejects_wrong_mode_and_unsafe_structure(self) -> None:
        with self.assertRaisesRegex(ValueError, "API Key"):
            normalize_openai_base_url(
                "https://api.example.test/v1",
                auth_mode="chatgpt_device_code",
                resolver=self.public_resolver,
            )
        invalid_values = (
            "http://api.example.test/v1",
            "https://user@api.example.test/v1",
            "https://api.example.test/v1?token=value",
            "https://api.example.test/v1#fragment",
            "https://api.example.test/v1\\private",
            " https://api.example.test/v1",
            "https://localhost/v1",
            "https://supervisor/v1",
            "https://service.internal/v1",
            "https://127.0.0.1/v1",
            "https://10.80.1.69/v1",
            "https://[::1]/v1",
        )
        for value in invalid_values:
            with self.subTest(value=value), self.assertRaises(ValueError):
                normalize_openai_base_url(value, auth_mode="api_key", resolver=self.public_resolver)

    def test_dns_with_any_non_public_result_is_rejected(self) -> None:
        def mixed_resolver(_host: str, port: int, **_kwargs: object) -> list[tuple]:
            return [
                (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", port)),
                (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("192.168.1.10", port)),
            ]

        with self.assertRaisesRegex(ValueError, "非公网"):
            normalize_openai_base_url(
                "https://api.example.test/v1",
                auth_mode="api_key",
                resolver=mixed_resolver,
            )

    def test_dns_failure_is_fail_closed_without_echoing_url(self) -> None:
        private_url = "https://tenant-secret.example.test/v1"

        def failing_resolver(_host: str, _port: int, **_kwargs: object) -> list[tuple]:
            raise socket.gaierror("fixture dns failure")

        with self.assertRaises(ValueError) as context:
            normalize_openai_base_url(private_url, auth_mode="api_key", resolver=failing_resolver)
        self.assertEqual(str(context.exception), "openai_base_url DNS 解析失败")
        self.assertNotIn(private_url, str(context.exception))

    def test_codex_config_is_atomic_private_and_contains_no_key(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            codex_home = root / "codex-home"
            socket_path = root / "runtime" / "tool-proxy.sock"
            mcp_pythonpath = Path(__file__).resolve().parents[1] / "codex_controller"
            write_codex_config(
                codex_home,
                socket_path,
                openai_base_url="https://api.example.test/v1",
                codex_model="gpt-5.6-sol",
                mcp_python=sys.executable,
                mcp_pythonpath=str(mcp_pythonpath),
            )
            config = codex_home / "config.toml"
            content = config.read_text(encoding="utf-8")
            parsed = tomllib.loads(content)
            mcp_config = parsed["mcp_servers"]["home_assistant_tools"]
            self.assertEqual(config.stat().st_mode & 0o777, 0o600)
            self.assertIn('openai_base_url = "https://api.example.test/v1"', content)
            self.assertIn('model = "gpt-5.6-sol"', content)
            self.assertIn("[mcp_servers.home_assistant_tools]", content)
            self.assertEqual(mcp_config["command"], sys.executable)
            self.assertEqual(mcp_config["env"]["CONTROLLER_MCP_SOCKET"], str(socket_path))
            self.assertEqual(mcp_config["env"]["PYTHONPATH"], str(mcp_pythonpath))
            self.assertEqual(
                mcp_config["tools"],
                {
                    "ledger_query": {"approval_mode": "approve"},
                    "ledger_show": {"approval_mode": "approve"},
                    "ledger_summary": {"approval_mode": "approve"},
                    "renovation_area_list": {"approval_mode": "approve"},
                    "renovation_dashboard": {"approval_mode": "approve"},
                    "renovation_project_list": {"approval_mode": "approve"},
                    "renovation_stage_list": {"approval_mode": "approve"},
                    "renovation_timeline": {"approval_mode": "approve"},
                },
            )
            self.assertNotIn("fixture-api-key-value", content)
            self.assertEqual(list(codex_home.glob(".config.toml.*")), [])
            write_codex_config(
                codex_home,
                socket_path,
                mcp_python=sys.executable,
                mcp_pythonpath=str(mcp_pythonpath),
            )
            self.assertNotIn("openai_base_url", config.read_text(encoding="utf-8"))
            self.assertNotIn("model =", config.read_text(encoding="utf-8"))

    def test_generated_mcp_command_loads_catalog_in_sanitized_environment(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            codex_home = root / "codex-home"
            socket_path = root / "runtime" / "tool-proxy.sock"
            mcp_pythonpath = Path(__file__).resolve().parents[1] / "codex_controller"
            write_codex_config(
                codex_home,
                socket_path,
                mcp_python=sys.executable,
                mcp_pythonpath=str(mcp_pythonpath),
            )
            config = tomllib.loads((codex_home / "config.toml").read_text(encoding="utf-8"))
            mcp_config = config["mcp_servers"]["home_assistant_tools"]
            router = ToolRouter(
                ledger_base_url="http://renovation-hub:8101",
                ledger_token="l" * 32,
            )
            proxy = ToolProxyServer(socket_path, router)
            proxy.start()
            process = subprocess.Popen(
                [mcp_config["command"], *mcp_config["args"]],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                env={"PATH": os.environ.get("PATH", ""), **mcp_config["env"]},
            )
            try:
                requests = "\n".join(
                    json.dumps(message)
                    for message in (
                        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
                        {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
                    )
                ) + "\n"
                output, error = process.communicate(input=requests, timeout=5)
                self.assertEqual(process.returncode, 0, error)
                initialized, catalog = [json.loads(line) for line in output.splitlines()]
                self.assertEqual(initialized["result"]["serverInfo"]["name"], "ha-controller-tools")
                self.assertEqual(len(catalog["result"]["tools"]), 26)
                by_name = {tool["name"]: tool for tool in catalog["result"]["tools"]}
                self.assertIn("renovation_dashboard", by_name)
                for name in (
                    "ledger_query",
                    "ledger_show",
                    "ledger_summary",
                    "renovation_area_list",
                    "renovation_dashboard",
                    "renovation_project_list",
                    "renovation_stage_list",
                    "renovation_timeline",
                ):
                    self.assertIn("只读", by_name[name]["description"])
                    self.assertIn("不会", by_name[name]["description"])
                    self.assertEqual(
                        by_name[name]["annotations"],
                        {
                            "readOnlyHint": True,
                            "destructiveHint": False,
                            "idempotentHint": True,
                            "openWorldHint": False,
                        },
                    )
                self.assertNotIn("annotations", by_name["ledger_add_payment"])
            finally:
                if process.poll() is None:
                    process.kill()
                    process.wait(timeout=5)
                proxy.stop()


class ControllerAuthenticationTests(unittest.TestCase):
    class StubApp:
        def __init__(self, *, auth_mode: str | None = None) -> None:
            self.auth_mode = auth_mode
            self.account_ready = auth_mode is not None
            self.running = True
            self.initialized = True
            self.protocol_error: str | None = None
            self.notification_handler = None
            self.api_key_calls: list[str] = []

        def start(self) -> None:
            return None

        def stop(self) -> None:
            return None

        def status(self) -> dict:
            return {
                "running": self.running,
                "initialized": self.initialized,
                "protocol_error": self.protocol_error,
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

    def test_runtime_failure_disables_intake_and_returns_watchdog_failure(self) -> None:
        app = self.StubApp(auth_mode="apiKey")
        temporary, service = self.make_service(app, auth_mode="api_key", api_key="fixture-api-key-value")
        try:
            self.assertTrue(service.intake_enabled)
            for field, value in (
                ("running", False),
                ("initialized", False),
                ("protocol_error", "app_server_protocol_error"),
            ):
                with self.subTest(field=field):
                    app.running = True
                    app.initialized = True
                    app.protocol_error = None
                    setattr(app, field, value)
                    self.assertFalse(service.intake_enabled)
                    status, document = controller_request(service, "GET", "/healthz")
                    self.assertEqual(status, 503)
                    self.assertEqual(document, {"ready": False, "status": "runtime_failed"})
        finally:
            service.stop()
            temporary.cleanup()

    def test_auth_not_ready_keeps_watchdog_healthy_without_enabling_intake(self) -> None:
        app = self.StubApp()
        temporary, service = self.make_service(app, auth_mode="chatgpt_device_code")
        try:
            self.assertFalse(service.intake_enabled)
            status, document = controller_request(service, "GET", "/healthz")
            self.assertEqual(status, 200)
            self.assertEqual(document, {"ready": False, "status": "ok"})
        finally:
            service.stop()
            temporary.cleanup()

    def test_custom_api_base_status_is_redacted(self) -> None:
        app = self.StubApp()
        temporary, service = self.make_service(
            app,
            auth_mode="api_key",
            api_key="fixture-api-key-value",
            api_base_mode="custom",
            codex_model_mode="custom",
        )
        try:
            service.start()
            status = service.status()
            self.assertEqual(status["api_base_mode"], "custom")
            self.assertTrue(status["api_base_configured"])
            self.assertIsNone(status["api_base_error"])
            self.assertEqual(status["codex_model_mode"], "custom")
            serialized = json.dumps(status, ensure_ascii=False)
            self.assertNotIn("example.test", serialized)
            self.assertNotIn("fixture-api-key-value", serialized)
        finally:
            service.stop()
            temporary.cleanup()

    def test_status_exposes_only_sanitized_tool_catalog(self) -> None:
        app = self.StubApp(auth_mode="apiKey")
        router = ToolRouter(
            ledger_base_url="http://renovation-hub:8101",
            ledger_token="l" * 32,
            operations_base_url="http://ha-operations-broker:8098",
            operations_token="o" * 32,
        )
        temporary, service = self.make_service(
            app,
            auth_mode="api_key",
            api_key="fixture-api-key-value",
            tool_context=router,
        )
        try:
            status = service.status()
            self.assertEqual(status["tools"]["count"], 31)
            self.assertTrue(status["tools"]["renovation_hub"])
            self.assertTrue(status["tools"]["operations"])
            self.assertIn("ledger_summary", status["tools"]["names"])
            serialized = json.dumps(status, ensure_ascii=False)
            self.assertNotIn("renovation-hub:8101", serialized)
            self.assertNotIn("fixture-api-key-value", serialized)
            self.assertNotIn("l" * 32, serialized)
            self.assertNotIn("o" * 32, serialized)
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
        self.assertIn("页面不会显示 URL 或 Key 内容", DASHBOARD_JS)
        self.assertIn("API 端点", DASHBOARD_JS)
        self.assertNotIn("<input", DASHBOARD_HTML.lower())
        self.assertNotIn("openai_api_key", DASHBOARD_HTML + DASHBOARD_JS)
        self.assertNotIn("openai_base_url", DASHBOARD_HTML + DASHBOARD_JS)

    def test_addon_config_and_run_script_keep_key_out_of_environment(self) -> None:
        root = Path(__file__).resolve().parents[1] / "codex_controller"
        config = (root / "config.yaml").read_text(encoding="utf-8")
        run_script = (root / "run.sh").read_text(encoding="utf-8")
        self.assertIn('auth_mode: "list(chatgpt_device_code|api_key)"', config)
        self.assertIn("openai_api_key: password", config)
        self.assertIn("openai_base_url: str", config)
        self.assertIn("codex_model: str", config)
        self.assertIn("CONTROLLER_OPENAI_API_KEY_FD", run_script)
        self.assertIn("CONTROLLER_OPENAI_BASE_URL", run_script)
        self.assertIn("CONTROLLER_CODEX_MODEL", run_script)
        self.assertNotIn("export CONTROLLER_OPENAI_API_KEY=", run_script)

    def test_addon_version_is_consistent_across_runtime_surfaces(self) -> None:
        root = Path(__file__).resolve().parents[1] / "codex_controller"
        expected = "0.1.9"
        self.assertIn(f'version: "{expected}"', (root / "config.yaml").read_text(encoding="utf-8"))
        for relative in (
            "codex_controller/api.py",
            "codex_controller/service.py",
            "codex_controller/app_server.py",
            "codex_controller/mcp_proxy.py",
            "README.md",
            "DOCS.md",
            "CHANGELOG.md",
        ):
            with self.subTest(relative=relative):
                self.assertIn(expected, (root / relative).read_text(encoding="utf-8"))

    def test_dispatch_resumes_an_existing_conversation_thread(self) -> None:
        class App:
            notification_handler = None

            def __init__(self) -> None:
                self.started: list[str] = []
                self.resumed: list[str] = []

            def start_thread(self) -> str:
                self.started.append("new")
                return "thread-new"

            def resume_thread(self, thread_id: str) -> None:
                self.resumed.append(thread_id)

            def start_turn(self, thread_id: str, _text: str, _message_id: str, *, input_items: object = None) -> str:
                self.assert_no_input_items = input_items
                return f"turn-for-{thread_id}"

        with tempfile.TemporaryDirectory() as temporary:
            store = ControllerStore(Path(temporary) / "controller.sqlite3")
            first = store.create_job(fixture_job("fixture-existing-1"))
            first_running = store.claim_next()
            assert first_running is not None
            store.assign_thread(first["job_id"], "thread-existing")
            store.assign_turn(first["job_id"], "turn-existing")
            store.complete_turn("turn-existing", "completed")

            store.create_job(fixture_job("fixture-existing-2"))
            second_running = store.claim_next()
            assert second_running is not None
            app = App()
            service = ControllerService(store, app, intake_enabled=False)  # type: ignore[arg-type]
            service._dispatch(second_running)

            self.assertEqual(app.started, [])
            self.assertEqual(app.resumed, ["thread-existing"])
            self.assertEqual(store.get_job(second_running["job_id"])["thread_id"], "thread-existing")

    def test_new_thread_command_is_exact_and_attachment_free(self) -> None:
        self.assertTrue(is_new_thread_command(fixture_job(text="打开新会话")))
        self.assertTrue(is_new_thread_command(fixture_job(text="  打开新会话  ")))
        self.assertTrue(is_new_thread_command(fixture_job(text="/new")))
        self.assertTrue(is_new_thread_command(fixture_job(text="  /new  ")))
        self.assertFalse(is_new_thread_command(fixture_job(text="请打开新会话")))
        self.assertFalse(is_new_thread_command(fixture_job(text="/new topic")))
        self.assertFalse(is_new_thread_command(fixture_job(text="打开一个新会话")))
        with_attachment = fixture_job(text="打开新会话")
        with_attachment["attachments"] = [
            {
                "attachment_ref": "a" * 43,
                "media_type": "image",
                "size_bytes": 1,
                "sha256": "sha256:" + "b" * 64,
            }
        ]
        self.assertFalse(is_new_thread_command(with_attachment))

    def test_dispatch_replaces_existing_thread_without_starting_a_model_turn(self) -> None:
        class App:
            notification_handler = None

            def __init__(self) -> None:
                self.started = 0
                self.resumed: list[str] = []
                self.turns: list[tuple[str, str]] = []

            def start_thread(self) -> str:
                self.started += 1
                return "thread-refreshed"

            def resume_thread(self, thread_id: str) -> None:
                self.resumed.append(thread_id)

            def start_turn(self, thread_id: str, text: str, _message_id: str, *, input_items: object = None) -> str:
                self.turns.append((thread_id, text))
                return "turn-after-refresh"

        with tempfile.TemporaryDirectory() as temporary:
            store = ControllerStore(Path(temporary) / "controller.sqlite3")
            first = store.create_job(fixture_job("fixture-before-refresh"))
            first_running = store.claim_next()
            assert first_running is not None
            store.assign_thread(first["job_id"], "thread-stale")
            store.assign_turn(first["job_id"], "turn-stale")
            store.complete_turn("turn-stale", "completed")

            reset = store.create_job(fixture_job("fixture-reset", text="打开新会话"))
            reset_running = store.claim_next()
            assert reset_running is not None
            app = App()
            service = ControllerService(store, app, intake_enabled=False)  # type: ignore[arg-type]
            service._dispatch(reset_running)

            completed = store.get_job(reset["job_id"])
            self.assertEqual(completed["state"], "completed")
            self.assertEqual(completed["thread_id"], "thread-refreshed")
            self.assertEqual(completed["result"], NEW_THREAD_RESULT)
            self.assertEqual(app.started, 1)
            self.assertEqual(app.resumed, [])
            self.assertEqual(app.turns, [])

            duplicate = store.create_job(fixture_job("fixture-reset", text="打开新会话"))
            self.assertEqual(duplicate["job_id"], reset["job_id"])
            self.assertEqual(duplicate["state"], "completed")

            store.create_job(fixture_job("fixture-after-refresh", text="当前连接装修账本了吗"))
            next_running = store.claim_next()
            assert next_running is not None
            service._dispatch(next_running)
            self.assertEqual(app.resumed, ["thread-refreshed"])
            self.assertEqual(app.turns, [("thread-refreshed", "当前连接装修账本了吗")])


class ToolRouterTests(unittest.TestCase):
    def test_mcp_catalog_filters_unconfigured_tools_and_operations_schemas_are_closed(self) -> None:
        catalog = tool_catalog(["ledger_summary", "ha_operations_propose_restart"])
        self.assertEqual([tool["name"] for tool in catalog], ["ledger_summary", "ha_operations_propose_restart"])
        operations = catalog[1]["inputSchema"]
        self.assertFalse(operations["additionalProperties"])
        self.assertEqual(operations["required"], ["target"])

    def test_tool_catalog_requires_complete_private_routes(self) -> None:
        cases = (
            (ToolRouter(), 0),
            (ToolRouter(ledger_base_url="http://renovation-hub:8101"), 0),
            (ToolRouter(ledger_token="l" * 32), 0),
            (ToolRouter(ledger_base_url="http://renovation-hub:8101", ledger_token="short"), 0),
            (ToolRouter(operations_base_url="http://ha-operations-broker:8098"), 0),
            (ToolRouter(operations_token="o" * 32), 0),
            (ToolRouter(operations_base_url="http://ha-operations-broker:8098", operations_token="short"), 0),
            (
                ToolRouter(
                    operations_base_url="http://ha-operations-broker:8098",
                    operations_token="o" * 32,
                ),
                5,
            ),
        )
        for router, expected_count in cases:
            with self.subTest(expected_count=expected_count, router=router):
                self.assertEqual(len(router.available_tools()), expected_count)

    def test_unix_socket_catalog_matches_default_ledger_operations_and_combined_routes(self) -> None:
        routers = (
            (ToolRouter(), 0),
            (ToolRouter(ledger_base_url="http://renovation-hub:8101", ledger_token="l" * 32), 26),
            (
                ToolRouter(
                    operations_base_url="http://ha-operations-broker:8098",
                    operations_token="o" * 32,
                ),
                5,
            ),
            (
                ToolRouter(
                    ledger_base_url="http://renovation-hub:8101",
                    ledger_token="l" * 32,
                    operations_base_url="http://ha-operations-broker:8098",
                    operations_token="o" * 32,
                ),
                31,
            ),
        )
        with tempfile.TemporaryDirectory() as temporary:
            for index, (router, expected_count) in enumerate(routers):
                with self.subTest(expected_count=expected_count):
                    socket_path = Path(temporary) / f"tool-proxy-{index}.sock"
                    proxy = ToolProxyServer(socket_path, router)
                    proxy.start()
                    try:
                        response = socket_call(str(socket_path), "__catalog__", {})
                    finally:
                        proxy.stop()
                    self.assertTrue(response["ok"])
                    self.assertEqual(len(response["result"]["tools"]), expected_count)

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

    def test_natural_query_tools_are_read_only_without_job_context(self) -> None:
        observed: list[tuple[str, str, dict | None]] = []

        def fake_request(method: str, url: str, _token: str, payload: dict | None) -> dict:
            observed.append((method, url, payload))
            return {"version": 1, "result": {"ok": True}}

        router = ToolRouter(
            ledger_base_url="http://renovation-hub:8101",
            ledger_token="l" * 32,
            request_json=fake_request,
        )
        for name, arguments in (
            ("ledger_query", {"limit": 5}),
            ("ledger_summary", {}),
            ("renovation_dashboard", {}),
        ):
            with self.subTest(name=name):
                router.call(name, arguments)
                self.assertEqual(observed[-1][0:2], ("POST", "http://renovation-hub:8101/internal/v1/tools/call"))
                assert observed[-1][2] is not None
                self.assertEqual(observed[-1][2]["name"], name)
                self.assertNotIn("idempotency_key", observed[-1][2]["arguments"])

        for name, arguments in (
            ("ledger_add_payment", {"amount": "1.00"}),
            ("renovation_event_create", {"title": "fixture"}),
        ):
            with self.subTest(name=name), self.assertRaises(ToolProxyError) as context:
                router.call(name, arguments)
            self.assertEqual(context.exception.code, "tool_context_unavailable")

    def test_mcp_catalog_distinguishes_core_read_only_queries_from_writes(self) -> None:
        catalog = {tool["name"]: tool for tool in tool_catalog()}
        for name in (
            "ledger_query",
            "ledger_show",
            "ledger_summary",
            "renovation_area_list",
            "renovation_dashboard",
            "renovation_project_list",
            "renovation_stage_list",
            "renovation_timeline",
        ):
            self.assertIn("只读", catalog[name]["description"])
            self.assertIn("无需 Passkey", catalog[name]["description"])
            self.assertTrue(catalog[name]["annotations"]["readOnlyHint"])
            self.assertFalse(catalog[name]["annotations"]["destructiveHint"])
        self.assertIn("写操作", catalog["ledger_add_payment"]["description"])
        self.assertNotIn("annotations", catalog["ledger_add_payment"])

    def test_operations_routes_are_closed_and_proposal_idempotency_is_controller_derived(self) -> None:
        observed: list[tuple[str, str, dict | None]] = []

        def fake_request(method: str, url: str, _token: str, payload: dict | None) -> dict:
            observed.append((method, url, payload))
            return {"version": 1, "result": {"ok": True}}

        router = ToolRouter(
            operations_base_url="http://ha-operations-broker:8098",
            operations_token="o" * 32,
            request_json=fake_request,
        )
        self.assertIn("ha_operations_execute_restart", router.available_tools())
        with self.assertRaises(ToolProxyError) as no_context:
            router.call("ha_operations_propose_restart", {"target": "local_renovation_hub"})
        self.assertEqual(no_context.exception.code, "tool_context_unavailable")

        router.begin_job("fixture-ops-job", "fixture-ops-message")
        router.call("ha_operations_propose_restart", {"target": "local_renovation_hub"})
        first_payload = observed[-1][2]
        assert first_payload is not None
        self.assertEqual(observed[-1][0:2], ("POST", "http://ha-operations-broker:8098/v1/proposals"))
        self.assertRegex(first_payload["idempotency_key"], r"^sha256:[a-f0-9]{64}$")
        router.call("ha_operations_propose_restart", {"target": "local_renovation_hub"})
        self.assertEqual(observed[-1][2], first_payload)

        with self.assertRaises(ToolProxyError) as extra:
            router.call(
                "ha_operations_authorization_request",
                {"action_id": "OPS-20260804-ABCDEF123456", "unexpected": True},
            )
        self.assertEqual(extra.exception.code, "invalid_arguments")
        router.call("ha_operations_authorization_request", {"action_id": "OPS-20260804-ABCDEF123456"})
        self.assertEqual(
            observed[-1],
            (
                "POST",
                "http://ha-operations-broker:8098/v1/authorization/requests",
                {"version": 1, "action_id": "OPS-20260804-ABCDEF123456"},
            ),
        )
        router.call("ha_operations_authorization_status", {"approval_id": "approval-fixture-1234"})
        self.assertEqual(
            observed[-1],
            (
                "GET",
                "http://ha-operations-broker:8098/v1/authorization/requests/approval-fixture-1234",
                None,
            ),
        )
        router.call(
            "ha_operations_execute_restart",
            {
                "receipt_id": "RCPT-" + "C" * 32,
                "action_id": "OPS-20260804-ABCDEF123456",
                "proposal_hash": "sha256:" + "a" * 64,
                "idempotency_key": "sha256:" + "b" * 64,
            },
        )
        self.assertEqual(observed[-1][0:2], ("POST", "http://ha-operations-broker:8098/v1/executions"))
        router.call("ha_operations_execution_status", {"action_id": "OPS-20260804-ABCDEF123456"})
        self.assertEqual(
            observed[-1],
            ("GET", "http://ha-operations-broker:8098/v1/executions/OPS-20260804-ABCDEF123456", None),
        )
        valid_execution = {
            "receipt_id": "RCPT-" + "C" * 32,
            "action_id": "OPS-20260804-ABCDEF123456",
            "proposal_hash": "sha256:" + "a" * 64,
            "idempotency_key": "sha256:" + "b" * 64,
        }
        for field, invalid_value in (
            ("receipt_id", "not-a-receipt"),
            ("action_id", "invalid-action"),
            ("proposal_hash", "sha256:short"),
            ("idempotency_key", "sha256:short"),
        ):
            with self.subTest(field=field):
                invalid_payload = {**valid_execution, field: invalid_value}
                with self.assertRaises(ToolProxyError) as invalid_execution:
                    router.call("ha_operations_execute_restart", invalid_payload)
                self.assertEqual(invalid_execution.exception.code, "invalid_arguments")
        with self.assertRaises(ToolProxyError) as extra_execution:
            router.call("ha_operations_execute_restart", {**valid_execution, "extra": True})
        self.assertEqual(extra_execution.exception.code, "invalid_arguments")

        with self.assertRaises(ToolProxyError) as invalid_slug:
            router.call("ha_operations_propose_restart", {"target": "LOCAL-invalid"})
        self.assertEqual(invalid_slug.exception.code, "invalid_target")

        first_idempotency = first_payload["idempotency_key"]
        router.clear_job("fixture-ops-job")
        router.begin_job("fixture-ops-job-2", "fixture-ops-message-2")
        router.call("ha_operations_propose_restart", {"target": "local_renovation_hub"})
        self.assertNotEqual(observed[-1][2]["idempotency_key"], first_idempotency)

    def test_turn_completion_clears_tool_context(self) -> None:
        router = ToolRouter(
            operations_base_url="http://ha-operations-broker:8098",
            operations_token="o" * 32,
            request_json=lambda *_args: {"version": 1, "result": {"ok": True}},
        )
        router.begin_job("fixture-context-job", "fixture-context-message")
        router.bind_turn("fixture-context-job", "turn-context")

        class App:
            notification_handler = None

        with tempfile.TemporaryDirectory() as temporary:
            store = ControllerStore(Path(temporary) / "controller.sqlite3")
            service = ControllerService(
                store,
                App(),  # type: ignore[arg-type]
                intake_enabled=False,
                tool_context=router,
            )
            service.handle_notification(
                {"method": "turn/completed", "params": {"turn": {"id": "turn-context", "status": "completed"}}}
            )

        with self.assertRaises(ToolProxyError) as context:
            router.call("ha_operations_propose_restart", {"target": "local_renovation_hub"})
        self.assertEqual(context.exception.code, "tool_context_unavailable")

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
            router.begin_job("fixture-job-attach", "fixture-message-attach")
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

    def test_attachment_preview_uses_non_consuming_gateway_endpoint(self) -> None:
        content = b"synthetic-image"
        digest = hashlib.sha256(content).hexdigest()
        observed: list[str] = []

        def fake_bytes(method: str, url: str, token: str, max_bytes: int) -> tuple[dict, bytes]:
            observed.append(url)
            self.assertEqual(method, "GET")
            self.assertEqual(token, "g" * 32)
            self.assertEqual(max_bytes, 20 * 1024 * 1024)
            return {
                "original_filename": "receipt.jpg",
                "mime_type": "image/jpeg",
                "size_bytes": len(content),
                "sha256": f"sha256:{digest}",
            }, content

        router = ToolRouter(
            gateway_base_url="http://weixin-gateway:8103",
            gateway_token="g" * 32,
            request_bytes=fake_bytes,
        )
        metadata, preview = router.preview_attachment("a" * 43)
        self.assertEqual(preview, content)
        self.assertEqual(metadata["sha256"], f"sha256:{digest}")
        self.assertEqual(observed, [f"http://weixin-gateway:8103/internal/v1/attachments/{'a' * 43}/preview"])


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
        router.begin_job("fixture-job-media", "fixture-message-media")
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


class TurnMediaManagerTests(unittest.TestCase):
    def test_image_preview_becomes_private_local_image_and_is_cleaned_after_turn(self) -> None:
        content = b"synthetic-jpeg-content"
        digest = f"sha256:{hashlib.sha256(content).hexdigest()}"
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "turn-media"

            def preview(reference: str) -> tuple[dict, bytes]:
                self.assertEqual(reference, "a" * 43)
                return {
                    "original_filename": "receipt.jpg",
                    "mime_type": "image/jpeg",
                    "size_bytes": len(content),
                    "sha256": digest,
                }, content

            manager = TurnMediaManager(root, preview)
            payload = fixture_job(text="请识别并记录")
            payload["attachments"] = [
                {
                    "attachment_ref": "a" * 43,
                    "media_type": "image",
                    "size_bytes": len(content),
                    "sha256": digest,
                }
            ]
            items = manager.prepare("fixture-job-image", payload)
            self.assertEqual([item["type"] for item in items], ["text", "localImage"])
            self.assertIn("attachment_ref=" + "a" * 43, items[0]["text"])
            image_path = Path(items[1]["path"])
            self.assertTrue(image_path.is_file())
            self.assertEqual(image_path.read_bytes(), content)
            self.assertEqual(image_path.stat().st_mode & 0o777, 0o600)
            manager.bind_turn("fixture-job-image", "turn-image")
            manager.cleanup_turn("turn-image")
            self.assertFalse(image_path.exists())
            self.assertEqual(list(root.iterdir()), [])


class ControllerServiceRaceTests(unittest.TestCase):
    def test_fast_notifications_are_replayed_after_turn_binding(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            store = ControllerStore(root / "controller.sqlite3")
            job = store.create_job(fixture_job())
            running = store.claim_next()

            class StubApp:
                notification_handler = None

            router = ToolRouter(
                operations_base_url="http://ha-operations-broker:8098",
                operations_token="o" * 32,
                request_json=lambda *_args: {"version": 1, "result": {"ok": True}},
            )
            router.begin_job(running["job_id"], running["message_id"])
            service = ControllerService(
                store,
                StubApp(),  # type: ignore[arg-type]
                intake_enabled=True,
                tool_context=router,
            )
            service.handle_notification(
                {"method": "item/completed", "params": {"turnId": "turn-fast", "item": {"type": "agentMessage", "text": "完成"}}}
            )
            service.handle_notification(
                {"method": "turn/completed", "params": {"turn": {"id": "turn-fast", "status": "completed"}}}
            )
            store.assign_thread(running["job_id"], "thread-fast")
            router.bind_turn(running["job_id"], "turn-fast")
            store.assign_turn(running["job_id"], "turn-fast")
            service._flush_turn_events("turn-fast")
            completed = store.get_job(job["job_id"])
            self.assertEqual(completed["state"], "completed")
            self.assertEqual(completed["result"], "完成")
            with self.assertRaises(ToolProxyError) as context:
                router.call("ha_operations_propose_restart", {"target": "local_renovation_hub"})
            self.assertEqual(context.exception.code, "tool_context_unavailable")


if __name__ == "__main__":
    unittest.main()
