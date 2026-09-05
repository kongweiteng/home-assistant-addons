from __future__ import annotations

import datetime as dt
from pathlib import Path
import sqlite3
import tempfile
import threading
import time
import unittest

from codex_controller.desktop_api import post_desktop_api
from codex_controller.desktop_protocol import (
    DesktopProtocolError,
    body_digest,
    validate_desktop_document,
)
from codex_controller.desktop_service import DesktopControllerService
from codex_controller.desktop_store import DesktopStore
from codex_controller.store import StoreError


NOW = dt.datetime(2026, 9, 5, 8, 30, tzinfo=dt.timezone(dt.timedelta(hours=8)))
RUNNER_ID = "RN-" + "E" * 20
HOST_REF = "HS-" + "A" * 20
PROJECT_REF = "PJ-" + "B" * 20
THREAD_REF = "TH-" + "C" * 20
TURN_REF = "TR-" + "D" * 20
QUEUE_REF = "QS-" + "F" * 20
SECOND_QUEUE_REF = "QS-" + "G" * 20
CREATED_THREAD_REF = "TH-" + "H" * 20
CREATED_TURN_REF = "TR-" + "J" * 20


def digest(document: dict) -> dict:
    result = dict(document)
    result["body_digest"] = body_digest(result)
    return result


def model_catalog(*, complete: bool) -> list[dict]:
    model = {"id": "gpt-5.6-sol", "display_name": "GPT-5.6 Sol", "is_default": True}
    if complete:
        model.update(
            {
                "default_reasoning_effort": "medium",
                "supported_reasoning_efforts": [
                    {"id": "low", "description": "更快"},
                    {"id": "medium", "description": "均衡"},
                ],
            }
        )
    return [model]


def snapshot(
    *,
    complete_models: bool = True,
    reasoning_capability: bool = True,
    queue: list[dict] | None = None,
    sequence: int | None = 1,
) -> dict:
    capabilities = [
        "list_read",
        "interrupt_expected_turn",
        "continue_same_thread",
        "thread_queue_v1",
        "model_override_v1",
    ]
    if reasoning_capability:
        capabilities.append("reasoning_effort_v1")
    document = {
        "version": 1,
        "message_type": "desktop_snapshot",
        "runner_id": RUNNER_ID,
        "created_at": NOW.isoformat(),
        "host_ref": HOST_REF,
        "project_ref": PROJECT_REF,
        "thread_ref": THREAD_REF,
        "thread_revision": 7,
        "snapshot": {
            "project_alias": "demo-project",
            "project_ref": PROJECT_REF,
            "thread_ref": THREAD_REF,
            "title": "排队消息测试",
            "preview": "公开摘要",
            "status": "active",
            "active_turn_ref": TURN_REF,
            "thread_revision": 7,
            "control_revision": 12,
            "created_at": NOW.isoformat(),
            "updated_at": NOW.isoformat(),
            "history_incomplete": False,
            "turns": [],
            "control_state": "ready",
            "model": "gpt-5.6-sol",
            "reasoning_effort": "medium",
            "queued_submissions": queue
            if queue is not None
            else [
                {
                    "queue_ref": QUEUE_REF,
                    "position": 0,
                    "text": "稍后执行第一项",
                    "editable": True,
                    "input_kind": "text",
                },
                {
                    "queue_ref": SECOND_QUEUE_REF,
                    "position": 1,
                    "text": "[包含手机端不可编辑的非文本内容]",
                    "editable": False,
                    "input_kind": "non_text",
                },
            ],
        },
        "host": {
            "host_ref": HOST_REF,
            "state": "normal",
            "app_version": "26.810.52044",
            "app_build": "6662",
            "cli_version": "0.148.0-alpha.9",
            "schema_digest": "a" * 64,
            "socket_mode": "0600",
            "tcp_listener_count": 0,
            "capabilities": capabilities,
            "control_enabled": True,
            "models": model_catalog(complete=complete_models),
            "synced_at": NOW.isoformat(),
        },
    }
    if sequence is not None:
        document["snapshot_sequence"] = sequence
    return digest(document)


def receipt(request_id: str, state: str, *, action: str = "queue_update") -> dict:
    return digest(
        {
            "version": 1,
            "message_type": "desktop_receipt",
            "runner_id": RUNNER_ID,
            "created_at": (NOW + dt.timedelta(seconds=1 if state == "accepted" else 2)).isoformat(),
            "request_id": request_id,
            "host_ref": HOST_REF,
            "thread_ref": THREAD_REF,
            "turn_ref": TURN_REF,
            "action": action,
            "state": state,
            "thread_revision": 7,
            "queue_ref": QUEUE_REF,
        }
    )


def desktop_event(sequence: int = 1) -> dict:
    return digest(
        {
            "version": 1,
            "message_type": "desktop_event",
            "runner_id": RUNNER_ID,
            "created_at": NOW.isoformat(),
            "host_ref": HOST_REF,
            "project_ref": PROJECT_REF,
            "thread_ref": THREAD_REF,
            "turn_ref": TURN_REF,
            "thread_revision": 7,
            "event_sequence": sequence,
            "event_kind": "thread.updated",
            "source": "desktop",
            "payload": {"status": "active"},
        }
    )


class Publisher:
    def __init__(self) -> None:
        self.commands: list[tuple[str, dict]] = []

    def publish_desktop_command(self, runner_id: str, document: dict) -> None:
        self.commands.append((runner_id, dict(document)))


class DesktopQueueProtocolTests(unittest.TestCase):
    def test_legacy_model_catalog_is_accepted_without_reasoning_capability(self) -> None:
        document = snapshot(complete_models=False, reasoning_capability=False)
        validated = validate_desktop_document("desktop_snapshot", document)
        self.assertEqual(validated["host"]["models"], model_catalog(complete=False))

    def test_reasoning_capability_requires_complete_catalog(self) -> None:
        with self.assertRaises(DesktopProtocolError) as context:
            validate_desktop_document(
                "desktop_snapshot",
                snapshot(complete_models=False, reasoning_capability=True),
            )
        self.assertEqual(context.exception.code, "desktop_host_invalid")

    def test_queue_snapshot_requires_strict_public_shape(self) -> None:
        invalid = snapshot()
        invalid["snapshot"]["queued_submissions"][0]["position"] = 4
        invalid["body_digest"] = body_digest(invalid)
        with self.assertRaises(DesktopProtocolError) as context:
            validate_desktop_document("desktop_snapshot", invalid)
        self.assertEqual(context.exception.code, "desktop_snapshot_invalid")

    def test_queue_capability_requires_snapshot_sequence(self) -> None:
        document = snapshot(sequence=None)
        with self.assertRaises(DesktopProtocolError) as context:
            validate_desktop_document("desktop_snapshot", document)
        self.assertEqual(context.exception.code, "desktop_host_invalid")

    def test_snapshot_sequence_is_optional_but_must_be_a_positive_integer(self) -> None:
        legacy = snapshot(sequence=None)
        legacy["host"]["capabilities"].remove("thread_queue_v1")
        legacy["body_digest"] = body_digest(legacy)
        self.assertNotIn(
            "snapshot_sequence",
            validate_desktop_document("desktop_snapshot", legacy),
        )
        for invalid in (None, 0, -1, True, 1.0, "1", 1 << 63):
            with self.subTest(sequence=invalid):
                document = snapshot()
                document["snapshot_sequence"] = invalid
                document["body_digest"] = body_digest(document)
                with self.assertRaises(DesktopProtocolError) as context:
                    validate_desktop_document("desktop_snapshot", document)
                self.assertEqual(context.exception.code, "desktop_sequence_invalid")


class DesktopQueueServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.path = Path(self.temporary.name) / "controller.sqlite3"
        self.publisher = Publisher()
        self.store = DesktopStore(self.path)
        self.service = DesktopControllerService(
            self.store,
            publisher=self.publisher,
            now=lambda: NOW,
            runner_authorizer=lambda _runner_id: True,
        )
        self.service.receive("desktop_snapshot", snapshot())

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_same_revision_queue_only_refresh_is_stored(self) -> None:
        original = snapshot(sequence=1)
        duplicate = self.service.receive("desktop_snapshot", original)
        self.assertEqual(duplicate["status"], "duplicate")
        updated_queue = [
            {
                "queue_ref": QUEUE_REF,
                "position": 0,
                "text": "手机端刚刚修改",
                "editable": True,
                "input_kind": "text",
            },
            {
                "queue_ref": SECOND_QUEUE_REF,
                "position": 1,
                "text": "[包含手机端不可编辑的非文本内容]",
                "editable": False,
                "input_kind": "non_text",
            },
        ]
        refreshed = self.service.receive(
            "desktop_snapshot",
            snapshot(queue=updated_queue, sequence=2),
        )
        self.assertEqual(refreshed["status"], "refreshed")
        self.assertEqual(
            self.service.thread(THREAD_REF)["snapshot"]["queued_submissions"],
            updated_queue,
        )

        replayed = self.service.receive("desktop_snapshot", original)
        self.assertEqual(replayed["status"], "stale_ignored")
        self.assertEqual(
            self.service.thread(THREAD_REF)["snapshot"]["queued_submissions"],
            updated_queue,
        )
        with sqlite3.connect(self.path) as connection:
            thread_sequence = connection.execute(
                "SELECT snapshot_sequence FROM desktop_threads WHERE thread_ref=?",
                (THREAD_REF,),
            ).fetchone()[0]
            source_sequence = connection.execute(
                "SELECT source_sequence FROM desktop_snapshots "
                "WHERE thread_ref=? AND thread_revision=?",
                (THREAD_REF, 7),
            ).fetchone()[0]
        self.assertEqual((thread_sequence, source_sequence), (2, 2))

    def test_same_snapshot_sequence_with_different_queue_conflicts(self) -> None:
        updated_queue = [
            {
                "queue_ref": QUEUE_REF,
                "position": 0,
                "text": "same sequence must not overwrite",
                "editable": True,
                "input_kind": "text",
            }
        ]
        with self.assertRaises(StoreError) as context:
            self.service.receive(
                "desktop_snapshot",
                snapshot(queue=updated_queue, sequence=1),
            )
        self.assertEqual(context.exception.code, "desktop_revision_conflict")

    def test_same_snapshot_sequence_with_same_snapshot_refreshes_envelope(self) -> None:
        updated = snapshot(sequence=1)
        updated["created_at"] = (NOW + dt.timedelta(seconds=1)).isoformat()
        updated["body_digest"] = body_digest(updated)

        result = self.service.receive("desktop_snapshot", updated)

        self.assertEqual(result["status"], "refreshed")
        with sqlite3.connect(self.path) as connection:
            stored = connection.execute(
                "SELECT body_digest,source_sequence FROM desktop_snapshots "
                "WHERE thread_ref=? AND thread_revision=?",
                (THREAD_REF, 7),
            ).fetchone()
        self.assertEqual(stored, (updated["body_digest"], 1))

    def test_missing_snapshot_sequence_does_not_authorize_queue_only_refresh(self) -> None:
        updated_queue = [
            {
                "queue_ref": QUEUE_REF,
                "position": 0,
                "text": "legacy envelope cannot advance the queue",
                "editable": True,
                "input_kind": "text",
            }
        ]
        document = snapshot(queue=updated_queue, sequence=None)
        document["host"]["capabilities"].remove("thread_queue_v1")
        document["body_digest"] = body_digest(document)
        with self.assertRaises(StoreError) as context:
            self.store.ingest_snapshot(document, observed_at=NOW.isoformat())
        self.assertEqual(context.exception.code, "desktop_revision_conflict")

    def test_sequence_can_advance_queue_from_a_legacy_null_baseline(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            service = DesktopControllerService(
                DesktopStore(Path(temporary) / "controller.sqlite3"),
                publisher=Publisher(),
                now=lambda: NOW,
                runner_authorizer=lambda _runner_id: True,
            )
            legacy = snapshot(sequence=None)
            legacy["host"]["capabilities"].remove("thread_queue_v1")
            legacy["host"]["capabilities"].remove("reasoning_effort_v1")
            for field in ("model", "reasoning_effort", "queued_submissions"):
                legacy["snapshot"].pop(field)
            legacy["body_digest"] = body_digest(legacy)
            service.receive("desktop_snapshot", legacy)
            updated_queue = [
                {
                    "queue_ref": QUEUE_REF,
                    "position": 0,
                    "text": "sequence establishes the first queue watermark",
                    "editable": True,
                    "input_kind": "text",
                }
            ]

            result = service.receive(
                "desktop_snapshot",
                snapshot(queue=updated_queue, sequence=141861),
            )

            self.assertEqual(result["status"], "refreshed")
            self.assertEqual(result["thread"]["snapshot_sequence"], 141861)
            self.assertEqual(
                service.thread(THREAD_REF)["snapshot"]["queued_submissions"],
                updated_queue,
            )

    def test_first_sequence_enriches_a_pre_queue_snapshot_and_safe_control_state(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            service = DesktopControllerService(
                DesktopStore(Path(temporary) / "controller.sqlite3"),
                publisher=Publisher(),
                now=lambda: NOW,
                runner_authorizer=lambda _runner_id: True,
            )
            legacy = snapshot(sequence=None)
            legacy["host"]["capabilities"].remove("thread_queue_v1")
            legacy["host"]["capabilities"].remove("reasoning_effort_v1")
            for field in ("model", "reasoning_effort", "queued_submissions"):
                legacy["snapshot"].pop(field, None)
            legacy["body_digest"] = body_digest(legacy)
            service.receive("desktop_snapshot", legacy)

            enriched = snapshot(sequence=1)
            enriched["snapshot"]["control_revision"] = 13
            enriched["snapshot"]["turns"] = [
                {
                    "turn_ref": TURN_REF,
                    "status": "active",
                    "created_at": NOW.isoformat(),
                    "updated_at": NOW.isoformat(),
                }
            ]
            enriched["body_digest"] = body_digest(enriched)

            result = service.receive("desktop_snapshot", enriched)

            self.assertEqual(result["status"], "refreshed")
            self.assertEqual(result["thread"]["snapshot_sequence"], 1)
            stored = service.thread(THREAD_REF)
            self.assertEqual(stored["snapshot"]["control_revision"], 13)
            self.assertEqual(stored["snapshot"]["model"], "gpt-5.6-sol")
            self.assertEqual(stored["snapshot"]["reasoning_effort"], "medium")
            with sqlite3.connect(Path(temporary) / "controller.sqlite3") as connection:
                source_sequence = connection.execute(
                    "SELECT source_sequence FROM desktop_snapshots "
                    "WHERE thread_ref=? AND thread_revision=?",
                    (THREAD_REF, 7),
                ).fetchone()[0]
            self.assertEqual(source_sequence, 1)

    def test_first_sequence_enrichment_cannot_hide_business_field_drift(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            service = DesktopControllerService(
                DesktopStore(Path(temporary) / "controller.sqlite3"),
                publisher=Publisher(),
                now=lambda: NOW,
                runner_authorizer=lambda _runner_id: True,
            )
            legacy = snapshot(sequence=None)
            legacy["host"]["capabilities"].remove("thread_queue_v1")
            legacy["host"]["capabilities"].remove("reasoning_effort_v1")
            for field in ("model", "reasoning_effort", "queued_submissions"):
                legacy["snapshot"].pop(field, None)
            legacy["body_digest"] = body_digest(legacy)
            service.receive("desktop_snapshot", legacy)
            drifted = snapshot(sequence=1)
            drifted["snapshot"]["title"] = "不能借迁移改变标题"
            drifted["body_digest"] = body_digest(drifted)

            with self.assertRaises(StoreError) as context:
                service.receive("desktop_snapshot", drifted)

            self.assertEqual(context.exception.code, "desktop_revision_conflict")

    def test_first_sequence_enrichment_rejects_key_shape_bypass(self) -> None:
        for mutation in ("partial", "extra_null", "remove_legacy_null"):
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory() as temporary:
                path = Path(temporary) / "controller.sqlite3"
                service = DesktopControllerService(
                    DesktopStore(path),
                    publisher=Publisher(),
                    now=lambda: NOW,
                    runner_authorizer=lambda _runner_id: True,
                )
                legacy = snapshot(sequence=None)
                legacy["host"]["capabilities"].remove("thread_queue_v1")
                legacy["host"]["capabilities"].remove("reasoning_effort_v1")
                for field in ("model", "reasoning_effort", "queued_submissions"):
                    legacy["snapshot"].pop(field)
                legacy["snapshot"]["legacy_null"] = None
                legacy["body_digest"] = body_digest(legacy)
                service.receive("desktop_snapshot", legacy)

                incoming = snapshot(sequence=141861)
                incoming["snapshot"]["legacy_null"] = None
                if mutation == "partial":
                    incoming["snapshot"].pop("reasoning_effort")
                elif mutation == "extra_null":
                    incoming["snapshot"]["extra_null"] = None
                else:
                    incoming["snapshot"].pop("legacy_null")
                incoming["body_digest"] = body_digest(incoming)

                with self.assertRaises(StoreError) as context:
                    service.receive("desktop_snapshot", incoming)

                self.assertEqual(context.exception.code, "desktop_revision_conflict")
                with sqlite3.connect(path) as connection:
                    sequences = connection.execute(
                        "SELECT t.snapshot_sequence,s.source_sequence "
                        "FROM desktop_threads t JOIN desktop_snapshots s "
                        "ON s.thread_ref=t.thread_ref AND s.thread_revision=t.thread_revision "
                        "WHERE t.thread_ref=?",
                        (THREAD_REF,),
                    ).fetchone()
                self.assertEqual(sequences, (None, None))

    def test_first_sequence_enrichment_requires_unseeded_snapshot_row(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "controller.sqlite3"
            service = DesktopControllerService(
                DesktopStore(path),
                publisher=Publisher(),
                now=lambda: NOW,
                runner_authorizer=lambda _runner_id: True,
            )
            legacy = snapshot(sequence=None)
            legacy["host"]["capabilities"].remove("thread_queue_v1")
            legacy["host"]["capabilities"].remove("reasoning_effort_v1")
            for field in ("model", "reasoning_effort", "queued_submissions"):
                legacy["snapshot"].pop(field)
            legacy["body_digest"] = body_digest(legacy)
            service.receive("desktop_snapshot", legacy)
            with sqlite3.connect(path) as connection:
                connection.execute(
                    "UPDATE desktop_snapshots SET source_sequence=99 WHERE thread_ref=?",
                    (THREAD_REF,),
                )

            with self.assertRaises(StoreError) as context:
                service.receive("desktop_snapshot", snapshot(sequence=141861))

            self.assertEqual(context.exception.code, "desktop_revision_conflict")

    def test_same_revision_with_missing_stored_digest_fails_closed(self) -> None:
        with sqlite3.connect(self.path) as connection:
            connection.execute(
                "UPDATE desktop_threads SET snapshot_digest=NULL WHERE thread_ref=?",
                (THREAD_REF,),
            )
        updated = snapshot(sequence=2)
        updated["snapshot"]["queued_submissions"] = []
        updated["body_digest"] = body_digest(updated)

        with self.assertRaises(StoreError) as context:
            self.service.receive("desktop_snapshot", updated)

        self.assertEqual(context.exception.code, "desktop_revision_conflict")

    def test_first_sequence_enrichment_preserves_fields_during_degraded_latch(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "controller.sqlite3"
            service = DesktopControllerService(
                DesktopStore(path),
                publisher=Publisher(),
                now=lambda: NOW,
                runner_authorizer=lambda _runner_id: True,
            )
            legacy = snapshot(sequence=None)
            legacy["host"]["capabilities"].remove("thread_queue_v1")
            legacy["host"]["capabilities"].remove("reasoning_effort_v1")
            for field in ("model", "reasoning_effort", "queued_submissions"):
                legacy["snapshot"].pop(field)
            legacy["body_digest"] = body_digest(legacy)
            service.receive("desktop_snapshot", legacy)

            degraded = snapshot(sequence=141861)
            degraded["snapshot"]["control_revision"] = None
            degraded["snapshot"]["control_state"] = "recovery_required"
            degraded["body_digest"] = body_digest(degraded)
            result = service.receive("desktop_snapshot", degraded)

            self.assertEqual(result["status"], "degraded_latched")
            stored = service.thread(THREAD_REF)
            self.assertEqual(stored["snapshot_sequence"], 141861)
            self.assertEqual(stored["snapshot"]["control_revision"], 12)
            self.assertEqual(stored["snapshot"]["control_state"], "recovery_required")
            self.assertEqual(stored["snapshot"]["model"], "gpt-5.6-sol")
            self.assertEqual(stored["snapshot"]["reasoning_effort"], "medium")
            self.assertIn("queued_submissions", stored["snapshot"])
            with sqlite3.connect(path) as connection:
                source_sequence = connection.execute(
                    "SELECT source_sequence FROM desktop_snapshots WHERE thread_ref=?",
                    (THREAD_REF,),
                ).fetchone()[0]
            self.assertEqual(source_sequence, 141861)

    def test_snapshot_model_and_reasoning_effort_are_bounded(self) -> None:
        for field, value, code in (
            ("model", "contains spaces", "desktop_model_invalid"),
            ("reasoning_effort", "contains spaces", "desktop_effort_invalid"),
        ):
            with self.subTest(field=field):
                document = snapshot()
                document["snapshot"][field] = value
                document["body_digest"] = body_digest(document)
                with self.assertRaises(DesktopProtocolError) as context:
                    validate_desktop_document("desktop_snapshot", document)
                self.assertEqual(context.exception.code, code)

    def test_greater_sequence_with_queue_and_control_changes_conflicts(self) -> None:
        updated = snapshot(
            queue=[
                {
                    "queue_ref": QUEUE_REF,
                    "position": 0,
                    "text": "queue and control must not advance together",
                    "editable": True,
                    "input_kind": "text",
                }
            ],
            sequence=2,
        )
        updated["snapshot"]["control_revision"] = 13
        updated["body_digest"] = body_digest(updated)

        with self.assertRaises(StoreError) as context:
            self.service.receive("desktop_snapshot", updated)

        self.assertEqual(context.exception.code, "desktop_revision_conflict")

    def test_snapshot_sequence_columns_are_added_without_losing_legacy_rows(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "legacy.sqlite3"
            digest_value = "sha256:" + "a" * 64
            with sqlite3.connect(path) as connection:
                connection.executescript(
                    """
                    CREATE TABLE desktop_threads(
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        thread_ref TEXT NOT NULL UNIQUE,
                        host_ref TEXT NOT NULL,
                        project_ref TEXT NOT NULL,
                        runner_id TEXT NOT NULL,
                        title TEXT NOT NULL,
                        status TEXT NOT NULL,
                        active_turn_ref TEXT,
                        thread_revision INTEGER NOT NULL,
                        control_revision INTEGER,
                        control_state TEXT NOT NULL,
                        snapshot_digest TEXT,
                        snapshot_json TEXT NOT NULL,
                        source_created_at TEXT,
                        source_updated_at TEXT,
                        observed_at TEXT NOT NULL
                    );
                    CREATE TABLE desktop_snapshots(
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        thread_ref TEXT NOT NULL,
                        thread_revision INTEGER NOT NULL,
                        body_digest TEXT NOT NULL UNIQUE,
                        document_json TEXT NOT NULL,
                        observed_at TEXT NOT NULL,
                        UNIQUE(thread_ref,thread_revision)
                    );
                    """
                )
                connection.execute(
                    "INSERT INTO desktop_threads("
                    "thread_ref,host_ref,project_ref,runner_id,title,status,thread_revision,"
                    "control_revision,control_state,snapshot_digest,snapshot_json,observed_at"
                    ") VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        THREAD_REF,
                        HOST_REF,
                        PROJECT_REF,
                        RUNNER_ID,
                        "legacy queue",
                        "active",
                        7,
                        12,
                        "ready",
                        digest_value,
                        "{}",
                        NOW.isoformat(),
                    ),
                )
                connection.execute(
                    "INSERT INTO desktop_snapshots("
                    "thread_ref,thread_revision,body_digest,document_json,observed_at"
                    ") VALUES(?,?,?,?,?)",
                    (THREAD_REF, 7, digest_value, "{}", NOW.isoformat()),
                )

            DesktopStore(path)

            with sqlite3.connect(path) as connection:
                thread_columns = {
                    row[1] for row in connection.execute("PRAGMA table_info(desktop_threads)")
                }
                snapshot_columns = {
                    row[1] for row in connection.execute("PRAGMA table_info(desktop_snapshots)")
                }
                thread_row = connection.execute(
                    "SELECT title,snapshot_sequence FROM desktop_threads WHERE thread_ref=?",
                    (THREAD_REF,),
                ).fetchone()
                snapshot_row = connection.execute(
                    "SELECT body_digest,source_sequence FROM desktop_snapshots WHERE thread_ref=?",
                    (THREAD_REF,),
                ).fetchone()
            self.assertIn("snapshot_sequence", thread_columns)
            self.assertIn("source_sequence", snapshot_columns)
            self.assertEqual(thread_row, ("legacy queue", None))
            self.assertEqual(snapshot_row, (digest_value, None))

    def test_queue_api_routes_publish_immediately_over_existing_publisher(self) -> None:
        result = post_desktop_api(
            self.service,
            f"/api/desktop/v1/threads/{THREAD_REF}/queue/{QUEUE_REF}/update",
            {"request_id": "queue-update-0001", "thread_revision": 7, "input": "修改后的消息"},
        )
        self.assertEqual(result["state"], "submitted")
        self.assertEqual(result["delivery_stage"], "relay_delivered")
        self.assertEqual(len(self.publisher.commands), 1)
        runner_id, command = self.publisher.commands[0]
        self.assertEqual(runner_id, RUNNER_ID)
        self.assertEqual(command["action"], "queue_update")
        self.assertEqual(command["queue_ref"], QUEUE_REF)
        self.assertEqual(command["input"], "修改后的消息")
        self.assertIsNone(command["expected_control_revision"])

    def test_non_event_command_change_wakes_detail_long_poll(self) -> None:
        results: list[dict] = []
        host_results: list[dict] = []
        waiter = threading.Thread(
            target=lambda: results.append(
                self.service.events(THREAD_REF, after_cursor=0, limit=20, wait_seconds=2)
            )
        )
        host_waiter = threading.Thread(
            target=lambda: host_results.append(
                self.service.host_events(HOST_REF, after_cursor=0, limit=20, wait_seconds=2)
            )
        )
        waiter.start()
        host_waiter.start()
        time.sleep(0.02)
        post_desktop_api(
            self.service,
            f"/api/desktop/v1/threads/{THREAD_REF}/queue/{QUEUE_REF}/update",
            {"request_id": "queue-wakeup-0001", "thread_revision": 7, "input": "立即同步"},
        )
        waiter.join(timeout=1)
        host_waiter.join(timeout=1)
        self.assertFalse(waiter.is_alive())
        self.assertFalse(host_waiter.is_alive())
        self.assertEqual(results[0]["events"], [])
        self.assertTrue(results[0]["changed"])
        self.assertEqual(host_results[0]["events"], [])
        self.assertTrue(host_results[0]["changed"])

    def test_host_event_cursor_is_bounded_and_unknown_host_fails(self) -> None:
        stored = self.service.receive("desktop_event", desktop_event())
        listed = self.service.host_events(HOST_REF, after_cursor=0, limit=20, wait_seconds=0)
        self.assertEqual(len(listed["events"]), 1)
        self.assertEqual(listed["events"][0]["event_sequence"], 1)
        self.assertNotIn("runner_id", listed["events"][0])
        after = self.service.host_events(
            HOST_REF,
            after_cursor=stored["cursor"],
            limit=20,
            wait_seconds=0,
        )
        self.assertEqual(after["events"], [])
        self.assertEqual(after["next_cursor"], stored["cursor"])
        self.assertFalse(after["changed"])
        with self.assertRaises(StoreError) as context:
            self.service.host_events(
                "HS-" + "Z" * 20,
                after_cursor=0,
                limit=20,
                wait_seconds=0,
            )
        self.assertEqual(context.exception.code, "desktop_host_not_found")

    def test_host_separates_connection_observation_from_data_sync(self) -> None:
        service = DesktopControllerService(
            self.store,
            publisher=self.publisher,
            now=lambda: NOW,
            runner_authorizer=lambda _runner_id: True,
            runner_status_provider=lambda _runner_id: {
                "connectivity_state": "online",
                "last_heartbeat_at": "2026-09-05T00:30:03+00:00",
            },
        )
        host = service.hosts()["hosts"][0]
        self.assertEqual(host["connection_observed_at"], "2026-09-05T08:30:03+08:00")
        self.assertEqual(host["data_synced_at"], NOW.isoformat())

    def test_effort_only_uses_current_thread_model_before_default(self) -> None:
        document = snapshot()
        document["snapshot"]["model"] = "gpt-5.6-terra"
        document["host"]["models"].append(
            {
                "id": "gpt-5.6-terra",
                "display_name": "GPT-5.6 Terra",
                "is_default": False,
                "default_reasoning_effort": "xhigh",
                "supported_reasoning_efforts": [
                    {"id": "xhigh", "description": "更深入"}
                ],
            }
        )
        document["body_digest"] = body_digest(document)
        with tempfile.TemporaryDirectory() as temporary:
            store = DesktopStore(Path(temporary) / "effort.sqlite3")
            publisher = Publisher()
            service = DesktopControllerService(
                store,
                publisher=publisher,
                now=lambda: NOW,
                runner_authorizer=lambda _runner_id: True,
            )
            service.receive("desktop_snapshot", document)
            result = service.submit(
                THREAD_REF,
                "steer",
                {
                    "request_id": "effort-current-model-0001",
                    "expected_turn_ref": TURN_REF,
                    "thread_revision": 7,
                    "input": "调整方向",
                    "mode": "safe",
                    "effort": "xhigh",
                },
            )
        self.assertEqual(result["state"], "submitted")
        self.assertEqual(publisher.commands[-1][1]["effort"], "xhigh")
        self.assertNotIn("model", publisher.commands[-1][1])

    def test_receipts_advance_monotonically_from_runner_to_mac_confirmation(self) -> None:
        post_desktop_api(
            self.service,
            f"/api/desktop/v1/threads/{THREAD_REF}/queue/{QUEUE_REF}/update",
            {"request_id": "queue-stages-0001", "thread_revision": 7, "input": "更新"},
        )
        accepted = self.service.receive(
            "desktop_receipt", receipt("queue-stages-0001", "accepted")
        )["command"]
        self.assertEqual(accepted["state"], "accepted")
        self.assertEqual(accepted["delivery_stage"], "runner_received")
        confirmed = self.service.receive(
            "desktop_receipt", receipt("queue-stages-0001", "confirmed")
        )["command"]
        self.assertEqual(confirmed["state"], "confirmed")
        self.assertEqual(confirmed["delivery_stage"], "mac_confirmed")
        self.assertIsNotNone(confirmed["stage_timestamps"]["controller_received"])
        self.assertIsNotNone(confirmed["stage_timestamps"]["relay_delivered"])
        self.assertIsNotNone(confirmed["stage_timestamps"]["runner_received"])
        self.assertIsNotNone(confirmed["stage_timestamps"]["mac_confirmed"])
        with sqlite3.connect(self.path) as connection:
            count = connection.execute(
                "SELECT COUNT(*) FROM desktop_receipts WHERE request_id=?",
                ("queue-stages-0001",),
            ).fetchone()[0]
        self.assertEqual(count, 2)
        with self.assertRaises(StoreError) as context:
            self.service.receive(
                "desktop_receipt",
                digest(
                    {
                        **receipt("queue-stages-0001", "accepted"),
                        "created_at": (NOW + dt.timedelta(seconds=3)).isoformat(),
                    }
                ),
            )
        self.assertEqual(context.exception.code, "desktop_receipt_conflict")

    def test_create_receipt_has_the_same_delivery_stages(self) -> None:
        document = snapshot()
        document["host"]["capabilities"].append("create_thread_v1")
        document["body_digest"] = body_digest(document)
        self.service.receive("desktop_snapshot", document)
        submitted = self.service.create(
            {
                "request_id": "create-stages-0001",
                "host_ref": HOST_REF,
                "project_ref": PROJECT_REF,
                "input": "创建任务",
            }
        )
        self.assertEqual(submitted["delivery_stage"], "relay_delivered")
        accepted = digest(
            {
                "version": 1,
                "message_type": "desktop_receipt",
                "runner_id": RUNNER_ID,
                "created_at": (NOW + dt.timedelta(seconds=1)).isoformat(),
                "request_id": "create-stages-0001",
                "host_ref": HOST_REF,
                "project_ref": PROJECT_REF,
                "thread_ref": None,
                "turn_ref": None,
                "action": "create",
                "state": "accepted",
                "thread_revision": None,
            }
        )
        accepted_result = self.service.receive("desktop_receipt", accepted)["command"]
        self.assertEqual(accepted_result["delivery_stage"], "runner_received")
        confirmed = dict(accepted)
        confirmed.update(
            {
                "created_at": (NOW + dt.timedelta(seconds=2)).isoformat(),
                "thread_ref": CREATED_THREAD_REF,
                "turn_ref": CREATED_TURN_REF,
                "state": "confirmed",
                "thread_revision": 1,
            }
        )
        confirmed["body_digest"] = body_digest(confirmed)
        result = self.service.receive("desktop_receipt", confirmed)["command"]
        self.assertEqual(result["delivery_stage"], "mac_confirmed")
        self.assertEqual(result["thread_ref"], CREATED_THREAD_REF)
        late_accepted = self.service.receive("desktop_receipt", accepted)
        self.assertEqual(late_accepted["status"], "stale")
        self.assertEqual(late_accepted["command"]["state"], "confirmed")
        self.assertEqual(late_accepted["command"]["delivery_stage"], "mac_confirmed")
        self.assertEqual(late_accepted["command"]["thread_ref"], CREATED_THREAD_REF)

    def test_non_text_queue_item_cannot_be_edited(self) -> None:
        with self.assertRaises(StoreError) as context:
            post_desktop_api(
                self.service,
                f"/api/desktop/v1/threads/{THREAD_REF}/queue/{SECOND_QUEUE_REF}/update",
                {"request_id": "queue-non-text-0001", "thread_revision": 7, "input": "不允许"},
            )
        self.assertEqual(context.exception.code, "desktop_queue_not_editable")
        self.assertEqual(self.publisher.commands, [])

    def test_reorder_requires_exact_current_queue_set(self) -> None:
        with self.assertRaises(StoreError) as context:
            post_desktop_api(
                self.service,
                f"/api/desktop/v1/threads/{THREAD_REF}/queue/reorder",
                {
                    "request_id": "queue-reorder-stale-0001",
                    "thread_revision": 7,
                    "queue_refs": [QUEUE_REF],
                },
            )
        self.assertEqual(context.exception.code, "desktop_queue_conflict")
        result = post_desktop_api(
            self.service,
            f"/api/desktop/v1/threads/{THREAD_REF}/queue/reorder",
            {
                "request_id": "queue-reorder-0001",
                "thread_revision": 7,
                "queue_refs": [SECOND_QUEUE_REF, QUEUE_REF],
            },
        )
        self.assertEqual(result["state"], "submitted")
        self.assertEqual(self.publisher.commands[-1][1]["queue_refs"], [SECOND_QUEUE_REF, QUEUE_REF])


if __name__ == "__main__":
    unittest.main()
