from __future__ import annotations

import base64
import datetime as dt
from pathlib import Path
import tempfile
import unittest

from codex_controller.desktop_api import post_desktop_api
from codex_controller.desktop_dashboard import DESKTOP_DASHBOARD_HTML, DESKTOP_DASHBOARD_JS
from codex_controller.desktop_protocol import (
    DesktopProtocolError,
    body_digest,
    build_desktop_command,
    validate_desktop_document,
)
from codex_controller.desktop_service import DesktopControllerService
from codex_controller.desktop_store import DesktopStore
from codex_controller.store import StoreError


NOW = dt.datetime(2026, 9, 6, 15, 0, tzinfo=dt.timezone(dt.timedelta(hours=8)))
RUNNER_ID = "RN-" + "A" * 20
HOST_REF = "HS-" + "B" * 20
PROJECT_REF = "PJ-" + "C" * 20
THREAD_REF = "TH-" + "D" * 20
FILE_REF = "FL-" + "E" * 26
ARTIFACT_REF = "AR-" + "F" * 26
DIRECTORY_CURSOR = "FD-" + "G" * 26


def digest(document: dict) -> dict:
    result = dict(document)
    result["body_digest"] = body_digest(result)
    return result


def snapshot() -> dict:
    return digest(
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
                "title": "文件检查",
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
                "schema_digest": "a" * 64,
                "socket_mode": "0600",
                "tcp_listener_count": 0,
                "capabilities": ["list_read", "file_browse_v1", "artifact_read_v1", "fixed_diagnostics_v1"],
                "control_enabled": True,
                "models": [],
                "synced_at": NOW.isoformat(),
            },
        }
    )


def resource_event(kind: str, payload: dict, *, sequence: int = 1) -> dict:
    return digest(
        {
            "version": 1,
            "message_type": "desktop_event",
            "runner_id": RUNNER_ID,
            "created_at": NOW.isoformat(),
            "host_ref": HOST_REF,
            "project_ref": PROJECT_REF,
            "thread_ref": THREAD_REF,
            "turn_ref": None,
            "thread_revision": 9,
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


class DesktopResourceTests(unittest.TestCase):
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

    def test_file_list_is_thread_bound_and_durable_idempotent(self) -> None:
        payload = {
            "request_id": "files-1",
            "thread_revision": 9,
            "relative_path": "src/components",
            "limit": 40,
        }
        first = post_desktop_api(
            self.service,
            f"/api/desktop/v1/threads/{THREAD_REF}/files/list",
            payload,
        )
        replay = post_desktop_api(
            self.service,
            f"/api/desktop/v1/threads/{THREAD_REF}/files/list",
            payload,
        )
        self.assertEqual(first["state"], "submitted")
        self.assertEqual(replay["state"], "submitted")
        self.assertEqual(len(self.publisher.commands), 1)
        command = self.publisher.commands[0]
        self.assertEqual(command["project_ref"], PROJECT_REF)
        self.assertEqual(command["relative_path"], "src/components")
        self.assertEqual(command["expected_control_revision"], None)
        self.assertNotIn("path", command)
        self.assertNotIn("thread_id", command)

        conflicting = dict(payload, relative_path="tests")
        with self.assertRaises(StoreError) as context:
            self.service.resource_query(THREAD_REF, "file_list", conflicting)
        self.assertEqual(context.exception.code, "desktop_request_conflict")
        self.assertEqual(len(self.publisher.commands), 1)

    def test_file_events_are_strict_bounded_and_complete_the_request(self) -> None:
        self.service.resource_query(
            THREAD_REF,
            "file_list",
            {"request_id": "files-2", "thread_revision": 9, "relative_path": "", "limit": 100},
        )
        event = resource_event(
            "file.list",
            {
                "request_id": "files-2",
                "project_ref": PROJECT_REF,
                "relative_path": "",
                "entries": [
                    {"name": "src", "kind": "directory", "readable": True},
                    {
                        "name": "README.md",
                        "kind": "file",
                        "size": 120,
                        "modified_at_ms": 1_700_000_000_000,
                        "readable": True,
                        "ref": FILE_REF,
                        "mime_type": "text/markdown",
                        "ref_kind": "file",
                    },
                ],
                "next_cursor": DIRECTORY_CURSOR,
                "has_more": True,
            },
        )
        accepted = self.service.receive("desktop_event", event)
        self.assertTrue(accepted["accepted"])
        self.assertEqual(self.store.command("files-2")["state"], "confirmed")
        streamed = self.service.events(THREAD_REF, after_cursor=0, limit=10, wait_seconds=0)
        self.assertEqual(streamed["events"][0]["event_kind"], "file.list")

        oversized = resource_event(
            "file.list",
            {**event["payload"], "request_id": "files-3", "entries": event["payload"]["entries"] * 51},
            sequence=2,
        )
        with self.assertRaises(DesktopProtocolError) as context:
            validate_desktop_document("desktop_event", oversized)
        self.assertEqual(context.exception.code, "desktop_event_invalid")

        self.service.resource_query(
            THREAD_REF,
            "file_list",
            {"request_id": "files-bound", "thread_revision": 9, "relative_path": "", "limit": 100},
        )
        wrong_path = resource_event(
            "file.list",
            {
                **event["payload"],
                "request_id": "files-bound",
                "relative_path": "src",
            },
            sequence=3,
        )
        with self.assertRaises(StoreError) as binding_context:
            self.service.receive("desktop_event", wrong_path)
        self.assertEqual(binding_context.exception.code, "desktop_identity_mismatch")

    def test_text_and_artifact_chunks_enforce_distinct_caps(self) -> None:
        text_event = resource_event(
            "file.chunk",
            {
                "request_id": "read-text",
                "ref": FILE_REF,
                "mime_type": "text/plain",
                "offset": 0,
                "chunk_bytes": 5,
                "next_offset": None,
                "eof": True,
                "encoding": "utf-8",
                "text": "hello",
            },
        )
        self.assertEqual(validate_desktop_document("desktop_event", text_event)["event_kind"], "file.chunk")

        data = b"artifact"
        artifact_event = resource_event(
            "file.chunk",
            {
                "request_id": "read-artifact",
                "ref": ARTIFACT_REF,
                "mime_type": "application/pdf",
                "offset": 0,
                "chunk_bytes": len(data),
                "next_offset": None,
                "eof": True,
                "data_base64": base64.b64encode(data).decode("ascii"),
            },
        )
        self.assertEqual(validate_desktop_document("desktop_event", artifact_event)["event_kind"], "file.chunk")

        invalid = dict(artifact_event)
        invalid["payload"] = {**artifact_event["payload"], "data_base64": "not base64!"}
        invalid["body_digest"] = body_digest(invalid)
        with self.assertRaises(DesktopProtocolError):
            validate_desktop_document("desktop_event", invalid)

    def test_fixed_diagnostics_reject_shell_and_emit_only_allowlisted_id(self) -> None:
        result = self.service.resource_query(
            THREAD_REF,
            "diagnostic_run",
            {"request_id": "diagnostic-1", "thread_revision": 9, "diagnostic_id": "git_diff_check"},
        )
        self.assertEqual(result["state"], "submitted")
        self.assertEqual(self.publisher.commands[-1]["diagnostic_id"], "git_diff_check")
        self.assertNotIn("command", self.publisher.commands[-1])
        with self.assertRaises(StoreError) as context:
            self.service.resource_query(
                THREAD_REF,
                "diagnostic_run",
                {"request_id": "diagnostic-2", "thread_revision": 9, "diagnostic_id": "shell"},
            )
        self.assertEqual(context.exception.code, "desktop_diagnostic_not_allowed")

    def test_command_protocol_rejects_absolute_paths_and_unbounded_reads(self) -> None:
        with self.assertRaises(DesktopProtocolError):
            build_desktop_command(
                runner_id=RUNNER_ID,
                request_id="file-command-1",
                host_ref=HOST_REF,
                project_ref=PROJECT_REF,
                thread_ref=THREAD_REF,
                expected_thread_revision=9,
                expected_control_revision=None,
                action="file_open",
                relative_path="/private/project/secret.txt",
                file_kind="file",
                now=NOW,
            )
        with self.assertRaises(DesktopProtocolError) as protocol_context:
            build_desktop_command(
                runner_id=RUNNER_ID,
                request_id="file-command-text-limit",
                host_ref=HOST_REF,
                project_ref=PROJECT_REF,
                thread_ref=THREAD_REF,
                expected_thread_revision=9,
                expected_control_revision=None,
                action="file_read",
                file_ref=FILE_REF,
                offset=0,
                limit=32 * 1024 + 1,
                now=NOW,
            )
        self.assertEqual(protocol_context.exception.code, "desktop_file_limit_invalid")
        with self.assertRaises(StoreError) as context:
            self.service.resource_query(
                THREAD_REF,
                "file_read",
                {
                    "request_id": "file-command-2",
                    "thread_revision": 9,
                    "file_ref": FILE_REF,
                    "offset": 0,
                    "limit": 32 * 1024 + 1,
                },
            )
        self.assertEqual(context.exception.code, "desktop_file_limit_invalid")

    def test_dashboard_has_mobile_resource_drawer_and_async_event_rendering(self) -> None:
        self.assertIn('id="resourceButton"', DESKTOP_DASHBOARD_HTML)
        self.assertIn('id="resourceSheet"', DESKTOP_DASHBOARD_HTML)
        self.assertIn("file.list", DESKTOP_DASHBOARD_JS)
        self.assertIn("diagnostic.result", DESKTOP_DASHBOARD_JS)
        self.assertIn("files/read", DESKTOP_DASHBOARD_JS)
        self.assertIn("min-height:44px", DESKTOP_DASHBOARD_HTML)


if __name__ == "__main__":
    unittest.main()
