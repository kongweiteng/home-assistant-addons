from __future__ import annotations

import base64
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import tempfile
import threading
import unittest

from codex_controller.app_server import AppServerClient, AppServerError
from codex_controller.service import (
    ControllerService,
    direct_memo_complete_query,
    direct_memo_create_arguments,
    direct_memo_list_arguments,
)
from codex_controller.store import ControllerStore
from codex_controller.tool_catalog import MEMO_TOOLS
from codex_controller.tool_proxy import ToolProxyError, ToolRouter, _request_memo_json


class FamilyMemoToolTests(unittest.TestCase):
    def memo_router(self, observed: list[tuple] | None = None) -> ToolRouter:
        calls = [] if observed is None else observed

        def request(
            method: str,
            url: str,
            username: str,
            password: str,
            token: str,
            payload: dict | None,
        ) -> dict:
            calls.append((method, url, username, password, token, payload))
            return {"version": 1, "result": {"ok": True}}

        return ToolRouter(
            memo_base_url="http://a0d7b954-nodered:80",
            memo_http_username="family-memo",
            memo_http_password="fixture-password-value",
            memo_api_token="m" * 32,
            request_memo_json=request,
        )

    def test_memo_tools_require_complete_private_route(self) -> None:
        configured = self.memo_router()
        self.assertEqual(set(configured.available_tools()), set(MEMO_TOOLS))
        for router in (
            ToolRouter(memo_base_url="http://a0d7b954-nodered:80"),
            ToolRouter(
                memo_base_url="http://a0d7b954-nodered:80",
                memo_http_username="family-memo",
                memo_http_password="fixture-password-value",
                memo_api_token="short",
            ),
            ToolRouter(
                memo_base_url="http://a0d7b954-nodered:80",
                memo_http_username="invalid:user",
                memo_http_password="fixture-password-value",
                memo_api_token="m" * 32,
            ),
        ):
            self.assertFalse(set(router.available_tools()) & set(MEMO_TOOLS))

    def test_create_derives_private_idempotency_from_gateway_message(self) -> None:
        observed: list[tuple] = []
        router = self.memo_router(observed)
        router.begin_job("fixture-job-1", "gateway-message-001", "owner")
        router.call(
            "memo_create",
            {
                "content": "询问铝瓦进度",
                "due_at": "2026-08-15T10:00:00+08:00",
                "priority": "normal",
                "category": "装修",
            },
        )
        first = observed[-1]
        self.assertEqual(first[:2], ("POST", "http://a0d7b954-nodered:80/endpoint/api/memos"))
        self.assertEqual(first[2:5], ("family-memo", "fixture-password-value", "m" * 32))
        payload = first[5]
        self.assertEqual(payload["source"], "wechat")
        self.assertEqual(
            payload["source_message_id"],
            "wechat:" + hashlib.sha256(b"gateway-message-001").hexdigest(),
        )
        self.assertNotIn("gateway-message-001", json.dumps(payload))

        router.clear_job("fixture-job-1")
        router.begin_job("fixture-job-2", "gateway-message-001", "owner")
        router.call("memo_create", {"content": "询问铝瓦进度"})
        self.assertEqual(
            observed[-1][5]["source_message_id"],
            payload["source_message_id"],
        )

    def test_list_filters_are_bounded_and_member_write_is_rejected(self) -> None:
        observed: list[tuple] = []
        router = self.memo_router(observed)
        router.begin_job("fixture-member-job", "gateway-message-member", "member_read_only")
        self.assertIn("memo_list", router.available_tools("member_read_only"))
        self.assertNotIn("memo_create", router.available_tools("member_read_only"))
        router.call(
            "memo_list",
            {"status": "pending", "date": "today", "overdue": False, "limit": 20},
        )
        self.assertEqual(observed[-1][0], "GET")
        self.assertIn("status=pending", observed[-1][1])
        self.assertIn("date=today", observed[-1][1])
        self.assertIn("overdue=false", observed[-1][1])
        self.assertIn("limit=20", observed[-1][1])
        with self.assertRaises(ToolProxyError) as denied:
            router.call("memo_complete", {"id": "memo-" + "a" * 32})
        self.assertEqual(denied.exception.code, "tool_not_allowed_for_profile")

    def test_update_complete_and_cancel_use_fixed_routes(self) -> None:
        observed: list[tuple] = []
        router = self.memo_router(observed)
        router.begin_job("fixture-owner-job", "gateway-message-owner", "owner")
        memo_id = "memo-" + "b" * 32
        router.call(
            "memo_update",
            {"id": memo_id, "due_at": "2026-08-16T15:00:00+08:00"},
        )
        self.assertEqual(observed[-1][0:2], ("PATCH", f"http://a0d7b954-nodered:80/endpoint/api/memos/{memo_id}"))
        self.assertEqual(observed[-1][5], {"due_at": "2026-08-16T15:00:00+08:00"})
        router.call("memo_complete", {"id": memo_id})
        self.assertEqual(observed[-1][0:2], ("POST", f"http://a0d7b954-nodered:80/endpoint/api/memos/{memo_id}/complete"))
        router.call("memo_cancel", {"id": memo_id})
        self.assertEqual(observed[-1][0:2], ("POST", f"http://a0d7b954-nodered:80/endpoint/api/memos/{memo_id}/cancel"))
        with self.assertRaises(ToolProxyError):
            router.call("memo_update", {"id": memo_id})

    def test_http_transport_uses_basic_auth_and_separate_module_token(self) -> None:
        observed: dict[str, object] = {}

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, _format: str, *_args: object) -> None:
                return None

            def do_POST(self) -> None:  # noqa: N802
                length = int(self.headers.get("Content-Length", "0"))
                observed.update(
                    {
                        "path": self.path,
                        "authorization": self.headers.get("Authorization"),
                        "memo_token": self.headers.get("X-Family-Memo-Token"),
                        "payload": json.loads(self.rfile.read(length)),
                    }
                )
                body = b'{"version":1,"result":{"ok":true}}'
                self.send_response(201)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            result = _request_memo_json(
                "POST",
                f"http://127.0.0.1:{server.server_port}/endpoint/api/memos",
                "family-memo",
                "fixture-password",
                "t" * 32,
                {"content": "fixture"},
            )
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)
        expected_basic = base64.b64encode(b"family-memo:fixture-password").decode("ascii")
        self.assertEqual(result["result"]["ok"], True)
        self.assertEqual(observed["authorization"], f"Basic {expected_basic}")
        self.assertEqual(observed["memo_token"], "t" * 32)
        self.assertNotIn("t" * 32, str(observed["path"]))

    def test_developer_context_explains_natural_language_and_disambiguation(self) -> None:
        router = self.memo_router()
        instructions = AppServerClient.build_developer_instructions(
            router.available_tools("owner"),
            "owner",
            router.tool_definitions_by_name(),
        )
        self.assertIn("家庭备忘录", instructions)
        self.assertIn("memo_create", instructions)
        self.assertIn("memo_list", instructions)
        self.assertIn("Asia/Shanghai", instructions)
        self.assertIn("Node-RED 独立持久化、调度和通知", instructions)
        self.assertIn("不属于 Codex Goal", instructions)
        self.assertIn("必须调用 memo_create", instructions)
        self.assertIn("不得回复不能创建定时提醒或主动推送", instructions)
        self.assertIn("不得建议改用手机日历", instructions)
        self.assertIn("唯一匹配", instructions)
        self.assertIn("多个候选", instructions)

    def test_explicit_timed_create_is_parsed_deterministically(self) -> None:
        arguments = direct_memo_create_arguments(
            {
                "text": "记一下，明天上午十点询问铝瓦进度",
                "received_at": "2026-08-15T00:16:00+08:00",
                "attachments": [],
            }
        )
        self.assertEqual(
            arguments,
            {
                "content": "询问铝瓦进度",
                "due_at": "2026-08-16T10:00:00+08:00",
                "priority": "normal",
                "category": "装修",
            },
        )
        self.assertIsNone(
            direct_memo_create_arguments(
                {
                    "text": "讨论一下明天上午十点的安排",
                    "received_at": "2026-08-15T00:16:00+08:00",
                    "attachments": [],
                }
            )
        )

    def test_dispatch_routes_explicit_timed_create_without_model_turn(self) -> None:
        observed: list[tuple] = []

        def request(
            method: str,
            url: str,
            username: str,
            password: str,
            token: str,
            payload: dict | None,
        ) -> dict:
            observed.append((method, url, username, password, token, payload))
            assert payload is not None
            return {
                "version": 1,
                "result": {
                    "memo": {
                        "content": payload["content"],
                        "due_at": payload["due_at"],
                    },
                    "idempotent_replay": False,
                },
            }

        class NoModelApp:
            notification_handler = None

            def configure_developer_context(self, *_args, **_kwargs) -> None:
                return None

            def start_thread(self) -> str:
                raise AssertionError("deterministic memo create must not start a model thread")

        with tempfile.TemporaryDirectory() as temporary:
            store = ControllerStore(Path(temporary) / "controller.sqlite3")
            router = ToolRouter(
                memo_base_url="http://a0d7b954-nodered:80",
                memo_http_username="family-memo",
                memo_http_password="fixture-password-value",
                memo_api_token="m" * 32,
                request_memo_json=request,
                store=store,
            )
            payload = {
                "version": 1,
                "message_id": "gateway-deterministic-memo-001",
                "conversation_key": "sha256:" + hashlib.sha256(b"weixin:fixture-owner").hexdigest(),
                "received_at": "2026-08-15T00:16:00+08:00",
                "text": "记一下，明天上午十点询问铝瓦进度",
                "attachments": [],
                "reply_capabilities": ["text"],
                "capability_profile": "owner",
            }
            queued = store.create_job(payload)
            running = store.claim_next()
            assert running is not None
            service = ControllerService(
                store,
                NoModelApp(),  # type: ignore[arg-type]
                intake_enabled=False,
                tool_context=router,
            )
            service._dispatch(running)
            completed = store.get_job(queued["job_id"])
            self.assertEqual(completed["state"], "completed")
            self.assertIn("已记下：询问铝瓦进度", completed["result"])
            self.assertIn("2026年8月16日 10:00", completed["result"])
            self.assertEqual(observed[-1][5]["source_message_id"], "wechat:" + hashlib.sha256(b"gateway-deterministic-memo-001").hexdigest())
            tool_status = {
                item["name"]: item
                for item in store.tool_control_document(set(MEMO_TOOLS))["tools"]
            }
            self.assertEqual(tool_status["memo_create"]["last_invocation"]["outcome"], "succeeded")

    def test_explicit_list_and_complete_are_parsed_deterministically(self) -> None:
        base = {"received_at": "2026-08-15T00:16:00+08:00", "attachments": []}
        self.assertEqual(
            direct_memo_list_arguments({**base, "text": "显示未完成的备忘录"}),
            {"status": "pending", "limit": 20},
        )
        self.assertEqual(
            direct_memo_list_arguments({**base, "text": "显示今天的事情"}),
            {"status": "pending", "date": "today", "limit": 20},
        )
        self.assertEqual(
            direct_memo_list_arguments({**base, "text": "有哪些逾期备忘录"}),
            {"status": "pending", "overdue": True, "limit": 20},
        )
        self.assertEqual(
            direct_memo_complete_query({**base, "text": "完成询问铝瓦进度"}),
            "询问铝瓦进度",
        )
        self.assertIsNone(direct_memo_complete_query({**base, "text": "询问铝瓦进度完成了吗？"}))

    def test_dispatch_routes_list_and_unique_complete_without_model_turn(self) -> None:
        observed: list[tuple] = []
        memo_id = "memo-" + "c" * 32

        def request(
            method: str,
            url: str,
            username: str,
            password: str,
            token: str,
            payload: dict | None,
        ) -> dict:
            observed.append((method, url, username, password, token, payload))
            if method == "GET":
                return {
                    "version": 1,
                    "result": {
                        "count": 1,
                        "items": [
                            {
                                "id": memo_id,
                                "content": "询问铝瓦进度",
                                "due_at": "2026-08-16T10:00:00+08:00",
                                "status": "pending",
                            }
                        ],
                    },
                }
            return {
                "version": 1,
                "result": {
                    "memo": {
                        "id": memo_id,
                        "content": "询问铝瓦进度",
                        "status": "completed",
                    }
                },
            }

        class NoModelApp:
            notification_handler = None

            def configure_developer_context(self, *_args, **_kwargs) -> None:
                return None

            def start_thread(self) -> str:
                raise AssertionError("deterministic memo commands must not start a model thread")

        with tempfile.TemporaryDirectory() as temporary:
            store = ControllerStore(Path(temporary) / "controller.sqlite3")
            router = ToolRouter(
                memo_base_url="http://a0d7b954-nodered:80",
                memo_http_username="family-memo",
                memo_http_password="fixture-password-value",
                memo_api_token="m" * 32,
                request_memo_json=request,
                store=store,
            )
            service = ControllerService(
                store,
                NoModelApp(),  # type: ignore[arg-type]
                intake_enabled=False,
                tool_context=router,
            )
            common = {
                "version": 1,
                "conversation_key": "sha256:" + hashlib.sha256(b"weixin:fixture-owner").hexdigest(),
                "received_at": "2026-08-15T00:16:00+08:00",
                "attachments": [],
                "reply_capabilities": ["text"],
                "capability_profile": "owner",
            }
            list_job = store.create_job(
                {**common, "message_id": "gateway-list-001", "text": "显示未完成的备忘录"}
            )
            running = store.claim_next()
            assert running is not None
            service._dispatch(running)
            listed = store.get_job(list_job["job_id"])
            self.assertEqual(listed["state"], "completed")
            self.assertIn("找到 1 条备忘录", listed["result"])
            self.assertIn("询问铝瓦进度", listed["result"])
            self.assertEqual(observed[-1][0], "GET")
            self.assertIn("status=pending", observed[-1][1])
            self.assertIn("limit=20", observed[-1][1])

            complete_job = store.create_job(
                {**common, "message_id": "gateway-complete-001", "text": "完成询问铝瓦进度"}
            )
            running = store.claim_next()
            assert running is not None
            service._dispatch(running)
            completed = store.get_job(complete_job["job_id"])
            self.assertEqual(completed["state"], "completed")
            self.assertEqual(completed["result"], "已完成：询问铝瓦进度")
            self.assertEqual([call[0] for call in observed[-2:]], ["GET", "POST"])
            self.assertIn("status=pending", observed[-2][1])
            self.assertTrue(observed[-1][1].endswith(f"/{memo_id}/complete"))
            tool_status = {
                item["name"]: item
                for item in store.tool_control_document(set(MEMO_TOOLS))["tools"]
            }
            self.assertEqual(tool_status["memo_list"]["last_invocation"]["outcome"], "succeeded")
            self.assertEqual(tool_status["memo_complete"]["last_invocation"]["outcome"], "succeeded")

    def test_app_server_prewarms_complete_memo_catalog_before_intake(self) -> None:
        router = self.memo_router()
        expected = router.available_tools("owner_legacy")
        client = AppServerClient(
            ["codex", "app-server", "--stdio"],
            codex_home="/tmp/family-memo-codex-home",
            workspace="/tmp/family-memo-workspace",
        )
        observed: list[tuple[str, dict]] = []

        def complete_request(method: str, params: dict) -> dict:
            observed.append((method, params))
            return {
                "data": [
                    {
                        "name": "home_assistant_tools",
                        "tools": {name: {} for name in expected},
                    }
                ]
            }

        client.request = complete_request  # type: ignore[method-assign]
        self.assertEqual(set(client.refresh_mcp_catalog(expected)), set(expected))
        self.assertEqual(observed, [("mcpServerStatus/list", {"detail": "full"})])

        client.request = lambda _method, _params: {  # type: ignore[method-assign]
            "data": [{"name": "home_assistant_tools", "tools": {"memo_list": {}}}]
        }
        with self.assertRaises(AppServerError) as context:
            client.refresh_mcp_catalog(expected)
        self.assertEqual(context.exception.code, "mcp_catalog_incomplete")


if __name__ == "__main__":
    unittest.main()
