from __future__ import annotations

import datetime as dt
from http.client import HTTPConnection
import json
from pathlib import Path
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
) -> dict:
    active_turn = TURN_REF if status == "active" else None
    thread = {
        "project_alias": "demo-project",
        "project_ref": PROJECT_REF,
        "thread_ref": THREAD_REF,
        "title": title,
        "preview": "公开摘要",
        "status": status,
        "active_turn_ref": active_turn,
        "thread_revision": revision,
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
        conflicting = snapshot(self.runner_id, revision=7, title="冲突快照")
        with self.assertRaises(StoreError) as context:
            self.service.receive("desktop_snapshot", conflicting)
        self.assertEqual(context.exception.code, "desktop_revision_conflict")
        encoded = json.dumps(self.service.thread(THREAD_REF), ensure_ascii=False)
        self.assertNotIn(self.runner_id, encoded)

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
        self.assertIn("桌面原任务工作台", body)
        self.assertIn('src="desktop.js"', body)
        self.assertIn("viewport-fit=cover", body)
        self.assertIn("safe-area-inset-bottom", body)
        self.assertIn("#detailContent{display:flex;flex-direction:column}", body)
        self.assertIn(".composer{position:relative;bottom:auto;order:4", body)
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
        self.assertIn("原生快速调整保持同一 Turn，不允许切换模型", script)
        self.assertIn("wait_seconds=20", script)
        self.assertNotIn("innerHTML", script)
        self.assertNotIn("localStorage", script)
        self.assertNotIn("sessionStorage", script)
        self.assertNotIn("thread_id", script)
        self.assertNotIn("turn_id", script)
        self.assertNotIn("01a01f1b-b2cb-7762-b352-590ea7a3ae57", script)
        self.assertIn("同一个 threadId", script)

        combined = DESKTOP_DASHBOARD_HTML + DESKTOP_DASHBOARD_JS
        for required in (
            "安全调整",
            "原生快速调整",
            "停止当前 Turn",
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
