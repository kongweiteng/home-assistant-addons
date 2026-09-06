from __future__ import annotations

import datetime as dt
from http.client import HTTPConnection
import io
import json
import threading
import unittest
from urllib.error import HTTPError

from codex_controller.api import create_server
from codex_controller.desktop_dashboard import DESKTOP_DASHBOARD_HTML, DESKTOP_DASHBOARD_JS
from codex_controller.error_center import ErrorCenter


NOW = dt.datetime(2026, 9, 6, 12, 0, tzinfo=dt.timezone(dt.timedelta(hours=8)))
THREAD = "TH-" + "A" * 20


class _Response:
    def __init__(self, payload: dict) -> None:
        self.payload = json.dumps(payload).encode()

    def __enter__(self):
        return self

    def __exit__(self, *_args) -> None:
        return None

    def read(self, _limit: int = -1) -> bytes:
        return self.payload


class _Desktop:
    def __init__(self) -> None:
        self.status = "failed"

    def threads(self, **_kwargs) -> dict:
        return {
            "threads": [
                {
                    "thread_ref": THREAD,
                    "status": self.status,
                    "updated_at": NOW.isoformat(),
                    "title": "private task title must not be exposed",
                }
            ]
        }


class _Controller:
    def __init__(self, center: ErrorCenter) -> None:
        self.center = center

    def errors(self, *, cursor: int, limit: int) -> dict:
        return self.center.page(cursor=cursor, limit=limit)

    def error_stream(self):
        return self.center.stream(heartbeat_seconds=10)


class ErrorCenterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.desktop = _Desktop()
        self.gateway_payload = {
            "version": 1,
            "result": {
                "items": [
                    {
                        "failure_short": "ER-ABCDEFG234",
                        "job_short": "JB-ABCDEFG234",
                        "source": "controller",
                        "occurred_at": NOW.isoformat(),
                        "error": {
                            "code": "response_stream_disconnected",
                            "message": "上游响应流中断，自动重试后仍未完成。请稍后再试。",
                            "recommended_action": "检查网络和 Runner 在线状态，恢复后重试。",
                        },
                        "sender": "private-sender",
                    }
                ]
            },
        }
        self.center = ErrorCenter(
            desktop_controller=self.desktop,
            component_status=lambda: {"runner_error": "relay_publish_indeterminate"},
            gateway_base_url="http://weixin-gateway:8103",
            gateway_token="a" * 32,
            now=lambda: NOW,
            opener=lambda _request, timeout: _Response(self.gateway_payload),
        )
        self.center._receive_gateway_frame(  # type: ignore[attr-defined]
            {"version": 1, "revision": 1, "summary": {"count": 1, "latest_occurred_at": NOW.isoformat()}}
        )

    def test_page_is_paged_and_never_forwards_gateway_private_fields(self) -> None:
        first = self.center.page(cursor=0, limit=2)
        self.assertEqual(first["summary"]["total"], 3)
        self.assertTrue(first["page"]["has_more"])
        self.assertEqual(len(first["items"]), 2)
        second = self.center.page(cursor=first["page"]["next_cursor"], limit=2)
        self.assertFalse(second["page"]["has_more"])
        self.assertEqual(len(second["items"]), 1)
        encoded = json.dumps(first, ensure_ascii=False)
        all_pages = encoded + json.dumps(second, ensure_ascii=False)
        self.assertNotIn("private-sender", encoded)
        self.assertNotIn("private task title", encoded)
        self.assertIn("response_stream_disconnected", all_pages)
        self.assertIn(THREAD, all_pages)
        failed_thread = next(
            item
            for item in first["items"] + second["items"]
            if item["reference"] == THREAD
        )
        self.assertEqual(failed_thread["lifecycle"], "historical")
        self.assertEqual(failed_thread["lifecycle_label"], "历史错误")

    def test_lifecycle_distinguishes_current_history_and_recovery(self) -> None:
        self.desktop.status = "recovery_required"
        initial = self.center.page(cursor=0, limit=12)
        self.assertEqual(initial["summary"]["current"], 2)
        self.assertEqual(initial["summary"]["historical"], 1)
        self.assertEqual(initial["summary"]["recovered"], 0)
        gateway = next(item for item in initial["items"] if item["origin"] == "wechat")
        self.assertEqual(gateway["lifecycle"], "historical")
        self.assertEqual(gateway["lifecycle_label"], "历史错误")

        self.desktop.status = "idle"
        self.center.notify_local_change()
        changed = self.center.page(cursor=0, limit=12)
        recovered = next(item for item in changed["items"] if item["reference"] == THREAD)
        self.assertEqual(changed["summary"]["current"], 1)
        self.assertEqual(changed["summary"]["historical"], 1)
        self.assertEqual(changed["summary"]["recovered"], 1)
        self.assertEqual(recovered["lifecycle"], "recovered")
        self.assertEqual(recovered["lifecycle_label"], "已恢复")
        self.assertEqual(recovered["recovered_at"], NOW.isoformat())
        self.assertFalse(recovered["error"]["retryable"])

    def test_create_and_bridge_codes_have_fixed_public_diagnoses(self) -> None:
        codes = {
            "create_identity_unknown",
            "create_unknown",
            "create_thread_not_visible",
            "create_turn_not_visible",
            "create_turn_interrupted",
            "create_turn_failed",
            "create_image_queue_unknown",
            "bridge_policy_invalid",
            "bridge_path_invalid",
            "bridge_caller_invalid",
            "bridge_timeout_invalid",
            "bridge_capability_unavailable",
            "bridge_projects_invalid",
            "bridge_project_catalog_invalid",
            "bridge_project_catalog_unavailable",
            "bridge_project_not_allowed",
            "bridge_image_unsupported",
            "bridge_create_pending",
            "bridge_create_response_invalid",
            "bridge_tool_failed",
            "bridge_tool_response_invalid",
            "bridge_request_invalid",
            "bridge_request_too_large",
            "bridge_request_failed",
            "bridge_busy",
            "bridge_closed",
            "bridge_unavailable",
            "bridge_identity_invalid",
            "bridge_response_invalid",
            "bridge_response_too_large",
            "bridge_app_identity_invalid",
            "bridge_runtime_identity_invalid",
            "bridge_app_signature_invalid",
            "bridge_app_pipe_unavailable",
            "bridge_app_pipe_invalid",
            "bridge_app_timeout",
            "bridge_app_unavailable",
            "bridge_app_response_invalid",
            "bridge_app_request_failed",
            "bridge_install_policy_invalid",
            "bridge_install_source_invalid",
            "bridge_config_read_failed",
            "bridge_config_invalid",
            "bridge_config_conflict",
            "bridge_config_write_failed",
            "bridge_config_verify_failed",
        }
        for code in codes:
            with self.subTest(code=code):
                center = ErrorCenter(
                    desktop_controller=None,
                    component_status=lambda code=code: {
                        "runner_error": code,
                        "private_detail": "token=secret https://private.invalid",
                    },
                    now=lambda: NOW,
                )
                item = center.page(cursor=0, limit=1)["items"][0]
                self.assertEqual(item["error"]["code"], code)
                self.assertEqual(item["lifecycle_label"], "当前故障")
                self.assertTrue(item["error"]["label"])
                self.assertTrue(item["message"])
                self.assertTrue(item["recommended_action"])
                encoded = json.dumps(item, ensure_ascii=False)
                self.assertNotIn("secret", encoded)
                self.assertNotIn("private.invalid", encoded)

    def test_create_receipts_use_safe_references_and_recovery_lifecycle(self) -> None:
        class Journal:
            def __init__(self) -> None:
                self.commands = [
                    {
                        "request_id": "private-request https://private.invalid token=secret",
                        "state": "recovery_required",
                        "error_code": "create_thread_not_visible",
                        "thread_ref": THREAD,
                        "updated_at": NOW.isoformat(),
                        "private_exception": "must not be exposed",
                    }
                ]

            def host_commands(self, _host_ref: str) -> list[dict]:
                return self.commands

        class DesktopWithCreates:
            def __init__(self) -> None:
                self._create_journal = Journal()

            def hosts(self) -> dict:
                return {"hosts": [{"host_ref": "HS-" + "A" * 20}]}

            def threads(self, **_kwargs) -> dict:
                return {"threads": []}

        desktop = DesktopWithCreates()
        center = ErrorCenter(desktop_controller=desktop, component_status=lambda: {}, now=lambda: NOW)
        current = center.page(cursor=0, limit=12)
        item = current["items"][0]
        self.assertRegex(item["reference"], r"^CR-[A-F0-9]{12}$")
        self.assertEqual(item["lifecycle_label"], "当前故障")
        self.assertEqual(item["error"]["code"], "create_thread_not_visible")
        self.assertEqual(item["task_reference"], THREAD)
        self.assertEqual(item["task_href"], f"desktop/?thread_ref={THREAD}")
        encoded = json.dumps(current, ensure_ascii=False)
        self.assertNotIn("private.invalid", encoded)
        self.assertNotIn("secret", encoded)
        self.assertNotIn("private_exception", encoded)

        desktop._create_journal.commands[0]["state"] = "confirmed"
        desktop._create_journal.commands[0]["error_code"] = None
        recovered = center.page(cursor=0, limit=12)
        self.assertEqual(recovered["summary"]["current"], 0)
        self.assertEqual(recovered["summary"]["recovered"], 1)
        self.assertEqual(recovered["items"][0]["lifecycle_label"], "已恢复")

        desktop._create_journal.commands[0]["state"] = "recovery_required"
        desktop._create_journal.commands[0]["error_code"] = "create_unknown"
        self.assertEqual(center.page(cursor=0, limit=12)["summary"]["current"], 1)
        desktop._create_journal.commands[0]["state"] = "failed"
        desktop._create_journal.commands[0]["error_code"] = "create_turn_failed"
        terminal = center.page(cursor=0, limit=12)
        self.assertEqual(terminal["summary"]["current"], 0)
        self.assertEqual(terminal["summary"]["historical"], 1)
        self.assertEqual(terminal["summary"]["recovered"], 0)

    def test_terminal_create_failure_is_history_not_current_fault(self) -> None:
        class Journal:
            def host_commands(self, _host_ref: str) -> list[dict]:
                return [{
                    "request_id": "safe-public-fixture",
                    "state": "failed",
                    "error_code": "create_turn_failed",
                    "thread_ref": THREAD,
                    "updated_at": NOW.isoformat(),
                }]

        class DesktopWithFailure:
            _create_journal = Journal()

            def hosts(self) -> dict:
                return {"hosts": [{"host_ref": "HS-" + "A" * 20}]}

            def threads(self, **_kwargs) -> dict:
                return {"threads": []}

        center = ErrorCenter(
            desktop_controller=DesktopWithFailure(),
            component_status=lambda: {},
            now=lambda: NOW,
        )
        page = center.page(cursor=0, limit=12)
        self.assertEqual(page["summary"]["current"], 0)
        self.assertEqual(page["summary"]["historical"], 1)
        item = page["items"][0]
        self.assertEqual(item["lifecycle_label"], "历史错误")
        self.assertEqual(item["error"]["code"], "create_turn_failed")
        self.assertIn("首个 Turn 执行失败", item["message"])

    def test_gateway_details_are_exactly_whitelisted_and_controller_fallback_is_not_gateway(self) -> None:
        page = self.center.page(cursor=0, limit=12)
        gateway = next(item for item in page["items"] if item["origin"] == "wechat")
        self.assertEqual(gateway["message"], "上游响应流中断，自动重试后仍未完成。请稍后再试。")
        self.assertEqual(gateway["recommended_action"], "检查网络和 Runner 在线状态，恢复后重试。")

        error = self.gateway_payload["result"]["items"][0]["error"]
        error["code"] = "turn_failed"
        error["message"] = "private token=secret\nnext line"
        error["recommended_action"] = "visit https://private.invalid"
        self.center._receive_gateway_frame(  # type: ignore[attr-defined]
            {"version": 1, "revision": 3, "summary": {"count": 1, "latest_occurred_at": NOW.isoformat()}}
        )
        changed = next(item for item in self.center.page(cursor=0, limit=12)["items"] if item["origin"] == "wechat")
        self.assertEqual(changed["error"]["code"], "turn_failed")
        self.assertEqual(changed["error"]["label"], "Codex 任务执行失败")
        self.assertNotIn("message", changed)
        self.assertNotIn("recommended_action", changed)
        encoded = json.dumps(changed, ensure_ascii=False)
        self.assertNotIn("secret", encoded)
        self.assertNotIn("private.invalid", encoded)

        error["message"] = "任务未完成，系统保留了可用于定位问题的稳定错误码。"
        error["recommended_action"] = "在 Codex Controller 错误中心按错误码检查 Runner、登录和上游服务状态。"
        self.center._receive_gateway_frame(  # type: ignore[attr-defined]
            {"version": 1, "revision": 4, "summary": {"count": 1, "latest_occurred_at": NOW.isoformat()}}
        )
        whitelisted = next(item for item in self.center.page(cursor=0, limit=12)["items"] if item["origin"] == "wechat")
        self.assertEqual(whitelisted["error"]["code"], "turn_failed")
        self.assertIn("稳定错误码", whitelisted["message"])
        self.assertIn("错误中心", whitelisted["recommended_action"])

    def test_current_upstream_failure_keeps_stable_code_and_public_diagnosis(self) -> None:
        item = self.gateway_payload["result"]["items"][0]
        item["error"] = {
            "code": "response_too_many_failed_attempts",
            "message": "上游连续失败，自动重试后仍未完成。请稍后再试。",
            "recommended_action": "检查网络、Runner 和上游服务状态后重试。",
        }
        self.center._receive_gateway_frame(  # type: ignore[attr-defined]
            {
                "version": 1,
                "revision": 5,
                "summary": {"count": 1, "latest_occurred_at": NOW.isoformat()},
            }
        )

        failure = next(
            item
            for item in self.center.page(cursor=0, limit=12)["items"]
            if item["origin"] == "wechat"
        )
        self.assertEqual(
            failure["error"]["code"],
            "response_too_many_failed_attempts",
        )
        self.assertEqual(failure["error"]["label"], "上游连续失败")
        self.assertEqual(
            failure["message"],
            "上游连续失败，自动重试后仍未完成。请稍后再试。",
        )
        self.assertEqual(
            failure["recommended_action"],
            "检查网络、Runner 和上游服务状态后重试。",
        )

    def test_task_scan_reaches_bounded_second_page(self) -> None:
        alphabet = "ABCDEFGHJKMNPQRSTUVWXYZ234567"

        def ref(index: int) -> str:
            return "TH-" + "A" * 18 + alphabet[index // len(alphabet)] + alphabet[index % len(alphabet)]

        class PagedDesktop:
            def threads(self, *, after_cursor, limit, **_kwargs):
                values = [
                    {
                        "thread_ref": ref(index),
                        "status": "failed" if index == 200 else "idle",
                        "updated_at": NOW.isoformat(),
                    }
                    for index in range(201)
                ]
                page = values[after_cursor:after_cursor + limit]
                return {
                    "threads": page,
                    "next_cursor": after_cursor + len(page),
                    "has_more": after_cursor + len(page) < len(values),
                }

        center = ErrorCenter(desktop_controller=PagedDesktop(), component_status=lambda: {})
        page = center.page(cursor=0, limit=12)
        self.assertEqual(page["summary"]["tasks"], 1)
        self.assertEqual(page["items"][0]["reference"], ref(200))

    def test_gateway_failure_read_failure_is_a_stable_component_error(self) -> None:
        def unavailable(_request, timeout):
            raise HTTPError("http://weixin-gateway:8103", 503, "private detail", {}, io.BytesIO())

        self.center.opener = unavailable
        self.center._set_gateway_sync_error()  # type: ignore[attr-defined]
        page = self.center.page(cursor=0, limit=12)
        encoded = json.dumps(page, ensure_ascii=False)
        self.assertIn("gateway_failure_sync_unavailable", encoded)
        self.assertNotIn("private detail", encoded)
        self.assertNotIn("weixin-gateway:8103", encoded)

    def test_unknown_upstream_identifier_falls_back_without_echoing_it(self) -> None:
        self.gateway_payload["result"]["items"][0]["error"]["code"] = "private_token_from_upstream"
        self.center._receive_gateway_frame(  # type: ignore[attr-defined]
            {"version": 1, "revision": 2, "summary": {"count": 1, "latest_occurred_at": NOW.isoformat()}}
        )
        page = self.center.page(cursor=0, limit=12)
        encoded = json.dumps(page, ensure_ascii=False)
        self.assertIn("controller_task_failed", encoded)
        self.assertNotIn("private_token_from_upstream", encoded)

    def test_sse_revision_changes_without_sending_full_error_items(self) -> None:
        stream = self.center.stream(heartbeat_seconds=10)
        ready = next(stream)
        self.desktop.status = "recovery_required"
        self.center.notify_local_change()
        changed = next(stream)
        stream.close()
        self.assertEqual(ready["event"], "ready")
        self.assertEqual(changed["event"], "errors")
        self.assertNotEqual(ready["data"]["error_revision"], changed["data"]["error_revision"])
        self.assertNotIn("items", changed["data"])

    def test_gateway_revision_fetches_details_once_and_component_change_wakes_immediately(self) -> None:
        calls: list[str] = []
        component = {"runner_error": None}
        gateway = {
            "version": 1,
            "result": {"items": []},
        }

        def opener(request, timeout):
            calls.append(request.full_url)
            return _Response(gateway)

        center = ErrorCenter(
            desktop_controller=None,
            component_status=lambda: component,
            gateway_base_url="http://weixin-gateway:8103",
            gateway_token="a" * 32,
            opener=opener,
        )
        frame = {"version": 1, "revision": 7, "summary": {"count": 0, "latest_occurred_at": None}}
        center._receive_gateway_frame(frame)  # type: ignore[attr-defined]
        center._receive_gateway_frame(frame)  # type: ignore[attr-defined]
        self.assertEqual(calls, ["http://weixin-gateway:8103/internal/v1/failures"])
        stream = center.stream(heartbeat_seconds=10)
        ready = next(stream)
        component["runner_error"] = "relay_publish_indeterminate"
        center.notify_local_change()
        changed = next(stream)
        stream.close()
        self.assertEqual(changed["event"], "errors")
        self.assertNotEqual(ready["data"]["error_revision"], changed["data"]["error_revision"])

    def test_http_api_and_stream_are_safe_and_incremental(self) -> None:
        server = create_server(
            "127.0.0.1",
            0,
            service=_Controller(self.center),  # type: ignore[arg-type]
            api_token="x" * 32,
            max_request_bytes=1024,
        )
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        connection = HTTPConnection("127.0.0.1", server.server_port, timeout=3)
        try:
            connection.request("GET", "/api/errors?limit=1")
            response = connection.getresponse()
            document = json.loads(response.read())
            self.assertEqual(response.status, 200)
            self.assertEqual(len(document["result"]["items"]), 1)
            self.assertTrue(document["result"]["page"]["has_more"])

            connection.close()
            connection = HTTPConnection("127.0.0.1", server.server_port, timeout=3)
            connection.request("GET", "/api/errors/stream")
            response = connection.getresponse()
            self.assertEqual(response.status, 200)
            lines = [response.fp.readline().decode().strip() for _ in range(3)]
            self.assertEqual(lines[1], "event: ready")
            frame = json.loads(lines[2].removeprefix("data: "))
            self.assertIn("error_revision", frame)
            self.assertNotIn("items", frame)
        finally:
            connection.close()
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)

    def test_management_ui_uses_error_sse_and_not_gateway_management_api(self) -> None:
        combined = DESKTOP_DASHBOARD_HTML + DESKTOP_DASHBOARD_JS
        root_ui = __import__("codex_controller.api", fromlist=["DASHBOARD_HTML"]).DASHBOARD_HTML
        script = __import__("codex_controller.runner_dashboard", fromlist=["DASHBOARD_JS"]).DASHBOARD_JS
        self.assertIn("errorSummaryList", root_ui)
        self.assertIn("id=\"errors\"", root_ui)
        self.assertIn("api/errors/stream", script)
        self.assertIn("error_revision", script)
        self.assertNotIn("/api/failures", combined + root_ui + script)
        self.assertNotIn("/internal/v1/failures", script)


if __name__ == "__main__":
    unittest.main()
