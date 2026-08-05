"""Contract and runtime tests for the Weixin Gateway notification adapter."""

from __future__ import annotations

import asyncio
import datetime as dt
import hashlib
import json
import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from weixin_gateway.notification import (
    HA_BIRTH_TOPIC,
    MQTT_CLIENT_ID,
    REQUEST_TOPIC,
    RESULT_TOPIC,
    STATUS_TOPIC,
    GatewayNotificationRuntime,
    NotificationConfig,
    NotificationLedger,
    NotificationProcessor,
    RequestValidationError,
    discovery_messages,
    format_weixin_text,
    parse_request,
    result_payload,
)
from weixin_gateway.protocol import ProtocolError, SESSION_EXPIRED_ERRCODE
from weixin_gateway.service import GatewayService
from weixin_gateway.store import GatewayStore, IdentityStore, StoreError


ROOT = Path(__file__).resolve().parents[1]
ADDON = ROOT / "weixin_gateway"


def config(root: Path) -> NotificationConfig:
    return NotificationConfig(
        mqtt_host="broker.example",
        mqtt_port=1883,
        mqtt_username="notification",
        mqtt_password="not-a-real-secret",
        mqtt_tls=False,
        allowed_audiences=frozenset({"owner"}),
        ledger_path=root / "notification-ledger.sqlite3",
        addon_version="0.2.0",
    )


def payload(
    now: dt.datetime,
    *,
    message_id: str = "ha-message-1",
    dedupe_key: str = "test_notice",
    source: str = "automation.test_notice",
    ttl: int = 600,
) -> dict:
    return {
        "version": 1,
        "message_id": message_id,
        "created_at": now.isoformat(),
        "level": "warning",
        "title": "测试标题",
        "message": "测试正文",
        "source": source,
        "dedupe_key": dedupe_key,
        "ttl": ttl,
        "audience": "owner",
    }


def identity(*, owners: list[str] | None = None, contexts: dict[str, str] | None = None) -> dict:
    return {
        "account_id": "fixture-account",
        "token": "fixture-ilink-token-0000000000000000",
        "base_url": "https://ilinkai.weixin.qq.com",
        "cdn_base_url": "https://novac2c.cdn.weixin.qq.com/c2c",
        "user_id": "fixture-bot",
        "allowed_user_ids": ["fixture-owner"] if owners is None else owners,
        "get_updates_buf": "",
        "context_tokens": {"fixture-owner": "fixture-context"} if contexts is None else contexts,
    }


class PackagingTests(unittest.TestCase):
    def test_feature_is_opt_in_versioned_and_pins_paho(self) -> None:
        config_text = (ADDON / "config.yaml").read_text(encoding="utf-8")
        dockerfile = (ADDON / "Dockerfile").read_text(encoding="utf-8")
        run = (ADDON / "run.sh").read_text(encoding="utf-8")
        main = (ADDON / "weixin_gateway" / "main.py").read_text(encoding="utf-8")
        self.assertIn('version: "0.2.1"', config_text)
        self.assertIn("notification_bridge_enabled: false", config_text)
        self.assertIn('notification_mqtt_host: ""', config_text)
        self.assertIn('notification_mqtt_username: "str?"', config_text)
        self.assertIn('notification_mqtt_password: "password?"', config_text)
        self.assertIn("paho-mqtt==2.1.0", dockerfile)
        self.assertIn("MQTT host、username 和 password", run)
        self.assertIn("GatewayNotificationRuntime", main)

    def test_machine_contract_matches_request_result_and_status_shapes(self) -> None:
        schema = json.loads(
            (ROOT / "contracts" / "unified_notification_v1.schema.json").read_text(encoding="utf-8")
        )
        definitions = schema["$defs"]
        self.assertEqual(
            set(definitions["request"]["required"]),
            set(payload(dt.datetime.now(dt.timezone.utc))),
        )
        self.assertEqual(
            definitions["request"]["properties"]["level"]["enum"],
            ["info", "warning", "critical"],
        )
        self.assertEqual(definitions["request"]["properties"]["audience"]["const"], "owner")
        self.assertEqual(
            set(definitions["result"]["required"]),
            set(result_payload(message_id="fixture", status="sent", attempt=1, error_code=None)),
        )
        self.assertEqual(
            set(definitions["status"]["required"]),
            {"online", "channel", "version", "updated_at"},
        )

    def test_environment_validation_fails_closed_and_owner_is_fixed(self) -> None:
        base = {
            "WEIXIN_DATA_DIR": "/tmp/fixture",
            "WEIXIN_NOTIFICATION_MQTT_HOST": "broker.example",
            "WEIXIN_NOTIFICATION_MQTT_PORT": "1883",
            "WEIXIN_NOTIFICATION_MQTT_USERNAME": "notification",
            "WEIXIN_NOTIFICATION_MQTT_PASSWORD": "fixture-secret",
            "WEIXIN_NOTIFICATION_MQTT_TLS": "false",
            "WEIXIN_NOTIFICATION_ALLOWED_AUDIENCES": "owner",
        }
        parsed = NotificationConfig.from_env(base)
        self.assertEqual(parsed.allowed_audiences, frozenset({"owner"}))
        for key in (
            "WEIXIN_NOTIFICATION_MQTT_HOST",
            "WEIXIN_NOTIFICATION_MQTT_USERNAME",
            "WEIXIN_NOTIFICATION_MQTT_PASSWORD",
        ):
            with self.subTest(key=key):
                invalid = dict(base)
                invalid[key] = ""
                with self.assertRaises(ValueError):
                    NotificationConfig.from_env(invalid)
        invalid = dict(base)
        invalid["WEIXIN_NOTIFICATION_ALLOWED_AUDIENCES"] = "owner,family"
        with self.assertRaises(ValueError):
            NotificationConfig.from_env(invalid)


class ContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.now = dt.datetime(2026, 8, 4, 1, 2, 3, tzinfo=dt.timezone.utc)

    def test_request_validation_and_exact_model_free_text(self) -> None:
        request = parse_request(payload(self.now), allowed_audiences={"owner"}, now=self.now)
        self.assertEqual(format_weixin_text(request), "【警告】测试标题\n测试正文")
        cases = [
            ({**payload(self.now), "unexpected": "blocked"}, "invalid_payload"),
            ({key: value for key, value in payload(self.now).items() if key != "source"}, "invalid_payload"),
            ({**payload(self.now), "version": 2}, "unsupported_version"),
            ({**payload(self.now), "ttl": 10}, "invalid_ttl"),
            ({**payload(self.now), "level": "debug"}, "invalid_level"),
            ({**payload(self.now), "audience": "family"}, "audience_not_allowed"),
            ({**payload(self.now), "title": ""}, "invalid_payload"),
            ({**payload(self.now), "message": "x" * 4001}, "invalid_payload"),
        ]
        for document, code in cases:
            with self.subTest(code=code):
                with self.assertRaises(RequestValidationError) as raised:
                    parse_request(document, allowed_audiences={"owner"}, now=self.now)
                self.assertEqual(raised.exception.code, code)

    def test_discovery_keeps_topics_and_uses_gateway_identity(self) -> None:
        messages = discovery_messages("0.2.0")
        self.assertIn(
            "homeassistant/binary_sensor/weixin_gateway_notification_online/config",
            messages,
        )
        self.assertIn(
            "homeassistant/sensor/weixin_gateway_notification_last_result/config",
            messages,
        )
        encoded = json.dumps(messages)
        self.assertIn(RESULT_TOPIC, encoded)
        self.assertIn(STATUS_TOPIC, encoded)
        self.assertIn("weixin_gateway", encoded)
        self.assertNotIn("hermes_notification_bridge", encoded)
        self.assertNotIn("测试正文", encoded)


class ProcessorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.config = config(self.root)
        self.ledger = NotificationLedger(self.config.ledger_path)
        self.now = dt.datetime(2026, 8, 4, 1, 2, 3, tzinfo=dt.timezone.utc)
        self.events: list[dict] = []
        self.sends: list[tuple[str, str]] = []

    def tearDown(self) -> None:
        self.ledger.close()
        self.temporary.cleanup()

    def publish(self, event: dict) -> bool:
        self.events.append(event)
        return True

    def sender(self, request, text: str):
        self.sends.append((request.message_id, text))
        return True, None, False

    def processor(self, **kwargs) -> NotificationProcessor:
        return NotificationProcessor(
            config=self.config,
            ledger=self.ledger,
            publish_result=kwargs.pop("publish_result", self.publish),
            sender=kwargs.pop("sender", self.sender),
            sleeper=kwargs.pop("sleeper", lambda _seconds: None),
            clock=kwargs.pop("clock", lambda: self.now),
            retry_delays=kwargs.pop("retry_delays", ()),
            **kwargs,
        )

    def test_success_message_id_idempotency_and_business_dedupe(self) -> None:
        processor = self.processor()
        raw = json.dumps(payload(self.now)).encode()
        self.assertTrue(processor.process(raw))
        self.assertTrue(processor.process(raw))
        second = payload(self.now, message_id="ha-message-2")
        self.assertTrue(processor.process(json.dumps(second).encode()))
        self.assertEqual(self.sends, [("ha-message-1", "【警告】测试标题\n测试正文")])
        self.assertEqual(self.events[-1]["status"], "duplicate")

    def test_expired_invalid_json_rate_limit_and_retry(self) -> None:
        expired = payload(self.now - dt.timedelta(minutes=2), ttl=30)
        self.assertTrue(self.processor().process(json.dumps(expired).encode()))
        self.assertEqual(self.events[-1]["status"], "expired")
        self.assertTrue(self.processor().process(b"not-json"))
        self.assertEqual(self.events[-1]["error_code"], "invalid_json")

        for index in range(3):
            document = payload(
                self.now,
                message_id=f"rate-{index}",
                dedupe_key=f"rate-{index}",
                source="automation.rate",
            )
            self.assertTrue(self.processor().process(json.dumps(document).encode()))
        limited = payload(
            self.now,
            message_id="rate-limited",
            dedupe_key="rate-limited",
            source="automation.rate",
        )
        before = len(self.sends)
        self.assertTrue(self.processor().process(json.dumps(limited).encode()))
        self.assertEqual(len(self.sends), before)
        self.assertEqual(self.events[-1]["error_code"], "rate_limited")

        retry_calls = []

        def retry_sender(request, text):
            retry_calls.append(text)
            return (False, "send_rate_limited", True) if len(retry_calls) == 1 else (True, None, False)

        retry_document = payload(
            self.now,
            message_id="retry-1",
            dedupe_key="retry-1",
            source="automation.retry",
        )
        self.assertTrue(
            self.processor(sender=retry_sender, retry_delays=(5,)).process(
                json.dumps(retry_document).encode()
            )
        )
        self.assertEqual(len(retry_calls), 2)
        self.assertIn("retrying", [event["status"] for event in self.events])

    def test_restart_unknown_state_does_not_blindly_resend(self) -> None:
        request = parse_request(payload(self.now), allowed_audiences={"owner"}, now=self.now)
        self.ledger.insert(request, status="sending", now_ts=self.now.timestamp())
        self.ledger.update(
            request.message_id,
            status="sending",
            attempts=1,
            error_code=None,
        )
        self.assertTrue(self.processor().process(json.dumps(payload(self.now)).encode()))
        self.assertFalse(self.sends)
        self.assertEqual(self.events[-1]["error_code"], "delivery_state_unknown")

    def test_final_result_publish_failure_leaves_request_unacked_without_resend(self) -> None:
        publish_calls = []

        def fail_final(event: dict) -> bool:
            publish_calls.append(event)
            return event["status"] != "sent"

        raw = json.dumps(payload(self.now)).encode()
        self.assertFalse(self.processor(publish_result=fail_final).process(raw))
        self.assertEqual(len(self.sends), 1)
        self.assertTrue(self.processor().process(raw))
        self.assertEqual(len(self.sends), 1)
        self.assertEqual(self.events[-1]["status"], "sent")

    def test_ledger_never_persists_body_credentials_or_identity(self) -> None:
        document = payload(self.now)
        document["title"] = "绝不落盘标题"
        document["message"] = "绝不落盘正文"
        self.assertTrue(self.processor().process(json.dumps(document).encode()))
        with sqlite3.connect(self.config.ledger_path) as connection:
            columns = [row[1] for row in connection.execute("PRAGMA table_info(messages)")]
            row = connection.execute("SELECT * FROM messages").fetchone()
        self.assertEqual(
            columns,
            [
                "message_id",
                "dedupe_key",
                "source",
                "received_at",
                "expires_at",
                "status",
                "attempts",
                "error_code",
                "finished_at",
            ],
        )
        self.assertIsNotNone(row)
        database_bytes = self.config.ledger_path.read_bytes()
        for forbidden in ("绝不落盘标题", "绝不落盘正文", "fixture-owner", "fixture-secret"):
            self.assertNotIn(forbidden.encode("utf-8"), database_bytes)


class StubController:
    configured = False

    async def start(self) -> None:
        return None

    async def close(self) -> None:
        return None


class RecordingIlinkClient:
    def __init__(self, *, response: dict | None = None, delay: float = 0.0) -> None:
        self.response = response or {"ret": 0}
        self.delay = delay
        self.sent: list[dict] = []
        self.active = 0
        self.max_active = 0

    async def send_text(self, to_user_id: str, text: str, context_token: str | None, client_id: str) -> dict:
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        try:
            if self.delay:
                await asyncio.sleep(self.delay)
            self.sent.append(
                {
                    "to_user_id": to_user_id,
                    "text": text,
                    "context_token": context_token,
                    "client_id": client_id,
                }
            )
            return self.response
        finally:
            self.active -= 1

    async def close(self) -> None:
        return None


class GatewayOutboundTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.identity_store = IdentityStore(self.root / "data")
        self.store = GatewayStore(self.root / "data" / "gateway.sqlite3", data_dir=self.root / "data")

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def service(self, document: dict, client: RecordingIlinkClient) -> GatewayService:
        self.identity_store.save_identity(document)
        service = GatewayService(
            identity_store=self.identity_store,
            store=self.store,
            controller=StubController(),  # type: ignore[arg-type]
            bootstrap_identity={},
            poller_enabled=False,
            owner_pairing_enabled=False,
            activation_confirmation="",
            max_media_bytes=1024,
        )
        service.client = client  # type: ignore[assignment]
        return service

    def run_async(self, coroutine):
        return asyncio.run(coroutine)

    def test_exact_owner_context_and_deterministic_client_id(self) -> None:
        client = RecordingIlinkClient()
        service = self.service(identity(), client)
        self.run_async(service.send_notification("message-1", "【通知】标题\n正文"))
        self.assertEqual(client.sent[0]["to_user_id"], "fixture-owner")
        self.assertEqual(client.sent[0]["context_token"], "fixture-context")
        expected = "codex-weixin-notification-" + hashlib.sha256(b"message-1").hexdigest()[:32]
        self.assertEqual(client.sent[0]["client_id"], expected)

    def test_members_never_join_notification_audience_and_owner_transfer_is_atomic(self) -> None:
        document = identity(contexts={"fixture-owner": "owner-context", "fixture-member": "member-context"})
        client = RecordingIlinkClient()
        service = self.service(document, client)
        invitation = service.create_member_invitation(
            {"revision": self.store.users_revision(), "request_id": "notification-member-invite", "ttl_seconds": 900}
        )
        self.store.claim_member_invitation(user_id="fixture-member", text=invitation["code"])
        self.run_async(service.send_notification("before-transfer", "正文"))
        self.assertEqual(client.sent[-1]["to_user_id"], "fixture-owner")
        users = service.users()
        member = next(user for user in users["users"] if user["role"] == "member")
        transferred = self.run_async(
            service.transfer_owner(
                {
                    "target_wx_short": member["wx_short"],
                    "revision": users["revision"],
                    "request_id": "notification-owner-transfer",
                    "confirmation": "TRANSFER_OWNER",
                }
            )
        )
        self.assertEqual(transferred["owner"]["wx_short"], member["wx_short"])
        self.assertEqual(service.identity["allowed_user_ids"], ["fixture-member"])
        self.run_async(service.send_notification("after-transfer", "正文"))
        self.assertEqual(client.sent[-1]["to_user_id"], "fixture-member")
        self.assertEqual(client.sent[-1]["context_token"], "member-context")

    def test_owner_transfer_waits_for_inflight_notification_then_changes_target(self) -> None:
        document = identity(contexts={"fixture-owner": "owner-context", "fixture-member": "member-context"})

        class BlockingClient(RecordingIlinkClient):
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
        service = self.service(document, client)
        invitation = service.create_member_invitation(
            {"revision": self.store.users_revision(), "request_id": "linear-notification-invite", "ttl_seconds": 900}
        )
        self.store.claim_member_invitation(user_id="fixture-member", text=invitation["code"])
        member = next(user for user in service.users()["users"] if user["role"] == "member")

        async def exercise() -> None:
            notification_task = asyncio.create_task(service.send_notification("linear-notification-before", "第一条"))
            await asyncio.wait_for(client.started.wait(), timeout=1)
            transfer_task = asyncio.create_task(
                service.transfer_owner(
                    {
                        "target_wx_short": member["wx_short"],
                        "revision": self.store.users_revision(),
                        "request_id": "linear-notification-transfer",
                        "confirmation": "TRANSFER_OWNER",
                    }
                )
            )
            await asyncio.sleep(0)
            self.assertFalse(transfer_task.done())
            client.release.set()
            await asyncio.wait_for(notification_task, timeout=2)
            await asyncio.wait_for(transfer_task, timeout=2)
            await service.send_notification("linear-notification-after", "第二条")

        self.run_async(exercise())
        self.assertEqual([item["to_user_id"] for item in client.sent], ["fixture-owner", "fixture-member"])
        self.assertEqual(self.store.active_owner()["private_user_id"], "fixture-member")

    def test_owner_transfer_and_notification_share_a_linearizable_authorization_fence(self) -> None:
        document = identity(contexts={"fixture-owner": "owner-context", "fixture-member": "member-context"})

        class BlockingClient(RecordingIlinkClient):
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
            service = self.service(document, client)
            invitation = service.create_member_invitation(
                {"revision": self.store.users_revision(), "request_id": "notification-race-invite", "ttl_seconds": 900}
            )
            self.store.claim_member_invitation(user_id="fixture-member", text=invitation["code"])
            users = service.users()
            member = next(user for user in users["users"] if user["role"] == "member")
            notification_task = asyncio.create_task(service.send_notification("notification-before-transfer", "正文"))
            await client.entered.wait()
            transfer_task = asyncio.create_task(
                service.transfer_owner(
                    {
                        "target_wx_short": member["wx_short"],
                        "revision": users["revision"],
                        "request_id": "notification-race-transfer",
                        "confirmation": "TRANSFER_OWNER",
                    }
                )
            )
            await asyncio.sleep(0)
            self.assertFalse(transfer_task.done())
            client.release.set()
            await notification_task
            await transfer_task
            self.assertEqual(client.sent[-1]["to_user_id"], "fixture-owner")
            client.entered = asyncio.Event()
            client.release = asyncio.Event()
            client.release.set()
            await service.send_notification("notification-after-transfer", "正文")
            self.assertEqual(client.sent[-1]["to_user_id"], "fixture-member")

        self.run_async(exercise())

    def test_no_owner_ambiguous_migration_and_missing_context_fail_closed(self) -> None:
        client = RecordingIlinkClient()
        no_owner = self.service(identity(owners=[], contexts={}), client)

        async def reject_no_owner() -> None:
            with self.assertRaises(StoreError) as raised:
                await no_owner.send_notification("message-no-owner", "正文")
            self.assertEqual(raised.exception.code, "notification_owner_unavailable")

        self.run_async(reject_no_owner())
        self.assertFalse(client.sent)
        with self.assertRaises(StoreError) as ambiguous:
            self.service(identity(owners=["one", "two"], contexts={"one": "a", "two": "b"}), RecordingIlinkClient())
        self.assertEqual(ambiguous.exception.code, "owner_migration_ambiguous")

        missing_client = RecordingIlinkClient()
        missing = self.service(identity(owners=["fixture-owner"], contexts={}), missing_client)

        async def reject_missing_context() -> None:
            with self.assertRaises(StoreError) as raised:
                await missing.send_notification("message-missing-context", "正文")
            self.assertEqual(raised.exception.code, "notification_context_missing")

        self.run_async(reject_missing_context())
        self.assertFalse(missing_client.sent)

    def test_session_expired_calls_ilink_once_and_stops_followup(self) -> None:
        client = RecordingIlinkClient(response={"errcode": SESSION_EXPIRED_ERRCODE})
        service = self.service(identity(), client)

        async def exercise() -> None:
            for _ in range(2):
                with self.assertRaises(StoreError) as raised:
                    await service.send_notification("message-expired", "正文")
                self.assertEqual(raised.exception.code, "session_expired")

        self.run_async(exercise())
        self.assertEqual(len(client.sent), 1)
        self.assertEqual(service.poller_state, "session_expired")

    def test_controller_reply_and_notification_share_one_outbound_lock(self) -> None:
        client = RecordingIlinkClient(delay=0.02)
        service = self.service(identity(), client)
        stored = self.store.store_message(
            message_id="incoming-1",
            sender_id="fixture-owner",
            conversation_key="sha256:fixture",
            text="问题",
            media=[],
        )
        self.store.mark_submitted(stored["message_id"], "controller-job-1")

        async def exercise() -> None:
            await asyncio.gather(
                service._send_result(
                    {"controller_job_id": "controller-job-1", "sender_id": "fixture-owner"},
                    "普通回复",
                ),
                service.send_notification("notification-1", "【通知】标题\n正文"),
            )

        self.run_async(exercise())
        self.assertEqual(client.max_active, 1)
        self.assertEqual(len(client.sent), 2)


class FakePublishInfo:
    def __init__(self, published: bool = True) -> None:
        self.published = published
        self.wait_calls: list[float] = []

    def wait_for_publish(self, timeout: float) -> None:
        self.wait_calls.append(timeout)
        return None

    def is_published(self) -> bool:
        return self.published


class FakeProperties:
    def __init__(self, packet_type: int) -> None:
        self.packet_type = packet_type


class FakeClient:
    def __init__(self, **kwargs) -> None:
        self.init_kwargs = kwargs
        self.published: list[dict] = []
        self.subscriptions: list = []
        self.acks: list[tuple[int, int]] = []
        self.connect_call = None

    def username_pw_set(self, username: str, password: str) -> None:
        self.username = username
        self.password = password

    def tls_set(self) -> None:
        self.tls = True

    def reconnect_delay_set(self, **kwargs) -> None:
        self.reconnect = kwargs

    def will_set(self, *args, **kwargs) -> None:
        self.will = (args, kwargs)

    def publish(self, topic: str, **kwargs) -> FakePublishInfo:
        info = FakePublishInfo()
        self.published.append({"topic": topic, "info": info, **kwargs})
        return info

    def subscribe(self, topics) -> None:
        self.subscriptions.append(topics)

    def connect(self, *args, **kwargs) -> None:
        self.connect_call = (args, kwargs)

    def ack(self, mid: int, qos: int) -> None:
        self.acks.append((mid, qos))

    def disconnect(self) -> None:
        return None

    def loop_forever(self, **kwargs) -> None:
        return None


class FakeMqtt:
    class PacketTypes:
        CONNECT = 1

    class CallbackAPIVersion:
        VERSION2 = 2

    MQTTv5 = 5
    Properties = FakeProperties
    Client = FakeClient


class RuntimeTests(unittest.TestCase):
    def test_mqtt_v5_persistent_session_manual_ack_and_reconnect(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            loop = asyncio.new_event_loop()
            runtime = GatewayNotificationRuntime(
                config(Path(tmp)), FakeMqtt, service=object(), loop=loop
            )
            try:
                runtime._connect()
                args, kwargs = runtime.client.connect_call
                self.assertEqual(args, ("broker.example", 1883))
                self.assertFalse(kwargs["clean_start"])
                self.assertEqual(kwargs["properties"].SessionExpiryInterval, 24 * 60 * 60)
                self.assertEqual(runtime.client.init_kwargs["client_id"], MQTT_CLIENT_ID)
                self.assertEqual(runtime.client.init_kwargs["protocol"], FakeMqtt.MQTTv5)
                self.assertTrue(runtime.client.init_kwargs["manual_ack"])
                runtime._on_connect(runtime.client, None, None, 0, None)
                self.assertTrue(runtime.client.published)
                self.assertTrue(
                    all(not item["info"].wait_calls for item in runtime.client.published)
                )
                runtime._on_disconnect(runtime.client, None, None, "lost", None)
                runtime._on_connect(runtime.client, None, None, 0, None)
                self.assertEqual(len(runtime.client.subscriptions), 2)
                self.assertEqual(runtime.client.subscriptions[0], [(REQUEST_TOPIC, 1), (HA_BIRTH_TOPIC, 0)])
                self.assertTrue(
                    runtime._publish_result(
                        {
                            "version": 1,
                            "message_id": "fixture-result",
                            "status": "sent",
                        }
                    )
                )
                self.assertEqual(runtime.client.published[-1]["info"].wait_calls, [5])
                retained_topics = {
                    item["topic"]
                    for item in runtime.client.published
                    if item.get("retain")
                }
                self.assertIn(STATUS_TOPIC, retained_topics)
                self.assertFalse(
                    any(item["topic"] == RESULT_TOPIC and item.get("retain") for item in runtime.client.published)
                )
            finally:
                runtime.ledger.close()
                loop.close()

    def test_threadsafe_gateway_callback_uses_async_service(self) -> None:
        async def exercise() -> None:
            calls = []

            class Service:
                async def send_notification(inner_self, message_id: str, text: str) -> None:
                    calls.append((message_id, text))

            with tempfile.TemporaryDirectory() as tmp:
                runtime = GatewayNotificationRuntime(
                    config(Path(tmp)),
                    FakeMqtt,
                    service=Service(),
                    loop=asyncio.get_running_loop(),
                )
                try:
                    request = parse_request(
                        payload(dt.datetime.now(dt.timezone.utc)),
                        allowed_audiences={"owner"},
                    )
                    result = await asyncio.to_thread(
                        runtime._send_via_gateway,
                        request,
                        "【警告】测试标题\n测试正文",
                    )
                    self.assertEqual(result, (True, None, False))
                    self.assertEqual(calls, [("ha-message-1", "【警告】测试标题\n测试正文")])
                finally:
                    runtime.ledger.close()

        asyncio.run(exercise())

    def test_gateway_send_timeout_is_unknown_and_never_auto_retried(self) -> None:
        class PendingFuture:
            def __init__(self) -> None:
                self.cancelled = False

            def result(self, timeout: float) -> None:
                self.timeout = timeout
                raise TimeoutError

            def cancel(self) -> None:
                self.cancelled = True

        class Service:
            async def send_notification(self, message_id: str, text: str) -> None:
                return None

        with tempfile.TemporaryDirectory() as tmp:
            loop = asyncio.new_event_loop()
            future = PendingFuture()
            runtime = GatewayNotificationRuntime(
                config(Path(tmp)), FakeMqtt, service=Service(), loop=loop
            )
            request = parse_request(
                payload(dt.datetime.now(dt.timezone.utc)),
                allowed_audiences={"owner"},
            )
            try:
                def capture(coroutine, _loop):
                    coroutine.close()
                    return future

                with patch(
                    "weixin_gateway.notification.asyncio.run_coroutine_threadsafe",
                    side_effect=capture,
                ):
                    result = runtime._send_via_gateway(request, "正文")
                self.assertEqual(result, (False, "delivery_state_unknown", False))
                self.assertTrue(future.cancelled)
            finally:
                runtime.ledger.close()
                loop.close()

    def test_only_explicit_rate_limit_is_retryable_after_gateway_send(self) -> None:
        async def exercise(error: Exception) -> tuple[bool, str | None, bool]:
            class Service:
                async def send_notification(self, message_id: str, text: str) -> None:
                    raise error

            with tempfile.TemporaryDirectory() as tmp:
                runtime = GatewayNotificationRuntime(
                    config(Path(tmp)),
                    FakeMqtt,
                    service=Service(),
                    loop=asyncio.get_running_loop(),
                )
                try:
                    request = parse_request(
                        payload(dt.datetime.now(dt.timezone.utc)),
                        allowed_audiences={"owner"},
                    )
                    return await asyncio.to_thread(
                        runtime._send_via_gateway,
                        request,
                        "正文",
                    )
                finally:
                    runtime.ledger.close()

        self.assertEqual(
            asyncio.run(exercise(ProtocolError("send_rate_limited", "限流", retryable=True))),
            (False, "send_rate_limited", True),
        )
        self.assertEqual(
            asyncio.run(exercise(ProtocolError("ilink_timeout", "超时", retryable=True))),
            (False, "delivery_state_unknown", False),
        )
        self.assertEqual(
            asyncio.run(exercise(RuntimeError("unknown"))),
            (False, "delivery_state_unknown", False),
        )

    def test_result_publish_failure_does_not_ack_request(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            loop = asyncio.new_event_loop()
            runtime = GatewayNotificationRuntime(
                config(Path(tmp)), FakeMqtt, service=object(), loop=loop
            )

            class Message:
                payload = b"{}"
                qos = 1
                mid = 42

            class Processor:
                def process(inner_self, _payload: bytes) -> bool:
                    runtime.stop_event.set()
                    return False

            try:
                runtime.processor = Processor()  # type: ignore[assignment]
                runtime.work_queue.put(Message())
                runtime._worker_loop()
                self.assertFalse(runtime.client.acks)
            finally:
                runtime.ledger.close()
                loop.close()

    def test_successful_final_result_allows_manual_ack(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            loop = asyncio.new_event_loop()
            runtime = GatewayNotificationRuntime(
                config(Path(tmp)), FakeMqtt, service=object(), loop=loop
            )

            class Message:
                payload = b"{}"
                qos = 1
                mid = 43

            class Processor:
                def process(inner_self, _payload: bytes) -> bool:
                    runtime.stop_event.set()
                    return True

            try:
                runtime.processor = Processor()  # type: ignore[assignment]
                runtime.work_queue.put(Message())
                runtime._worker_loop()
                self.assertEqual(runtime.client.acks, [(43, 1)])
            finally:
                runtime.ledger.close()
                loop.close()


if __name__ == "__main__":
    unittest.main()
