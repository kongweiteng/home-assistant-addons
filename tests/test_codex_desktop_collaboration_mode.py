from __future__ import annotations

import datetime as dt
from pathlib import Path
import tempfile
import unittest

from codex_controller.desktop_api import get_desktop_api, post_desktop_api
from codex_controller.desktop_dashboard import DESKTOP_DASHBOARD_HTML, DESKTOP_DASHBOARD_JS
from codex_controller.desktop_protocol import DesktopProtocolError, body_digest, validate_desktop_document
from codex_controller.desktop_service import DesktopControllerService
from codex_controller.desktop_store import DesktopStore
from codex_controller.store import StoreError


NOW = dt.datetime(2026, 9, 6, 15, 0, tzinfo=dt.timezone(dt.timedelta(hours=8)))
RUNNER_ID = "RN-" + "E" * 20
HOST_REF = "HS-" + "A" * 20
PROJECT_REF = "PJ-" + "B" * 20
THREAD_REF = "TH-" + "C" * 20
TURN_REF = "TR-" + "D" * 20
MODES = [
    {"id": "default", "label": "默认", "mode": "default"},
    {"id": "plan", "label": "计划", "mode": "plan"},
]
MODE_CAPABILITIES = [
    "create_thread_v1",
    "collaboration_mode_catalog_v1",
    "collaboration_mode_turn_v1",
    "thread_collaboration_mode_update_v1",
]


class Publisher:
    def __init__(self) -> None:
        self.commands: list[dict] = []
        self.error: Exception | None = None

    def publish_desktop_command(self, _runner_id: str, document: dict) -> None:
        self.commands.append(dict(document))
        if self.error is not None:
            raise self.error


def snapshot(
    *,
    status: str = "idle",
    sequence: int = 1,
    capabilities: list[str] | None = None,
    current: dict | None = None,
    modes: list[dict] | None = None,
) -> dict:
    active_turn = TURN_REF if status == "active" else None
    document = {
        "version": 1,
        "message_type": "desktop_snapshot",
        "runner_id": RUNNER_ID,
        "created_at": NOW.isoformat(),
        "host_ref": HOST_REF,
        "project_ref": PROJECT_REF,
        "thread_ref": THREAD_REF,
        "thread_revision": 7,
        "snapshot_sequence": sequence,
        "snapshot": {
            "project_alias": "demo-project",
            "project_ref": PROJECT_REF,
            "thread_ref": THREAD_REF,
            "title": "计划模式测试",
            "preview": "公开摘要",
            "status": status,
            "active_turn_ref": active_turn,
            "thread_revision": 7,
            "control_revision": 9,
            "created_at": NOW.isoformat(),
            "updated_at": NOW.isoformat(),
            "history_incomplete": False,
            "turns": [],
            "control_state": "ready",
            "collaboration_mode": current
            or {
                "id": None,
                "label": None,
                "mode": None,
                "editable": True,
                "reason": "current_mode_unavailable",
            },
        },
        "host": {
            "host_ref": HOST_REF,
            "state": "normal",
            "app_version": "26.900.1",
            "app_build": "7000",
            "cli_version": "0.153.4",
            "schema_digest": "a" * 64,
            "socket_mode": "0600",
            "tcp_listener_count": 0,
            "capabilities": list(capabilities if capabilities is not None else MODE_CAPABILITIES),
            "control_enabled": True,
            "models": [],
            "collaboration_modes": [dict(item) for item in (MODES if modes is None else modes)],
            "synced_at": NOW.isoformat(),
        },
    }
    document["body_digest"] = body_digest(document)
    return document


class CollaborationModeTests(unittest.TestCase):
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

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_catalog_and_current_state_are_strict_and_private(self) -> None:
        validated = validate_desktop_document("desktop_snapshot", snapshot())
        self.assertEqual(validated["host"]["collaboration_modes"], MODES)
        self.assertEqual(
            set(validated["snapshot"]["collaboration_mode"]),
            {"id", "label", "mode", "editable", "reason"},
        )
        for field in ("settings", "developer_instructions"):
            invalid = snapshot()
            invalid["host"]["collaboration_modes"][0][field] = {}
            invalid["body_digest"] = body_digest(invalid)
            with self.assertRaises(DesktopProtocolError):
                validate_desktop_document("desktop_snapshot", invalid)

    def test_capabilities_are_independent_but_require_catalog_contract(self) -> None:
        for capability in ("collaboration_mode_turn_v1", "thread_collaboration_mode_update_v1"):
            invalid = snapshot(capabilities=[capability])
            invalid["host"].pop("collaboration_modes")
            invalid["snapshot"].pop("collaboration_mode")
            invalid["body_digest"] = body_digest(invalid)
            with self.assertRaises(DesktopProtocolError):
                validate_desktop_document("desktop_snapshot", invalid)

        catalog_only = snapshot(capabilities=["collaboration_mode_catalog_v1"])
        validate_desktop_document("desktop_snapshot", catalog_only)

    def test_turn_mode_is_forwarded_for_create_continue_and_safe_steer(self) -> None:
        self.service.receive("desktop_snapshot", snapshot())
        created = self.service.create(
            {
                "request_id": "mode-create-1",
                "host_ref": HOST_REF,
                "project_ref": PROJECT_REF,
                "input": "新建任务",
                "collaboration_mode_id": "plan",
            }
        )
        self.assertEqual(created["state"], "submitted")
        self.assertEqual(self.publisher.commands[-1]["collaboration_mode_id"], "plan")

        other = DesktopStore(Path(self.temporary.name) / "continue.sqlite3")
        publisher = Publisher()
        service = DesktopControllerService(
            other,
            publisher=publisher,
            now=lambda: NOW,
            runner_authorizer=lambda _runner_id: True,
        )
        service.receive(
            "desktop_snapshot",
            snapshot(capabilities=MODE_CAPABILITIES + ["continue_same_thread"]),
        )
        service.submit(
            THREAD_REF,
            "continue",
            {
                "request_id": "mode-continue-1",
                "thread_revision": 7,
                "input": "继续",
                "collaboration_mode_id": "plan",
            },
        )
        self.assertEqual(publisher.commands[-1]["collaboration_mode_id"], "plan")

        active_store = DesktopStore(Path(self.temporary.name) / "steer.sqlite3")
        active_publisher = Publisher()
        active_service = DesktopControllerService(
            active_store,
            publisher=active_publisher,
            now=lambda: NOW,
            runner_authorizer=lambda _runner_id: True,
        )
        active_service.receive(
            "desktop_snapshot",
            snapshot(
                status="active",
                capabilities=MODE_CAPABILITIES
                + ["interrupt_expected_turn", "continue_same_thread", "native_steer_racy"],
            ),
        )
        active_service.submit(
            THREAD_REF,
            "steer",
            {
                "request_id": "mode-steer-1",
                "thread_revision": 7,
                "expected_turn_ref": TURN_REF,
                "input": "安全调整",
                "mode": "safe",
                "collaboration_mode_id": "plan",
            },
        )
        self.assertEqual(active_publisher.commands[-1]["collaboration_mode_id"], "plan")
        with self.assertRaises(StoreError) as context:
            active_service.submit(
                THREAD_REF,
                "steer",
                {
                    "request_id": "mode-native-1",
                    "thread_revision": 7,
                    "expected_turn_ref": TURN_REF,
                    "input": "快速调整",
                    "mode": "native",
                    "collaboration_mode_id": "plan",
                },
            )
        self.assertEqual(context.exception.code, "desktop_collaboration_mode_invalid")

    def test_thread_settings_and_update_are_cas_gated(self) -> None:
        self.service.receive("desktop_snapshot", snapshot())
        settings = get_desktop_api(
            self.service,
            f"/api/desktop/v1/threads/{THREAD_REF}/settings",
            "",
        )
        self.assertEqual(settings["catalog"], MODES)
        self.assertEqual(settings["current"]["reason"], "current_mode_unavailable")
        self.assertTrue(settings["features"]["update"]["available"])

        result = post_desktop_api(
            self.service,
            f"/api/desktop/v1/threads/{THREAD_REF}/collaboration-mode",
            {
                "request_id": "mode-update-1",
                "thread_revision": 7,
                "collaboration_mode_id": "plan",
            },
        )
        self.assertEqual(result["state"], "submitted")
        command = self.publisher.commands[-1]
        self.assertEqual(command["action"], "collaboration_mode_update")
        self.assertEqual(command["project_ref"], PROJECT_REF)
        self.assertEqual(command["expected_control_revision"], 9)
        self.assertEqual(command["collaboration_mode_id"], "plan")
        self.assertEqual(self.store.command("mode-update-1")["collaboration_mode_id"], "plan")

        receipt = {
            "version": 1,
            "message_type": "desktop_receipt",
            "runner_id": RUNNER_ID,
            "created_at": NOW.isoformat(),
            "request_id": "mode-update-1",
            "host_ref": HOST_REF,
            "project_ref": PROJECT_REF,
            "thread_ref": THREAD_REF,
            "turn_ref": None,
            "action": "collaboration_mode_update",
            "state": "confirmed",
            "thread_revision": 8,
            "collaboration_mode_id": "plan",
        }
        receipt["body_digest"] = body_digest(receipt)
        accepted = self.service.receive("desktop_receipt", receipt)
        self.assertEqual(accepted["command"]["state"], "confirmed")

        stale_store = DesktopStore(Path(self.temporary.name) / "stale.sqlite3")
        stale_service = DesktopControllerService(
            stale_store,
            publisher=Publisher(),
            now=lambda: NOW,
            runner_authorizer=lambda _runner_id: True,
        )
        stale_service.receive("desktop_snapshot", snapshot())
        with self.assertRaises(StoreError) as context:
            post_desktop_api(
                stale_service,
                f"/api/desktop/v1/threads/{THREAD_REF}/collaboration-mode",
                {
                    "request_id": "mode-update-stale",
                    "thread_revision": 6,
                    "collaboration_mode_id": "plan",
                },
            )
        self.assertEqual(context.exception.code, "desktop_thread_revision_stale")

    def test_unknown_update_is_not_automatically_replayed(self) -> None:
        self.service.receive("desktop_snapshot", snapshot())
        self.publisher.error = RuntimeError("indeterminate")
        payload = {
            "request_id": "mode-update-unknown",
            "thread_revision": 7,
            "collaboration_mode_id": "plan",
        }
        first = self.service.submit(THREAD_REF, "collaboration_mode_update", payload)
        second = self.service.submit(THREAD_REF, "collaboration_mode_update", payload)
        self.assertEqual(first["state"], "unknown")
        self.assertEqual(second["state"], "unknown")
        self.assertEqual(len(self.publisher.commands), 1)

    def test_update_receipt_must_match_project_and_mode_binding(self) -> None:
        self.service.receive("desktop_snapshot", snapshot())
        self.service.submit(
            THREAD_REF,
            "collaboration_mode_update",
            {
                "request_id": "mode-update-binding",
                "thread_revision": 7,
                "collaboration_mode_id": "plan",
            },
        )
        receipt = {
            "version": 1,
            "message_type": "desktop_receipt",
            "runner_id": RUNNER_ID,
            "created_at": NOW.isoformat(),
            "request_id": "mode-update-binding",
            "host_ref": HOST_REF,
            "project_ref": PROJECT_REF,
            "thread_ref": THREAD_REF,
            "turn_ref": None,
            "action": "collaboration_mode_update",
            "state": "confirmed",
            "thread_revision": 8,
            "collaboration_mode_id": "default",
        }
        receipt["body_digest"] = body_digest(receipt)
        with self.assertRaises(StoreError) as context:
            self.service.receive("desktop_receipt", receipt)
        self.assertEqual(context.exception.code, "desktop_receipt_binding_conflict")

    def test_same_revision_higher_snapshot_sequence_refreshes_current_mode(self) -> None:
        self.service.receive("desktop_snapshot", snapshot(sequence=1))
        known = {
            "id": "plan",
            "label": "计划",
            "mode": "plan",
            "editable": True,
            "reason": None,
        }
        refreshed = self.service.receive(
            "desktop_snapshot",
            snapshot(sequence=2, current=known),
        )
        self.assertEqual(refreshed["status"], "refreshed")
        self.assertEqual(self.service.thread_settings(THREAD_REF)["current"], known)

    def test_mobile_ui_exposes_turn_and_task_mode_without_sensitive_fields(self) -> None:
        for target in ("taskSettingsButton", "taskSettingsSheet", "taskCollaborationMode"):
            self.assertIn(f'id="{target}"', DESKTOP_DASHBOARD_HTML)
        for target in ("collaborationModeSelect", "newTaskCollaborationMode"):
            self.assertIn(target, DESKTOP_DASHBOARD_JS)
        self.assertIn("min-height:44px", DESKTOP_DASHBOARD_HTML)
        self.assertIn("max-width:759px", DESKTOP_DASHBOARD_HTML)
        self.assertIn("快速调整保持当前 Turn，不能切换计划模式", DESKTOP_DASHBOARD_JS)
        self.assertNotIn("developer_instructions", DESKTOP_DASHBOARD_HTML + DESKTOP_DASHBOARD_JS)


if __name__ == "__main__":
    unittest.main()
