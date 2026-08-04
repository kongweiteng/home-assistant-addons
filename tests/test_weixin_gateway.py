from __future__ import annotations

import asyncio
import base64
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import secrets
import tempfile
import unittest
import zipfile

from weixin_gateway.protocol import (
    ProtocolError,
    aes128_ecb_decrypt,
    aes128_ecb_encrypt,
    assert_cdn_url,
    extract_message,
    parse_aes_key,
)
from weixin_gateway.api import DASHBOARD_HTML, DASHBOARD_JS
from weixin_gateway.service import ControllerClient, GatewayService, split_text
from weixin_gateway.store import GatewayStore, IdentityStore, StoreError


ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests" / "fixtures" / "codex_hermes_migration" / "weixin_updates.json"


def fixture_identity(*, allowed: list[str] | None = None) -> dict:
    return {
        "account_id": "fixture-account",
        "token": "fixture-ilink-token-0000000000000000",
        "base_url": "https://ilinkai.weixin.qq.com",
        "cdn_base_url": "https://novac2c.cdn.weixin.qq.com/c2c",
        "user_id": "fixture-bot-user",
        "allowed_user_ids": allowed if allowed is not None else ["fixture-owner"],
        "get_updates_buf": "",
        "context_tokens": {},
    }


def fixture_update() -> dict:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


class StubController:
    configured = False

    async def start(self) -> None:
        return None

    async def close(self) -> None:
        return None


class StubIlinkClient:
    def __init__(self, media: bytes = b"fixture-image"):
        self.media = media
        self.closed = False
        self.sent: list[dict] = []

    async def download_media(self, _spec: dict) -> bytes:
        return self.media

    async def send_text(self, to_user_id: str, text: str, context_token: str | None, client_id: str) -> dict:
        self.sent.append(
            {
                "to_user_id": to_user_id,
                "text": text,
                "context_token": context_token,
                "client_id": client_id,
            }
        )
        return {"ret": 0}

    async def close(self) -> None:
        self.closed = True


class StubHttpContent:
    def __init__(self, body: bytes):
        self.body = body

    async def read(self, _limit: int) -> bytes:
        return self.body


class StubHttpResponse:
    def __init__(self, document: dict, status: int = 200):
        self.status = status
        self.content = StubHttpContent(json.dumps(document).encode("utf-8"))

    async def __aenter__(self) -> "StubHttpResponse":
        return self

    async def __aexit__(self, *_args: object) -> None:
        return None


class StubHttpSession:
    def __init__(self):
        self.closed = False
        self.calls: list[dict] = []

    def request(self, method: str, url: str, **kwargs: object) -> StubHttpResponse:
        self.calls.append({"method": method, "url": url, **kwargs})
        if method == "POST":
            return StubHttpResponse({"version": 1, "result": {"job_id": "fixture-job", "state": "queued"}})
        return StubHttpResponse({"version": 1, "result": {"job_id": "fixture-job", "state": "completed", "result": "完成"}})

    async def close(self) -> None:
        self.closed = True


class ProtocolTests(unittest.TestCase):
    def test_aes_round_trip_and_supported_key_formats(self) -> None:
        key = bytes.fromhex("00112233445566778899aabbccddeeff")
        plaintext = "合成微信图片".encode("utf-8")
        ciphertext = aes128_ecb_encrypt(plaintext, key)
        self.assertNotEqual(ciphertext, plaintext)
        self.assertEqual(aes128_ecb_decrypt(ciphertext, key), plaintext)
        self.assertEqual(parse_aes_key(base64.b64encode(key).decode("ascii")), key)
        encoded_hex = base64.b64encode(key.hex().encode("ascii")).decode("ascii")
        self.assertEqual(parse_aes_key(encoded_hex), key)
        with self.assertRaises(ProtocolError):
            parse_aes_key("not-base64")

    def test_fixture_message_and_cdn_allowlist_are_strict(self) -> None:
        raw = fixture_update()["msgs"][0]
        message = extract_message(raw, "fixture-account")
        self.assertIsNotNone(message)
        assert message is not None
        self.assertEqual(message["text"], "查询装修支出")
        self.assertEqual(message["sender_id"], "fixture-owner")
        self.assertFalse(message["is_group"])
        self.assertEqual(message["media"][0]["media_type"], "image")
        self.assertEqual(assert_cdn_url(message["media"][0]["full_url"]), message["media"][0]["full_url"])
        with self.assertRaises(ProtocolError):
            assert_cdn_url("https://example.invalid/file")

    def test_dashboard_exposes_admin_pairing_without_unsafe_html_rendering(self) -> None:
        self.assertIn("api/owner-pairing/start", DASHBOARD_JS)
        self.assertIn("绑定消息不会进入 Codex", DASHBOARD_HTML)
        self.assertIn("textContent=j.result.code", DASHBOARD_JS)
        self.assertNotIn("innerHTML", DASHBOARD_JS)


class StoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.identity_store = IdentityStore(self.root / "data")
        self.identity_store.save_identity(fixture_identity())
        self.store = GatewayStore(self.root / "data" / "gateway.sqlite3", data_dir=self.root / "data", spool_ttl_seconds=60)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_message_dedup_attachment_path_digest_expiry_and_one_time_consumption(self) -> None:
        content = b"synthetic-receipt"
        spec = {"media_type": "image", "filename": "receipt.jpg", "mime_type": "image/jpeg"}
        first = self.store.store_message(
            message_id="fixture-message-store",
            sender_id="fixture-owner",
            conversation_key="sha256:fixture",
            text="记账",
            media=[(spec, content)],
        )
        second = self.store.store_message(
            message_id="fixture-message-store",
            sender_id="fixture-owner",
            conversation_key="sha256:fixture",
            text="不同的重投正文不会覆盖首条",
            media=[],
        )
        self.assertEqual(first, second)
        self.assertEqual(len(first["attachments"]), 1)
        reference = first["attachments"][0]["attachment_ref"]
        digest = hashlib.sha256(content).hexdigest()
        stored = self.store.spool_dir / digest
        self.assertTrue(stored.is_file())
        self.assertEqual(stored.resolve().parent, self.store.spool_dir.resolve())
        preview_metadata, preview_content = self.store.preview_attachment(reference)
        self.assertEqual(preview_content, content)
        self.assertEqual(preview_metadata["sha256"], f"sha256:{digest}")
        second_preview_metadata, second_preview_content = self.store.preview_attachment(reference)
        self.assertEqual(second_preview_content, content)
        self.assertEqual(second_preview_metadata, preview_metadata)
        metadata, consumed = self.store.consume_attachment(reference)
        self.assertEqual(consumed, content)
        self.assertEqual(metadata["sha256"], f"sha256:{digest}")
        with self.assertRaises(StoreError) as context:
            self.store.consume_attachment(reference)
        self.assertEqual(context.exception.code, "attachment_unavailable")
        with self.assertRaises(StoreError) as context:
            self.store.preview_attachment(reference)
        self.assertEqual(context.exception.code, "attachment_unavailable")

        expired = self.store.store_message(
            message_id="fixture-message-expired",
            sender_id="fixture-owner",
            conversation_key="sha256:fixture",
            text="过期附件",
            media=[(spec, b"expired")],
        )["attachments"][0]["attachment_ref"]
        with self.store._connect() as connection:
            connection.execute(
                "UPDATE attachments SET expires_at=? WHERE attachment_ref=?",
                ((datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat(), expired),
            )
        with self.assertRaises(StoreError) as context:
            self.store.consume_attachment(expired)
        self.assertEqual(context.exception.code, "attachment_unavailable")

    def test_single_token_lock_conflict(self) -> None:
        first = self.identity_store.acquire_token_lock(fixture_identity()["token"])
        second = self.identity_store.acquire_token_lock(fixture_identity()["token"])
        first.acquire()
        try:
            with self.assertRaises(StoreError) as context:
                second.acquire()
            self.assertEqual(context.exception.code, "token_in_use")
        finally:
            first.release()

    def test_encrypted_identity_migration_import_and_tamper_rejection(self) -> None:
        key = secrets.token_bytes(32)
        package = self.identity_store.migration_dir / "identity.zip"
        IdentityStore.build_migration_package(fixture_identity(), key, package)
        inspected = self.identity_store.inspect_migration(package.name)
        self.assertTrue(inspected["valid_envelope"])
        imported = self.identity_store.import_migration(package.name, base64.urlsafe_b64encode(key).decode("ascii"))
        self.assertEqual(imported["state"], "credential_ready")

        tampered = self.identity_store.migration_dir / "tampered.zip"
        with zipfile.ZipFile(package) as source, zipfile.ZipFile(tampered, "w") as target:
            target.writestr("manifest.json", source.read("manifest.json"))
            target.writestr("identity.enc", source.read("identity.enc") + b"tamper")
        with self.assertRaises(StoreError) as context:
            self.identity_store.inspect_migration(tampered.name)
        self.assertEqual(context.exception.code, "migration_invalid")

    def test_owner_pairing_code_is_one_time_hashed_and_binds_exact_sender(self) -> None:
        identity = fixture_identity(allowed=[])
        self.identity_store.save_identity(identity)
        pairing = self.identity_store.start_owner_pairing(identity)
        self.assertEqual(pairing["state"], "waiting")
        self.assertNotIn(pairing["code"].encode("utf-8"), self.identity_store.owner_pairing_path.read_bytes())
        self.assertFalse(
            self.identity_store.claim_owner(
                identity,
                user_id="fixture-stranger",
                text="绑定-CODEX-WRONG",
                context_token="wrong-context",
            )
        )
        self.assertTrue(
            self.identity_store.claim_owner(
                identity,
                user_id="fixture-owner",
                text=pairing["code"],
                context_token="fixture-context",
            )
        )
        self.assertEqual(identity["allowed_user_ids"], ["fixture-owner"])
        self.assertEqual(identity["context_tokens"], {"fixture-owner": "fixture-context"})
        self.assertEqual(self.identity_store.owner_pairing_summary(identity)["state"], "bound")
        self.assertFalse(self.identity_store.owner_pairing_path.exists())

    def test_text_chunking_and_client_ids_are_deterministic(self) -> None:
        text = ("第一段。\n" * 1200) + "结尾"
        chunks = split_text(text, 4000)
        self.assertEqual("".join(chunks), text)
        self.assertTrue(all(0 < len(chunk) <= 4000 for chunk in chunks))
        first = self.store.prepare_chunk("fixture-job", 0)
        second = self.store.prepare_chunk("fixture-job", 0)
        self.assertEqual(first, second)
        self.store.mark_chunk("fixture-job", 0, success=True)
        client_id, already_sent = self.store.prepare_chunk("fixture-job", 0)
        self.assertEqual(client_id, first[0])
        self.assertTrue(already_sent)


class ServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.identity_store = IdentityStore(self.root / "data")
        self.identity_store.save_identity(fixture_identity())
        self.store = GatewayStore(self.root / "data" / "gateway.sqlite3", data_dir=self.root / "data")

    def tearDown(self) -> None:
        self.temporary.cleanup()

    @staticmethod
    def run_async(coroutine: object) -> object:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            return loop.run_until_complete(coroutine)  # type: ignore[arg-type]
        finally:
            loop.close()
            asyncio.set_event_loop(None)

    def service(
        self,
        *,
        poller_enabled: bool = False,
        owner_pairing_enabled: bool = False,
        confirmation: str = "",
    ) -> GatewayService:
        service = GatewayService(
            identity_store=self.identity_store,
            store=self.store,
            controller=StubController(),  # type: ignore[arg-type]
            bootstrap_identity={},
            poller_enabled=poller_enabled,
            owner_pairing_enabled=owner_pairing_enabled,
            activation_confirmation=confirmation,
            max_media_bytes=1024 * 1024,
        )
        service.client = StubIlinkClient()  # type: ignore[assignment]
        return service

    def test_allowlist_accepts_owner_and_rejects_group_and_unknown_sender(self) -> None:
        raw = fixture_update()["msgs"][0]

        async def exercise() -> None:
            service = self.service()
            await service._ingest({**raw, "message_id": "fixture-group", "room_id": "fixture-room"})
            await service._ingest({**raw, "message_id": "fixture-unknown", "from_user_id": "fixture-stranger"})
            await service._ingest(raw)

        self.run_async(exercise())
        self.assertFalse(self.store.message_exists("fixture-group"))
        self.assertFalse(self.store.message_exists("fixture-unknown"))
        self.assertTrue(self.store.message_exists("fixture-message-1"))

    def test_new_identity_only_accepts_exact_one_time_owner_pairing_code(self) -> None:
        self.identity_store.save_identity(fixture_identity(allowed=[]))
        service = self.service(owner_pairing_enabled=True)
        service.poller_state = "pairing"
        pairing = service.start_owner_pairing()
        raw = fixture_update()["msgs"][0]
        wrong = json.loads(json.dumps(raw))
        wrong["message_id"] = "fixture-pairing-wrong"
        wrong["item_list"] = [{"type": 1, "text_item": {"text": "普通问题"}}]
        correct = json.loads(json.dumps(raw))
        correct["message_id"] = "fixture-pairing-correct"
        correct["item_list"] = [{"type": 1, "text_item": {"text": pairing["code"]}}]

        async def exercise() -> None:
            await service._ingest(wrong)
            await service._ingest(correct)

        self.run_async(exercise())
        identity = self.identity_store.load_identity()
        self.assertIsNotNone(identity)
        assert identity is not None
        self.assertEqual(identity["allowed_user_ids"], ["fixture-owner"])
        self.assertFalse(self.store.message_exists("fixture-pairing-wrong"))
        self.assertFalse(self.store.message_exists("fixture-pairing-correct"))
        self.assertEqual(service.poller_state, "polling")
        self.assertEqual(len(service.client.sent), 1)  # type: ignore[union-attr]

    def test_poll_loop_persists_cursor_after_message_and_deduplicates_replay(self) -> None:
        async def exercise() -> None:
            service = self.service()

            class OnePollClient(StubIlinkClient):
                async def get_updates(inner_self, _cursor: str, *, timeout_ms: int) -> dict:
                    service._stop.set()
                    return fixture_update()

            service.client = OnePollClient()  # type: ignore[assignment]
            await service._poll_loop()
            await service._ingest(fixture_update()["msgs"][0])

        self.run_async(exercise())
        identity = self.identity_store.load_identity()
        self.assertIsNotNone(identity)
        assert identity is not None
        self.assertEqual(identity["get_updates_buf"], "opaque-next-cursor")
        self.assertEqual(len(self.store.pending_controller()), 1)

    def test_controller_job_state_survives_store_restart(self) -> None:
        message = self.store.store_message(
            message_id="fixture-recovery",
            sender_id="fixture-owner",
            conversation_key="sha256:fixture",
            text="查询",
            media=[],
        )
        self.store.mark_submitted(message["message_id"], "fixture-controller-job")
        reopened = GatewayStore(self.store.database_path, data_dir=self.root / "data")
        submitted = reopened.submitted()
        self.assertEqual(submitted[0]["controller_job_id"], "fixture-controller-job")

    def test_controller_client_submits_and_recovers_job_asynchronously(self) -> None:
        session = StubHttpSession()
        token = "c" * 32
        client = ControllerClient("http://codex-controller:8102", token, session=session)  # type: ignore[arg-type]

        async def exercise() -> None:
            submitted = await client.submit({"version": 1, "message_id": "fixture-message"})
            recovered = await client.job(submitted["job_id"])
            self.assertEqual(submitted["state"], "queued")
            self.assertEqual(recovered["state"], "completed")

        self.run_async(exercise())
        self.assertEqual([call["method"] for call in session.calls], ["POST", "GET"])
        self.assertTrue(all(call["headers"]["Authorization"] == f"Bearer {token}" for call in session.calls))

    def test_poller_defaults_disabled_and_exact_activation_is_required(self) -> None:
        async def start_and_stop() -> None:
            disabled = self.service()
            await disabled.start()
            self.assertEqual(disabled.poller_state, "disabled")
            await disabled.stop()

        self.run_async(start_and_stop())

        async def reject_wrong_confirmation() -> None:
            wrong = self.service(poller_enabled=True, confirmation="hermes stopped")
            with self.assertRaises(StoreError) as context:
                await wrong.start()
            self.assertEqual(context.exception.code, "activation_confirmation_required")

        self.run_async(reject_wrong_confirmation())

        async def accept_exact_confirmation() -> None:
            class PollingClient(StubIlinkClient):
                async def get_updates(self, _cursor: str, *, timeout_ms: int) -> dict:
                    await asyncio.sleep(60)
                    return {"ret": 0, "msgs": [], "get_updates_buf": ""}

            class LocalGatewayService(GatewayService):
                def _refresh_client(inner_self) -> None:
                    inner_self.client = PollingClient()  # type: ignore[assignment]

            service = LocalGatewayService(
                identity_store=self.identity_store,
                store=self.store,
                controller=StubController(),  # type: ignore[arg-type]
                bootstrap_identity={},
                poller_enabled=True,
                owner_pairing_enabled=False,
                activation_confirmation="HERMES_POLLER_STOPPED",
                max_media_bytes=1024 * 1024,
            )
            await service.start()
            self.assertEqual(service.poller_state, "polling")
            await service.stop()

        self.run_async(accept_exact_confirmation())

    def test_unbound_identity_requires_explicit_pairing_mode(self) -> None:
        self.identity_store.save_identity(fixture_identity(allowed=[]))

        async def reject_unbound() -> None:
            service = self.service(poller_enabled=True, confirmation="HERMES_POLLER_STOPPED")
            with self.assertRaises(StoreError) as context:
                await service.start()
            self.assertEqual(context.exception.code, "owner_binding_required")

        self.run_async(reject_unbound())


if __name__ == "__main__":
    unittest.main()
