from __future__ import annotations

from io import BytesIO
import hashlib
from pathlib import Path
import tempfile
import unittest

from PIL import Image

from codex_controller.progress_capture import ProgressCaptureCoordinator
from renovation_hub.hub import RenovationHubStore
from renovation_hub.ledger import LedgerError
from renovation_hub.media import MediaService
from renovation_hub.progress_capture import ProgressCaptureService
from weixin_gateway.store import GatewayStore, progress_capture_instruction


def jpeg_bytes(color: str = "#9A6A45") -> bytes:
    buffer = BytesIO()
    Image.new("RGB", (320, 180), color).save(buffer, "JPEG", quality=88)
    return buffer.getvalue()


def capture_key(label: str) -> str:
    return "fixture-capture-" + label + "-" + "0" * 24


class GatewayProgressCaptureTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.store = GatewayStore(
            self.root / "data" / "gateway.sqlite3",
            data_dir=self.root / "data",
            spool_ttl_seconds=3600,
        )
        self.conversation = "sha256:" + "a" * 64
        self.spec = {
            "media_type": "image",
            "filename": "现场.jpg",
            "mime_type": "image/jpeg",
        }

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def message(
        self,
        message_id: str,
        *,
        text: str = "",
        media: list[tuple[dict, bytes]] | None = None,
        conversation: str | None = None,
        sender: str = "fixture-owner",
    ) -> dict:
        return self.store.store_message(
            message_id=message_id,
            sender_id=sender,
            conversation_key=conversation or self.conversation,
            text=text,
            media=media or [],
            capability_profile="owner_legacy",
        )

    def test_intent_parser_distinguishes_explicit_ambiguous_and_control_phrases(self) -> None:
        self.assertEqual(progress_capture_instruction("开始记录今天厨房装修进度", has_attachments=False)["kind"], "start")
        self.assertEqual(progress_capture_instruction("我要记录装修进度", has_attachments=False)["kind"], "start")
        self.assertEqual(progress_capture_instruction("把这些图片保存为装修进度", has_attachments=True)["kind"], "start")
        self.assertIsNone(progress_capture_instruction("把刚才这些图片归档为装修进度", has_attachments=False))
        self.assertEqual(progress_capture_instruction("厨房施工进度", has_attachments=True)["kind"], "confirm")
        self.assertIsNone(progress_capture_instruction("看看这张照片", has_attachments=True))
        self.assertEqual(progress_capture_instruction("记录完成", has_attachments=False)["kind"], "finalize")
        self.assertEqual(progress_capture_instruction("暂停记录", has_attachments=False)["kind"], "pause")
        self.assertEqual(progress_capture_instruction("继续记录", has_attachments=False)["kind"], "resume")

    def test_explicit_capture_survives_restart_and_supports_more_than_sixteen_items(self) -> None:
        started = self.message("capture-start", text="开始记录今天厨房装修进度")
        context = started["progress_capture_context"]
        self.assertEqual(context["action"], "started")
        self.assertEqual(context["state"], "active")
        session_id = context["session_id"]

        for index in range(20):
            item = self.message(
                f"capture-image-{index}",
                media=[(self.spec, f"image-{index}".encode("ascii"))],
            )
            self.assertEqual(item["progress_capture_context"]["session_id"], session_id)
            self.assertEqual(item["progress_capture_context"]["received_count"], index + 1)

        self.store = GatewayStore(
            self.store.database_path,
            data_dir=self.root / "data",
            spool_ttl_seconds=3600,
        )
        finalizing = self.message("capture-finish", text="记录完成")
        context = finalizing["progress_capture_context"]
        self.assertEqual(context["action"], "finalize_requested")
        self.assertEqual(context["state"], "finalizing")
        self.assertEqual(context["received_count"], 20)
        self.assertEqual(len(context["source_attachments"]), 20)
        self.assertTrue(all(item["position"] == index + 1 for index, item in enumerate(context["source_attachments"])))

    def test_ambiguous_media_is_retained_but_ordinary_media_is_not_captured(self) -> None:
        ordinary = self.message("ordinary", text="看看这张照片", media=[(self.spec, b"ordinary")])
        self.assertEqual(ordinary["progress_capture_context"], {})

        ambiguous = self.message("ambiguous", text="厨房施工进度", media=[(self.spec, b"ambiguous")])
        self.assertEqual(ambiguous["progress_capture_context"]["action"], "confirmation_required")
        self.assertEqual(ambiguous["progress_capture_context"]["received_count"], 1)

        confirmed = self.message("confirm", text="是的，开始记录")
        self.assertEqual(confirmed["progress_capture_context"]["action"], "started")
        self.assertEqual(confirmed["progress_capture_context"]["received_count"], 1)
        self.assertEqual(len(confirmed["progress_capture_context"]["source_attachments"]), 1)

    def test_image_first_explicit_renovation_archive_upgrades_to_capture_session(self) -> None:
        first = self.message("image-first-1", media=[(self.spec, b"image-first-one")])
        second = self.message("image-first-2", media=[(self.spec, b"image-first-two")])
        self.assertEqual(first["progress_capture_context"], {})
        self.assertEqual(second["progress_capture_context"], {})

        command = self.message(
            "image-first-command",
            text="刚才两张图片归档为厨房装修进度",
        )
        archive = command["media_archive_context"]
        capture = command["progress_capture_context"]
        self.assertTrue(archive["authorized"])
        self.assertEqual(capture["action"], "started")
        self.assertEqual(capture["state"], "active")
        self.assertEqual(capture["received_count"], 2)
        self.assertEqual(len(capture["source_attachments"]), 2)
        self.assertEqual(
            [item["source_message_hash"] for item in capture["source_attachments"]],
            [
                self.store._capture_source_message_hash("image-first-1"),
                self.store._capture_source_message_hash("image-first-2"),
            ],
        )
        self.assertEqual(
            [item["attachment_ref"] for item in capture["source_attachments"]],
            [first["attachments"][0]["attachment_ref"], second["attachments"][0]["attachment_ref"]],
        )

    def test_pause_resume_cancel_and_scope_isolation(self) -> None:
        self.message("scope-start", text="开始记录装修进度")
        paused = self.message("scope-pause", text="暂停记录")
        self.assertEqual(paused["progress_capture_context"]["state"], "paused")
        not_captured = self.message("paused-image", media=[(self.spec, b"paused")])
        self.assertEqual(not_captured["progress_capture_context"]["action"], "paused_media_ignored")

        other = self.message(
            "other-image",
            media=[(self.spec, b"other")],
            conversation="sha256:" + "b" * 64,
        )
        self.assertEqual(other["progress_capture_context"], {})

        resumed = self.message("scope-resume", text="继续记录")
        self.assertEqual(resumed["progress_capture_context"]["state"], "active")
        captured = self.message("resumed-image", media=[(self.spec, b"resumed")])
        self.assertEqual(captured["progress_capture_context"]["received_count"], 1)

        cancelled = self.message("scope-cancel", text="取消记录")
        self.assertEqual(cancelled["progress_capture_context"]["state"], "cancelled")
        after = self.message("after-cancel", media=[(self.spec, b"after")])
        self.assertEqual(after["progress_capture_context"], {})


class HubProgressCaptureTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.store = RenovationHubStore(
            self.root / "data" / "hub.sqlite3",
            data_dir=self.root / "data",
            share_dir=self.root / "share",
        )
        self.store.set_writer_mode("read_only", force_initial=True)
        self.store.set_writer_mode("shadow_validated")
        self.store.set_writer_mode("cutover_ready")
        self.store.set_writer_mode("primary_writer")
        self.project = self.store.create_project(
            {"idempotency_key": capture_key("project"), "name": "瞰湖湾装修"}
        )["project"]
        self.area = self.store.create_area(
            {
                "idempotency_key": capture_key("area"),
                "project_id": self.project["id"],
                "name": "厨房",
            }
        )["area"]
        self.media = MediaService(
            self.store,
            media_root=self.root / "media",
            preview_root=self.root / "previews",
            staging_root=self.root / "staging",
            max_media_bytes=50 * 1024 * 1024,
        )
        self.capture = ProgressCaptureService(self.store, self.media)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def start(self, session_id: str = "PCS-" + "A" * 26) -> dict:
        return self.capture.action(
            {
                "action": "start",
                "session_id": session_id,
                "scope_hash": "sha256:" + "1" * 64,
                "source_message_hash": "sha256:" + "2" * 64,
                "text": "开始记录瞰湖湾厨房装修进度",
                "occurred_at": "2026-08-24T10:30:00+08:00",
                "idempotency_key": capture_key("start"),
            }
        )

    def register(self, session_id: str, count: int) -> list[dict]:
        items = [
            {
                "position": index + 1,
                "source_message_hash": "sha256:" + hashlib.sha256(f"m-{index}".encode()).hexdigest(),
                "source_ref_hash": "sha256:" + hashlib.sha256(f"r-{index}".encode()).hexdigest(),
                "sha256": "sha256:" + hashlib.sha256(f"content-{index}".encode()).hexdigest(),
                "media_type": "image",
                "size_bytes": 100 + index,
                "display_name": f"现场-{index + 1}.jpg",
            }
            for index in range(count)
        ]
        result = self.capture.action(
            {
                "action": "register_items",
                "session_id": session_id,
                "items": items,
                "idempotency_key": capture_key(f"register-{count}"),
            }
        )
        return result["items"]

    def ingest(self, session: dict, item: dict, *, label: str) -> dict:
        content = jpeg_bytes()
        digest = hashlib.sha256(content).hexdigest()
        prepared = self.media.prepare_upload(
            idempotency_key=capture_key(f"media-{label}"),
            source_ref_hash=item["source_ref_hash"],
            original_filename=f"{label}.jpg",
            mime_type="image/jpeg",
            expected_bytes=len(content),
        )
        Path(prepared["path"]).write_bytes(content)
        return self.media.finalize_upload(
            prepared,
            received_bytes=len(content),
            sha256=digest,
            expected_sha256=digest,
            metadata={
                "idempotency_key": capture_key(f"media-{label}"),
                "source_ref_hash": item["source_ref_hash"],
                "project_id": session["project_id"],
                "source": "weixin",
                "links": [{"target_type": "event", "target_id": session["event_id"]}],
                "capture_session_id": session["id"],
                "capture_item_id": item["id"],
            },
            actor_hash="sha256:fixture",
        )

    def test_start_infers_project_and_area_and_finalize_requires_exact_reconciliation(self) -> None:
        session = self.start()["session"]
        self.assertEqual(session["project_id"], self.project["id"])
        self.assertEqual(session["area_id"], self.area["id"])
        self.assertEqual(session["business_date"], "2026-08-24")

        items = self.register(session["id"], 2)
        replay = self.register(session["id"], 2)
        self.assertEqual([item["id"] for item in replay], [item["id"] for item in items])
        with self.assertRaises(LedgerError) as context:
            self.capture.action(
                {
                    "action": "finalize",
                    "session_id": session["id"],
                    "expected_received_count": 2,
                    "idempotency_key": capture_key("finalize-early"),
                }
            )
        self.assertEqual(context.exception.code, "capture_reconciliation_pending")

        self.ingest(session, items[0], label="one")
        self.ingest(session, items[1], label="two")
        completed = self.capture.action(
            {
                "action": "finalize",
                "session_id": session["id"],
                "expected_received_count": 2,
                "idempotency_key": capture_key("finalize"),
            }
        )
        self.assertEqual(completed["session"]["state"], "completed")
        self.assertEqual(completed["reconciliation"], {"received": 2, "registered": 2, "stored": 2, "linked": 2, "failed": 0, "pending": 0})
        with self.store._connect() as connection:
            event = connection.execute(
                "SELECT status,description FROM events WHERE id=?",
                (session["event_id"],),
            ).fetchone()
            self.assertIsNotNone(event)
            assert event is not None
            self.assertEqual(event["status"], "active")
            self.assertEqual(event["description"].count("开始记录瞰湖湾厨房装修进度"), 1)
            self.assertIn("已完成归档：共 2 项图片/视频。", event["description"])

    def test_notes_corrections_and_restart_keep_durable_draft(self) -> None:
        session = self.start("PCS-" + "B" * 26)["session"]
        items = self.register(session["id"], 1)
        noted = self.capture.action(
            {
                "action": "note",
                "session_id": session["id"],
                "text": "第1张：吊顶龙骨焊接完成",
                "target_position": 1,
                "idempotency_key": capture_key("note"),
            }
        )
        self.assertEqual(noted["item"]["caption"], "吊顶龙骨焊接完成")

        reopened = ProgressCaptureService(
            RenovationHubStore(
                self.store.database_path,
                data_dir=self.store.data_dir,
                share_dir=self.store.share_dir,
            ),
            self.media,
        )
        status = reopened.action({"action": "status", "session_id": session["id"]})
        self.assertEqual(status["session"]["state"], "active")
        self.assertEqual(status["items"][0]["caption"], "吊顶龙骨焊接完成")
        self.assertEqual(status["reconciliation"]["registered"], 1)
        self.assertEqual(items[0]["id"], status["items"][0]["id"])

    def test_failed_processing_keeps_original_and_retry_can_complete_item(self) -> None:
        session = self.start("PCS-" + "C" * 26)["session"]
        item = self.register(session["id"], 1)[0]
        original_process = self.media._process
        self.media._process = lambda *_args: {"status": "failed", "error_code": "fixture_processing_failed"}  # type: ignore[method-assign]
        result = self.ingest(session, item, label="failed")
        self.assertEqual(result["media"]["processing_status"], "failed")
        originals = [path for path in self.media.media_root.rglob("*") if path.is_file()]
        self.assertEqual(len(originals), 1)
        status = self.capture.action({"action": "status", "session_id": session["id"]})
        self.assertEqual(status["reconciliation"]["failed"], 1)

        self.media._process = original_process  # type: ignore[method-assign]
        retried = self.capture.action(
            {
                "action": "retry_failed",
                "session_id": session["id"],
                "idempotency_key": capture_key("retry"),
            }
        )
        self.assertEqual(retried["reconciliation"]["stored"], 1)
        self.assertEqual(retried["reconciliation"]["failed"], 0)


class StubCaptureRouter:
    def __init__(
        self,
        *,
        finalize_matches: bool = True,
        registered_state: str | None = None,
        attachment_consumption: str | None = None,
    ) -> None:
        self.actions: list[dict] = []
        self.streamed: list[dict] = []
        self.acks: list[dict] = []
        self.finalize_matches = finalize_matches
        self.registered_state = registered_state
        self.attachment_consumption = attachment_consumption

    def progress_capture_action(self, payload: dict) -> dict:
        self.actions.append(dict(payload))
        action = payload["action"]
        if action == "register_items":
            return {
                "items": [
                    {
                        **item,
                        "id": "PCI-" + f"{item['position']:026d}",
                        **({"state": self.registered_state} if self.registered_state else {}),
                    }
                    for item in payload["items"]
                ]
            }
        if action == "finalize":
            received = payload["expected_received_count"]
            stored = received if self.finalize_matches else received - 1
            if not self.finalize_matches:
                raise LedgerError("capture_reconciliation_pending", "尚有媒体未归档", status=409)
            return {
                "session": {"id": payload["session_id"], "state": "completed", "title": "厨房进度"},
                "reconciliation": {"received": received, "registered": received, "stored": stored, "linked": stored, "failed": 0, "pending": 0},
            }
        return {
            "session": {
                "id": payload["session_id"],
                "state": "active",
                "project_id": "project-fixture",
                "event_id": "event-fixture",
                "title": "厨房进度",
            },
            "reconciliation": {"received": 0, "registered": 0, "stored": 0, "linked": 0, "failed": 0, "pending": 0},
        }

    def progress_capture_media(self, attachment_ref: str, payload: dict) -> dict:
        self.streamed.append({"attachment_ref": attachment_ref, **payload})
        result = {"media": {"id": "media-" + attachment_ref[-6:], "processing_status": "ready"}}
        if self.attachment_consumption is not None:
            result["attachment_consumption"] = self.attachment_consumption
        return {"result": result}

    def progress_capture_ack(self, payload: dict) -> dict:
        self.acks.append(dict(payload))
        return {"acknowledged": True}


class ControllerProgressCaptureTests(unittest.TestCase):
    @staticmethod
    def context(action: str, count: int, *, text: str = "") -> dict:
        return {
            "version": 1,
            "session_id": "PCS-" + "D" * 26,
            "scope_hash": "sha256:" + "3" * 64,
            "state": "finalizing" if action == "finalize_requested" else "active",
            "action": action,
            "intent_text": "开始记录厨房装修进度",
            "note_text": text,
            "received_count": count,
            "source_message_hash": "sha256:" + hashlib.sha256(b"capture-controller-message").hexdigest(),
            "source_attachments": [
                {
                    "position": index + 1,
                    "attachment_ref": "R" * 31 + f"{index:02d}",
                    "source_message_hash": "sha256:" + hashlib.sha256(f"message-{index}".encode()).hexdigest(),
                    "source_ref_hash": "sha256:" + hashlib.sha256(f"ref-{index}".encode()).hexdigest(),
                    "sha256": "sha256:" + hashlib.sha256(f"content-{index}".encode()).hexdigest(),
                    "media_type": "image",
                    "size_bytes": 100,
                    "display_name": f"现场-{index + 1}.jpg",
                }
                for index in range(count)
            ],
        }

    def test_more_than_sixteen_items_are_registered_in_bounded_batches_and_streamed_once(self) -> None:
        router = StubCaptureRouter()
        coordinator = ProgressCaptureCoordinator(router)
        outcome = coordinator.handle(
            {
                "message_id": "capture-controller-media",
                "received_at": "2026-08-24T12:00:00+08:00",
                "text": "",
                "progress_capture_context": self.context("media_received", 20),
            }
        )
        register_calls = [item for item in router.actions if item["action"] == "register_items"]
        self.assertEqual([len(item["items"]) for item in register_calls], [16, 4])
        self.assertEqual(len(router.streamed), 20)
        self.assertFalse(outcome.suppress_reply)

    def test_finalize_acks_gateway_only_after_exact_reconciliation(self) -> None:
        router = StubCaptureRouter()
        outcome = ProgressCaptureCoordinator(router).handle(
            {
                "message_id": "capture-controller-finalize",
                "received_at": "2026-08-24T12:00:00+08:00",
                "text": "记录完成",
                "progress_capture_context": self.context("finalize_requested", 2),
            }
        )
        self.assertEqual(len(router.acks), 1)
        self.assertIn("共 2 项", outcome.text)
        self.assertFalse(outcome.suppress_reply)

        mismatch = StubCaptureRouter(finalize_matches=False)
        outcome = ProgressCaptureCoordinator(mismatch).handle(
            {
                "message_id": "capture-controller-finalize-mismatch",
                "received_at": "2026-08-24T12:00:00+08:00",
                "text": "记录完成",
                "progress_capture_context": self.context("finalize_requested", 2),
            }
        )
        self.assertEqual(mismatch.acks, [])
        self.assertIn("暂未结束", outcome.text)

    def test_stored_item_replays_attachment_ack_and_pending_ack_blocks_completion(self) -> None:
        pending = StubCaptureRouter(
            registered_state="stored",
            attachment_consumption="pending",
        )
        outcome = ProgressCaptureCoordinator(pending).handle(
            {
                "message_id": "capture-controller-ack-pending",
                "received_at": "2026-08-24T12:00:00+08:00",
                "text": "记录完成",
                "progress_capture_context": self.context("finalize_requested", 1),
            }
        )
        self.assertEqual(len(pending.streamed), 1)
        self.assertEqual(pending.acks, [])
        self.assertIn("暂未结束", outcome.text)

        confirmed = StubCaptureRouter(
            registered_state="stored",
            attachment_consumption="confirmed",
        )
        outcome = ProgressCaptureCoordinator(confirmed).handle(
            {
                "message_id": "capture-controller-ack-confirmed",
                "received_at": "2026-08-24T12:01:00+08:00",
                "text": "记录完成",
                "progress_capture_context": self.context("finalize_requested", 1),
            }
        )
        self.assertEqual(len(confirmed.streamed), 1)
        self.assertEqual(len(confirmed.acks), 1)
        self.assertIn("共 1 项", outcome.text)

    def test_cancel_before_hub_draft_does_not_force_project_resolution(self) -> None:
        class MissingSessionRouter(StubCaptureRouter):
            def progress_capture_action(self, payload: dict) -> dict:
                self.actions.append(dict(payload))
                if payload["action"] == "status":
                    raise LedgerError("capture_session_not_found", "尚未建档", status=404)
                raise AssertionError(f"unexpected action: {payload['action']}")

        router = MissingSessionRouter()
        context = self.context("cancelled", 2)
        context["state"] = "cancelled"
        outcome = ProgressCaptureCoordinator(router).handle(
            {
                "message_id": "capture-controller-cancel-before-draft",
                "received_at": "2026-08-24T12:00:00+08:00",
                "text": "取消记录",
                "progress_capture_context": context,
            }
        )
        self.assertEqual([item["action"] for item in router.actions], ["status"])
        self.assertIn("已取消本次采集", outcome.text)


class EndToEndProgressCaptureTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.gateway = GatewayStore(
            self.root / "gateway" / "gateway.sqlite3",
            data_dir=self.root / "gateway",
            spool_ttl_seconds=3600,
        )
        self.hub = RenovationHubStore(
            self.root / "hub" / "hub.sqlite3",
            data_dir=self.root / "hub",
            share_dir=self.root / "share",
        )
        self.hub.set_writer_mode("read_only", force_initial=True)
        self.hub.set_writer_mode("shadow_validated")
        self.hub.set_writer_mode("cutover_ready")
        self.hub.set_writer_mode("primary_writer")
        self.project = self.hub.create_project(
            {"idempotency_key": capture_key("e2e-project"), "name": "瞰湖湾装修"}
        )["project"]
        self.area = self.hub.create_area(
            {
                "idempotency_key": capture_key("e2e-area"),
                "project_id": self.project["id"],
                "name": "厨房",
            }
        )["area"]
        self.media = MediaService(
            self.hub,
            media_root=self.root / "media",
            preview_root=self.root / "previews",
            staging_root=self.root / "staging",
            max_media_bytes=50 * 1024 * 1024,
        )
        self.capture = ProgressCaptureService(self.hub, self.media)
        outer = self

        class Router:
            def progress_capture_action(self, payload: dict) -> dict:
                return outer.capture.action(payload)

            def progress_capture_media(self, attachment_ref: str, payload: dict) -> dict:
                metadata, handle = outer.gateway.open_stream_attachment(attachment_ref)
                with handle:
                    content = handle.read()
                digest = hashlib.sha256(content).hexdigest()
                prepared = outer.media.prepare_upload(
                    idempotency_key=payload["idempotency_key"],
                    source_ref_hash=payload["source_ref_hash"],
                    original_filename=metadata["original_filename"],
                    mime_type=metadata["mime_type"],
                    expected_bytes=metadata["size_bytes"],
                )
                Path(prepared["path"]).write_bytes(content)
                result = outer.media.finalize_upload(
                    prepared,
                    received_bytes=len(content),
                    sha256=digest,
                    expected_sha256=digest,
                    metadata={
                        "idempotency_key": payload["idempotency_key"],
                        "source_ref_hash": payload["source_ref_hash"],
                        "project_id": payload["project_id"],
                        "source": "weixin",
                        "captured_at": payload["captured_at"],
                        "links": [{"target_type": "event", "target_id": payload["event_id"]}],
                        "capture_session_id": payload["session_id"],
                        "capture_item_id": payload["item_id"],
                    },
                    actor_hash="sha256:e2e-controller",
                )
                outer.gateway.acknowledge_attachment(
                    attachment_ref,
                    f"sha256:{digest}",
                )
                return result

            def progress_capture_ack(self, payload: dict) -> dict:
                return outer.gateway.complete_progress_capture(
                    payload["session_id"],
                    received_count=payload["received_count"],
                    stored_count=payload["stored_count"],
                    linked_count=payload["linked_count"],
                )

        self.coordinator = ProgressCaptureCoordinator(Router())

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def message(self, message_id: str, *, text: str = "", media: list[tuple[dict, bytes]] | None = None) -> dict:
        return self.gateway.store_message(
            message_id=message_id,
            sender_id="fixture-owner",
            conversation_key="sha256:" + "e" * 64,
            text=text,
            media=media or [],
            capability_profile="owner_legacy",
        )

    def test_image_first_batch_is_fully_stored_linked_consumed_and_completed(self) -> None:
        spec = {"media_type": "image", "filename": "厨房现场.jpg", "mime_type": "image/jpeg"}
        first = self.message("e2e-image-1", media=[(spec, jpeg_bytes("#9A6A45"))])
        second = self.message("e2e-image-2", media=[(spec, jpeg_bytes("#456A9A"))])
        command = self.message(
            "e2e-command",
            text="刚才两张图片归档为瞰湖湾厨房装修进度",
        )
        started = self.coordinator.handle(
            {
                "message_id": command["message_id"],
                "received_at": command["received_at"],
                "text": command["text"],
                "progress_capture_context": command["progress_capture_context"],
            }
        )
        self.assertIn("已开始记录", started.text)
        session_id = command["progress_capture_context"]["session_id"]
        status = self.capture.status(session_id)
        self.assertEqual(
            status["reconciliation"],
            {"received": 2, "registered": 2, "stored": 2, "linked": 2, "failed": 0, "pending": 0},
        )

        completed_message = self.message("e2e-complete", text="记录完成")
        completed = self.coordinator.handle(
            {
                "message_id": completed_message["message_id"],
                "received_at": completed_message["received_at"],
                "text": completed_message["text"],
                "progress_capture_context": completed_message["progress_capture_context"],
            }
        )
        self.assertIn("共 2 项图片/视频已全部保存", completed.text)
        self.assertEqual(self.capture.status(session_id)["session"]["state"], "completed")
        with self.gateway._connect() as connection:
            gateway_session = connection.execute(
                "SELECT state,received_count FROM progress_capture_sessions WHERE session_id=?",
                (session_id,),
            ).fetchone()
            self.assertIsNotNone(gateway_session)
            assert gateway_session is not None
            self.assertEqual(dict(gateway_session), {"state": "completed", "received_count": 2})
            consumed = connection.execute(
                "SELECT COUNT(*) FROM attachments WHERE attachment_ref IN (?,?) AND consumed_at IS NOT NULL",
                (first["attachments"][0]["attachment_ref"], second["attachments"][0]["attachment_ref"]),
            ).fetchone()[0]
            self.assertEqual(consumed, 2)
        with self.hub._connect() as connection:
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM progress_capture_items WHERE session_id=? AND state='stored'",
                    (session_id,),
                ).fetchone()[0],
                2,
            )
            event_id = status["session"]["event_id"]
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM media_links WHERE target_type='event' AND target_id=?",
                    (event_id,),
                ).fetchone()[0],
                2,
            )


if __name__ == "__main__":
    unittest.main()
