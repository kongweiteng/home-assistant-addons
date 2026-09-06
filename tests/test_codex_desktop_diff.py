from __future__ import annotations

import datetime as dt
from pathlib import Path
import tempfile
import unittest

from codex_controller.desktop_api import post_desktop_api
from codex_controller.desktop_dashboard import DESKTOP_DASHBOARD_HTML, DESKTOP_DASHBOARD_JS
from codex_controller.desktop_protocol import DesktopProtocolError, body_digest, validate_desktop_document
from codex_controller.desktop_service import DesktopControllerService
from codex_controller.desktop_store import DesktopStore
from codex_controller.store import StoreError


NOW = dt.datetime(2026, 9, 6, 18, 0, tzinfo=dt.timezone(dt.timedelta(hours=8)))
RUNNER_ID = "RN-" + "A" * 20
HOST_REF = "HS-" + "B" * 20
PROJECT_REF = "PJ-" + "C" * 20
THREAD_REF = "TH-" + "D" * 20
DIFF_REF = "DF-" + "E" * 26
DIFF_CURSOR = "DC-" + "F" * 26
DIGEST = "sha256:" + "a" * 64


def signed(document: dict) -> dict:
    result = dict(document)
    result["body_digest"] = body_digest(result)
    return result


def snapshot() -> dict:
    return signed(
        {
            "version": 1,
            "message_type": "desktop_snapshot",
            "runner_id": RUNNER_ID,
            "created_at": NOW.isoformat(),
            "host_ref": HOST_REF,
            "project_ref": PROJECT_REF,
            "thread_ref": THREAD_REF,
            "thread_revision": 9,
            "snapshot": {
                "project_alias": "demo-project",
                "project_ref": PROJECT_REF,
                "thread_ref": THREAD_REF,
                "title": "Diff 检查",
                "preview": "公开摘要",
                "status": "idle",
                "active_turn_ref": None,
                "thread_revision": 9,
                "control_revision": 9,
                "created_at": NOW.isoformat(),
                "updated_at": NOW.isoformat(),
                "history_incomplete": False,
                "turns": [],
                "control_state": "ready",
            },
            "host": {
                "host_ref": HOST_REF,
                "state": "normal",
                "app_version": "26.900.1",
                "app_build": "7000",
                "cli_version": "0.153.0",
                "schema_digest": "b" * 64,
                "socket_mode": "0600",
                "tcp_listener_count": 0,
                "capabilities": ["list_read", "git_diff_v1"],
                "control_enabled": True,
                "models": [],
                "synced_at": NOW.isoformat(),
            },
        }
    )


def event(kind: str, payload: dict, *, revision: int = 9, sequence: int = 1) -> dict:
    return signed(
        {
            "version": 1,
            "message_type": "desktop_event",
            "runner_id": RUNNER_ID,
            "created_at": NOW.isoformat(),
            "host_ref": HOST_REF,
            "project_ref": PROJECT_REF,
            "thread_ref": THREAD_REF,
            "turn_ref": None,
            "thread_revision": revision,
            "event_sequence": sequence,
            "event_kind": kind,
            "source": "app",
            "payload": payload,
        }
    )


class Publisher:
    def __init__(self) -> None:
        self.commands: list[dict] = []

    def publish_desktop_command(self, _runner_id: str, document: dict) -> None:
        self.commands.append(dict(document))


class DesktopDiffControllerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.store = DesktopStore(Path(self.temporary.name) / "desktop.sqlite3")
        self.publisher = Publisher()
        self.service = DesktopControllerService(
            self.store,
            publisher=self.publisher,
            now=lambda: NOW,
            runner_authorizer=lambda _runner_id: True,
        )
        self.service.receive("desktop_snapshot", snapshot())

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_summary_and_chunk_are_async_thread_bound_resources(self) -> None:
        result = post_desktop_api(
            self.service,
            f"/api/desktop/v1/threads/{THREAD_REF}/diff/summary",
            {"request_id": "diff-summary-1", "thread_revision": 9, "limit": 100},
        )
        self.assertEqual(result["state"], "submitted")
        command = self.publisher.commands[-1]
        self.assertEqual(command["action"], "diff_summary")
        self.assertEqual(command["project_ref"], PROJECT_REF)
        self.assertIsNone(command["expected_control_revision"])
        self.assertNotIn("path", str(command))

        summary = event(
            "diff.summary",
            {
                "request_id": "diff-summary-1",
                "project_ref": PROJECT_REF,
                "workspace_digest": DIGEST,
                "files": [
                    {
                        "diff_ref": DIFF_REF,
                        "relative_path": "src/app.py",
                        "status": "modified",
                        "added_lines": 4,
                        "deleted_lines": 2,
                        "binary": False,
                    }
                ],
                "file_count": 1,
                "has_more": False,
                "limit": 100,
            },
        )
        self.service.receive("desktop_event", summary)
        self.assertEqual(self.store.command("diff-summary-1")["state"], "confirmed")

        post_desktop_api(
            self.service,
            f"/api/desktop/v1/threads/{THREAD_REF}/diff/read",
            {
                "request_id": "diff-read-1",
                "thread_revision": 9,
                "diff_ref": DIFF_REF,
                "limit": 32 * 1024,
            },
        )
        chunk = event(
            "diff.chunk",
            {
                "request_id": "diff-read-1",
                "project_ref": PROJECT_REF,
                "diff_ref": DIFF_REF,
                "relative_path": "src/app.py",
                "status": "modified",
                "diff_digest": DIGEST,
                "content": "@@ -1 +1 @@\n-old\n+new\n",
                "next_cursor": DIFF_CURSOR,
                "complete": False,
            },
            sequence=2,
        )
        self.service.receive("desktop_event", chunk)
        self.assertEqual(self.store.command("diff-read-1")["state"], "confirmed")

    def test_diff_binding_and_protocol_are_strict(self) -> None:
        self.service.resource_query(
            THREAD_REF,
            "diff_read",
            {
                "request_id": "diff-bound-1",
                "thread_revision": 9,
                "diff_ref": DIFF_REF,
                "limit": 1024,
            },
        )
        wrong_ref = event(
            "diff.chunk",
            {
                "request_id": "diff-bound-1",
                "project_ref": PROJECT_REF,
                "diff_ref": "DF-" + "G" * 26,
                "relative_path": "src/app.py",
                "status": "modified",
                "diff_digest": DIGEST,
                "content": "patch",
                "next_cursor": None,
                "complete": True,
            },
        )
        with self.assertRaises(StoreError) as context:
            self.service.receive("desktop_event", wrong_ref)
        self.assertEqual(context.exception.code, "desktop_identity_mismatch")

        wrong_revision = event("diff.error", {"request_id": "diff-bound-1", "action": "diff_read", "error_code": "diff_revision_stale"}, revision=8)
        with self.assertRaises(StoreError) as revision_context:
            self.service.receive("desktop_event", wrong_revision)
        self.assertEqual(revision_context.exception.code, "desktop_identity_mismatch")

        invalid = dict(wrong_ref)
        invalid["payload"] = {**wrong_ref["payload"], "next_cursor": "native-cursor", "complete": False}
        invalid["body_digest"] = body_digest(invalid)
        with self.assertRaises(DesktopProtocolError):
            validate_desktop_document("desktop_event", invalid)

    def test_mobile_ui_loads_summary_then_diff_pages(self) -> None:
        combined = DESKTOP_DASHBOARD_HTML + DESKTOP_DASHBOARD_JS
        for value in (
            'id="resourceDiffTab"',
            "git_diff_v1",
            "diff/summary",
            "diff/read",
            "requestDiffSummary",
            "requestDiffChunk",
            "每次最多加载 32 KiB",
        ):
            self.assertIn(value, combined)
        self.assertNotIn("innerHTML", DESKTOP_DASHBOARD_JS)


if __name__ == "__main__":
    unittest.main()
