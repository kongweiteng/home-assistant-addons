from __future__ import annotations

import asyncio
import base64
from datetime import datetime, timedelta, timezone
import hashlib
import http.client
import json
from pathlib import Path
import secrets
import sqlite3
import tempfile
import threading
import unittest
from unittest import mock
import zipfile
from concurrent.futures import ThreadPoolExecutor

from weixin_gateway.protocol import (
    ProtocolError,
    SESSION_EXPIRED_ERRCODE,
    aes128_ecb_decrypt,
    aes128_ecb_encrypt,
    assert_cdn_url,
    extract_message,
    parse_aes_key,
)
from weixin_gateway.api import DASHBOARD_HTML, DASHBOARD_JS, create_server
from weixin_gateway.service import ControllerClient, GatewayService, split_text, with_thread_short
from weixin_gateway.store import (
    GatewayStore,
    IdentityStore,
    StoreError,
    conversation_key,
    user_hash,
    utc_now,
)


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


class CompletedController(StubController):
    configured = True

    def __init__(self) -> None:
        self.job_calls = 0

    async def job(self, _job_id: str) -> dict:
        self.job_calls += 1
        return {"state": "completed", "result": "完成", "thread_short": "TH-ABCDEFGHIJ"}


class LegacySubmittingController(StubController):
    configured = True

    def __init__(self) -> None:
        self.submissions: list[dict] = []

    async def submit(self, payload: dict) -> dict:
        self.submissions.append(payload)
        return {"job_id": "legacy-job", "state": "queued"}

    async def job(self, _job_id: str) -> dict:
        return {"state": "queued"}


class CompatibleSubmittingController(LegacySubmittingController):
    capability_state = "compatible"

    async def supports_capability(self, capability: str) -> bool:
        return capability == "job_capability_profile_v1"


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
    def test_http_server_version_matches_addon_version(self) -> None:
        api_source = (ROOT / "weixin_gateway" / "weixin_gateway" / "api.py").read_text(encoding="utf-8")
        self.assertIn('server_version = "WeixinGateway/0.2.0"', api_source)

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
        self.assertIn("api/users/invitations", DASHBOARD_JS)
        self.assertIn("api/owner-transfer", DASHBOARD_JS)
        self.assertIn("X-CSRF-Token", DASHBOARD_JS)
        self.assertIn("HMAC 短标识", DASHBOARD_HTML)
        self.assertNotIn("innerHTML", DASHBOARD_JS)


class StoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.identity_store = IdentityStore(self.root / "data")
        self.identity_store.save_identity(fixture_identity())
        self.store = GatewayStore(self.root / "data" / "gateway.sqlite3", data_dir=self.root / "data", spool_ttl_seconds=60)
        self.store.migrate_identity_allowlist(["fixture-owner"])

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_user_directory_enforces_documented_thirty_two_user_limit(self) -> None:
        for index in range(31):
            invitation = self.store.create_member_invitation(
                expected_revision=self.store.users_revision(),
                request_id=f"capacity-invite-{index:02d}",
            )
            claimed = self.store.claim_member_invitation(
                user_id=f"fixture-member-{index:02d}",
                text=invitation["code"],
            )
            self.assertIsNotNone(claimed)

        users = self.store.list_users()
        self.assertEqual(users["limits"]["max_users"], 32)
        self.assertEqual(len(users["users"]), 32)
        with self.assertRaises(StoreError) as context:
            self.store.create_member_invitation(
                expected_revision=users["revision"],
                request_id="capacity-invite-overflow",
            )
        self.assertEqual(context.exception.code, "user_limit_reached")
        self.assertEqual(context.exception.status, 409)

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
        self.assertEqual(with_thread_short("完成", "invalid"), "完成")
        self.assertEqual(
            with_thread_short("完成", "TH-ABCDEFGHIJ"),
            "完成\n\nThread：TH-ABCDEFGHIJ",
        )
        self.assertEqual(
            with_thread_short("完成\n\nThread：TH-ABCDEFGHIJ", "TH-ABCDEFGHIJ").count("TH-ABCDEFGHIJ"),
            1,
        )

    def test_allowlist_zero_one_and_ambiguous_migration(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            empty = GatewayStore(Path(tmp) / "empty.sqlite3", data_dir=Path(tmp) / "empty")
            self.assertEqual(empty.migrate_identity_allowlist([])["state"], "empty")
            self.assertFalse(empty.list_users()["users"])
        with tempfile.TemporaryDirectory() as tmp:
            single = GatewayStore(Path(tmp) / "single.sqlite3", data_dir=Path(tmp) / "single")
            migrated = single.migrate_identity_allowlist(["fixture-owner"])
            self.assertEqual(migrated["state"], "migrated")
            self.assertEqual(single.list_users()["users"][0]["role"], "owner")
            reopened = GatewayStore(single.database_path, data_dir=Path(tmp) / "single")
            self.assertEqual(reopened.migrate_identity_allowlist(["fixture-owner"])["state"], "existing")
        with tempfile.TemporaryDirectory() as tmp:
            ambiguous = GatewayStore(Path(tmp) / "ambiguous.sqlite3", data_dir=Path(tmp) / "ambiguous")
            with self.assertRaises(StoreError) as raised:
                ambiguous.migrate_identity_allowlist(["one", "two"])
            self.assertEqual(raised.exception.code, "owner_migration_ambiguous")
            self.assertFalse(ambiguous.list_users()["users"])

    def test_legacy_schema_upgrade_is_additive_and_restart_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            database = root / "gateway.sqlite3"
            with sqlite3.connect(database) as connection:
                connection.execute(
                    """
                    CREATE TABLE inbound_messages (
                        message_id TEXT PRIMARY KEY,
                        sender_id TEXT NOT NULL,
                        conversation_key TEXT NOT NULL,
                        text TEXT NOT NULL,
                        attachments_json TEXT NOT NULL,
                        state TEXT NOT NULL,
                        controller_job_id TEXT,
                        received_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL,
                        error_code TEXT
                    )
                    """
                )
                connection.execute(
                    "INSERT INTO inbound_messages VALUES (?,?,?,?,?,'completed',NULL,?,?,NULL)",
                    ("legacy-message", "legacy-owner", "sha256:legacy", "完成", "[]", utc_now(), utc_now()),
                )
            upgraded = GatewayStore(database, data_dir=root)
            document = upgraded.get_message("legacy-message")
            self.assertEqual(document["capability_profile"], "owner_legacy")
            self.assertIsNone(document["user_hash"])
            reopened = GatewayStore(database, data_dir=root)
            self.assertEqual(reopened.get_message("legacy-message"), document)

    def test_legacy_pending_owner_message_is_backfilled_for_role_revalidation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            database = root / "gateway.sqlite3"
            with sqlite3.connect(database) as connection:
                connection.execute(
                    """
                    CREATE TABLE inbound_messages (
                        message_id TEXT PRIMARY KEY,
                        sender_id TEXT NOT NULL,
                        conversation_key TEXT NOT NULL,
                        text TEXT NOT NULL,
                        attachments_json TEXT NOT NULL,
                        state TEXT NOT NULL,
                        controller_job_id TEXT,
                        received_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL,
                        error_code TEXT
                    )
                    """
                )
                connection.execute(
                    "INSERT INTO inbound_messages VALUES (?,?,?,?,?,'pending_controller',NULL,?,?,NULL)",
                    (
                        "legacy-pending-owner",
                        "legacy-owner",
                        conversation_key("legacy-owner"),
                        "执行所有者任务",
                        "[]",
                        utc_now(),
                        utc_now(),
                    ),
                )
            upgraded = GatewayStore(database, data_dir=root)
            upgraded.migrate_identity_allowlist(["legacy-owner"])
            pending = upgraded.get_message("legacy-pending-owner")
            self.assertEqual(pending["user_hash"], user_hash("legacy-owner"))
            self.assertEqual(pending["capability_profile"], "owner")

            invitation = upgraded.create_member_invitation(
                expected_revision=upgraded.users_revision(),
                request_id="legacy-pending-invite",
            )
            member = upgraded.claim_member_invitation(
                user_id="legacy-new-owner",
                text=invitation["code"],
            )
            assert member is not None
            public_member = next(
                user for user in upgraded.list_users()["users"] if user["role"] == "member"
            )
            upgraded.transfer_owner(
                target_wx_short=public_member["wx_short"],
                expected_revision=upgraded.users_revision(),
                request_id="legacy-pending-transfer",
                confirmation="TRANSFER_OWNER",
            )
            authorization = upgraded.authorize_stored_message(
                pending["user_hash"],
                pending["capability_profile"],
            )
            self.assertTrue(authorization["allowed"])
            self.assertEqual(authorization["capability_profile"], "member_read_only")

    def test_member_invitation_is_hashed_one_time_concurrent_and_not_replayable(self) -> None:
        users = self.store.list_users()
        invitation = self.store.create_member_invitation(
            expected_revision=users["revision"],
            request_id="invite-request-0001",
        )
        self.assertIn("code", invitation)
        self.assertNotIn(invitation["code"].encode("utf-8"), self.store.database_path.read_bytes())
        replay = self.store.create_member_invitation(
            expected_revision=users["revision"],
            request_id="invite-request-0001",
        )
        self.assertNotIn("code", replay)
        self.assertEqual(replay["state"], "created_code_already_shown")

        def claim(value: str):
            return self.store.claim_member_invitation(user_id=value, text=invitation["code"])

        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(claim, ["fixture-member-a", "fixture-member-b"]))
        claimed = [result for result in results if result is not None]
        self.assertEqual(len(claimed), 1)
        self.assertIsNone(self.store.claim_member_invitation(user_id="fixture-member-c", text=invitation["code"]))
        self.assertEqual(len(self.store.list_users()["users"]), 2)

    def test_invitation_expiry_cancel_revision_and_idempotency(self) -> None:
        revision = self.store.users_revision()
        expired = self.store.create_member_invitation(
            expected_revision=revision,
            request_id="invite-expire-0001",
        )
        with self.store._connect() as connection:
            connection.execute(
                "UPDATE pairing_invitations SET expires_at=? WHERE state='waiting'",
                ((datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat(),),
            )
        self.assertIsNone(self.store.claim_member_invitation(user_id="late-user", text=expired["code"]))
        current = self.store.users_revision()
        cancellable = self.store.create_member_invitation(
            expected_revision=current,
            request_id="invite-cancel-create",
        )
        cancelled = self.store.cancel_member_invitation(
            invite_short=cancellable["invite_short"],
            expected_revision=cancellable["revision"],
            request_id="invite-cancel-0001",
        )
        self.assertEqual(cancelled["state"], "cancelled")
        self.assertIsNone(self.store.claim_member_invitation(user_id="cancelled-user", text=cancellable["code"]))
        with self.assertRaises(StoreError) as stale:
            self.store.update_alias(
                wx_short=self.store.list_users()["users"][0]["wx_short"],
                alias="新管理员",
                expected_revision=0,
                request_id="alias-stale-0001",
            )
        self.assertEqual(stale.exception.code, "revision_conflict")

    def test_owner_protection_transfer_and_short_ids_persist(self) -> None:
        before = self.store.list_users()
        owner = before["users"][0]
        with self.assertRaises(StoreError) as protected:
            self.store.change_user_state(
                wx_short=owner["wx_short"],
                action="suspend",
                expected_revision=before["revision"],
                request_id="owner-suspend-0001",
            )
        self.assertEqual(protected.exception.code, "owner_protected")
        invitation = self.store.create_member_invitation(
            expected_revision=before["revision"],
            request_id="owner-transfer-invite",
        )
        self.store.claim_member_invitation(user_id="fixture-member", text=invitation["code"])
        current = self.store.list_users()
        member = next(user for user in current["users"] if user["role"] == "member")
        with self.assertRaises(StoreError) as confirmation:
            self.store.transfer_owner(
                target_wx_short=member["wx_short"],
                expected_revision=current["revision"],
                request_id="owner-transfer-bad",
                confirmation="yes",
            )
        self.assertEqual(confirmation.exception.code, "owner_transfer_confirmation_required")
        transferred = self.store.transfer_owner(
            target_wx_short=member["wx_short"],
            expected_revision=current["revision"],
            request_id="owner-transfer-good",
            confirmation="TRANSFER_OWNER",
        )
        self.assertEqual(transferred["owner"]["wx_short"], member["wx_short"])
        after = self.store.list_users()
        self.assertEqual(sum(user["role"] == "owner" and user["status"] == "active" for user in after["users"]), 1)
        reopened = GatewayStore(self.store.database_path, data_dir=self.root / "data")
        self.assertEqual(
            {user["wx_short"] for user in after["users"]},
            {user["wx_short"] for user in reopened.list_users()["users"]},
        )
        encoded = json.dumps(reopened.list_users(), ensure_ascii=False)
        self.assertNotIn("fixture-owner", encoded)
        self.assertNotIn("fixture-member", encoded)

    def test_owner_transfer_and_suspend_race_keeps_one_owner(self) -> None:
        initial = self.store.list_users()
        invitation = self.store.create_member_invitation(
            expected_revision=initial["revision"], request_id="race-invite-create"
        )
        self.store.claim_member_invitation(user_id="race-member", text=invitation["code"])
        current = self.store.list_users()
        member = next(user for user in current["users"] if user["role"] == "member")

        def transfer():
            try:
                return self.store.transfer_owner(
                    target_wx_short=member["wx_short"],
                    expected_revision=current["revision"],
                    request_id="race-transfer-request",
                    confirmation="TRANSFER_OWNER",
                )
            except StoreError as exc:
                return exc.code

        def suspend():
            try:
                return self.store.change_user_state(
                    wx_short=member["wx_short"],
                    action="suspend",
                    expected_revision=current["revision"],
                    request_id="race-suspend-request",
                )
            except StoreError as exc:
                return exc.code

        with ThreadPoolExecutor(max_workers=2) as executor:
            results = [executor.submit(transfer), executor.submit(suspend)]
            outcomes = [future.result() for future in results]
        self.assertIn("revision_conflict", outcomes)
        final = self.store.list_users()["users"]
        self.assertEqual(sum(user["role"] == "owner" and user["status"] == "active" for user in final), 1)

    def test_identity_replacement_resets_principals_and_fails_pending_messages_closed(self) -> None:
        owner = self.store.user_by_private_id("fixture-owner")
        assert owner is not None
        self.store.store_message(
            message_id="identity-replacement-pending",
            sender_id="fixture-owner",
            conversation_key=owner["conversation_key"],
            text="尚未提交",
            media=[],
            user_digest=owner["user_hash"],
            capability_profile="owner",
        )
        self.store.reset_access_directory_for_identity_replacement()
        self.assertFalse(self.store.list_users()["users"])
        message = self.store.get_message("identity-replacement-pending")
        self.assertEqual(message["state"], "failed")
        self.assertEqual(message["error_code"], "identity_replaced")


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

    def test_same_account_qr_refresh_restores_existing_owner_mirror(self) -> None:
        service = self.service()
        service.qr_state = {
            "state": "scanned",
            "qrcode": "fixture-qr",
            "has_image": True,
            "base_url": "https://ilinkai.weixin.qq.com",
        }

        class ConfirmedQrClient(StubIlinkClient):
            async def api_get(self, _path: str, *, base_url: str | None = None) -> dict:
                return {
                    "status": "confirmed",
                    "ilink_bot_id": "fixture-account",
                    "bot_token": "refreshed-ilink-token-000000000000",
                    "baseurl": base_url or "https://ilinkai.weixin.qq.com",
                    "ilink_user_id": "fixture-bot-user",
                }

        self.run_async(service._poll_qr(ConfirmedQrClient()))
        identity = self.identity_store.load_identity()
        assert identity is not None
        self.assertEqual(service.qr_state["state"], "credential_ready")
        self.assertEqual(identity["allowed_user_ids"], ["fixture-owner"])
        self.assertEqual(self.store.active_owner()["private_user_id"], "fixture-owner")

    def test_different_account_qr_refresh_clears_old_access_directory(self) -> None:
        service = self.service()
        service.qr_state = {
            "state": "scanned",
            "qrcode": "fixture-qr-new-account",
            "has_image": True,
            "base_url": "https://ilinkai.weixin.qq.com",
        }

        class ConfirmedQrClient(StubIlinkClient):
            async def api_get(self, _path: str, *, base_url: str | None = None) -> dict:
                return {
                    "status": "confirmed",
                    "ilink_bot_id": "replacement-account",
                    "bot_token": "replacement-ilink-token-00000000000",
                    "baseurl": base_url or "https://ilinkai.weixin.qq.com",
                    "ilink_user_id": "replacement-bot-user",
                }

        self.run_async(service._poll_qr(ConfirmedQrClient()))
        identity = self.identity_store.load_identity()
        assert identity is not None
        self.assertEqual(identity["allowed_user_ids"], [])
        self.assertFalse(self.store.list_users()["users"])

    def test_restart_repairs_owner_mirror_after_database_only_transfer(self) -> None:
        service = self.service()
        invitation = service.create_member_invitation(
            {"revision": self.store.users_revision(), "request_id": "restart-owner-invite", "ttl_seconds": 900}
        )
        self.store.claim_member_invitation(user_id="restart-new-owner", text=invitation["code"])
        member = next(user for user in self.store.list_users()["users"] if user["role"] == "member")
        self.store.transfer_owner(
            target_wx_short=member["wx_short"],
            expected_revision=self.store.users_revision(),
            request_id="restart-database-only-transfer",
            confirmation="TRANSFER_OWNER",
        )
        before_restart = self.identity_store.load_identity()
        assert before_restart is not None
        self.assertEqual(before_restart["allowed_user_ids"], ["fixture-owner"])

        self.service()
        after_restart = self.identity_store.load_identity()
        assert after_restart is not None
        self.assertEqual(after_restart["allowed_user_ids"], ["restart-new-owner"])
        self.assertEqual(self.store.active_owner()["private_user_id"], "restart-new-owner")

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

    def test_session_expired_keeps_completed_result_pending_without_weixin_retry(self) -> None:
        message = self.store.store_message(
            message_id="fixture-session-expired",
            sender_id="fixture-owner",
            conversation_key="sha256:fixture",
            text="查询",
            media=[],
        )
        self.store.mark_submitted(message["message_id"], "fixture-controller-job")
        service = self.service()
        controller = CompletedController()
        service.controller = controller  # type: ignore[assignment]
        service.poller_state = "session_expired"

        async def exercise() -> None:
            async def stop_after_cycle(_delay: float) -> None:
                service._stop.set()

            with mock.patch("weixin_gateway.service.asyncio.sleep", new=stop_after_cycle):
                await service._delivery_loop()

        self.run_async(exercise())
        self.assertEqual(controller.job_calls, 1)
        self.assertEqual(len(service.client.sent), 0)  # type: ignore[union-attr]
        self.assertEqual(len(self.store.submitted()), 1)

    def test_send_session_expiry_stops_followup_outbound_attempts(self) -> None:
        service = self.service()
        assert service.identity is not None
        self.identity_store.set_context(service.identity, "fixture-owner", "fixture-context")

        class ExpiredSendClient(StubIlinkClient):
            async def send_text(self, to_user_id: str, text: str, context_token: str | None, client_id: str) -> dict:
                await super().send_text(to_user_id, text, context_token, client_id)
                return {"errcode": SESSION_EXPIRED_ERRCODE}

        service.client = ExpiredSendClient()  # type: ignore[assignment]
        message = {"controller_job_id": "fixture-controller-job", "sender_id": "fixture-owner"}

        async def exercise() -> None:
            with self.assertRaises(StoreError) as first:
                await service._send_result(message, "完成")
            self.assertEqual(first.exception.code, "session_expired")
            self.assertEqual(service.poller_state, "session_expired")
            with self.assertRaises(StoreError) as second:
                await service._send_result(message, "完成")
            self.assertEqual(second.exception.code, "session_expired")

        self.run_async(exercise())
        self.assertEqual(len(service.client.sent), 1)  # type: ignore[union-attr]
        self.assertEqual(service.client.sent[0]["context_token"], "fixture-context")  # type: ignore[union-attr]
        self.assertEqual(self.identity_store.context(service.identity, "fixture-owner"), "fixture-context")

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

    def test_member_invitation_claim_keeps_owner_mirror_and_creates_read_only_message(self) -> None:
        service = self.service()
        revision = self.store.users_revision()
        invitation = service.create_member_invitation(
            {"revision": revision, "request_id": "service-member-invite", "ttl_seconds": 900}
        )
        raw = fixture_update()["msgs"][0]
        claim = json.loads(json.dumps(raw))
        claim["message_id"] = "member-claim-message"
        claim["from_user_id"] = "fixture-member"
        claim["item_list"] = [{"type": 1, "text_item": {"text": invitation["code"]}}]
        question = json.loads(json.dumps(raw))
        question["message_id"] = "member-question-message"
        question["from_user_id"] = "fixture-member"

        async def exercise() -> None:
            await service._ingest(claim)
            await service._ingest(question)

        self.run_async(exercise())
        identity = self.identity_store.load_identity()
        assert identity is not None
        self.assertEqual(identity["allowed_user_ids"], ["fixture-owner"])
        self.assertEqual(len(service.client.sent), 1)  # type: ignore[union-attr]
        stored = self.store.get_message("member-question-message")
        self.assertEqual(stored["capability_profile"], "member_read_only")
        self.assertNotEqual(stored["conversation_key"], self.store.user_by_private_id("fixture-owner")["conversation_key"])

    def test_old_controller_fails_closed_for_member_without_submission(self) -> None:
        service = self.service()
        invitation = service.create_member_invitation(
            {"revision": self.store.users_revision(), "request_id": "legacy-member-invite", "ttl_seconds": 900}
        )
        member = self.store.claim_member_invitation(user_id="legacy-member", text=invitation["code"])
        assert member is not None
        message = self.store.store_message(
            message_id="legacy-member-message",
            sender_id="legacy-member",
            conversation_key=member["conversation_key"],
            text="查询装修汇总",
            media=[],
            user_digest=member["user_hash"],
            capability_profile="member_read_only",
        )
        controller = LegacySubmittingController()
        service.controller = controller  # type: ignore[assignment]

        async def exercise() -> None:
            async def stop_after_cycle(_delay: float) -> None:
                service._stop.set()

            with mock.patch("weixin_gateway.service.asyncio.sleep", new=stop_after_cycle):
                await service._delivery_loop()

        self.run_async(exercise())
        self.assertFalse(controller.submissions)
        self.assertEqual(self.store.get_message(message["message_id"])["error_code"], "controller_capability_incompatible")
        self.assertEqual(len(service.client.sent), 1)  # type: ignore[union-attr]

    def test_old_controller_keeps_owner_legacy_submission_without_profile(self) -> None:
        service = self.service()
        owner = self.store.user_by_private_id("fixture-owner")
        assert owner is not None
        self.store.store_message(
            message_id="legacy-owner-message",
            sender_id="fixture-owner",
            conversation_key=owner["conversation_key"],
            text="普通讨论",
            media=[],
            user_digest=owner["user_hash"],
            capability_profile="owner",
        )
        controller = LegacySubmittingController()
        service.controller = controller  # type: ignore[assignment]

        async def exercise() -> None:
            async def stop_after_cycle(_delay: float) -> None:
                service._stop.set()

            with mock.patch("weixin_gateway.service.asyncio.sleep", new=stop_after_cycle):
                await service._delivery_loop()

        self.run_async(exercise())
        self.assertEqual(len(controller.submissions), 1)
        self.assertNotIn("capability_profile", controller.submissions[0])

    def test_compatible_controller_receives_member_profile(self) -> None:
        service = self.service()
        invitation = service.create_member_invitation(
            {"revision": self.store.users_revision(), "request_id": "compatible-member-invite", "ttl_seconds": 900}
        )
        member = self.store.claim_member_invitation(user_id="compatible-member", text=invitation["code"])
        assert member is not None
        self.store.store_message(
            message_id="compatible-member-message",
            sender_id="compatible-member",
            conversation_key=member["conversation_key"],
            text="查询装修汇总",
            media=[],
            user_digest=member["user_hash"],
            capability_profile="member_read_only",
        )
        controller = CompatibleSubmittingController()
        service.controller = controller  # type: ignore[assignment]

        async def exercise() -> None:
            async def stop_after_cycle(_delay: float) -> None:
                service._stop.set()

            with mock.patch("weixin_gateway.service.asyncio.sleep", new=stop_after_cycle):
                await service._delivery_loop()

        self.run_async(exercise())
        self.assertEqual(len(controller.submissions), 1)
        self.assertEqual(controller.submissions[0]["capability_profile"], "member_read_only")

    def test_role_change_uses_least_privilege_for_queued_messages(self) -> None:
        service = self.service()
        owner = self.store.user_by_private_id("fixture-owner")
        assert owner is not None
        old_owner_message = self.store.store_message(
            message_id="queued-owner-before-transfer",
            sender_id="fixture-owner",
            conversation_key=owner["conversation_key"],
            text="执行所有者任务",
            media=[],
            user_digest=owner["user_hash"],
            capability_profile="owner",
        )
        invitation = service.create_member_invitation(
            {"revision": self.store.users_revision(), "request_id": "queued-owner-invite", "ttl_seconds": 900}
        )
        member = self.store.claim_member_invitation(user_id="queued-new-owner", text=invitation["code"])
        assert member is not None
        new_owner_message = self.store.store_message(
            message_id="queued-member-before-transfer",
            sender_id="queued-new-owner",
            conversation_key=member["conversation_key"],
            text="查询装修汇总",
            media=[],
            user_digest=member["user_hash"],
            capability_profile="member_read_only",
        )
        public_member = next(user for user in service.users()["users"] if user["role"] == "member")

        async def exercise() -> None:
            await service.transfer_owner(
                {
                    "target_wx_short": public_member["wx_short"],
                    "revision": self.store.users_revision(),
                    "request_id": "queued-owner-transfer",
                    "confirmation": "TRANSFER_OWNER",
                }
            )
            controller = CompatibleSubmittingController()
            service.controller = controller  # type: ignore[assignment]

            async def stop_after_cycle(_delay: float) -> None:
                service._stop.set()

            with mock.patch("weixin_gateway.service.asyncio.sleep", new=stop_after_cycle):
                await service._delivery_loop()
            self.assertEqual(len(controller.submissions), 2)
            self.assertEqual(
                [payload["capability_profile"] for payload in controller.submissions],
                ["member_read_only", "member_read_only"],
            )

        self.run_async(exercise())
        self.assertEqual(self.store.get_message(old_owner_message["message_id"])["state"], "controller_submitted")
        self.assertEqual(self.store.get_message(new_owner_message["message_id"])["state"], "controller_submitted")

    def test_completed_reply_appends_thread_short_once(self) -> None:
        service = self.service()
        owner = self.store.user_by_private_id("fixture-owner")
        assert owner is not None
        stored = self.store.store_message(
            message_id="thread-short-reply",
            sender_id="fixture-owner",
            conversation_key=owner["conversation_key"],
            text="普通讨论",
            media=[],
            user_digest=owner["user_hash"],
            capability_profile="owner",
        )
        self.store.mark_submitted(stored["message_id"], "thread-short-job")
        service.controller = CompletedController()  # type: ignore[assignment]

        async def exercise() -> None:
            async def stop_after_cycle(_delay: float) -> None:
                service._stop.set()

            with mock.patch("weixin_gateway.service.asyncio.sleep", new=stop_after_cycle):
                await service._delivery_loop()

        self.run_async(exercise())
        sent = service.client.sent  # type: ignore[union-attr]
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0]["text"].count("Thread：TH-ABCDEFGHIJ"), 1)
        self.assertEqual(self.store.get_message(stored["message_id"])["state"], "completed")

    def test_suspended_member_result_is_suppressed_before_weixin_send(self) -> None:
        service = self.service()
        invitation = service.create_member_invitation(
            {"revision": self.store.users_revision(), "request_id": "suppress-member-invite", "ttl_seconds": 900}
        )
        member = self.store.claim_member_invitation(user_id="suppress-member", text=invitation["code"])
        assert member is not None
        stored = self.store.store_message(
            message_id="suppress-member-message",
            sender_id="suppress-member",
            conversation_key=member["conversation_key"],
            text="查询装修汇总",
            media=[],
            user_digest=member["user_hash"],
            capability_profile="member_read_only",
        )
        self.store.mark_submitted(stored["message_id"], "completed-member-job")
        public_member = next(user for user in self.store.list_users()["users"] if user["role"] == "member")
        self.store.change_user_state(
            wx_short=public_member["wx_short"],
            action="suspend",
            expected_revision=self.store.users_revision(),
            request_id="suppress-member-action",
        )
        service.controller = CompletedController()  # type: ignore[assignment]

        async def exercise() -> None:
            async def stop_after_cycle(_delay: float) -> None:
                service._stop.set()

            with mock.patch("weixin_gateway.service.asyncio.sleep", new=stop_after_cycle):
                await service._delivery_loop()

        self.run_async(exercise())
        message = self.store.get_message(stored["message_id"])
        self.assertEqual(message["error_code"], "reply_suppressed_user_inactive")
        self.assertFalse(service.client.sent)  # type: ignore[union-attr]

    def test_member_suspend_waits_for_complete_multichunk_reply(self) -> None:
        service = self.service()
        invitation = service.create_member_invitation(
            {"revision": self.store.users_revision(), "request_id": "linear-send-invite", "ttl_seconds": 900}
        )
        member = self.store.claim_member_invitation(user_id="linear-send-member", text=invitation["code"])
        assert member is not None
        stored = self.store.store_message(
            message_id="linear-send-message",
            sender_id="linear-send-member",
            conversation_key=member["conversation_key"],
            text="查询",
            media=[],
            user_digest=member["user_hash"],
            capability_profile="member_read_only",
        )
        self.store.mark_submitted(stored["message_id"], "linear-send-job")
        message = self.store.get_message(stored["message_id"])
        public_member = next(user for user in self.store.list_users()["users"] if user["role"] == "member")

        class BlockingClient(StubIlinkClient):
            def __init__(self) -> None:
                super().__init__()
                self.started = asyncio.Event()
                self.release = asyncio.Event()

            async def send_text(self, to_user_id: str, text: str, context_token: str | None, client_id: str) -> dict:
                if not self.sent:
                    self.started.set()
                    await self.release.wait()
                return await super().send_text(to_user_id, text, context_token, client_id)

        client = BlockingClient()
        service.client = client  # type: ignore[assignment]

        async def exercise() -> None:
            send_task = asyncio.create_task(service._send_result(message, "甲" * 4500))
            await asyncio.wait_for(client.started.wait(), timeout=1)
            suspend_task = asyncio.create_task(
                service.change_user_state(
                    public_member["wx_short"],
                    "suspend",
                    {
                        "revision": self.store.users_revision(),
                        "request_id": "linear-send-suspend",
                    },
                )
            )
            await asyncio.sleep(0)
            self.assertFalse(suspend_task.done())
            client.release.set()
            self.assertIsNone(await asyncio.wait_for(send_task, timeout=2))
            await asyncio.wait_for(suspend_task, timeout=2)

        self.run_async(exercise())
        self.assertEqual(len(client.sent), 2)
        self.assertEqual(self.store.user_by_private_id("linear-send-member")["status"], "suspended")

    def test_suspend_and_final_send_share_a_linearizable_authorization_fence(self) -> None:
        service = self.service()
        invitation = service.create_member_invitation(
            {"revision": self.store.users_revision(), "request_id": "fence-member-invite", "ttl_seconds": 900}
        )
        member = self.store.claim_member_invitation(user_id="fence-member", text=invitation["code"])
        assert member is not None
        public_member = next(user for user in service.users()["users"] if user["role"] == "member")

        class BlockingClient(StubIlinkClient):
            def __init__(self) -> None:
                super().__init__()
                self.entered = asyncio.Event()
                self.release = asyncio.Event()

            async def send_text(self, to_user_id: str, text: str, context_token: str | None, client_id: str) -> dict:
                self.entered.set()
                await self.release.wait()
                return await super().send_text(to_user_id, text, context_token, client_id)

        async def exercise() -> None:
            client = BlockingClient()
            service.client = client  # type: ignore[assignment]
            message = {
                "message_id": "fence-message",
                "controller_job_id": "fence-job",
                "sender_id": "fence-member",
                "user_hash": member["user_hash"],
                "capability_profile": "member_read_only",
            }
            send_task = asyncio.create_task(service._send_result(message, "第一条回复"))
            await client.entered.wait()
            suspend_task = asyncio.create_task(
                service.change_user_state(
                    public_member["wx_short"],
                    "suspend",
                    {
                        "revision": self.store.users_revision(),
                        "request_id": "fence-member-suspend",
                    },
                )
            )
            await asyncio.sleep(0)
            self.assertFalse(suspend_task.done())
            client.release.set()
            self.assertIsNone(await send_task)
            await suspend_task
            sent_count = len(client.sent)
            suppression = await service._send_result(
                {**message, "controller_job_id": "fence-job-after-suspend"},
                "第二条回复",
            )
            self.assertEqual(suppression, "reply_suppressed_user_inactive")
            self.assertEqual(len(client.sent), sent_count)

        self.run_async(exercise())

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


class AdminApiTests(unittest.TestCase):
    def test_json_csrf_revision_idempotency_and_privacy(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            identity_store = IdentityStore(root / "data")
            identity_store.save_identity(fixture_identity())
            store = GatewayStore(root / "data" / "gateway.sqlite3", data_dir=root / "data")
            service = GatewayService(
                identity_store=identity_store,
                store=store,
                controller=StubController(),  # type: ignore[arg-type]
                bootstrap_identity={},
                poller_enabled=False,
                owner_pairing_enabled=False,
                activation_confirmation="",
                max_media_bytes=1024,
            )
            loop = asyncio.new_event_loop()
            server = create_server(
                "127.0.0.1",
                0,
                service=service,
                loop=loop,
                attachment_api_token="a" * 32,
            )
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=5)
            try:
                connection.request("GET", "/api/status")
                response = connection.getresponse()
                status = json.loads(response.read())
                self.assertEqual(response.status, 200)
                csrf = status["csrf_token"]
                revision = status["users"]["revision"]

                body = json.dumps(
                    {"revision": revision, "request_id": "api-invite-request-01", "ttl_seconds": 900}
                )
                connection.request("POST", "/api/users/invitations", body, {"Content-Type": "application/json"})
                denied = connection.getresponse()
                denied_document = json.loads(denied.read())
                self.assertEqual(denied.status, 403)
                self.assertEqual(denied_document["error"]["code"], "csrf_invalid")

                connection.request(
                    "POST",
                    "/api/users/invitations",
                    body,
                    {"Content-Type": "text/plain", "X-CSRF-Token": csrf},
                )
                wrong_type = connection.getresponse()
                wrong_type.read()
                self.assertEqual(wrong_type.status, 415)

                headers = {"Content-Type": "application/json", "X-CSRF-Token": csrf}
                connection.request("POST", "/api/users/invitations", body, headers)
                created = connection.getresponse()
                created_document = json.loads(created.read())
                self.assertEqual(created.status, 200)
                self.assertIn("code", created_document["result"])

                connection.request("POST", "/api/users/invitations", body, headers)
                replay = connection.getresponse()
                replay_document = json.loads(replay.read())
                self.assertEqual(replay.status, 200)
                self.assertNotIn("code", replay_document["result"])

                connection.request("GET", "/api/users")
                users_response = connection.getresponse()
                users_document = json.loads(users_response.read())
                encoded = json.dumps(users_document, ensure_ascii=False)
                self.assertNotIn("fixture-owner", encoded)
                self.assertNotIn("conversation_key", encoded)
                self.assertRegex(users_document["result"]["users"][0]["wx_short"], r"^WX-[A-Z2-7]{10}$")
            finally:
                connection.close()
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)
                loop.close()


if __name__ == "__main__":
    unittest.main()
