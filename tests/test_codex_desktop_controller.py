from __future__ import annotations

import datetime as dt
from http.client import HTTPConnection
import json
from pathlib import Path
import sqlite3
import tempfile
import threading
import time
import unittest
from unittest import mock

from codex_controller.api import create_server
from codex_controller.desktop_protocol import (
    DesktopProtocolError,
    body_digest,
    validate_desktop_document,
)
from codex_controller.desktop_dashboard import DESKTOP_DASHBOARD_HTML, DESKTOP_DASHBOARD_JS
from codex_controller.desktop_service import DesktopControllerService
from codex_controller.desktop_store import DesktopStore
from codex_controller.runner_relay import RelayPublishError
from codex_controller.runner_service import RunnerManagerService, desktop_runner_authorized
from codex_controller.runner_store import RunnerStore
from codex_controller.service import ControllerService
from codex_controller.store import ControllerStore, StoreError


NOW = dt.datetime(2026, 8, 21, 14, 30, tzinfo=dt.timezone(dt.timedelta(hours=8)))
HOST_REF = "HS-" + "A" * 20
PROJECT_REF = "PJ-" + "B" * 20
THREAD_REF = "TH-" + "C" * 20
TURN_REF = "TR-" + "D" * 20
MODEL_CATALOG = [
    {"id": "gpt-5.6-sol", "display_name": "GPT-5.6 Sol", "is_default": True},
    {"id": "gpt-5.6-terra", "display_name": "GPT-5.6 Terra", "is_default": False},
]
_DEFAULT_CONTROL_REVISION = object()


class App:
    auth_mode = "apiKey"
    account_ready = True
    notification_handler = None

    @staticmethod
    def status() -> dict:
        return {
            "running": True,
            "initialized": True,
            "protocol_error": None,
            "account": {"auth_mode": "apiKey", "plan_type": None, "ready": True},
        }

    @staticmethod
    def stop() -> None:
        return None


class Publisher:
    def __init__(self) -> None:
        self.desktop_commands: list[tuple[str, dict]] = []
        self.error: Exception | None = None

    def publish_request(self, _runner_id: str, _document: dict) -> None:
        return None

    def publish_control(self, _runner_id: str, _document: dict) -> None:
        return None

    def publish_desktop_command(self, runner_id: str, document: dict) -> None:
        if self.error is not None:
            raise self.error
        self.desktop_commands.append((runner_id, dict(document)))


class ImmediateReceiptPublisher(Publisher):
    def __init__(self) -> None:
        super().__init__()
        self.service: DesktopControllerService | None = None

    def publish_desktop_command(self, runner_id: str, document: dict) -> None:
        super().publish_desktop_command(runner_id, document)
        assert self.service is not None
        self.service.receive(
            "desktop_receipt",
            receipt(
                runner_id,
                str(document["request_id"]),
                revision=int(document["expected_thread_revision"]) + 1,
                action=str(document["action"]),
                turn_ref=document.get("expected_turn_ref"),
            ),
        )


def digest(document: dict) -> dict:
    result = dict(document)
    result["body_digest"] = body_digest(result)
    return result


def snapshot(
    runner_id: str,
    *,
    revision: int = 7,
    title: str = "原桌面任务",
    status: str = "active",
    capabilities: list[str] | None = None,
    models: list[dict] | None = None,
    control_revision: int | None | object = _DEFAULT_CONTROL_REVISION,
) -> dict:
    active_turn = TURN_REF if status == "active" else None
    effective_control_revision = revision if control_revision is _DEFAULT_CONTROL_REVISION else control_revision
    thread = {
        "project_alias": "demo-project",
        "project_ref": PROJECT_REF,
        "thread_ref": THREAD_REF,
        "title": title,
        "preview": "公开摘要",
        "status": status,
        "active_turn_ref": active_turn,
        "thread_revision": revision,
        "control_revision": effective_control_revision,
        "created_at": "2026-08-20T09:00:00+08:00",
        "updated_at": NOW.isoformat(),
        "can_accept_direct_input": True,
        "history_incomplete": False,
        "turns": [],
        "control_state": "ready" if status != "archived" else "read_only",
    }
    return digest(
        {
            "version": 1,
            "message_type": "desktop_snapshot",
            "runner_id": runner_id,
            "created_at": NOW.isoformat(),
            "host_ref": HOST_REF,
            "project_ref": PROJECT_REF,
            "thread_ref": THREAD_REF,
            "thread_revision": revision,
            "snapshot": thread,
            "host": {
                "host_ref": HOST_REF,
                "state": "normal",
                "app_version": "26.810.52044",
                "app_build": "6662",
                "cli_version": "0.148.0-alpha.9",
                "schema_digest": "a" * 64,
                "socket_mode": "0600",
                "tcp_listener_count": 0,
                "capabilities": capabilities
                or [
                    "list_read",
                    "deep_link_load",
                    "owner_follower",
                    "interrupt_expected_turn",
                    "continue_same_thread",
                    "native_steer_racy",
                    "archive_control_v1",
                    "model_override_v1",
                ],
                "control_enabled": True,
                "models": list(MODEL_CATALOG if models is None else models),
                "synced_at": NOW.isoformat(),
            },
        }
    )


def event(runner_id: str, *, sequence: int = 1, revision: int = 7) -> dict:
    return digest(
        {
            "version": 1,
            "message_type": "desktop_event",
            "runner_id": runner_id,
            "created_at": NOW.isoformat(),
            "host_ref": HOST_REF,
            "project_ref": PROJECT_REF,
            "thread_ref": THREAD_REF,
            "turn_ref": TURN_REF,
            "thread_revision": revision,
            "event_sequence": sequence,
            "event_kind": "thread.updated",
            "source": "desktop",
            "payload": {"status": "active", "title": "原桌面任务"},
        }
    )


def receipt(
    runner_id: str,
    request_id: str,
    *,
    revision: int = 8,
    action: str = "steer",
    turn_ref: str | None = TURN_REF,
) -> dict:
    return digest(
        {
            "version": 1,
            "message_type": "desktop_receipt",
            "runner_id": runner_id,
            "created_at": NOW.isoformat(),
            "request_id": request_id,
            "host_ref": HOST_REF,
            "thread_ref": THREAD_REF,
            "turn_ref": turn_ref,
            "action": action,
            "state": "confirmed",
            "thread_revision": revision,
        }
    )


def enrollment_payload() -> dict:
    return {
        "display_name": "Desktop Mac Runner",
        "os": "macos",
        "arch": "aarch64",
        "labels": ["desktop"],
        "allowed_projects": ["demo-project"],
        "max_concurrency": 1,
        "request_id": "desktop-runner-create-0001",
    }


def redeem_payload(runner_id: str, token: str) -> dict:
    return {
        "token": token,
        "runner_id": runner_id,
        "protocol_version": 2,
        "agent_version": "0.3.6",
        "codex_version": "0.146.0",
        "os": "macos",
        "arch": "aarch64",
        "capabilities": ["registered_projects", "desktop_takeover_v1"],
        "projects": ["demo-project"],
        "labels": ["desktop"],
        "policy_revision": 1,
        "self_check": {"ok": True, "checks": ["codex", "desktop"]},
    }


class DesktopProtocolTests(unittest.TestCase):
    def test_ref_only_snapshot_event_and_receipt_validate(self) -> None:
        runner_id = "RN-" + "E" * 20
        self.assertEqual(
            validate_desktop_document("desktop_snapshot", snapshot(runner_id))["thread_ref"],
            THREAD_REF,
        )
        self.assertEqual(
            validate_desktop_document("desktop_event", event(runner_id))["event_sequence"],
            1,
        )
        self.assertEqual(
            validate_desktop_document("desktop_receipt", receipt(runner_id, "desktop-steer-0001"))["state"],
            "confirmed",
        )

    def test_uuid_private_path_secret_and_non_shanghai_time_are_rejected(self) -> None:
        runner_id = "RN-" + "E" * 20
        for title in (
            "01a01f1b-b2cb-7762-b352-590ea7a3ae57",
            "/Users/example/private.txt",
            "api_key=fixture-secret",
        ):
            document = snapshot(runner_id, title=title)
            with self.assertRaises(DesktopProtocolError) as context:
                validate_desktop_document("desktop_snapshot", document)
            self.assertEqual(context.exception.code, "desktop_privacy_rejected")
        document = snapshot(runner_id)
        document["created_at"] = "2026-08-21T06:30:00+00:00"
        document["body_digest"] = body_digest(document)
        with self.assertRaises(DesktopProtocolError) as context:
            validate_desktop_document("desktop_snapshot", document)
        self.assertEqual(context.exception.code, "desktop_time_zone_invalid")

    def test_model_catalog_is_bounded_unique_and_backward_compatible(self) -> None:
        runner_id = "RN-" + "E" * 20
        legacy = snapshot(runner_id)
        legacy["host"].pop("models")
        legacy["host"]["capabilities"].remove("model_override_v1")
        legacy["body_digest"] = body_digest(legacy)
        self.assertNotIn("models", validate_desktop_document("desktop_snapshot", legacy)["host"])

        duplicate = [dict(MODEL_CATALOG[0]), dict(MODEL_CATALOG[0])]
        multiple_defaults = [dict(MODEL_CATALOG[0]), {**MODEL_CATALOG[1], "is_default": True}]
        oversized = [
            {"id": f"gpt-{index}", "display_name": f"GPT {index}", "is_default": index == 0}
            for index in range(33)
        ]
        for name, models in (
            ("duplicate", duplicate),
            ("multiple-defaults", multiple_defaults),
            ("oversized", oversized),
        ):
            with self.subTest(name=name):
                with self.assertRaises(DesktopProtocolError) as context:
                    validate_desktop_document("desktop_snapshot", snapshot(runner_id, models=models))
                self.assertEqual(context.exception.code, "desktop_host_invalid")

        inconsistent = snapshot(runner_id, models=[])
        with self.assertRaises(DesktopProtocolError) as context:
            validate_desktop_document("desktop_snapshot", inconsistent)
        self.assertEqual(context.exception.code, "desktop_host_invalid")


class DesktopStoreServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.path = Path(self.temporary.name) / "controller.sqlite3"
        self.runner_id = "RN-" + "E" * 20
        self.publisher = Publisher()
        self.store = DesktopStore(self.path)
        self.service = DesktopControllerService(
            self.store,
            publisher=self.publisher,
            now=lambda: NOW,
            runner_authorizer=lambda _runner_id: True,
        )
        self.service.receive("desktop_snapshot", snapshot(self.runner_id))

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_hosts_projects_threads_and_monotonic_snapshot_are_ref_only(self) -> None:
        hosts = self.service.hosts()
        self.assertTrue(hosts["hosts"][0]["online"])
        self.assertTrue(hosts["hosts"][0]["write_available"])
        self.assertEqual(hosts["hosts"][0]["models"], MODEL_CATALOG)
        projects = self.service.projects()["projects"]
        self.assertEqual(projects[0]["counts"]["active"], 1)
        threads = self.service.threads(
            host_ref=HOST_REF,
            project_ref=PROJECT_REF,
            status="active",
            after_cursor=0,
            limit=10,
        )
        self.assertEqual([item["thread_ref"] for item in threads["threads"]], [THREAD_REF])
        stale = self.service.receive(
            "desktop_snapshot",
            snapshot(self.runner_id, revision=6, title="旧快照"),
        )
        self.assertEqual(stale["status"], "stale_ignored")
        self.assertEqual(self.service.thread(THREAD_REF)["title"], "原桌面任务")
        refreshed = snapshot(self.runner_id)
        refreshed["created_at"] = (NOW + dt.timedelta(seconds=30)).isoformat()
        refreshed["host"]["synced_at"] = refreshed["created_at"]
        refreshed["body_digest"] = body_digest(refreshed)
        self.assertEqual(
            self.service.receive("desktop_snapshot", refreshed)["status"],
            "refreshed",
        )
        self.assertEqual(self.service.thread(THREAD_REF)["title"], "原桌面任务")
        availability = snapshot(self.runner_id, control_revision=8)
        availability["snapshot"]["status"] = "recovery_required"
        availability["snapshot"]["control_state"] = "recovery_required"
        availability["body_digest"] = body_digest(availability)
        self.assertEqual(
            self.service.receive("desktop_snapshot", availability)["status"],
            "refreshed",
        )
        current = self.service.thread(THREAD_REF)
        self.assertEqual(current["status"], "recovery_required")
        self.assertEqual(current["control_state"], "recovery_required")

        recovered = snapshot(self.runner_id, control_revision=9)
        recovered["body_digest"] = body_digest(recovered)
        self.assertEqual(
            self.service.receive("desktop_snapshot", recovered)["status"],
            "refreshed",
        )
        current = self.service.thread(THREAD_REF)
        self.assertEqual(current["control_revision"], 9)
        self.assertEqual(current["control_state"], "ready")

        live = snapshot(self.runner_id, control_revision=10, status="active")
        live["snapshot"]["turns"] = [
            {"turn_ref": TURN_REF, "status": "inProgress", "items": []}
        ]
        live["snapshot"]["active_turn_ref"] = TURN_REF
        live["snapshot"]["history_incomplete"] = True
        live["body_digest"] = body_digest(live)
        self.assertEqual(self.service.receive("desktop_snapshot", live)["status"], "refreshed")
        current = self.service.thread(THREAD_REF)
        self.assertEqual(current["control_revision"], 10)
        self.assertEqual(current["active_turn_ref"], TURN_REF)

        stale_control = snapshot(self.runner_id, control_revision=9, status="idle")
        stale_control["snapshot"]["turns"] = []
        stale_control["snapshot"]["active_turn_ref"] = None
        stale_control["body_digest"] = body_digest(stale_control)
        self.assertEqual(
            self.service.receive("desktop_snapshot", stale_control)["status"],
            "stale_ignored",
        )
        self.assertEqual(self.service.thread(THREAD_REF)["control_revision"], 10)

        stale_availability = json.loads(json.dumps(live))
        stale_availability["snapshot"]["control_revision"] = 9
        stale_availability["snapshot"]["status"] = "idle"
        stale_availability["snapshot"]["control_state"] = "recovery_required"
        stale_availability["body_digest"] = body_digest(stale_availability)
        self.assertEqual(
            self.service.receive("desktop_snapshot", stale_availability)["status"],
            "stale_ignored",
        )

        same_revision_availability = json.loads(json.dumps(live))
        same_revision_availability["snapshot"]["status"] = "idle"
        same_revision_availability["snapshot"]["control_state"] = "recovery_required"
        same_revision_availability["body_digest"] = body_digest(same_revision_availability)
        with self.assertRaises(StoreError) as context:
            self.service.receive("desktop_snapshot", same_revision_availability)
        self.assertEqual(context.exception.code, "desktop_revision_conflict")

        business_conflicts = {
            "title": "冲突快照",
            "preview": "不同公开摘要",
            "project_alias": "other-project",
            "updated_at": (NOW + dt.timedelta(seconds=1)).isoformat(),
            "can_accept_direct_input": False,
        }
        for field, value in business_conflicts.items():
            with self.subTest(field=field):
                conflicting = snapshot(self.runner_id, control_revision=11)
                conflicting["snapshot"][field] = value
                conflicting["body_digest"] = body_digest(conflicting)
                with self.assertRaises(StoreError) as context:
                    self.service.receive("desktop_snapshot", conflicting)
                self.assertEqual(context.exception.code, "desktop_revision_conflict")

        same_control_revision = json.loads(json.dumps(live))
        same_control_revision["snapshot"]["turns"] = []
        same_control_revision["snapshot"]["active_turn_ref"] = None
        same_control_revision["body_digest"] = body_digest(same_control_revision)
        with self.assertRaises(StoreError) as context:
            self.service.receive("desktop_snapshot", same_control_revision)
        self.assertEqual(context.exception.code, "desktop_revision_conflict")

        missing_control_revision = json.loads(json.dumps(live))
        missing_control_revision["snapshot"]["control_revision"] = None
        missing_control_revision["snapshot"]["turns"] = []
        missing_control_revision["snapshot"]["active_turn_ref"] = None
        missing_control_revision["body_digest"] = body_digest(missing_control_revision)
        with self.assertRaises(StoreError) as context:
            self.service.receive("desktop_snapshot", missing_control_revision)
        self.assertEqual(context.exception.code, "desktop_revision_conflict")
        encoded = json.dumps(self.service.thread(THREAD_REF), ensure_ascii=False)
        self.assertNotIn(self.runner_id, encoded)

    def test_same_revision_null_control_revision_latches_only_a_safe_degradation(self) -> None:
        degraded_statuses = {
            "load_required": "notLoaded",
            "recovery_required": "recovery_required",
            "protocol_degraded": "protocol_degraded",
            "control_offline": "idle",
        }
        for control_state, incoming_status in degraded_statuses.items():
            with self.subTest(control_state=control_state), tempfile.TemporaryDirectory() as temporary:
                service = DesktopControllerService(
                    DesktopStore(Path(temporary) / "controller.sqlite3"),
                    publisher=Publisher(),
                    now=lambda: NOW,
                    runner_authorizer=lambda _runner_id: True,
                )
                trusted = snapshot(
                    self.runner_id,
                    revision=18,
                    control_revision=8,
                    status="idle",
                )
                trusted["snapshot"]["turns"] = [
                    {"turn_ref": TURN_REF, "status": "completed", "items": []}
                ]
                trusted["snapshot"]["history_incomplete"] = True
                trusted["body_digest"] = body_digest(trusted)
                self.assertEqual(service.receive("desktop_snapshot", trusted)["status"], "stored")

                degraded = json.loads(json.dumps(trusted))
                degraded["snapshot"]["control_revision"] = None
                degraded["snapshot"]["control_state"] = control_state
                degraded["snapshot"]["status"] = incoming_status
                degraded["snapshot"]["active_turn_ref"] = None
                degraded["snapshot"]["turns"] = []
                degraded["snapshot"]["history_incomplete"] = False
                degraded["body_digest"] = body_digest(degraded)
                self.assertEqual(
                    service.receive("desktop_snapshot", degraded)["status"],
                    "degraded_latched",
                )
                current = service.thread(THREAD_REF)
                self.assertEqual(current["control_revision"], 8)
                self.assertEqual(current["control_state"], control_state)
                self.assertEqual(current["status"], "idle")
                self.assertEqual(current["snapshot"]["turns"], trusted["snapshot"]["turns"])
                self.assertTrue(current["snapshot"]["history_incomplete"])

                same_revision_ready = json.loads(json.dumps(trusted))
                same_revision_ready["body_digest"] = body_digest(same_revision_ready)
                with self.assertRaises(StoreError) as context:
                    service.receive("desktop_snapshot", same_revision_ready)
                self.assertEqual(context.exception.code, "desktop_revision_conflict")

                recovered = json.loads(json.dumps(trusted))
                recovered["snapshot"]["control_revision"] = 9
                recovered["body_digest"] = body_digest(recovered)
                self.assertEqual(
                    service.receive("desktop_snapshot", recovered)["status"],
                    "refreshed",
                )
                self.assertEqual(service.thread(THREAD_REF)["control_revision"], 9)
                self.assertEqual(service.thread(THREAD_REF)["control_state"], "ready")

    def test_same_revision_null_control_revision_accepts_only_non_writable_archive_transition(self) -> None:
        for existing_status, incoming_status, existing_state, incoming_state in (
            ("notLoaded", "archived", "load_required", "read_only"),
            ("archived", "notLoaded", "read_only", "load_required"),
            ("notLoaded", "archived", "load_required", "protocol_degraded"),
        ):
            with self.subTest(existing=existing_status, incoming=incoming_status), tempfile.TemporaryDirectory() as temporary:
                service = DesktopControllerService(
                    DesktopStore(Path(temporary) / "controller.sqlite3"),
                    publisher=Publisher(),
                    now=lambda: NOW,
                    runner_authorizer=lambda _runner_id: True,
                )
                trusted = snapshot(
                    self.runner_id,
                    revision=18,
                    control_revision=None,
                    status=existing_status,
                )
                trusted["snapshot"]["control_state"] = existing_state
                trusted["body_digest"] = body_digest(trusted)
                self.assertEqual(service.receive("desktop_snapshot", trusted)["status"], "stored")

                transition = json.loads(json.dumps(trusted))
                transition["snapshot"]["status"] = incoming_status
                transition["snapshot"]["control_state"] = incoming_state
                transition["body_digest"] = body_digest(transition)
                self.assertEqual(
                    service.receive("desktop_snapshot", transition)["status"],
                    "refreshed",
                )
                current = service.thread(THREAD_REF)
                self.assertEqual(current["status"], incoming_status)
                self.assertEqual(current["control_state"], incoming_state)
                self.assertIsNone(current["control_revision"])

    def test_same_revision_explicit_null_accepts_only_non_writable_control_state_overlay(self) -> None:
        non_writable = {
            "load_required",
            "read_only",
            "recovery_required",
            "protocol_degraded",
            "control_offline",
        }
        for existing_state in non_writable:
            for incoming_state in non_writable - {existing_state}:
                with (
                    self.subTest(existing=existing_state, incoming=incoming_state),
                    tempfile.TemporaryDirectory() as temporary,
                ):
                    service = DesktopControllerService(
                        DesktopStore(Path(temporary) / "controller.sqlite3"),
                        publisher=Publisher(),
                        now=lambda: NOW,
                        runner_authorizer=lambda _runner_id: True,
                    )
                    trusted = snapshot(self.runner_id, revision=18, control_revision=None)
                    trusted["snapshot"]["control_state"] = existing_state
                    trusted["snapshot"]["turns"] = [
                        {"turn_ref": TURN_REF, "status": "completed", "items": []}
                    ]
                    trusted["snapshot"]["history_incomplete"] = True
                    trusted["body_digest"] = body_digest(trusted)
                    self.assertEqual(service.receive("desktop_snapshot", trusted)["status"], "stored")

                    overlay = json.loads(json.dumps(trusted))
                    overlay["snapshot"]["control_state"] = incoming_state
                    overlay["body_digest"] = body_digest(overlay)
                    self.assertEqual(
                        service.receive("desktop_snapshot", overlay)["status"],
                        "refreshed",
                    )
                    current = service.thread(THREAD_REF)
                    self.assertIsNone(current["control_revision"])
                    self.assertEqual(current["control_state"], incoming_state)
                    self.assertEqual(current["snapshot"]["turns"], trusted["snapshot"]["turns"])
                    self.assertTrue(current["snapshot"]["history_incomplete"])

    def test_same_revision_null_control_state_overlay_fails_closed_on_any_writable_or_other_change(self) -> None:
        cases = {
            "existing-writable": ({"control_state": "ready"}, {"control_state": "protocol_degraded"}),
            "incoming-writable": ({"control_state": "load_required"}, {"control_state": "ready"}),
            "status": ({"control_state": "load_required"}, {"control_state": "protocol_degraded", "status": "failed"}),
            "business": ({"control_state": "load_required"}, {"control_state": "protocol_degraded", "title": "漂移标题"}),
            "history": (
                {"control_state": "load_required"},
                {
                    "control_state": "protocol_degraded",
                    "turns": [{"turn_ref": TURN_REF, "status": "completed", "items": []}],
                },
            ),
        }
        for name, (existing_changes, incoming_changes) in cases.items():
            with self.subTest(name=name), tempfile.TemporaryDirectory() as temporary:
                service = DesktopControllerService(
                    DesktopStore(Path(temporary) / "controller.sqlite3"),
                    publisher=Publisher(),
                    now=lambda: NOW,
                    runner_authorizer=lambda _runner_id: True,
                )
                trusted = snapshot(self.runner_id, revision=18, control_revision=None)
                trusted["snapshot"].update(existing_changes)
                trusted["body_digest"] = body_digest(trusted)
                self.assertEqual(service.receive("desktop_snapshot", trusted)["status"], "stored")

                overlay = json.loads(json.dumps(trusted))
                overlay["snapshot"].update(incoming_changes)
                overlay["body_digest"] = body_digest(overlay)
                with self.assertRaises(StoreError) as context:
                    service.receive("desktop_snapshot", overlay)
                self.assertEqual(context.exception.code, "desktop_revision_conflict")

        for missing_side in ("existing", "incoming"):
            with self.subTest(missing_side=missing_side), tempfile.TemporaryDirectory() as temporary:
                service = DesktopControllerService(
                    DesktopStore(Path(temporary) / "controller.sqlite3"),
                    publisher=Publisher(),
                    now=lambda: NOW,
                    runner_authorizer=lambda _runner_id: True,
                )
                trusted = snapshot(self.runner_id, revision=18, control_revision=None)
                trusted["snapshot"]["control_state"] = "load_required"
                if missing_side == "existing":
                    trusted["snapshot"].pop("control_revision")
                trusted["body_digest"] = body_digest(trusted)
                self.assertEqual(service.receive("desktop_snapshot", trusted)["status"], "stored")

                overlay = json.loads(json.dumps(trusted))
                overlay["snapshot"]["control_state"] = "protocol_degraded"
                if missing_side == "existing":
                    overlay["snapshot"]["control_revision"] = None
                else:
                    overlay["snapshot"].pop("control_revision")
                overlay["body_digest"] = body_digest(overlay)
                with self.assertRaises(StoreError) as context:
                    service.receive("desktop_snapshot", overlay)
                self.assertEqual(context.exception.code, "desktop_revision_conflict")

    def test_same_revision_safe_degradation_ignores_control_history_loss_but_rejects_business_and_writable_changes(self) -> None:
        candidates = {
            "business": {"title": "不能锁存的标题"},
            "writable": {"control_state": "ready"},
        }
        for name, changes in candidates.items():
            with self.subTest(name=name):
                document = snapshot(self.runner_id, control_revision=None)
                document["snapshot"]["status"] = "recovery_required"
                document["snapshot"]["control_state"] = "recovery_required"
                document["snapshot"].update(changes)
                document["body_digest"] = body_digest(document)
                with self.assertRaises(StoreError) as context:
                    self.service.receive("desktop_snapshot", document)
                self.assertEqual(context.exception.code, "desktop_revision_conflict")

        history_loss = snapshot(self.runner_id, control_revision=None)
        history_loss["snapshot"]["status"] = "notLoaded"
        history_loss["snapshot"]["control_state"] = "protocol_degraded"
        history_loss["snapshot"]["active_turn_ref"] = None
        history_loss["snapshot"]["turns"] = []
        history_loss["snapshot"]["history_incomplete"] = False
        history_loss["body_digest"] = body_digest(history_loss)
        self.assertEqual(
            self.service.receive("desktop_snapshot", history_loss)["status"],
            "degraded_latched",
        )

        omitted = snapshot(self.runner_id, control_revision=None)
        omitted["snapshot"].pop("control_revision")
        omitted["snapshot"]["status"] = "recovery_required"
        omitted["snapshot"]["control_state"] = "recovery_required"
        omitted["body_digest"] = body_digest(omitted)
        with self.assertRaises(StoreError) as context:
            self.service.receive("desktop_snapshot", omitted)
        self.assertEqual(context.exception.code, "desktop_revision_conflict")

        current = self.service.thread(THREAD_REF)
        self.assertEqual(current["control_revision"], 7)
        self.assertEqual(current["status"], "active")
        self.assertEqual(current["control_state"], "protocol_degraded")

    def test_old_snapshot_without_control_revision_is_readable_but_control_fails_closed(self) -> None:
        legacy = snapshot(self.runner_id, revision=8)
        legacy["snapshot"].pop("control_revision")
        legacy["body_digest"] = body_digest(legacy)
        self.assertEqual(self.service.receive("desktop_snapshot", legacy)["status"], "stored")
        current = self.service.thread(THREAD_REF)
        self.assertIsNone(current["control_revision"])
        with self.assertRaises(StoreError) as context:
            self.service.submit(
                THREAD_REF,
                "steer",
                {
                    "request_id": "desktop-legacy-control-0001",
                    "expected_turn_ref": TURN_REF,
                    "thread_revision": 8,
                    "input": "legacy snapshot must refresh",
                    "mode": "safe",
                },
            )
        self.assertEqual(context.exception.code, "desktop_snapshot_refresh_required")

    def test_same_revision_null_and_omitted_control_revision_are_semantically_equal(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "controller.sqlite3"
            service = DesktopControllerService(
                DesktopStore(path),
                publisher=Publisher(),
                now=lambda: NOW,
                runner_authorizer=lambda _runner_id: True,
            )
            explicit_null = snapshot(
                self.runner_id,
                revision=1788010200,
                control_revision=None,
                status="idle",
            )
            explicit_null["snapshot"]["active_turn_ref"] = None
            explicit_null["snapshot"]["control_state"] = "load_required"
            explicit_null["body_digest"] = body_digest(explicit_null)
            self.assertEqual(
                service.receive("desktop_snapshot", explicit_null)["status"],
                "stored",
            )

            omitted = json.loads(json.dumps(explicit_null))
            omitted["snapshot"].pop("control_revision")
            omitted["body_digest"] = body_digest(omitted)
            self.assertEqual(
                service.receive("desktop_snapshot", omitted)["status"],
                "refreshed",
            )

            explicit_null_again = json.loads(json.dumps(omitted))
            explicit_null_again["snapshot"]["control_revision"] = None
            explicit_null_again["body_digest"] = body_digest(explicit_null_again)
            self.assertEqual(
                service.receive("desktop_snapshot", explicit_null_again)["status"],
                "refreshed",
            )
            self.assertIsNone(service.thread(THREAD_REF)["control_revision"])
            with sqlite3.connect(path) as connection:
                snapshot_count = connection.execute(
                    "SELECT COUNT(*) FROM desktop_snapshots WHERE thread_ref=? AND thread_revision=?",
                    (THREAD_REF, 1788010200),
                ).fetchone()[0]
            self.assertEqual(snapshot_count, 1)

        with tempfile.TemporaryDirectory() as temporary:
            service = DesktopControllerService(
                DesktopStore(Path(temporary) / "controller.sqlite3"),
                publisher=Publisher(),
                now=lambda: NOW,
                runner_authorizer=lambda _runner_id: True,
            )
            integer_revision = snapshot(self.runner_id, control_revision=8)
            self.assertEqual(
                service.receive("desktop_snapshot", integer_revision)["status"],
                "stored",
            )
            omitted_after_integer = json.loads(json.dumps(integer_revision))
            omitted_after_integer["snapshot"].pop("control_revision")
            omitted_after_integer["body_digest"] = body_digest(omitted_after_integer)
            with self.assertRaises(StoreError) as context:
                service.receive("desktop_snapshot", omitted_after_integer)
            self.assertEqual(context.exception.code, "desktop_revision_conflict")

    def test_legacy_desktop_threads_schema_migrates_without_row_loss(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "legacy.sqlite3"
            with sqlite3.connect(path) as connection:
                connection.execute(
                    "CREATE TABLE desktop_threads("
                    "id INTEGER PRIMARY KEY AUTOINCREMENT,thread_ref TEXT NOT NULL UNIQUE,"
                    "host_ref TEXT NOT NULL,project_ref TEXT NOT NULL,runner_id TEXT NOT NULL,"
                    "title TEXT NOT NULL,status TEXT NOT NULL,active_turn_ref TEXT,"
                    "thread_revision INTEGER NOT NULL,control_state TEXT NOT NULL,"
                    "snapshot_digest TEXT,snapshot_json TEXT NOT NULL,source_created_at TEXT,"
                    "source_updated_at TEXT,observed_at TEXT NOT NULL)"
                )
                connection.execute(
                    "INSERT INTO desktop_threads("
                    "thread_ref,host_ref,project_ref,runner_id,title,status,active_turn_ref,"
                    "thread_revision,control_state,snapshot_digest,snapshot_json,observed_at"
                    ") VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        THREAD_REF,
                        HOST_REF,
                        PROJECT_REF,
                        self.runner_id,
                        "legacy",
                        "idle",
                        None,
                        7,
                        "load_required",
                        None,
                        "{}",
                        NOW.isoformat(),
                    ),
                )
            DesktopStore(path)
            with sqlite3.connect(path) as connection:
                columns = {row[1] for row in connection.execute("PRAGMA table_info(desktop_threads)")}
                row = connection.execute(
                    "SELECT title,control_revision FROM desktop_threads WHERE thread_ref=?",
                    (THREAD_REF,),
                ).fetchone()
            self.assertIn("control_revision", columns)
            self.assertEqual(row, ("legacy", None))

    def test_same_revision_availability_refresh_requires_monotonic_control_revision_at_551_document_scale(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "controller.sqlite3"
            service = DesktopControllerService(
                DesktopStore(path),
                publisher=Publisher(),
                now=lambda: NOW,
                runner_authorizer=lambda _runner_id: True,
            )
            statuses: list[str] = []
            for index in range(551):
                document = snapshot(self.runner_id, control_revision=7 + index)
                if index % 2:
                    document["snapshot"]["status"] = "recovery_required"
                    document["snapshot"]["control_state"] = "recovery_required"
                envelope_time = (NOW + dt.timedelta(milliseconds=index)).isoformat()
                document["created_at"] = envelope_time
                document["host"]["synced_at"] = envelope_time
                document["body_digest"] = body_digest(document)
                statuses.append(service.receive("desktop_snapshot", document)["status"])

            self.assertEqual(statuses.count("stored"), 1)
            self.assertEqual(statuses.count("refreshed"), 550)
            with sqlite3.connect(path) as connection:
                snapshot_count = connection.execute(
                    "SELECT COUNT(*) FROM desktop_snapshots WHERE thread_ref=? AND thread_revision=?",
                    (THREAD_REF, 7),
                ).fetchone()[0]
            self.assertEqual(snapshot_count, 1)

    def test_same_revision_control_history_refresh_handles_561_document_scale(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "controller.sqlite3"
            service = DesktopControllerService(
                DesktopStore(path),
                publisher=Publisher(),
                now=lambda: NOW,
                runner_authorizer=lambda _runner_id: True,
            )
            first = snapshot(self.runner_id, revision=1788009802, control_revision=None, status="idle")
            first["snapshot"]["active_turn_ref"] = None
            first["snapshot"]["turns"] = []
            first["snapshot"]["control_state"] = "load_required"
            first["body_digest"] = body_digest(first)
            self.assertEqual(service.receive("desktop_snapshot", first)["status"], "stored")

            recovered = snapshot(
                self.runner_id,
                revision=1788009802,
                control_revision=550,
                status="active",
            )
            recovered["snapshot"]["turns"] = [
                {"turn_ref": TURN_REF, "status": "inProgress", "items": []}
            ]
            recovered["snapshot"]["active_turn_ref"] = TURN_REF
            recovered["body_digest"] = body_digest(recovered)
            self.assertEqual(service.receive("desktop_snapshot", recovered)["status"], "refreshed")

            statuses: list[str] = []
            for index in range(560):
                document = json.loads(json.dumps(recovered))
                document["snapshot"]["control_revision"] = 551 + index
                document["snapshot"]["turns"][0]["status"] = (
                    "inProgress" if index % 2 == 0 else "completed"
                )
                document["snapshot"]["status"] = "active" if index % 2 == 0 else "idle"
                document["snapshot"]["active_turn_ref"] = TURN_REF if index % 2 == 0 else None
                document["body_digest"] = body_digest(document)
                statuses.append(service.receive("desktop_snapshot", document)["status"])

            self.assertEqual(statuses, ["refreshed"] * 560)
            current = service.thread(THREAD_REF)
            self.assertEqual(current["thread_revision"], 1788009802)
            self.assertEqual(current["control_revision"], 1110)

    def test_same_revision_binding_drift_still_fails_closed(self) -> None:
        conflicting = snapshot(self.runner_id)
        conflicting["project_ref"] = "PJ-" + "Z" * 20
        conflicting["snapshot"]["project_ref"] = conflicting["project_ref"]
        conflicting["body_digest"] = body_digest(conflicting)
        with self.assertRaises(StoreError) as context:
            self.service.receive("desktop_snapshot", conflicting)
        self.assertEqual(context.exception.code, "desktop_thread_binding_conflict")

    def test_event_cursor_duplicate_and_sequence_conflict(self) -> None:
        first = self.service.receive("desktop_event", event(self.runner_id))
        duplicate = self.service.receive("desktop_event", event(self.runner_id))
        self.assertEqual(duplicate["status"], "duplicate")
        listed = self.service.events(
            THREAD_REF,
            after_cursor=0,
            limit=10,
            wait_seconds=0,
        )
        self.assertEqual(listed["events"][0]["cursor"], first["cursor"])
        self.assertNotIn("runner_id", listed["events"][0])
        conflict = event(self.runner_id)
        conflict["payload"] = {"status": "idle"}
        conflict["body_digest"] = body_digest(conflict)
        with self.assertRaises(StoreError) as context:
            self.service.receive("desktop_event", conflict)
        self.assertEqual(context.exception.code, "desktop_event_sequence_conflict")

    def test_event_requires_a_bound_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            empty = DesktopControllerService(
                DesktopStore(Path(temporary) / "empty.sqlite3"),
                publisher=Publisher(),
                now=lambda: NOW,
                runner_authorizer=lambda _runner_id: True,
            )
            with self.assertRaises(StoreError) as context:
                empty.receive("desktop_event", event(self.runner_id))
        self.assertEqual(context.exception.code, "desktop_snapshot_required")

        with self.assertRaises(StoreError) as context:
            self.service.receive(
                "desktop_event",
                event(self.runner_id, sequence=2, revision=8),
            )
        self.assertEqual(context.exception.code, "desktop_snapshot_required")

    def test_event_for_pruned_superseded_revision_uses_existing_stale_ack_contract(self) -> None:
        for revision in (8, 9, 10, 11):
            self.service.receive(
                "desktop_snapshot",
                snapshot(self.runner_id, revision=revision, status="idle"),
            )
        with self.assertRaises(StoreError) as context:
            self.service.receive(
                "desktop_event",
                event(self.runner_id, sequence=2, revision=7),
            )
        self.assertEqual(context.exception.code, "desktop_event_sequence_stale")

        with self.assertRaises(StoreError) as context:
            self.service.receive(
                "desktop_event",
                event(self.runner_id, sequence=3, revision=12),
            )
        self.assertEqual(context.exception.code, "desktop_snapshot_required")

    def test_command_is_idempotent_conflicting_body_fails_and_receipt_confirms(self) -> None:
        payload = {
            "request_id": "desktop-steer-0001",
            "expected_turn_ref": TURN_REF,
            "thread_revision": 7,
            "input": "立即调整方向并先运行定向测试",
        }
        submitted = self.service.submit(THREAD_REF, "steer", payload)
        self.assertEqual(submitted["state"], "submitted")
        self.assertEqual(len(self.publisher.desktop_commands), 1)
        command = self.publisher.desktop_commands[0][1]
        self.assertEqual(command["mode"], "safe")
        self.assertEqual(command["expected_control_revision"], 7)
        replay = self.service.submit(THREAD_REF, "steer", payload)
        self.assertEqual(replay["state"], "submitted")
        self.assertEqual(len(self.publisher.desktop_commands), 1)
        with self.assertRaises(StoreError) as context:
            self.service.submit(THREAD_REF, "steer", {**payload, "input": "另一条正文"})
        self.assertEqual(context.exception.code, "desktop_request_conflict")
        stored = self.service.receive(
            "desktop_receipt",
            receipt(self.runner_id, payload["request_id"]),
        )
        self.assertFalse(stored["orphan"])
        confirmed = self.store.command(payload["request_id"])
        self.assertEqual(confirmed["state"], "confirmed")
        self.assertNotIn("runner_id", json.dumps(confirmed))
        conflicting_receipt = receipt(self.runner_id, payload["request_id"])
        conflicting_receipt["state"] = "unknown"
        conflicting_receipt["error_code"] = "late_unknown"
        conflicting_receipt["body_digest"] = body_digest(conflicting_receipt)
        with self.assertRaises(StoreError) as context:
            self.service.receive("desktop_receipt", conflicting_receipt)
        self.assertEqual(context.exception.code, "desktop_receipt_conflict")
        self.assertEqual(self.store.command(payload["request_id"])["state"], "confirmed")

    def test_model_override_is_digest_bound_and_fails_closed_outside_catalog(self) -> None:
        payload = {
            "request_id": "desktop-model-safe-steer-0001",
            "expected_turn_ref": TURN_REF,
            "thread_revision": 7,
            "input": "安全调整并切换运行模型",
            "mode": "safe",
            "model": "gpt-5.6-terra",
        }
        submitted = self.service.submit(THREAD_REF, "steer", payload)
        self.assertEqual(submitted["state"], "submitted")
        command = self.publisher.desktop_commands[-1][1]
        self.assertEqual(command["model"], "gpt-5.6-terra")
        self.assertEqual(command["thread_ref"], THREAD_REF)
        self.assertEqual(self.service.submit(THREAD_REF, "steer", payload)["state"], "submitted")
        with self.assertRaises(StoreError) as context:
            self.service.submit(THREAD_REF, "steer", {**payload, "model": "gpt-5.6-sol"})
        self.assertEqual(context.exception.code, "desktop_request_conflict")
        self.service.receive(
            "desktop_receipt",
            receipt(self.runner_id, payload["request_id"], action="steer"),
        )

        for name, invalid, expected_code in (
            (
                "native",
                {**payload, "request_id": "desktop-model-native-0001", "mode": "native"},
                "desktop_model_invalid",
            ),
            (
                "invalid-id",
                {**payload, "request_id": "desktop-model-invalid-0001", "model": "provider/model"},
                "desktop_model_invalid",
            ),
            (
                "outside-catalog",
                {**payload, "request_id": "desktop-model-missing-0001", "model": "gpt-5.6-missing"},
                "desktop_model_unavailable",
            ),
        ):
            with self.subTest(name=name):
                with self.assertRaises(StoreError) as context:
                    self.service.submit(THREAD_REF, "steer", invalid)
                self.assertEqual(context.exception.code, expected_code)

        self.service.receive("desktop_snapshot", snapshot(self.runner_id, revision=8, status="idle"))
        continued = self.service.submit(
            THREAD_REF,
            "continue",
            {
                "request_id": "desktop-model-continue-0001",
                "thread_revision": 8,
                "input": "继续同一个原任务",
                "model": "gpt-5.6-sol",
            },
        )
        self.assertEqual(continued["state"], "submitted")
        self.assertEqual(self.publisher.desktop_commands[-1][1]["model"], "gpt-5.6-sol")
        self.assertEqual(self.service.thread(THREAD_REF)["latest_command"]["model"], "gpt-5.6-sol")

    def test_action_capabilities_and_runner_authorization_gate_write_availability(self) -> None:
        limited = snapshot(
            self.runner_id,
            revision=8,
            capabilities=["list_read", "owner_follower"],
        )
        self.service.receive("desktop_snapshot", limited)
        with self.assertRaises(StoreError) as context:
            self.service.submit(
                THREAD_REF,
                "interrupt",
                {
                    "request_id": "desktop-capability-block-0001",
                    "expected_turn_ref": TURN_REF,
                    "thread_revision": 8,
                },
            )
        self.assertEqual(context.exception.code, "desktop_capability_unavailable")

        self.service.receive(
            "desktop_snapshot",
            snapshot(
                self.runner_id,
                revision=9,
                status="idle",
                capabilities=[
                    "list_read",
                    "owner_follower",
                    "continue_same_thread",
                ],
            ),
        )
        with self.assertRaises(StoreError) as context:
            self.service.submit(
                THREAD_REF,
                "archive",
                {
                    "request_id": "desktop-archive-capability-block-0001",
                    "thread_revision": 9,
                },
            )
        self.assertEqual(context.exception.code, "desktop_capability_unavailable")

        unauthorized = DesktopControllerService(
            self.store,
            publisher=Publisher(),
            now=lambda: NOW,
            runner_authorizer=lambda _runner_id: False,
        )
        self.assertFalse(unauthorized.hosts()["hosts"][0]["write_available"])
        self.assertFalse(
            desktop_runner_authorized(
                {
                    "admin_state": "enabled",
                    "os": "macos",
                    "archived": False,
                    "capabilities": ["desktop_takeover_v1"],
                },
                manager_enabled=False,
            )
        )

    def test_accepted_command_is_never_pruned_for_capacity(self) -> None:
        def command(request_id: str) -> dict:
            return digest(
                {
                    "version": 1,
                    "message_type": "desktop_command",
                    "runner_id": self.runner_id,
                    "request_id": request_id,
                    "host_ref": HOST_REF,
                    "thread_ref": THREAD_REF,
                    "expected_thread_revision": 7,
                    "action": "interrupt",
                    "expected_turn_ref": TURN_REF,
                    "created_at": NOW.isoformat(),
                    "expires_at": (NOW + dt.timedelta(minutes=2)).isoformat(),
                }
            )

        with mock.patch("codex_controller.desktop_store.MAX_COMMANDS", 1):
            first = command("desktop-capacity-accepted-0001")
            self.store.prepare_command(command=first, intent_digest="sha256:" + "1" * 64)
            self.store.mark_command(
                first["request_id"],
                state="accepted",
                error_code=None,
                updated_at=NOW.isoformat(),
            )
            second = command("desktop-capacity-second-0001")
            with self.assertRaises(StoreError) as context:
                self.store.prepare_command(command=second, intent_digest="sha256:" + "2" * 64)
        self.assertEqual(context.exception.code, "desktop_command_capacity")
        self.assertEqual(self.store.command(first["request_id"])["state"], "accepted")

    def test_continue_archive_unarchive_and_native_steer_keep_same_thread_ref(self) -> None:
        native = self.service.submit(
            THREAD_REF,
            "steer",
            {
                "request_id": "desktop-native-steer-0001",
                "expected_turn_ref": TURN_REF,
                "thread_revision": 7,
                "input": "使用原生快速调整",
                "mode": "native",
            },
        )
        self.assertEqual(native["state"], "submitted")
        self.assertEqual(self.publisher.desktop_commands[-1][1]["mode"], "native")
        self.assertEqual(self.publisher.desktop_commands[-1][1]["thread_ref"], THREAD_REF)
        self.service.receive(
            "desktop_receipt",
            receipt(self.runner_id, "desktop-native-steer-0001"),
        )

        self.service.receive(
            "desktop_snapshot",
            snapshot(self.runner_id, revision=8, status="idle"),
        )
        continued = self.service.submit(
            THREAD_REF,
            "continue",
            {
                "request_id": "desktop-continue-0001",
                "thread_revision": 8,
                "input": "在同一原任务继续下一步",
            },
        )
        with self.assertRaises(StoreError) as context:
            self.service.submit(
                THREAD_REF,
                "archive",
                {"request_id": "desktop-archive-too-early-0001", "thread_revision": 8},
            )
        self.assertEqual(context.exception.code, "desktop_command_inflight")
        self.service.receive(
            "desktop_receipt",
            receipt(
                self.runner_id,
                "desktop-continue-0001",
                revision=9,
                action="continue",
                turn_ref=TURN_REF,
            ),
        )
        self.service.receive(
            "desktop_snapshot",
            snapshot(self.runner_id, revision=9, status="idle"),
        )
        archived = self.service.submit(
            THREAD_REF,
            "archive",
            {"request_id": "desktop-archive-0001", "thread_revision": 9},
        )
        self.assertEqual((continued["state"], archived["state"]), ("submitted", "submitted"))
        self.assertTrue(
            all(
                document["thread_ref"] == THREAD_REF
                for _runner_id, document in self.publisher.desktop_commands
            )
        )
        self.service.receive(
            "desktop_receipt",
            receipt(
                self.runner_id,
                "desktop-archive-0001",
                revision=10,
                action="archive",
                turn_ref=None,
            ),
        )

        self.service.receive(
            "desktop_snapshot",
            snapshot(self.runner_id, revision=10, status="archived"),
        )
        restored = self.service.submit(
            THREAD_REF,
            "unarchive",
            {"request_id": "desktop-unarchive-0001", "thread_revision": 10},
        )
        self.assertEqual(restored["state"], "submitted")
        self.assertEqual(self.publisher.desktop_commands[-1][1]["action"], "unarchive")

    def test_indeterminate_publish_and_restart_pending_never_replay(self) -> None:
        self.publisher.error = RelayPublishError(
            "relay_publish_indeterminate",
            definitely_undelivered=False,
        )
        result = self.service.submit(
            THREAD_REF,
            "interrupt",
            {
                "request_id": "desktop-interrupt-0001",
                "expected_turn_ref": TURN_REF,
                "thread_revision": 7,
            },
        )
        self.assertEqual(result["state"], "unknown")
        replay = self.service.submit(
            THREAD_REF,
            "interrupt",
            {
                "request_id": "desktop-interrupt-0001",
                "expected_turn_ref": TURN_REF,
                "thread_revision": 7,
            },
        )
        self.assertEqual(replay["state"], "unknown")
        self.assertEqual(self.publisher.desktop_commands, [])
        with self.assertRaises(StoreError) as context:
            self.service.submit(
                THREAD_REF,
                "interrupt",
                {
                    "request_id": "desktop-blocked-by-unknown-0001",
                    "expected_turn_ref": TURN_REF,
                    "thread_revision": 7,
                },
            )
        self.assertEqual(context.exception.code, "desktop_command_recovery_required")
        self.service.receive(
            "desktop_receipt",
            receipt(
                self.runner_id,
                "desktop-interrupt-0001",
                action="interrupt",
            ),
        )
        self.service.receive(
            "desktop_snapshot",
            snapshot(self.runner_id, revision=8, status="active"),
        )

        self.publisher.error = None
        submitted = self.service.submit(
            THREAD_REF,
            "interrupt",
            {
                "request_id": "desktop-receipt-timeout-0001",
                "expected_turn_ref": TURN_REF,
                "thread_revision": 8,
            },
        )
        self.assertEqual(submitted["state"], "submitted")
        late = DesktopControllerService(
            self.store,
            publisher=self.publisher,
            now=lambda: NOW + dt.timedelta(minutes=3),
            runner_authorizer=lambda _runner_id: True,
        )
        self.assertEqual(late.sweep(), 1)
        self.assertEqual(
            self.store.command("desktop-receipt-timeout-0001")["state"],
            "unknown",
        )

        pending_command = digest(
            {
                "version": 1,
                "message_type": "desktop_command",
                "runner_id": self.runner_id,
                "request_id": "desktop-restart-0001",
                "host_ref": HOST_REF,
                "thread_ref": THREAD_REF,
                "expected_thread_revision": 7,
                "action": "interrupt",
                "expected_turn_ref": TURN_REF,
                "created_at": NOW.isoformat(),
                "expires_at": (NOW + dt.timedelta(minutes=2)).isoformat(),
            }
        )
        self.store.prepare_command(command=pending_command, intent_digest="sha256:" + "f" * 64)
        restarted = DesktopStore(self.path)
        self.assertEqual(restarted.command("desktop-restart-0001")["state"], "unknown")

    def test_fast_receipt_cannot_be_overwritten_by_late_transport_mark(self) -> None:
        publisher = ImmediateReceiptPublisher()
        service = DesktopControllerService(
            self.store,
            publisher=publisher,
            now=lambda: NOW,
            runner_authorizer=lambda _runner_id: True,
        )
        publisher.service = service
        result = service.submit(
            THREAD_REF,
            "interrupt",
            {
                "request_id": "desktop-fast-receipt-0001",
                "expected_turn_ref": TURN_REF,
                "thread_revision": 7,
            },
        )
        self.assertEqual(result["state"], "confirmed")
        self.assertEqual(self.store.command("desktop-fast-receipt-0001")["state"], "confirmed")

    def test_bounded_history_long_poll_offline_and_stale_host_fail_closed(self) -> None:
        with mock.patch("codex_controller.desktop_store.SNAPSHOTS_PER_THREAD", 2):
            self.service.receive("desktop_snapshot", snapshot(self.runner_id, revision=8))
            self.service.receive("desktop_snapshot", snapshot(self.runner_id, revision=9))
        with self.store._connect() as connection:  # noqa: SLF001 - verifies bounded persistence
            count = connection.execute(
                "SELECT COUNT(*) AS value FROM desktop_snapshots WHERE thread_ref=?",
                (THREAD_REF,),
            ).fetchone()["value"]
        self.assertEqual(count, 2)

        with mock.patch("codex_controller.desktop_store.EVENTS_PER_HOST", 2):
            for sequence in (1, 2, 3):
                self.service.receive(
                    "desktop_event",
                    event(self.runner_id, sequence=sequence, revision=9),
                )
        listed = self.service.events(
            THREAD_REF,
            after_cursor=0,
            limit=10,
            wait_seconds=0,
        )
        self.assertEqual([item["event_sequence"] for item in listed["events"]], [2, 3])
        after = listed["next_cursor"]
        result: list[dict] = []

        def wait_for_event() -> None:
            result.append(
                self.service.events(
                    THREAD_REF,
                    after_cursor=after,
                    limit=10,
                    wait_seconds=1,
                )
            )

        worker = threading.Thread(target=wait_for_event)
        worker.start()
        time.sleep(0.05)
        self.service.receive(
            "desktop_event",
            event(self.runner_id, sequence=4, revision=9),
        )
        worker.join(timeout=2)
        self.assertFalse(worker.is_alive())
        self.assertEqual(result[0]["events"][0]["event_sequence"], 4)

        self.publisher.error = RelayPublishError("runner_offline", definitely_undelivered=True)
        with self.assertRaises(StoreError) as context:
            self.service.submit(
                THREAD_REF,
                "interrupt",
                {
                    "request_id": "desktop-offline-0001",
                    "expected_turn_ref": TURN_REF,
                    "thread_revision": 9,
                },
            )
        self.assertEqual(context.exception.code, "runner_offline")
        self.assertEqual(self.store.command("desktop-offline-0001")["state"], "failed")

        stale = DesktopControllerService(
            self.store,
            publisher=Publisher(),
            now=lambda: NOW + dt.timedelta(minutes=2),
            host_stale_seconds=90,
            runner_authorizer=lambda _runner_id: True,
        )
        with self.assertRaises(StoreError) as context:
            stale.submit(
                THREAD_REF,
                "interrupt",
                {
                    "request_id": "desktop-stale-host-0001",
                    "expected_turn_ref": TURN_REF,
                    "thread_revision": 9,
                },
            )
        self.assertEqual(context.exception.code, "desktop_host_stale")

        heartbeat_backed = DesktopControllerService(
            self.store,
            publisher=Publisher(),
            now=lambda: NOW + dt.timedelta(days=1),
            runner_authorizer=lambda _runner_id: True,
            runner_status_provider=lambda _runner_id: {"connectivity_state": "online"},
        )
        self.assertTrue(heartbeat_backed.hosts()["hosts"][0]["online"])


class DesktopApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.path = Path(self.temporary.name) / "controller.sqlite3"
        self.controller_store = ControllerStore(self.path)
        self.runner_store = RunnerStore(self.path)
        created = self.runner_store.create_enrollment(enrollment_payload())
        self.runner_id = created["runner"]["runner_id"]
        redeemed = self.runner_store.redeem_enrollment(
            redeem_payload(self.runner_id, created["enrollment"]["token"])
        )
        self.credential = redeemed["credential"]["secret"]
        runner = self.runner_store.runner(self.runner_id)
        self.runner_store.update_runner(
            self.runner_id,
            {
                "admin_state": "enabled",
                "revision": runner["revision"],
                "request_id": "desktop-runner-enable-0001",
            },
        )
        self.publisher = Publisher()
        self.desktop_store = DesktopStore(self.path)
        self.desktop = DesktopControllerService(
            self.desktop_store,
            publisher=self.publisher,
            now=lambda: NOW,
            runner_authorizer=lambda runner_id: desktop_runner_authorized(
                self.runner_store.runner(runner_id)
            ),
        )
        self.manager = RunnerManagerService(
            self.runner_store,
            publisher=self.publisher,
            desktop_controller=self.desktop,
        )
        self.controller = ControllerService(
            self.controller_store,
            App(),  # type: ignore[arg-type]
            intake_enabled=False,
            auth_mode="api_key",
            api_key="fixture-api-key",
            runner_manager=self.manager,
            desktop_controller=self.desktop,
        )
        self.callback_token = "c" * 32
        self.server = create_server(
            "127.0.0.1",
            0,
            service=self.controller,
            api_token="a" * 32,
            runner_relay_controller_api_token=self.callback_token,
            max_request_bytes=1024 * 1024,
        )
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)
        self.temporary.cleanup()

    def request(
        self,
        method: str,
        path: str,
        payload: dict | None = None,
        headers: dict[str, str] | None = None,
    ) -> tuple[int, dict]:
        connection = HTTPConnection("127.0.0.1", self.server.server_port, timeout=3)
        body = None if payload is None else json.dumps(payload).encode("utf-8")
        request_headers = dict(headers or {})
        if body is not None:
            request_headers.setdefault("Content-Type", "application/json")
        try:
            connection.request(method, path, body=body, headers=request_headers)
            response = connection.getresponse()
            return response.status, json.loads(response.read())
        finally:
            connection.close()

    def csrf(self) -> str:
        status, result = self.request("GET", "/api/status")
        self.assertEqual(status, 200)
        return result["csrf_token"]

    def asset(self, path: str) -> tuple[int, dict[str, str], str]:
        connection = HTTPConnection("127.0.0.1", self.server.server_port, timeout=3)
        try:
            connection.request("GET", path)
            response = connection.getresponse()
            return response.status, dict(response.getheaders()), response.read().decode("utf-8")
        finally:
            connection.close()

    def test_desktop_ingress_page_assets_and_privacy_contract(self) -> None:
        status, headers, body = self.asset("/desktop")
        self.assertEqual(status, 308)
        self.assertEqual(headers["Location"], "desktop/")
        self.assertEqual(body, "")

        status, headers, body = self.asset("/desktop/")
        self.assertEqual(status, 200)
        self.assertIn("text/html", headers["Content-Type"])
        self.assertIn("Codex 控制器", body)
        self.assertIn('src="desktop.js"', body)
        self.assertIn("viewport-fit=cover", body)
        self.assertIn("safe-area-inset-bottom", body)
        self.assertIn("color-scheme:light", body)
        self.assertIn("--bg:#f7f7f5", body)
        self.assertIn(".detail-open .detail-panel{display:block}", body)
        self.assertIn(".composer{position:fixed", body)
        self.assertIn(".metrics{display:none}", body)
        self.assertIn("grid-template-columns:repeat(5,1fr)", body)
        self.assertIn('class="mobile-nav"', body)
        self.assertIn('<a href="../">设置</a>', body)
        self.assertIn('id="projectPanel"', body)
        self.assertIn('id="newTaskSheet"', body)
        self.assertIn('id="modelSelect"', body)

        status, headers, script = self.asset("/desktop/desktop.js")
        self.assertEqual(status, 200)
        self.assertIn("text/javascript", headers["Content-Type"])
        self.assertIn("../api/desktop/v1", script)
        self.assertIn("Asia/Shanghai", script)
        self.assertIn("X-CSRF-Token", script)
        self.assertIn("mode: state.mode", script)
        self.assertIn("model_override_v1", script)
        self.assertIn("state.selectedModel = ''", script)
        self.assertIn("...(model ? {model} : {})", script)
        self.assertIn("pendingCreate", script)
        self.assertIn("检查创建结果", script)
        self.assertIn("for (let attempt = 0; attempt < 12; attempt += 1)", script)
        self.assertIn("原生快速调整保持同一 Turn，不允许切换模型", script)
        self.assertIn("wait_seconds=20", script)
        self.assertNotIn("innerHTML", script)
        self.assertNotIn("localStorage", script)
        self.assertNotIn("sessionStorage", script)
        self.assertNotIn("thread_id", script)
        self.assertNotIn("turn_id", script)
        self.assertNotIn("01a01f1b-b2cb-7762-b352-590ea7a3ae57", script)
        self.assertIn("同一个 threadId", script)
        self.assertIn("detail.control_state === 'protocol_degraded'", script)
        self.assertIn("create_thread_v1", script)
        self.assertIn("`${API}/threads`", script)
        self.assertIn("result.state !== 'confirmed'", script)
        self.assertIn("草稿仍保留", script)
        self.assertIn("document.body.classList.add('detail-open')", script)
        self.assertIn("state.drafts", script)
        self.assertIn("host_ref: host.host_ref", script)
        self.assertIn("project_ref: projectRef", script)

        combined = DESKTOP_DASHBOARD_HTML + DESKTOP_DASHBOARD_JS
        for required in (
            "安全调整",
            "原生快速调整",
            "中断当前 Turn",
            "继续此任务",
            "归档",
            "恢复归档",
            "recovery_required",
            "protocol_degraded",
            "App 默认",
            "最近命令模型",
            "沿用原任务模型",
        ):
            self.assertIn(required, combined)

    def test_desktop_api_model_override_keeps_same_thread_and_rejects_native_or_unknown(self) -> None:
        callback_headers = {
            "Authorization": "Bearer " + self.callback_token,
            "X-Runner-Credential": self.credential,
        }
        status, result = self.request(
            "POST",
            "/internal/v2/runner-relay/events/desktop_snapshot",
            snapshot(self.runner_id),
            callback_headers,
        )
        self.assertEqual(status, 200, result)
        csrf = self.csrf()
        base = {
            "expected_turn_ref": TURN_REF,
            "thread_revision": 7,
            "input": "调整当前方向",
            "model": "gpt-5.6-terra",
        }
        status, result = self.request(
            "POST",
            f"/api/desktop/v1/threads/{THREAD_REF}/steer",
            {**base, "request_id": "desktop-api-native-model-0001", "mode": "native"},
            {"X-CSRF-Token": csrf},
        )
        self.assertEqual(status, 400, result)
        self.assertEqual(result["error"]["code"], "desktop_model_invalid")
        status, result = self.request(
            "POST",
            f"/api/desktop/v1/threads/{THREAD_REF}/steer",
            {**base, "request_id": "desktop-api-unknown-model-0001", "model": "gpt-5.6-missing"},
            {"X-CSRF-Token": csrf},
        )
        self.assertEqual(status, 409, result)
        self.assertEqual(result["error"]["code"], "desktop_model_unavailable")
        status, result = self.request(
            "POST",
            f"/api/desktop/v1/threads/{THREAD_REF}/steer",
            {**base, "request_id": "desktop-api-safe-model-0001", "mode": "safe"},
            {"X-CSRF-Token": csrf},
        )
        self.assertEqual(status, 202, result)
        safe_command = self.publisher.desktop_commands[-1][1]
        self.assertEqual(safe_command["thread_ref"], THREAD_REF)
        self.assertEqual(safe_command["model"], "gpt-5.6-terra")
        status, result = self.request(
            "POST",
            "/internal/v2/runner-relay/events/desktop_receipt",
            receipt(
                self.runner_id,
                "desktop-api-safe-model-0001",
                revision=8,
                action="steer",
            ),
            callback_headers,
        )
        self.assertEqual(status, 200, result)
        status, result = self.request(
            "POST",
            "/internal/v2/runner-relay/events/desktop_snapshot",
            snapshot(self.runner_id, revision=8, status="idle"),
            callback_headers,
        )
        self.assertEqual(status, 200, result)
        status, result = self.request(
            "POST",
            f"/api/desktop/v1/threads/{THREAD_REF}/continue",
            {
                "request_id": "desktop-api-continue-model-0001",
                "thread_revision": 8,
                "input": "继续同一个原任务",
                "model": "gpt-5.6-sol",
            },
            {"X-CSRF-Token": csrf},
        )
        self.assertEqual(status, 202, result)
        continued = self.publisher.desktop_commands[-1][1]
        self.assertEqual(continued["thread_ref"], THREAD_REF)
        self.assertEqual(continued["model"], "gpt-5.6-sol")

    def test_desktop_api_create_is_idempotent_and_waits_for_snapshot_truth(self) -> None:
        callback_headers = {
            "Authorization": "Bearer " + self.callback_token,
            "X-Runner-Credential": self.credential,
        }
        capabilities = [
            "list_read",
            "owner_follower",
            "continue_same_thread",
            "model_override_v1",
            "create_thread_v1",
        ]
        status, result = self.request(
            "POST",
            "/internal/v2/runner-relay/events/desktop_snapshot",
            snapshot(self.runner_id, capabilities=capabilities),
            callback_headers,
        )
        self.assertEqual(status, 200, result)
        payload = {
            "request_id": "desktop-api-create-0001",
            "host_ref": HOST_REF,
            "project_ref": PROJECT_REF,
            "input": "创建一个新任务",
            "model": "gpt-5.6-terra",
        }
        status, submitted = self.request(
            "POST",
            "/api/desktop/v1/threads",
            payload,
            {"X-CSRF-Token": self.csrf()},
        )
        self.assertEqual(status, 202, submitted)
        self.assertEqual(submitted["result"]["state"], "submitted")
        command = self.publisher.desktop_commands[-1][1]
        self.assertEqual(command["project_ref"], PROJECT_REF)
        self.assertIsNone(command["thread_ref"])
        self.assertIsNone(command["expected_thread_revision"])
        self.assertNotIn("cwd", command)

        created_thread_ref = "TH-" + "E" * 20
        created_turn_ref = "TR-" + "F" * 20
        confirmed = digest(
            {
                "version": 1,
                "message_type": "desktop_receipt",
                "runner_id": self.runner_id,
                "created_at": NOW.isoformat(),
                "request_id": payload["request_id"],
                "host_ref": HOST_REF,
                "project_ref": PROJECT_REF,
                "thread_ref": created_thread_ref,
                "turn_ref": created_turn_ref,
                "action": "create",
                "state": "confirmed",
                "thread_revision": 1,
            }
        )
        status, accepted = self.request(
            "POST",
            "/internal/v2/runner-relay/events/desktop_receipt",
            confirmed,
            callback_headers,
        )
        self.assertEqual(status, 200, accepted)
        status, replayed = self.request(
            "POST",
            "/api/desktop/v1/threads",
            payload,
            {"X-CSRF-Token": self.csrf()},
        )
        self.assertEqual(status, 202, replayed)
        self.assertEqual(replayed["result"]["state"], "confirmed")
        self.assertEqual(replayed["result"]["thread_ref"], created_thread_ref)
        self.assertEqual(len(self.publisher.desktop_commands), 1)
        status, threads = self.request("GET", "/api/desktop/v1/threads")
        self.assertEqual(status, 200, threads)
        self.assertNotIn(created_thread_ref, {item["thread_ref"] for item in threads["result"]["threads"]})

    def test_internal_relay_acl_ingress_queries_and_command_downlink(self) -> None:
        callback_headers = {
            "Authorization": "Bearer " + self.callback_token,
            "X-Runner-Credential": self.credential,
        }
        status, result = self.request(
            "POST",
            "/internal/v2/runner-relay/events/desktop_snapshot",
            snapshot(self.runner_id),
            callback_headers,
        )
        self.assertEqual(status, 200, result)
        self.assertEqual(result["result"]["status"], "stored")
        status, hosts = self.request("GET", "/api/desktop/v1/hosts")
        self.assertEqual(status, 200)
        self.assertTrue(hosts["result"]["hosts"][0]["online"])
        status, threads = self.request(
            "GET",
            f"/api/desktop/v1/threads?host_ref={HOST_REF}&project_ref={PROJECT_REF}&status=active",
        )
        self.assertEqual(status, 200)
        self.assertEqual(threads["result"]["threads"][0]["thread_ref"], THREAD_REF)
        csrf = self.csrf()
        status, command = self.request(
            "POST",
            f"/api/desktop/v1/threads/{THREAD_REF}/interrupt",
            {
                "request_id": "desktop-api-interrupt-0001",
                "expected_turn_ref": TURN_REF,
                "thread_revision": 7,
            },
            {"X-CSRF-Token": csrf},
        )
        self.assertEqual(status, 202, command)
        self.assertEqual(command["result"]["state"], "submitted")
        self.assertEqual(self.publisher.desktop_commands[0][0], self.runner_id)

    def test_bad_callback_credential_csrf_and_query_fail_closed(self) -> None:
        status, result = self.request(
            "POST",
            "/internal/v2/runner-relay/events/desktop_snapshot",
            snapshot(self.runner_id),
            {
                "Authorization": "Bearer " + self.callback_token,
                "X-Runner-Credential": "x" * 64,
            },
        )
        self.assertEqual(status, 401)
        self.assertEqual(result["error"]["code"], "runner_not_authorized")
        status, _result = self.request(
            "POST",
            f"/api/desktop/v1/threads/{THREAD_REF}/archive",
            {"request_id": "desktop-api-archive-0001", "thread_revision": 7},
        )
        self.assertEqual(status, 403)
        status, result = self.request("GET", "/api/desktop/v1/threads?cursor=01")
        self.assertEqual(status, 400)
        self.assertEqual(result["error"]["code"], "desktop_query_invalid")

    def test_project_allowlist_capability_and_disabled_runner_fail_closed(self) -> None:
        callback_headers = {
            "Authorization": "Bearer " + self.callback_token,
            "X-Runner-Credential": self.credential,
        }
        forbidden = snapshot(self.runner_id)
        forbidden["snapshot"]["project_alias"] = "other-project"
        forbidden["body_digest"] = body_digest(forbidden)
        status, result = self.request(
            "POST",
            "/internal/v2/runner-relay/events/desktop_snapshot",
            forbidden,
            callback_headers,
        )
        self.assertEqual(status, 403)
        self.assertEqual(result["error"]["code"], "desktop_project_not_allowed")

        with self.runner_store._connect() as connection:  # noqa: SLF001 - protocol capability gate
            connection.execute(
                "UPDATE runner_registry SET capabilities_json='[]' WHERE runner_id=?",
                (self.runner_id,),
            )
        status, result = self.request(
            "POST",
            "/internal/v2/runner-relay/events/desktop_snapshot",
            snapshot(self.runner_id),
            callback_headers,
        )
        self.assertEqual(status, 403)
        self.assertEqual(result["error"]["code"], "desktop_runner_capability_required")

        with self.runner_store._connect() as connection:  # noqa: SLF001 - restore fixture capability
            connection.execute(
                "UPDATE runner_registry SET capabilities_json=? WHERE runner_id=?",
                (json.dumps(["registered_projects", "desktop_takeover_v1"]), self.runner_id),
            )
        status, result = self.request(
            "POST",
            "/internal/v2/runner-relay/events/desktop_snapshot",
            snapshot(self.runner_id),
            callback_headers,
        )
        self.assertEqual(status, 200, result)
        runner = self.runner_store.runner(self.runner_id)
        self.runner_store.update_runner(
            self.runner_id,
            {
                "admin_state": "disabled",
                "revision": runner["revision"],
                "request_id": "desktop-runner-disable-0001",
            },
        )
        status, hosts = self.request("GET", "/api/desktop/v1/hosts")
        self.assertEqual(status, 200)
        self.assertFalse(hosts["result"]["hosts"][0]["write_available"])
        status, result = self.request(
            "POST",
            f"/api/desktop/v1/threads/{THREAD_REF}/interrupt",
            {
                "request_id": "desktop-disabled-runner-0001",
                "expected_turn_ref": TURN_REF,
                "thread_revision": 7,
            },
            {"X-CSRF-Token": self.csrf()},
        )
        self.assertEqual(status, 403)
        self.assertEqual(result["error"]["code"], "desktop_runner_not_authorized")


if __name__ == "__main__":
    unittest.main()
