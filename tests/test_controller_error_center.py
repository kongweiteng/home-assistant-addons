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
        self.assertEqual(changed["error"]["code"], "controller_task_failed")
        self.assertNotIn("message", changed)
        self.assertNotIn("recommended_action", changed)
        encoded = json.dumps(changed, ensure_ascii=False)
        self.assertNotIn("secret", encoded)
        self.assertNotIn("private.invalid", encoded)

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
