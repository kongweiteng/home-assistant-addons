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
    EP_GET_CONFIG,
    EP_GET_BOT_QR,
    EP_GET_UPLOAD_URL,
    EP_SEND_MESSAGE,
    EP_SEND_TYPING,
    IlinkClient,
    ProtocolError,
    SESSION_EXPIRED_ERRCODE,
    aes128_ecb_decrypt,
    aes128_ecb_encrypt,
    assert_cdn_url,
    extract_message,
    parse_aes_key,
)
from weixin_gateway.api import DASHBOARD_HTML, DASHBOARD_JS, create_server
from weixin_gateway.service import (
    ControllerClient,
    GatewayService,
    split_text,
    validate_controller_ingress_base_url,
    with_thread_short,
)
from weixin_gateway.store import (
    GatewayStore,
    IdentityStore,
    MAX_ONBOARDING_ATTEMPTS,
    StoreError,
    account_hash,
    conversation_key,
    identity_id,
    routed_message_id,
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


class CompletingController(LegacySubmittingController):
    async def job(self, _job_id: str) -> dict:
        return {"state": "completed", "result": "完成", "thread_short": "TH-ABCDEFGHIJ"}


class CompatibleSubmittingController(LegacySubmittingController):
    capability_state = "compatible"

    async def supports_capability(self, capability: str) -> bool:
        return capability == "job_capability_profile_v1"


class StubIlinkClient:
    def __init__(self, media: bytes = b"fixture-image"):
        self.media = media
        self.closed = False
        self.sent: list[dict] = []
        self.sent_media: list[dict] = []
        self.typing_calls: list[dict] = []
        self.events: list[str] = []

    async def download_media(self, _spec: dict) -> bytes:
        return self.media

    async def send_text(self, to_user_id: str, text: str, context_token: str | None, client_id: str) -> dict:
        self.events.append("text")
        self.sent.append(
            {
                "to_user_id": to_user_id,
                "text": text,
                "context_token": context_token,
                "client_id": client_id,
            }
        )
        return {"ret": 0}

    async def get_config(self, ilink_user_id: str, context_token: str | None = None) -> dict:
        return {"ret": 0, "typing_ticket": f"ticket-{ilink_user_id}"}

    async def send_typing(self, ilink_user_id: str, typing_ticket: str, status: int) -> dict:
        self.typing_calls.append(
            {
                "ilink_user_id": ilink_user_id,
                "typing_ticket": typing_ticket,
                "status": status,
            }
        )
        return {"ret": 0}

    async def send_media(
        self,
        to_user_id: str,
        path: Path,
        context_token: str | None,
        client_id: str,
    ) -> str:
        self.events.append("media")
        self.sent_media.append(
            {
                "to_user_id": to_user_id,
                "content": path.read_bytes(),
                "context_token": context_token,
                "client_id": client_id,
            }
        )
        return client_id

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
        self.assertIn('server_version = "WeixinGateway/0.4.0"', api_source)

    def test_typing_protocol_uses_ticket_and_status_contract(self) -> None:
        class TypingClient(IlinkClient):
            def __init__(self) -> None:
                super().__init__(
                    base_url="https://ilinkai.weixin.qq.com",
                    cdn_base_url="https://novac2c.cdn.weixin.qq.com/c2c",
                    token="fixture-token",
                    max_media_bytes=1024,
                )
                self.calls: list[tuple[str, dict]] = []

            async def api_post(self, endpoint: str, payload: dict) -> dict:
                self.calls.append((endpoint, payload))
                if endpoint == EP_GET_CONFIG:
                    return {"ret": 0, "typing_ticket": "fixture-ticket"}
                if endpoint == EP_SEND_TYPING:
                    return {"ret": 0}
                raise AssertionError(endpoint)

        async def exercise() -> None:
            client = TypingClient()
            self.assertEqual(
                await client.get_config("fixture-user", "fixture-context"),
                {"ret": 0, "typing_ticket": "fixture-ticket"},
            )
            self.assertEqual(
                await client.send_typing("fixture-user", "fixture-ticket", 1),
                {"ret": 0},
            )
            self.assertEqual(client.calls[0], (EP_GET_CONFIG, {"ilink_user_id": "fixture-user", "context_token": "fixture-context"}))
            self.assertEqual(client.calls[1], (EP_SEND_TYPING, {"ilink_user_id": "fixture-user", "typing_ticket": "fixture-ticket", "status": 1}))

        asyncio.run(exercise())

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

    def test_send_media_auto_starts_and_uses_the_supplied_deterministic_client_id(self) -> None:
        class UploadResponse:
            status = 200
            headers = {"x-encrypted-param": "fixture-encrypted-param"}

            async def __aenter__(self) -> "UploadResponse":
                return self

            async def __aexit__(self, *_args: object) -> None:
                return None

            async def read(self) -> bytes:
                return b""

        class UploadSession:
            closed = False

            def post(self, _url: str, **_kwargs: object) -> UploadResponse:
                return UploadResponse()

        class MediaClient(IlinkClient):
            def __init__(self) -> None:
                super().__init__(
                    base_url="https://ilinkai.weixin.qq.com",
                    cdn_base_url="https://novac2c.cdn.weixin.qq.com/c2c",
                    token="fixture-token",
                    max_media_bytes=1024 * 1024,
                )
                self.start_calls = 0
                self.api_calls: list[tuple[str, dict]] = []

            async def start(self) -> None:
                self.start_calls += 1
                self.session = UploadSession()  # type: ignore[assignment]

            async def api_post(self, endpoint: str, payload: dict) -> dict:
                self.api_calls.append((endpoint, payload))
                if endpoint == EP_GET_UPLOAD_URL:
                    return {
                        "upload_full_url": "https://novac2c.cdn.weixin.qq.com/c2c/upload"
                    }
                if endpoint == EP_SEND_MESSAGE:
                    return {"ret": 0}
                raise AssertionError(endpoint)

        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "chart.png"
            path.write_bytes(b"\x89PNG\r\n\x1a\nmedia")
            client = MediaClient()
            client_id = "codex-weixin-" + "a" * 32
            result = asyncio.run(client.send_media("fixture-owner", path, "fixture-context", client_id))
        self.assertEqual(result, client_id)
        self.assertEqual(client.start_calls, 1)
        sent_message = client.api_calls[-1][1]["msg"]
        self.assertEqual(sent_message["client_id"], client_id)
        self.assertEqual(sent_message["context_token"], "fixture-context")

    def test_qr_creation_uses_official_post_body_and_latest_ten_local_tokens(self) -> None:
        class QrSession:
            closed = False

            def __init__(self) -> None:
                self.calls: list[dict] = []

            def post(self, url: str, **kwargs: object) -> StubHttpResponse:
                self.calls.append({"url": url, **kwargs})
                return StubHttpResponse(
                    {"qrcode": "fixture-qr", "qrcode_img_content": "https://example.invalid/qr"}
                )

        session = QrSession()
        client = IlinkClient(
            base_url="https://ilinkai.weixin.qq.com",
            cdn_base_url="https://novac2c.cdn.weixin.qq.com/c2c",
            token="",
            max_media_bytes=1024,
            session=session,  # type: ignore[arg-type]
        )
        tokens = [f"token-{index:02d}-" + "x" * 16 for index in range(12)]
        result = asyncio.run(client.create_bot_qr(tokens))
        self.assertEqual(result["qrcode"], "fixture-qr")
        self.assertEqual(len(session.calls), 1)
        call = session.calls[0]
        self.assertTrue(str(call["url"]).endswith(f"/{EP_GET_BOT_QR}?bot_type=3"))
        body = json.loads(str(call["data"]))
        self.assertEqual(body, {"local_token_list": tokens[:10]})
        self.assertNotIn("base_info", body)
        self.assertNotIn("Authorization", call["headers"])

    def test_dashboard_exposes_admin_pairing_without_unsafe_html_rendering(self) -> None:
        self.assertIn("api/owner-pairing/start", DASHBOARD_JS)
        self.assertIn("绑定消息不会进入 Codex", DASHBOARD_HTML)
        self.assertIn("一人一个 ClawBot", DASHBOARD_HTML)
        self.assertIn("添加成员 ClawBot", DASHBOARD_HTML)
        self.assertIn("用户级权限", DASHBOARD_HTML)
        self.assertIn("第一个向 Owner ClawBot 私聊发送正确绑定码的微信用户成为 Owner", DASHBOARD_HTML)
        self.assertIn('id="ownerSetupPanel"', DASHBOARD_HTML)
        self.assertIn('id="currentOwnerPanel" hidden', DASHBOARD_HTML)
        self.assertIn("q('ownerSetupPanel').hidden=bound", DASHBOARD_JS)
        self.assertIn("q('currentOwnerPanel').hidden=!bound", DASHBOARD_JS)
        self.assertIn("if(currentIdentityPresent&&!confirm", DASHBOARD_JS)
        self.assertLess(DASHBOARD_HTML.index("Owner 身份"), DASHBOARD_HTML.index("用户级权限"))
        self.assertIn("api/onboarding/start", DASHBOARD_JS)
        self.assertIn("api/onboarding/qr/image", DASHBOARD_JS)
        self.assertIn("api/owner-transfer", DASHBOARD_JS)
        self.assertIn("X-CSRF-Token", DASHBOARD_JS)
        self.assertIn("脱敏短标识", DASHBOARD_HTML)
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

    def test_identity_store_keeps_owner_pointer_when_secondary_identity_changes(self) -> None:
        owner = self.identity_store.load_identity()
        assert owner is not None
        self.assertEqual(owner["identity_id"], identity_id("fixture-account"))
        owner_hash = account_hash(owner["account_id"])

        secondary = fixture_identity()
        secondary.update(
            {
                "account_id": "fixture-secondary-account",
                "token": "fixture-secondary-token-000000000000",
                "user_id": "fixture-secondary-bot",
                "allowed_user_ids": [],
            }
        )
        self.identity_store.save_identity(secondary, make_active=False)
        loaded_secondary = self.identity_store.load_identity_by_hash(account_hash(secondary["account_id"]))
        assert loaded_secondary is not None
        self.identity_store.set_cursor(loaded_secondary, "secondary-cursor")
        self.identity_store.set_context(loaded_secondary, "secondary-user", "secondary-context")

        self.assertEqual(self.identity_store.active_account_hash(), owner_hash)
        self.assertEqual(self.identity_store.load_identity()["account_id"], "fixture-account")  # type: ignore[index]
        self.assertEqual(loaded_secondary["get_updates_buf"], "secondary-cursor")
        with self.assertRaises(StoreError) as context:
            self.identity_store.save_identity(
                {**secondary, "identity_id": identity_id("different-account")},
                make_active=False,
            )
        self.assertEqual(context.exception.code, "identity_invalid")

    def test_identity_store_returns_newest_ten_unique_tokens_for_qr_creation(self) -> None:
        for index in range(12):
            identity = fixture_identity(allowed=[])
            identity.update(
                {
                    "account_id": f"fixture-token-account-{index:02d}",
                    "token": f"fixture-token-{index:02d}-" + "x" * 24,
                    "saved_at": f"2099-08-05T00:{index:02d}:00+00:00",
                }
            )
            self.identity_store.save_identity(identity, make_active=False)
        tokens = self.identity_store.recent_tokens()
        self.assertEqual(len(tokens), 10)
        self.assertEqual(tokens[0], "fixture-token-11-" + "x" * 24)
        self.assertEqual(tokens[-1], "fixture-token-02-" + "x" * 24)

    def test_legacy_identity_migration_preserves_conversation_and_allows_member_upgrade(self) -> None:
        owner_before = self.store.user_by_private_id("fixture-owner")
        assert owner_before is not None
        invitation = self.store.create_member_invitation(
            expected_revision=self.store.users_revision(),
            request_id="legacy-shared-member-invite",
        )
        member = self.store.claim_member_invitation(
            user_id="fixture-legacy-member",
            text=invitation["code"],
        )
        assert member is not None

        owner_identity = identity_id("fixture-account")
        migrated = self.store.migrate_legacy_identity(
            identity_identifier=owner_identity,
            account_digest=account_hash("fixture-account"),
        )
        self.assertEqual(migrated["bound_principals"], 2)
        self.assertEqual(
            self.store.user_by_private_id("fixture-owner")["conversation_key"],  # type: ignore[index]
            owner_before["conversation_key"],
        )
        self.assertEqual(self.store.owner_identity_route()["identity_id"], owner_identity)

        public_member = next(user for user in self.store.list_users()["users"] if user["role"] == "member")
        self.assertEqual(public_member["binding_type"], "legacy_shared")
        self.assertEqual(public_member["identity_state"], "active")
        onboarding = self.store.create_onboarding_session(
            expected_revision=self.store.users_revision(),
            request_id="upgrade-legacy-member-onboarding",
            alias="ignored-for-existing-member",
            target_wx_short=public_member["wx_short"],
        )
        member_identity = identity_id("fixture-member-account")
        attached = self.store.attach_onboarding_identity(
            session_id=onboarding["session_id"],
            identity_identifier=member_identity,
            account_digest=account_hash("fixture-member-account"),
            scanned_private_user_id="fixture-legacy-member",
        )
        self.assertEqual(attached["state"], "pending_pairing")

        def claim() -> dict | None:
            return self.store.claim_onboarding(
                identity_identifier=member_identity,
                private_user_id="fixture-legacy-member",
                text=onboarding["code"],
            )

        with ThreadPoolExecutor(max_workers=2) as executor:
            results = [future.result() for future in (executor.submit(claim), executor.submit(claim))]
        claimed = [result for result in results if result is not None]
        self.assertEqual(len(claimed), 1)
        self.assertEqual(claimed[0]["principal_id"], member["principal_id"])
        route = self.store.identity_route_for_principal(member["principal_id"])
        assert route is not None
        self.assertEqual(route["identity_id"], member_identity)
        self.assertEqual(route["binding_type"], "primary")
        self.assertEqual(self.store.owner_identity_route()["identity_id"], owner_identity)

    def test_identity_scoped_message_dedup_and_remote_work_reply_route(self) -> None:
        owner = self.store.user_by_private_id("fixture-owner")
        assert owner is not None
        owner_identity = identity_id("fixture-account")
        self.store.migrate_legacy_identity(
            identity_identifier=owner_identity,
            account_digest=account_hash("fixture-account"),
        )
        upstream_message_id = "same-upstream-message"
        first = self.store.store_message(
            message_id=routed_message_id(owner_identity, upstream_message_id),
            upstream_message_id=upstream_message_id,
            identity_identifier=owner_identity,
            principal_id_value=owner["principal_id"],
            sender_id="fixture-owner",
            conversation_key=owner["conversation_key"],
            text="第一身份",
            media=[],
            user_digest=owner["user_hash"],
            capability_profile="owner",
        )

        secondary_identity = identity_id("fixture-secondary-account")
        self.store.register_pending_identity(
            identity_identifier=secondary_identity,
            account_digest=account_hash("fixture-secondary-account"),
        )
        with self.store._connect() as connection:
            now = utc_now()
            secondary_principal = "PR-" + "b" * 32
            secondary_user_hash = user_hash("secondary-sender")
            connection.execute(
                "INSERT INTO weixin_users(user_hash,private_user_id,conversation_key,alias,role,status,revision,created_at,updated_at,last_seen_at,principal_id) "
                "VALUES (?,?,?,?,?,'active',?,?,?,?,?)",
                (
                    secondary_user_hash,
                    "secondary-sender",
                    "sha256:" + "c" * 64,
                    "第二成员",
                    "member",
                    self.store.users_revision(connection),
                    now,
                    now,
                    now,
                    secondary_principal,
                ),
            )
            connection.execute(
                "INSERT INTO conversation_links(user_hash,conversation_short,last_seen_at) VALUES (?,?,?)",
                (secondary_user_hash, self.store.short_id("CV", "sha256:" + "c" * 64), now),
            )
            connection.execute(
                "INSERT INTO identity_bindings(identity_id,principal_id,private_user_id,binding_type,state,created_at,updated_at) "
                "VALUES (?,?,?,'primary','active',?,?)",
                (secondary_identity, secondary_principal, "secondary-sender", now, now),
            )
            connection.execute(
                "UPDATE ilink_identities SET state='active',runtime_state='polling' WHERE identity_id=?",
                (secondary_identity,),
            )
        second = self.store.store_message(
            message_id=routed_message_id(secondary_identity, upstream_message_id),
            upstream_message_id=upstream_message_id,
            identity_identifier=secondary_identity,
            principal_id_value=secondary_principal,
            sender_id="secondary-sender",
            conversation_key="sha256:" + "c" * 64,
            text="第二身份",
            media=[],
            user_digest=secondary_user_hash,
            capability_profile="member_read_only",
        )
        duplicate = self.store.store_message(
            message_id=routed_message_id(owner_identity, upstream_message_id) + "-ignored",
            upstream_message_id=upstream_message_id,
            identity_identifier=owner_identity,
            principal_id_value=owner["principal_id"],
            sender_id="fixture-owner",
            conversation_key=owner["conversation_key"],
            text="重复正文不覆盖",
            media=[],
            user_digest=owner["user_hash"],
            capability_profile="owner",
        )
        self.assertNotEqual(first["message_id"], second["message_id"])
        self.assertEqual(duplicate["message_id"], first["message_id"])

        payload = {
            "version": 1,
            "message_id": "RM-ABCDEFGHIJ",
            "task_id": "RW-ABCDEFGHIJ",
            "operation": "start",
            "project_alias": "fixture",
            "instruction": "只读检查",
            "created_at": utc_now(),
            "expires_at": (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat(),
        }
        task = self.store.enqueue_remote_work_command(
            topic="home/codex-work/v1/request",
            payload=payload,
            sender_id="fixture-owner",
            user_digest=owner["user_hash"],
            identity_identifier=owner_identity,
            principal_id_value=owner["principal_id"],
        )
        self.assertEqual(task["identity_id"], owner_identity)
        self.store.record_remote_work_event(
            "home/codex-work/v1/result",
            {
                "version": 1,
                "task_id": payload["task_id"],
                "run_seq": 1,
                "sequence": 1,
                "state": "completed",
                "summary": "完成",
            },
        )
        reply = self.store.remote_work_pending_replies()[0]
        self.assertEqual(reply["identity_id"], owner_identity)
        self.assertEqual(reply["principal_id"], owner["principal_id"])

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

    def test_media_stream_is_non_consuming_until_ack_and_ack_is_idempotent(self) -> None:
        content = b"streamed-media"
        spec = {"media_type": "video", "filename": "progress.mp4", "mime_type": "video/mp4"}
        reference = self.store.store_message(
            message_id="fixture-message-stream-ack",
            sender_id="fixture-owner",
            conversation_key="sha256:fixture",
            text="施工视频",
            media=[(spec, content)],
        )["attachments"][0]["attachment_ref"]
        digest = f"sha256:{hashlib.sha256(content).hexdigest()}"

        metadata, first_handle = self.store.open_stream_attachment(reference)
        with first_handle:
            self.assertEqual(first_handle.read(), content)
        self.assertEqual(metadata["sha256"], digest)

        _metadata, second_handle = self.store.open_stream_attachment(reference)
        with second_handle:
            self.assertEqual(second_handle.read(), content)

        self.assertEqual(
            self.store.acknowledge_attachment(reference, digest),
            {"consumed": True, "idempotent_replay": False},
        )
        self.assertEqual(
            self.store.acknowledge_attachment(reference, digest),
            {"consumed": True, "idempotent_replay": True},
        )
        with self.assertRaises(StoreError) as context:
            self.store.open_stream_attachment(reference)
        self.assertEqual(context.exception.code, "attachment_consumed")

    def test_media_stream_rejects_symlinked_storage(self) -> None:
        content = b"symlinked-media"
        spec = {"media_type": "image", "filename": "site.jpg", "mime_type": "image/jpeg"}
        reference = self.store.store_message(
            message_id="fixture-message-stream-symlink",
            sender_id="fixture-owner",
            conversation_key="sha256:fixture",
            text="现场照片",
            media=[(spec, content)],
        )["attachments"][0]["attachment_ref"]
        stored = self.store.spool_dir / hashlib.sha256(content).hexdigest()
        link = self.store.spool_dir / "attachment-link"
        link.symlink_to(stored)
        with self.store._connect() as connection:
            connection.execute(
                "UPDATE attachments SET storage_name=? WHERE attachment_ref=?",
                (link.name, reference),
            )
        with self.assertRaises(StoreError) as context:
            self.store.open_stream_attachment(reference)
        self.assertEqual(context.exception.code, "attachment_invalid")

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

    def test_identity_replacement_cannot_clear_principals_or_pending_messages(self) -> None:
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
        with self.assertRaises(StoreError) as context:
            self.store.reset_access_directory_for_identity_replacement()
        self.assertEqual(context.exception.code, "identity_replacement_forbidden")
        self.assertEqual(len(self.store.list_users()["users"]), 1)
        message = self.store.get_message("identity-replacement-pending")
        self.assertEqual(message["state"], "pending_controller")
        self.assertIsNone(message["error_code"])

    def test_outbound_artifact_state_and_client_ids_survive_restart(self) -> None:
        artifact = {
            "artifact_id": "AR-" + "A" * 26,
            "mime_type": "image/png",
            "size_bytes": 16,
            "sha256": "sha256:" + "b" * 64,
        }
        first = self.store.prepare_artifact("fixture-job", artifact)
        self.assertRegex(first["client_id"], r"^codex-weixin-[a-f0-9]{32}$")
        self.store.mark_artifact("fixture-job", artifact["artifact_id"], success=False, error_code="delivery_state_unknown")
        self.store.mark_artifact_fallback("fixture-job", artifact["artifact_id"], success=True)
        reopened = GatewayStore(self.store.database_path, data_dir=self.root / "data")
        recovered = reopened.prepare_artifact("fixture-job", artifact)
        self.assertEqual(recovered["client_id"], first["client_id"])
        self.assertEqual(recovered["state"], "failed")
        self.assertEqual(recovered["error_code"], "delivery_state_unknown")
        self.assertEqual(recovered["fallback_state"], "sent")
        with self.assertRaises(StoreError) as conflict:
            reopened.prepare_artifact("fixture-job", {**artifact, "size_bytes": 17})
        self.assertEqual(conflict.exception.code, "artifact_idempotency_conflict")


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
        controller_ingress_base_url: str = "",
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
            controller_ingress_base_url=controller_ingress_base_url,
        )
        service.client = StubIlinkClient()  # type: ignore[assignment]
        return service

    def independent_member_service(self) -> tuple[GatewayService, str, dict]:
        self.service()
        onboarding = self.store.create_onboarding_session(
            expected_revision=self.store.users_revision(),
            request_id="service-independent-member-create",
            alias="独立成员",
        )
        secondary = fixture_identity(allowed=[])
        secondary.update(
            {
                "account_id": "fixture-independent-account",
                "token": "fixture-independent-token-000000000000",
                "user_id": "fixture-independent-bot",
            }
        )
        secondary_identity = identity_id(secondary["account_id"])
        self.identity_store.save_identity(secondary, make_active=False)
        self.store.attach_onboarding_identity(
            session_id=onboarding["session_id"],
            identity_identifier=secondary_identity,
            account_digest=account_hash(secondary["account_id"]),
            scanned_private_user_id="fixture-independent-member",
        )
        member = self.store.claim_onboarding(
            identity_identifier=secondary_identity,
            private_user_id="fixture-independent-member",
            text=onboarding["code"],
        )
        assert member is not None
        stored_secondary = self.identity_store.load_identity_by_hash(account_hash(secondary["account_id"]))
        assert stored_secondary is not None
        stored_secondary["allowed_user_ids"] = ["fixture-independent-member"]
        stored_secondary["context_tokens"] = {"fixture-independent-member": "independent-context"}
        self.identity_store.save_identity(stored_secondary, make_active=False)
        service = self.service()
        return service, secondary_identity, member

    def test_controller_delivery_shows_typing_until_final_reply(self) -> None:
        service = self.service()
        owner = self.store.user_by_private_id("fixture-owner")
        assert owner is not None
        self.store.store_message(
            message_id="typing-lifecycle-message",
            sender_id="fixture-owner",
            conversation_key=owner["conversation_key"],
            text="等待处理",
            media=[],
            user_digest=owner["user_hash"],
            capability_profile="owner",
        )
        service.controller = CompletingController()  # type: ignore[assignment]

        async def exercise() -> None:
            async def stop_after_cycle(_delay: float) -> None:
                service._stop.set()

            with mock.patch("weixin_gateway.service.asyncio.sleep", new=stop_after_cycle):
                await service._delivery_loop()

        self.run_async(exercise())
        client = service.client
        assert isinstance(client, StubIlinkClient)
        self.assertEqual([call["status"] for call in client.typing_calls], [1, 2])
        self.assertEqual(self.store.get_message("typing-lifecycle-message")["state"], "completed")
        self.assertFalse(service._runtime_for_identity(None).typing_sessions)

    def test_typing_session_expired_protocol_error_stops_keepalive_and_marks_runtime(self) -> None:
        service = self.service()
        runtime = service._runtime_for_identity(None)

        class ExpiringTypingClient(StubIlinkClient):
            async def send_typing(self, ilink_user_id: str, typing_ticket: str, status: int) -> dict:
                if status == 1:
                    raise ProtocolError("session_expired", "fixture session expired")
                return await super().send_typing(ilink_user_id, typing_ticket, status)

        client = ExpiringTypingClient()
        service.client = client  # type: ignore[assignment]
        runtime.client = client  # type: ignore[assignment]
        message = {
            "identity_id": runtime.identity_id,
            "sender_id": "fixture-owner",
            "message_id": "typing-session-expired",
        }

        self.run_async(service._start_typing(message))

        self.assertEqual(runtime.poller_state, "session_expired")
        self.assertEqual(runtime.last_error, "session_expired")
        self.assertFalse(runtime.typing_sessions)

    def test_two_identity_ingest_and_replies_stay_on_original_runtime(self) -> None:
        service, secondary_identity, member = self.independent_member_service()
        owner_client = StubIlinkClient()
        secondary_client = StubIlinkClient()
        service.client = owner_client  # type: ignore[assignment]
        secondary_runtime = service._runtime_for_identity(secondary_identity)
        secondary_runtime.client = secondary_client  # type: ignore[assignment]
        secondary_runtime.poller_state = "polling"

        owner_raw = fixture_update()["msgs"][0]
        secondary_raw = json.loads(json.dumps(owner_raw))
        secondary_raw["from_user_id"] = "fixture-independent-member"
        secondary_raw["context_token"] = "independent-context"

        async def exercise() -> None:
            await service._ingest(owner_raw)
            await service._ingest(secondary_raw, secondary_runtime)
            messages = self.store.pending_controller()
            self.assertEqual(len(messages), 2)
            owner_message = next(message for message in messages if message["identity_id"] != secondary_identity)
            member_message = next(message for message in messages if message["identity_id"] == secondary_identity)
            self.assertEqual(owner_message["message_id"], owner_raw["message_id"])
            self.assertEqual(
                member_message["message_id"],
                routed_message_id(secondary_identity, owner_raw["message_id"]),
            )
            await service._send_result(
                {**owner_message, "controller_job_id": "owner-route-job"},
                "Owner 回复",
            )
            await service._send_result(
                {**member_message, "controller_job_id": "member-route-job"},
                "成员回复",
            )

        self.run_async(exercise())
        self.assertEqual([item["to_user_id"] for item in owner_client.sent], ["fixture-owner"])
        self.assertEqual(
            [item["to_user_id"] for item in secondary_client.sent],
            ["fixture-independent-member"],
        )
        self.assertEqual(secondary_client.sent[0]["context_token"], "independent-context")
        self.assertEqual(member["role"], "member")

    def test_secondary_session_expiry_does_not_block_owner_reply(self) -> None:
        service, secondary_identity, member = self.independent_member_service()
        owner_client = StubIlinkClient()

        class ExpiredSecondaryClient(StubIlinkClient):
            async def send_text(
                self,
                to_user_id: str,
                text: str,
                context_token: str | None,
                client_id: str,
            ) -> dict:
                await super().send_text(to_user_id, text, context_token, client_id)
                return {"errcode": SESSION_EXPIRED_ERRCODE}

        service.client = owner_client  # type: ignore[assignment]
        secondary_runtime = service._runtime_for_identity(secondary_identity)
        secondary_runtime.client = ExpiredSecondaryClient()  # type: ignore[assignment]
        secondary_runtime.poller_state = "polling"
        owner = self.store.user_by_private_id("fixture-owner")
        assert owner is not None
        owner_route = self.store.owner_identity_route()

        async def exercise() -> None:
            with self.assertRaises(StoreError) as expired:
                await service._send_result(
                    {
                        "message_id": "secondary-expiry-message",
                        "sender_id": "fixture-independent-member",
                        "user_hash": member["user_hash"],
                        "principal_id": member["principal_id"],
                        "identity_id": secondary_identity,
                        "capability_profile": "member_read_only",
                        "controller_job_id": "secondary-expiry-job",
                    },
                    "成员回复",
                )
            self.assertEqual(expired.exception.code, "session_expired")
            await service._send_result(
                {
                    "message_id": "owner-after-secondary-expiry",
                    "sender_id": "fixture-owner",
                    "user_hash": owner["user_hash"],
                    "principal_id": owner["principal_id"],
                    "identity_id": owner_route["identity_id"],
                    "capability_profile": "owner",
                    "controller_job_id": "owner-after-secondary-expiry-job",
                },
                "Owner 仍可回复",
            )

        self.run_async(exercise())
        self.assertEqual(secondary_runtime.poller_state, "session_expired")
        self.assertEqual(service.poller_state, "disabled")
        self.assertEqual(owner_client.sent[-1]["to_user_id"], "fixture-owner")

    def test_owner_transfer_between_independent_identities_switches_active_mirror(self) -> None:
        service, secondary_identity, member = self.independent_member_service()
        public_member = next(user for user in service.users()["users"] if user["role"] == "member")

        async def exercise() -> None:
            result = await service.transfer_owner(
                {
                    "target_wx_short": public_member["wx_short"],
                    "revision": self.store.users_revision(),
                    "request_id": "independent-owner-transfer",
                    "confirmation": "TRANSFER_OWNER",
                }
            )
            self.assertEqual(result["owner"]["wx_short"], public_member["wx_short"])

        self.run_async(exercise())
        active = self.identity_store.load_identity()
        assert active is not None
        self.assertEqual(active["identity_id"], secondary_identity)
        self.assertEqual(active["allowed_user_ids"], ["fixture-independent-member"])
        self.assertEqual(self.store.owner_identity_route()["identity_id"], secondary_identity)
        owner_id, context = service.notification_owner_context()
        self.assertEqual(owner_id, "fixture-independent-member")
        self.assertEqual(context, "independent-context")
        self.assertEqual(member["conversation_key"], self.store.active_owner()["conversation_key"])

    def test_primary_member_suspend_resume_and_revoke_control_only_its_runtime(self) -> None:
        service, secondary_identity, _member = self.independent_member_service()
        service.poller_enabled = True
        runtime = service._runtime_for_identity(secondary_identity)

        class PollingClient(StubIlinkClient):
            async def get_updates(self, _cursor: str, *, timeout_ms: int) -> dict:
                await asyncio.sleep(60)
                return {"ret": 0, "msgs": [], "get_updates_buf": ""}

        runtime.client = PollingClient()  # type: ignore[assignment]
        public_member = next(user for user in service.users()["users"] if user["role"] == "member")

        async def exercise() -> None:
            await service._resume_identity_runtime(runtime)
            self.assertEqual(runtime.poller_state, "polling")
            self.assertIsNotNone(runtime.token_lock)
            self.assertIsNotNone(runtime.poll_task)
            await service.change_user_state(
                public_member["wx_short"],
                "suspend",
                {"revision": self.store.users_revision(), "request_id": "runtime-suspend-01"},
            )
            self.assertEqual(self.store.identity_record(secondary_identity)["state"], "paused")
            self.assertIsNone(runtime.token_lock)
            self.assertIsNone(runtime.poll_task)
            await service.change_user_state(
                public_member["wx_short"],
                "resume",
                {"revision": self.store.users_revision(), "request_id": "runtime-resume-01"},
            )
            self.assertEqual(runtime.poller_state, "polling")
            self.assertIsNotNone(runtime.token_lock)
            await service.change_user_state(
                public_member["wx_short"],
                "revoke",
                {"revision": self.store.users_revision(), "request_id": "runtime-revoke-01"},
            )
            self.assertNotIn(secondary_identity, service._runtimes)
            self.assertEqual(self.store.identity_record(secondary_identity)["state"], "revoked")
            self.assertIsNone(
                self.identity_store.load_identity_by_hash(account_hash("fixture-independent-account"))
            )

        self.run_async(exercise())

    def test_token_conflict_isolated_to_the_conflicting_identity(self) -> None:
        service, secondary_identity, _member = self.independent_member_service()
        service.poller_enabled = True
        owner_runtime = service._runtime_for_identity(None)
        secondary_runtime = service._runtime_for_identity(secondary_identity)
        secondary_runtime.identity["token"] = owner_runtime.identity["token"]

        class PollingClient(StubIlinkClient):
            async def get_updates(self, _cursor: str, *, timeout_ms: int) -> dict:
                await asyncio.sleep(60)
                return {"ret": 0, "msgs": [], "get_updates_buf": ""}

        owner_runtime.client = PollingClient()  # type: ignore[assignment]
        secondary_runtime.client = PollingClient()  # type: ignore[assignment]

        async def exercise() -> None:
            await service._resume_identity_runtime(owner_runtime)
            await service._resume_identity_runtime(secondary_runtime)
            self.assertEqual(owner_runtime.poller_state, "polling")
            self.assertEqual(secondary_runtime.poller_state, "token_conflict")
            self.assertEqual(service.poller_state, "polling")
            await service._stop_identity_runtime(owner_runtime)

        self.run_async(exercise())

    def test_delivery_loop_skips_expired_identity_and_continues_other_identity(self) -> None:
        service, secondary_identity, member = self.independent_member_service()
        owner = self.store.user_by_private_id("fixture-owner")
        assert owner is not None
        owner_route = self.store.owner_identity_route()
        secondary_message = self.store.store_message(
            message_id=routed_message_id(secondary_identity, "delivery-secondary"),
            upstream_message_id="delivery-secondary",
            identity_identifier=secondary_identity,
            principal_id_value=member["principal_id"],
            sender_id="fixture-independent-member",
            conversation_key=member["conversation_key"],
            text="成员任务",
            media=[],
            user_digest=member["user_hash"],
            capability_profile="member_read_only",
        )
        owner_message = self.store.store_message(
            message_id="delivery-owner",
            upstream_message_id="delivery-owner",
            identity_identifier=owner_route["identity_id"],
            principal_id_value=owner["principal_id"],
            sender_id="fixture-owner",
            conversation_key=owner["conversation_key"],
            text="Owner 任务",
            media=[],
            user_digest=owner["user_hash"],
            capability_profile="owner",
        )
        self.store.mark_submitted(secondary_message["message_id"], "delivery-secondary-job")
        self.store.mark_submitted(owner_message["message_id"], "delivery-owner-job")
        service.controller = CompletedController()  # type: ignore[assignment]
        owner_client = StubIlinkClient()
        service.client = owner_client  # type: ignore[assignment]
        service._runtime_for_identity(secondary_identity).poller_state = "session_expired"

        async def exercise() -> None:
            async def stop_after_cycle(_delay: float) -> None:
                service._stop.set()

            with mock.patch("weixin_gateway.service.asyncio.sleep", new=stop_after_cycle):
                await service._delivery_loop()

        self.run_async(exercise())
        self.assertEqual(self.store.get_message(secondary_message["message_id"])["state"], "controller_submitted")
        self.assertEqual(self.store.get_message(owner_message["message_id"])["state"], "completed")
        self.assertEqual([item["to_user_id"] for item in owner_client.sent], ["fixture-owner"])

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
            async def get_bot_qr_status(
                self,
                _qrcode: str,
                *,
                base_url: str | None = None,
                verify_code: str | None = None,
            ) -> dict:
                return {
                    "status": "confirmed",
                    "ilink_bot_id": "fixture-account",
                    "bot_token": "refreshed-ilink-token-000000000000",
                    "baseurl": base_url or "https://ilinkai.weixin.qq.com",
                    "ilink_user_id": "fixture-owner",
                }

        self.run_async(service._poll_qr(ConfirmedQrClient()))
        identity = self.identity_store.load_identity()
        assert identity is not None
        self.assertEqual(service.qr_state["state"], "credential_ready")
        self.assertEqual(identity["allowed_user_ids"], ["fixture-owner"])
        self.assertEqual(self.store.active_owner()["private_user_id"], "fixture-owner")

    def test_different_account_owner_qr_is_rejected_without_clearing_access_directory(self) -> None:
        service = self.service()
        service.qr_state = {
            "state": "scanned",
            "qrcode": "fixture-qr-new-account",
            "has_image": True,
            "base_url": "https://ilinkai.weixin.qq.com",
        }

        class ConfirmedQrClient(StubIlinkClient):
            async def get_bot_qr_status(
                self,
                _qrcode: str,
                *,
                base_url: str | None = None,
                verify_code: str | None = None,
            ) -> dict:
                return {
                    "status": "confirmed",
                    "ilink_bot_id": "replacement-account",
                    "bot_token": "replacement-ilink-token-00000000000",
                    "baseurl": base_url or "https://ilinkai.weixin.qq.com",
                    "ilink_user_id": "fixture-owner",
                }

        self.run_async(service._poll_qr(ConfirmedQrClient()))
        identity = self.identity_store.load_identity()
        assert identity is not None
        self.assertEqual(service.qr_state["state"], "failed")
        self.assertEqual(service.qr_state["error_code"], "owner_identity_mismatch")
        self.assertEqual(identity["account_id"], "fixture-account")
        self.assertEqual(identity["allowed_user_ids"], ["fixture-owner"])
        self.assertEqual(len(self.store.list_users()["users"]), 1)

    def test_member_onboarding_qr_verify_and_pairing_activate_independent_runtime(self) -> None:
        service = self.service(poller_enabled=True, confirmation="HERMES_POLLER_STOPPED")

        class MemberQrClient(StubIlinkClient):
            def __init__(self) -> None:
                super().__init__()
                self.verify_codes: list[str | None] = []

            async def start(self) -> None:
                return None

            async def create_bot_qr(self, local_tokens: list[str]) -> dict:
                self.local_tokens = local_tokens
                return {"qrcode": "member-onboarding-qr", "qrcode_img_content": "fixture-member-qr"}

            async def get_bot_qr_status(
                self,
                _qrcode: str,
                *,
                base_url: str | None = None,
                verify_code: str | None = None,
            ) -> dict:
                self.verify_codes.append(verify_code)
                if verify_code != "2468":
                    return {"status": "need_verifycode"}
                return {
                    "status": "confirmed",
                    "ilink_bot_id": "fixture-onboarded-account",
                    "bot_token": "fixture-onboarded-token-000000000000",
                    "baseurl": base_url or "https://ilinkai.weixin.qq.com",
                    "ilink_user_id": "fixture-onboarded-member",
                }

        qr_client = MemberQrClient()
        service._new_qr_client = lambda: qr_client  # type: ignore[method-assign]
        service._render_qr_image = lambda target, content: (
            target.parent.mkdir(parents=True, exist_ok=True),
            target.write_bytes(content.encode("utf-8")),
        )  # type: ignore[method-assign]

        async def start_pairing_runtime(runtime: object) -> None:
            service._set_runtime_state(runtime, "pairing", identity_state="pending_pairing")  # type: ignore[arg-type]

        service._resume_identity_runtime = start_pairing_runtime  # type: ignore[method-assign]

        async def exercise() -> None:
            created = await service.start_member_onboarding(
                {
                    "alias": "二维码成员",
                    "revision": self.store.users_revision(),
                    "request_id": "member-qr-onboarding-create",
                    "ttl_seconds": 900,
                }
            )
            self.assertIn("code", created)
            for _ in range(20):
                if service.member_qr_state and service.member_qr_state.get("state") == "need_verifycode":
                    break
                await asyncio.sleep(0.01)
            self.assertEqual(service.member_qr_state["state"], "need_verifycode")  # type: ignore[index]
            service.submit_member_onboarding_verify_code(
                created["session_short"],
                {"verify_code": "2468"},
            )
            assert service._member_qr_task is not None
            await asyncio.wait_for(service._member_qr_task, timeout=2)
            self.assertEqual(service.member_qr_state["state"], "pending_pairing")  # type: ignore[index]
            identity_identifier = identity_id("fixture-onboarded-account")
            runtime = service._runtime_for_identity(identity_identifier)
            self.assertEqual(runtime.poller_state, "pairing")
            runtime.client = StubIlinkClient()  # type: ignore[assignment]
            raw = fixture_update()["msgs"][0]
            pairing_message = json.loads(json.dumps(raw))
            pairing_message["message_id"] = "member-onboarding-code-message"
            pairing_message["from_user_id"] = "fixture-onboarded-member"
            pairing_message["context_token"] = "fixture-onboarded-context"
            pairing_message["item_list"] = [
                {"type": 1, "text_item": {"text": created["code"]}}
            ]
            await service._ingest(pairing_message, runtime)
            self.assertEqual(runtime.poller_state, "polling")
            self.assertEqual(service.member_qr_state["state"], "active")  # type: ignore[index]
            user = self.store.user_by_identity_sender(identity_identifier, "fixture-onboarded-member")
            assert user is not None
            self.assertEqual(user["role"], "member")
            self.assertEqual(user["capability_profile"] if "capability_profile" in user else "member_read_only", "member_read_only")
            self.assertFalse(self.store.message_exists("member-onboarding-code-message"))
            self.assertEqual(
                self.identity_store.active_account_hash(),
                account_hash("fixture-account"),
            )

        self.run_async(exercise())

    def test_member_qr_terminal_states_fail_closed(self) -> None:
        cases = (
            ("binded_redirect", "already_bound", "already_bound"),
            ("expired", "expired", "expired"),
            ("verify_code_blocked", "verify_code_blocked", "failed"),
        )
        for upstream_status, public_state, stored_state in cases:
            with self.subTest(status=upstream_status), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                identity_store = IdentityStore(root / "data")
                identity_store.save_identity(fixture_identity())
                store = GatewayStore(root / "data" / "gateway.sqlite3", data_dir=root / "data")
                service = GatewayService(
                    identity_store=identity_store,
                    store=store,
                    controller=StubController(),  # type: ignore[arg-type]
                    bootstrap_identity={},
                    poller_enabled=True,
                    owner_pairing_enabled=False,
                    activation_confirmation="HERMES_POLLER_STOPPED",
                    max_media_bytes=1024,
                )

                class TerminalQrClient(StubIlinkClient):
                    async def start(self) -> None:
                        return None

                    async def create_bot_qr(self, _local_tokens: list[str]) -> dict:
                        return {
                            "qrcode": f"terminal-{upstream_status}",
                            "qrcode_img_content": f"image-{upstream_status}",
                        }

                    async def get_bot_qr_status(
                        self,
                        _qrcode: str,
                        *,
                        base_url: str | None = None,
                        verify_code: str | None = None,
                    ) -> dict:
                        return {"status": upstream_status}

                client = TerminalQrClient()
                service._new_qr_client = lambda: client  # type: ignore[method-assign]
                service._render_qr_image = lambda target, content: (
                    target.parent.mkdir(parents=True, exist_ok=True),
                    target.write_bytes(content.encode("utf-8")),
                )  # type: ignore[method-assign]

                async def no_sleep(_delay: float) -> None:
                    return None

                async def exercise() -> None:
                    with mock.patch("weixin_gateway.service.asyncio.sleep", new=no_sleep):
                        created = await service.start_member_onboarding(
                            {
                                "alias": f"终态成员-{upstream_status}",
                                "revision": store.users_revision(),
                                "request_id": f"terminal-{upstream_status}-create",
                                "ttl_seconds": 900,
                            }
                        )
                        assert service._member_qr_task is not None
                        await asyncio.wait_for(service._member_qr_task, timeout=2)
                    self.assertEqual(service.member_qr_state["state"], public_state)  # type: ignore[index]
                    self.assertEqual(
                        store.onboarding_session(created["session_short"])["state"],
                        stored_state,
                    )
                    self.assertTrue(client.closed)

                self.run_async(exercise())

    def test_pairing_attempt_limit_stops_runtime_and_removes_pending_credentials(self) -> None:
        self.service()
        onboarding = self.store.create_onboarding_session(
            expected_revision=self.store.users_revision(),
            request_id="pairing-attempt-limit-create",
            alias="错误码成员",
        )
        secondary = fixture_identity(allowed=[])
        secondary.update(
            {
                "account_id": "fixture-pairing-limit-account",
                "token": "fixture-pairing-limit-token-000000000",
                "user_id": "fixture-pairing-limit-bot",
            }
        )
        secondary_identity = identity_id(secondary["account_id"])
        self.identity_store.save_identity(secondary, make_active=False)
        self.store.attach_onboarding_identity(
            session_id=onboarding["session_id"],
            identity_identifier=secondary_identity,
            account_digest=account_hash(secondary["account_id"]),
            scanned_private_user_id="fixture-pairing-limit-member",
        )
        service = self.service()
        runtime = service._runtime_for_identity(secondary_identity)
        self.assertEqual(runtime.pairing_session_id, onboarding["session_id"])
        runtime.poller_state = "pairing"
        runtime.client = StubIlinkClient()  # type: ignore[assignment]
        runtime.token_lock = self.identity_store.acquire_token_lock(runtime.identity["token"])
        runtime.token_lock.acquire()

        async def exercise() -> None:
            raw = fixture_update()["msgs"][0]
            for attempt in range(MAX_ONBOARDING_ATTEMPTS):
                wrong = json.loads(json.dumps(raw))
                wrong["message_id"] = f"pairing-attempt-{attempt}"
                wrong["from_user_id"] = "fixture-pairing-limit-member"
                wrong["item_list"] = [{"type": 1, "text_item": {"text": "错误接入码"}}]
                await service._ingest(wrong, runtime)

        self.run_async(exercise())
        self.assertEqual(
            self.store.onboarding_session(onboarding["session_short"])["state"],
            "failed",
        )
        self.assertEqual(self.store.identity_record(secondary_identity)["state"], "revoked")
        self.assertNotIn(secondary_identity, service._runtimes)
        self.assertIsNone(runtime.token_lock)
        self.assertTrue(runtime.client.closed)
        self.assertIsNone(
            self.identity_store.load_identity_by_hash(account_hash(secondary["account_id"]))
        )

    def test_owner_migration_mismatch_does_not_leave_or_overwrite_identity_file(self) -> None:
        service = self.service()
        key = secrets.token_bytes(32)
        different = fixture_identity()
        different.update(
            {
                "account_id": "fixture-different-migration-account",
                "token": "fixture-different-migration-token-000000",
            }
        )
        package = self.identity_store.migration_dir / "different-owner.zip"
        self.identity_store.build_migration_package(different, key, package)

        with self.assertRaises(StoreError) as mismatch:
            service.import_migration(
                package.name,
                base64.urlsafe_b64encode(key).decode("ascii"),
            )
        self.assertEqual(mismatch.exception.code, "owner_identity_mismatch")
        self.assertEqual(self.identity_store.load_identity()["account_id"], "fixture-account")  # type: ignore[index]
        self.assertIsNone(
            self.identity_store.load_identity_by_hash(account_hash(different["account_id"]))
        )

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

    def test_completed_artifact_sends_summary_then_native_image_without_link_and_replays_once(self) -> None:
        content = b"\x89PNG\r\n\x1a\nwechat-chart"

        class ArtifactController(StubController):
            configured = True

            async def artifact(self, _job_id: str, _artifact: dict, *, max_bytes: int) -> bytes:
                self.assert_limit = max_bytes
                return content

        service = self.service(controller_ingress_base_url="https://ha.example/api/hassio_ingress/controller")
        service.controller = ArtifactController()  # type: ignore[assignment]
        owner = self.store.user_by_private_id("fixture-owner")
        assert owner is not None
        message = {
            "controller_job_id": "11111111-1111-1111-1111-111111111111",
            "sender_id": "fixture-owner",
            "user_hash": owner["user_hash"],
            "capability_profile": "owner",
            "thread_short": "TH-ABCDEFGHIJ",
        }
        artifact = {
            "artifact_id": "AR-" + "B" * 26,
            "type": "image",
            "mime_type": "image/png",
            "size_bytes": len(content),
            "sha256": f"sha256:{hashlib.sha256(content).hexdigest()}",
            "width": 1280,
            "height": 960,
            "fallback_path": "/downloads/artifacts/" + "a" * 43,
        }
        job = {
            "state": "completed",
            "result": "模型完成文本",
            "result_summary": "已生成装修账单统计图：共 5 笔记录，净支出 ¥88.00。",
            "artifacts": [artifact],
        }

        async def exercise() -> None:
            self.assertIsNone(await service._send_completed_job(message, job))
            self.assertIsNone(await service._send_completed_job(message, job))

        self.run_async(exercise())
        client = service.client
        assert isinstance(client, StubIlinkClient)
        self.assertEqual(client.events, ["text", "media"])
        self.assertEqual(client.sent_media[0]["content"], content)
        self.assertRegex(client.sent_media[0]["client_id"], r"^codex-weixin-[a-f0-9]{32}$")
        self.assertNotIn("http", client.sent[0]["text"])
        self.assertIn("共 5 笔记录", client.sent[0]["text"])
        state = self.store.prepare_artifact(message["controller_job_id"], artifact)
        self.assertEqual(state["state"], "sent")
        self.assertEqual(state["fallback_state"], "pending")

    def test_known_and_unknown_media_failures_send_one_fallback_link(self) -> None:
        content = b"\x89PNG\r\n\x1a\nfailed-chart"

        class ArtifactController(StubController):
            configured = True

            async def artifact(self, _job_id: str, _artifact: dict, *, max_bytes: int) -> bytes:
                return content

        for unknown in (False, True):
            with self.subTest(unknown=unknown):
                temporary = tempfile.TemporaryDirectory()
                try:
                    root = Path(temporary.name)
                    identity_store = IdentityStore(root / "data")
                    identity_store.save_identity(fixture_identity())
                    store = GatewayStore(root / "data" / "gateway.sqlite3", data_dir=root / "data")
                    service = GatewayService(
                        identity_store=identity_store,
                        store=store,
                        controller=ArtifactController(),  # type: ignore[arg-type]
                        bootstrap_identity={},
                        poller_enabled=False,
                        owner_pairing_enabled=False,
                        activation_confirmation="",
                        max_media_bytes=1024 * 1024,
                        controller_ingress_base_url="https://ha.example/api/hassio_ingress/controller",
                    )

                    class FailedMediaClient(StubIlinkClient):
                        async def send_media(
                            self,
                            to_user_id: str,
                            path: Path,
                            context_token: str | None,
                            client_id: str,
                        ) -> str:
                            self.events.append("media")
                            raise ProtocolError(
                                "media_upload_failed",
                                "fixture",
                                delivery_unknown=unknown,
                            )

                    service.client = FailedMediaClient()  # type: ignore[assignment]
                    owner = store.user_by_private_id("fixture-owner")
                    assert owner is not None
                    job_id = "22222222-2222-2222-2222-" + ("2" * 12 if not unknown else "3" * 12)
                    artifact = {
                        "artifact_id": "AR-" + ("C" if not unknown else "D") * 26,
                        "type": "image",
                        "mime_type": "image/png",
                        "size_bytes": len(content),
                        "sha256": f"sha256:{hashlib.sha256(content).hexdigest()}",
                        "width": 1280,
                        "height": 960,
                        "fallback_path": "/downloads/artifacts/" + ("b" if not unknown else "c") * 43,
                    }
                    message = {
                        "controller_job_id": job_id,
                        "sender_id": "fixture-owner",
                        "user_hash": owner["user_hash"],
                        "capability_profile": "owner",
                    }
                    job = {
                        "state": "completed",
                        "result_summary": "已生成装修账单统计图：共 1 笔记录，净支出 ¥10.00。",
                        "artifacts": [artifact],
                    }

                    async def exercise() -> None:
                        self.assertIsNone(await service._send_completed_job(message, job))
                        self.assertIsNone(await service._send_completed_job(message, job))

                    self.run_async(exercise())
                    client = service.client
                    assert isinstance(client, FailedMediaClient)
                    self.assertEqual(client.events, ["text", "media", "text"])
                    self.assertIn("https://ha.example/api/hassio_ingress/controller/downloads/artifacts/", client.sent[-1]["text"])
                    self.assertIn("状态暂无法确认" if unknown else "发送失败", client.sent[-1]["text"])
                    state = store.prepare_artifact(job_id, artifact)
                    self.assertEqual(state["state"], "failed")
                    self.assertEqual(
                        state["error_code"],
                        "delivery_state_unknown" if unknown else "media_upload_failed",
                    )
                    self.assertEqual(state["fallback_state"], "sent")
                finally:
                    temporary.cleanup()

    def test_media_failure_without_fallback_configuration_fails_closed_without_repeating_media(self) -> None:
        content = b"\x89PNG\r\n\x1a\nno-fallback"

        class ArtifactController(StubController):
            configured = True

            async def artifact(self, _job_id: str, _artifact: dict, *, max_bytes: int) -> bytes:
                return content

        class FailedMediaClient(StubIlinkClient):
            async def send_media(
                self,
                to_user_id: str,
                path: Path,
                context_token: str | None,
                client_id: str,
            ) -> str:
                self.events.append("media")
                raise ProtocolError("media_upload_failed", "fixture")

        service = self.service()
        service.controller = ArtifactController()  # type: ignore[assignment]
        service.client = FailedMediaClient()  # type: ignore[assignment]
        owner = self.store.user_by_private_id("fixture-owner")
        assert owner is not None
        job_id = "55555555-5555-5555-5555-555555555555"
        artifact = {
            "artifact_id": "AR-" + "G" * 26,
            "type": "image",
            "mime_type": "image/png",
            "size_bytes": len(content),
            "sha256": f"sha256:{hashlib.sha256(content).hexdigest()}",
            "width": 1280,
            "height": 960,
            "fallback_path": "/downloads/artifacts/" + "e" * 43,
        }
        message = {
            "controller_job_id": job_id,
            "sender_id": "fixture-owner",
            "user_hash": owner["user_hash"],
            "capability_profile": "owner",
        }
        job = {
            "state": "completed",
            "result_summary": "已生成装修账单统计图：共 1 笔记录，净支出 ¥10.00。",
            "artifacts": [artifact],
        }

        async def exercise() -> None:
            self.assertEqual(
                await service._send_completed_job(message, job),
                "artifact_fallback_unconfigured",
            )
            self.assertEqual(
                await service._send_completed_job(message, job),
                "artifact_fallback_unconfigured",
            )

        self.run_async(exercise())
        client = service.client
        assert isinstance(client, FailedMediaClient)
        self.assertEqual(client.events, ["text", "media"])
        state = self.store.prepare_artifact(job_id, artifact)
        self.assertEqual(state["state"], "failed")
        self.assertEqual(state["fallback_state"], "failed")
        self.assertEqual(state["fallback_error_code"], "artifact_fallback_unconfigured")

    def test_media_session_expiry_stops_before_fallback_and_keeps_delivery_pending(self) -> None:
        content = b"\x89PNG\r\n\x1a\nsession-expired"

        class ArtifactController(StubController):
            configured = True

            async def artifact(self, _job_id: str, _artifact: dict, *, max_bytes: int) -> bytes:
                return content

        class ExpiredMediaClient(StubIlinkClient):
            async def send_media(
                self,
                to_user_id: str,
                path: Path,
                context_token: str | None,
                client_id: str,
            ) -> str:
                self.events.append("media")
                raise ProtocolError("session_expired", "fixture")

        service = self.service(
            controller_ingress_base_url="https://ha.example/api/hassio_ingress/controller"
        )
        service.controller = ArtifactController()  # type: ignore[assignment]
        service.client = ExpiredMediaClient()  # type: ignore[assignment]
        owner = self.store.user_by_private_id("fixture-owner")
        assert owner is not None
        job_id = "66666666-6666-6666-6666-666666666666"
        artifact = {
            "artifact_id": "AR-" + "H" * 26,
            "type": "image",
            "mime_type": "image/png",
            "size_bytes": len(content),
            "sha256": f"sha256:{hashlib.sha256(content).hexdigest()}",
            "width": 1280,
            "height": 960,
            "fallback_path": "/downloads/artifacts/" + "f" * 43,
        }
        message = {
            "controller_job_id": job_id,
            "sender_id": "fixture-owner",
            "user_hash": owner["user_hash"],
            "capability_profile": "owner",
        }
        job = {
            "state": "completed",
            "result_summary": "已生成装修账单统计图：共 1 笔记录，净支出 ¥10.00。",
            "artifacts": [artifact],
        }
        with self.assertRaises(StoreError) as context:
            self.run_async(service._send_completed_job(message, job))
        self.assertEqual(context.exception.code, "session_expired")
        self.assertEqual(service.poller_state, "session_expired")
        client = service.client
        assert isinstance(client, ExpiredMediaClient)
        self.assertEqual(client.events, ["text", "media"])
        state = self.store.prepare_artifact(job_id, artifact)
        self.assertEqual(state["state"], "pending")
        self.assertEqual(state["fallback_state"], "pending")

    def test_artifact_reply_is_suppressed_if_original_user_is_no_longer_authorized(self) -> None:
        content = b"\x89PNG\r\n\x1a\nsuppressed-chart"

        class ArtifactController(StubController):
            configured = True

            async def artifact(self, _job_id: str, _artifact: dict, *, max_bytes: int) -> bytes:
                return content

        service = self.service(controller_ingress_base_url="https://ha.example/controller")
        service.controller = ArtifactController()  # type: ignore[assignment]
        artifact = {
            "artifact_id": "AR-" + "E" * 26,
            "type": "image",
            "mime_type": "image/png",
            "size_bytes": len(content),
            "sha256": f"sha256:{hashlib.sha256(content).hexdigest()}",
            "width": 1280,
            "height": 960,
            "fallback_path": "/downloads/artifacts/" + "d" * 43,
        }
        result = self.run_async(
            service._send_completed_job(
                {
                    "controller_job_id": "33333333-3333-3333-3333-333333333333",
                    "sender_id": "fixture-owner",
                    "user_hash": "0" * 64,
                    "capability_profile": "owner",
                },
                {
                    "state": "completed",
                    "result_summary": "已生成装修账单统计图：共 1 笔记录，净支出 ¥10.00。",
                    "artifacts": [artifact],
                },
            )
        )
        self.assertEqual(result, "reply_suppressed_user_inactive")
        client = service.client
        assert isinstance(client, StubIlinkClient)
        self.assertEqual(client.events, [])

    def test_controller_ingress_base_url_requires_https_without_query_or_credentials(self) -> None:
        self.assertEqual(
            validate_controller_ingress_base_url("https://ha.example/api/hassio_ingress/token/"),
            "https://ha.example/api/hassio_ingress/token",
        )
        for value in (
            "http://ha.example/api/hassio_ingress/token",
            "https://user:pass@ha.example/path",
            "https://ha.example/path?token=secret",
        ):
            with self.subTest(value=value), self.assertRaises(StoreError):
                validate_controller_ingress_base_url(value)

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

    def test_controller_client_downloads_artifact_in_bounded_chunks_and_rejects_extra_bytes(self) -> None:
        content = b"\x89PNG\r\n\x1a\n" + b"x" * (150 * 1024)
        artifact = {
            "artifact_id": "AR-" + "F" * 26,
            "type": "image",
            "mime_type": "image/png",
            "size_bytes": len(content),
            "sha256": f"sha256:{hashlib.sha256(content).hexdigest()}",
        }

        class ChunkedContent:
            def __init__(self, body: bytes):
                self.body = body
                self.offset = 0
                self.read_limits: list[int] = []

            async def read(self, limit: int) -> bytes:
                self.read_limits.append(limit)
                if self.offset >= len(self.body):
                    return b""
                size = min(limit, 8192, len(self.body) - self.offset)
                chunk = self.body[self.offset : self.offset + size]
                self.offset += size
                return chunk

        class ArtifactResponse:
            status = 200
            content_type = "image/png"

            def __init__(self, body: bytes):
                self.headers = {
                    "Content-Length": str(len(content)),
                    "X-Content-SHA256": artifact["sha256"],
                }
                self.content = ChunkedContent(body)

            async def __aenter__(self) -> "ArtifactResponse":
                return self

            async def __aexit__(self, *_args: object) -> None:
                return None

        class ArtifactSession:
            closed = False

            def __init__(self, body: bytes):
                self.response = ArtifactResponse(body)

            def get(self, _url: str, **_kwargs: object) -> ArtifactResponse:
                return self.response

        session = ArtifactSession(content)
        client = ControllerClient(
            "http://codex-controller:8102",
            "c" * 32,
            session=session,  # type: ignore[arg-type]
        )
        loaded = self.run_async(
            client.artifact(
                "44444444-4444-4444-4444-444444444444",
                artifact,
                max_bytes=200 * 1024,
            )
        )
        self.assertEqual(loaded, content)
        self.assertGreater(len(session.response.content.read_limits), 2)
        self.assertLessEqual(max(session.response.content.read_limits), 64 * 1024)

        overflow = ArtifactSession(content + b"unexpected")
        overflow_client = ControllerClient(
            "http://codex-controller:8102",
            "c" * 32,
            session=overflow,  # type: ignore[arg-type]
        )
        with self.assertRaises(StoreError) as context:
            self.run_async(
                overflow_client.artifact(
                    "44444444-4444-4444-4444-444444444444",
                    artifact,
                    max_bytes=200 * 1024,
                )
            )
        self.assertEqual(context.exception.code, "artifact_invalid")

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

    def test_poller_configuration_does_not_depend_on_hermes_confirmation(self) -> None:
        async def start_and_stop() -> None:
            disabled = self.service()
            await disabled.start()
            self.assertEqual(disabled.poller_state, "disabled")
            await disabled.stop()

        self.run_async(start_and_stop())

        async def starts_without_hermes_confirmation() -> None:
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
                activation_confirmation="not-hermes",
                max_media_bytes=1024 * 1024,
            )
            await service.start()
            self.assertEqual(service.poller_state, "polling")
            stopped = await service.stop_poller(
                {"revision": 0, "request_id": "service-poller-stop-0001"}
            )
            self.assertFalse(service.poller_enabled)
            self.assertEqual(stopped["poller_state"], "stopped")
            started = await service.start_poller(
                {"revision": stopped["revision"], "request_id": "service-poller-start-0001"}
            )
            self.assertTrue(service.poller_enabled)
            self.assertEqual(started["poller_state"], "polling")
            await service.stop()

        self.run_async(starts_without_hermes_confirmation())

    def test_poller_control_is_persistent_revisioned_and_idempotent(self) -> None:
        first = self.store.set_poller_enabled(
            False,
            expected_revision=0,
            request_id="poller-control-stop-0001",
        )
        self.assertEqual(first["override"], "disabled")
        self.assertEqual(first["revision"], 1)
        replay = self.store.set_poller_enabled(
            False,
            expected_revision=0,
            request_id="poller-control-stop-0001",
        )
        self.assertTrue(replay["replayed"])
        self.assertEqual(self.store.poller_control(), {"override": "disabled", "revision": 1})
        with self.assertRaises(StoreError) as conflict:
            self.store.set_poller_enabled(
                True,
                expected_revision=0,
                request_id="poller-control-start-0001",
            )
        self.assertEqual(conflict.exception.code, "poller_revision_conflict")
        reopened = GatewayStore(self.root / "data" / "gateway.sqlite3", data_dir=self.root / "data")
        self.assertEqual(reopened.poller_control(), {"override": "disabled", "revision": 1})

    def test_poller_override_wins_over_addon_default_on_restart(self) -> None:
        self.store.set_poller_enabled(
            False,
            expected_revision=0,
            request_id="poller-control-restart-0001",
        )
        service = self.service(poller_enabled=True)
        self.assertTrue(service.poller_default_enabled)
        self.assertFalse(service.poller_enabled)

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
            async def fake_start_pollers() -> None:
                service.poller_state = "polling"

            service._start_pollers_unlocked = fake_start_pollers  # type: ignore[method-assign]
            loop = asyncio.new_event_loop()
            loop_thread = threading.Thread(target=loop.run_forever, daemon=True)
            loop_thread.start()
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
                poller_revision = status["poller_revision"]

                headers = {"Content-Type": "application/json", "X-CSRF-Token": csrf}
                stop_body = json.dumps(
                    {"revision": poller_revision, "request_id": "api-poller-stop-01"}
                )
                connection.request("POST", "/api/poller/stop", stop_body, headers)
                stopped = json.loads(connection.getresponse().read())
                self.assertEqual(stopped["result"]["override"], "disabled")
                self.assertEqual(stopped["result"]["poller_state"], "stopped")

                start_body = json.dumps(
                    {"revision": stopped["result"]["revision"], "request_id": "api-poller-start-01"}
                )
                connection.request("POST", "/api/poller/start", start_body, headers)
                started = json.loads(connection.getresponse().read())
                self.assertEqual(started["result"]["override"], "enabled")
                self.assertEqual(started["result"]["poller_state"], "polling")

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
                loop.call_soon_threadsafe(loop.stop)
                loop_thread.join(timeout=5)
                loop.close()

    def test_onboarding_start_verify_cancel_and_status_are_private(self) -> None:
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
                poller_enabled=True,
                owner_pairing_enabled=False,
                activation_confirmation="HERMES_POLLER_STOPPED",
                max_media_bytes=1024,
            )

            class ApiQrClient(StubIlinkClient):
                async def start(self) -> None:
                    return None

                async def create_bot_qr(self, _local_tokens: list[str]) -> dict:
                    return {
                        "qrcode": "api-member-qr-private-value",
                        "qrcode_img_content": "api-member-qr-image",
                    }

                async def get_bot_qr_status(
                    self,
                    _qrcode: str,
                    *,
                    base_url: str | None = None,
                    verify_code: str | None = None,
                ) -> dict:
                    if verify_code is None:
                        return {"status": "need_verifycode"}
                    return {"status": "wait"}

            qr_client = ApiQrClient()
            service._new_qr_client = lambda: qr_client  # type: ignore[method-assign]
            service._render_qr_image = lambda target, content: (
                target.parent.mkdir(parents=True, exist_ok=True),
                target.write_bytes(content.encode("utf-8")),
            )  # type: ignore[method-assign]

            loop = asyncio.new_event_loop()
            loop_thread = threading.Thread(target=loop.run_forever, daemon=True)
            loop_thread.start()
            server = create_server(
                "127.0.0.1",
                0,
                service=service,
                loop=loop,
                attachment_api_token="a" * 32,
            )
            server_thread = threading.Thread(target=server.serve_forever, daemon=True)
            server_thread.start()
            connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=5)
            try:
                connection.request("GET", "/api/status")
                response = connection.getresponse()
                status = json.loads(response.read())
                csrf = status["csrf_token"]
                headers = {"Content-Type": "application/json", "X-CSRF-Token": csrf}

                body = json.dumps(
                    {
                        "alias": "API 二维码成员",
                        "revision": status["users"]["revision"],
                        "request_id": "api-onboarding-start-01",
                        "ttl_seconds": 900,
                    }
                )
                connection.request("POST", "/api/onboarding/start", body, headers)
                created_response = connection.getresponse()
                created = json.loads(created_response.read())["result"]
                self.assertEqual(created_response.status, 200)
                self.assertIn("code", created)
                self.assertRegex(created["session_short"], r"^OB-[A-Z2-7]{10}$")

                connection.request("GET", "/api/onboarding/qr/image")
                image_response = connection.getresponse()
                self.assertEqual(image_response.status, 200)
                self.assertEqual(image_response.read(), b"api-member-qr-image")

                for _ in range(100):
                    connection.request("GET", "/api/status")
                    current_response = connection.getresponse()
                    current_status = json.loads(current_response.read())
                    if current_status["onboarding"]["qr"]["state"] == "need_verifycode":
                        break
                    threading.Event().wait(0.01)
                self.assertEqual(current_status["onboarding"]["qr"]["state"], "need_verifycode")

                verify_body = json.dumps({"verify_code": "2468"})
                connection.request(
                    "POST",
                    f"/api/onboarding/{created['session_short']}/verify",
                    verify_body,
                    headers,
                )
                verify_response = connection.getresponse()
                verify_document = json.loads(verify_response.read())
                self.assertEqual(verify_response.status, 200)
                self.assertEqual(verify_document["result"]["state"], "verifying")

                connection.request("GET", "/api/status")
                private_response = connection.getresponse()
                private_status = json.loads(private_response.read())
                encoded_status = json.dumps(private_status, ensure_ascii=False)
                self.assertNotIn("fixture-owner", encoded_status)
                self.assertNotIn("fixture-account", encoded_status)
                self.assertNotIn("fixture-ilink-token", encoded_status)
                self.assertNotIn(account_hash("fixture-account"), encoded_status)
                self.assertNotIn("context_tokens", encoded_status)
                self.assertNotIn("api-member-qr-private-value", encoded_status)
                self.assertNotIn(created["code"], encoded_status)

                cancel_body = json.dumps(
                    {
                        "revision": created["revision"],
                        "request_id": "api-onboarding-cancel-01",
                    }
                )
                connection.request(
                    "POST",
                    f"/api/onboarding/{created['session_short']}/cancel",
                    cancel_body,
                    headers,
                )
                cancel_response = connection.getresponse()
                cancelled = json.loads(cancel_response.read())["result"]
                self.assertEqual(cancel_response.status, 200)
                self.assertEqual(cancelled["state"], "cancelled")
                self.assertTrue(qr_client.closed)

                connection.request(
                    "POST",
                    f"/api/onboarding/{created['session_short']}/cancel",
                    cancel_body,
                    headers,
                )
                replay_response = connection.getresponse()
                replay = json.loads(replay_response.read())["result"]
                self.assertEqual(replay_response.status, 200)
                self.assertEqual(replay, cancelled)
            finally:
                connection.close()
                server.shutdown()
                server.server_close()
                server_thread.join(timeout=5)
                loop.call_soon_threadsafe(loop.stop)
                loop_thread.join(timeout=5)
                loop.close()


if __name__ == "__main__":
    unittest.main()
