from __future__ import annotations

import asyncio
import datetime as dt
import json
from pathlib import Path
import tempfile
import unittest

from weixin_gateway.remote_work import (
    AGENT_TOPIC,
    CONTROL_TOPIC,
    REQUEST_TOPIC,
    RESULT_TOPIC,
    STATUS_TOPIC,
    GatewayRemoteWorkRuntime,
    RemoteWorkConfig,
    RemoteWorkValidationError,
    WorkCommandError,
    build_command_document,
    parse_work_command,
    validate_incoming_document,
    validate_outgoing_document,
)
from weixin_gateway.service import GatewayService
from weixin_gateway.store import GatewayStore, IdentityStore, StoreError


def identity() -> dict:
    return {
        "account_id": "fixture-account",
        "token": "fixture-ilink-token-0000000000000000",
        "base_url": "https://ilinkai.weixin.qq.com",
        "cdn_base_url": "https://novac2c.cdn.weixin.qq.com/c2c",
        "user_id": "fixture-bot",
        "allowed_user_ids": ["fixture-owner"],
        "get_updates_buf": "",
        "context_tokens": {},
    }


def raw_message(message_id: str, sender: str, text: str, *, with_media: bool = False) -> dict:
    items = [{"type": 1, "text_item": {"text": text}}]
    if with_media:
        items.append(
            {
                "type": 2,
                "image_item": {
                    "aeskey": "00000000000000000000000000000000",
                    "media": {"full_url": "https://novac2c.cdn.weixin.qq.com/c2c/fixture-image"},
                },
            }
        )
    return {
        "message_id": message_id,
        "from_user_id": sender,
        "to_user_id": "fixture-account",
        "context_token": f"context-{sender}",
        "item_list": items,
    }


def status_payload(task_id: str, *, run_seq: int = 1, sequence: int = 1, state: str = "queued") -> dict:
    return {
        "version": 1,
        "task_id": task_id,
        "run_seq": run_seq,
        "sequence": sequence,
        "state": state,
        "stage": "queued" if state == "queued" else "codex",
        "updated_at": "2026-08-05T10:01:00+08:00",
    }


def result_payload(task_id: str, *, run_seq: int = 1, sequence: int = 2, state: str = "completed") -> dict:
    payload = {
        "version": 1,
        "task_id": task_id,
        "run_seq": run_seq,
        "sequence": sequence,
        "state": state,
        "finished_at": "2026-08-05T10:02:00+08:00",
        "summary": "已完成本地代码、测试与选择性提交。",
        "branch": f"codex/weixin-{task_id}",
        "commits": ["0123456789abcdef"],
        "test_summary": "12 tests passed",
        "changed_path_count": 4,
        "next_actions": ["P8 仍需独立生产确认"],
        "result_hash": "sha256:" + "a" * 64,
    }
    return payload


class CommandContractTests(unittest.TestCase):
    def test_exact_parser_keeps_near_matches_as_ordinary_chat(self) -> None:
        self.assertIsNone(parse_work_command("请问能不能修改页面"))
        self.assertIsNone(parse_work_command("/workx renovation-hub 修改页面"))
        command = parse_work_command("/work renovation-hub 增加合同编号并补测试")
        assert command is not None
        self.assertEqual(command.operation, "start")
        self.assertEqual(command.project_alias, "renovation-hub")
        self.assertEqual(command.instruction, "增加合同编号并补测试")
        self.assertEqual(parse_work_command("/work status RW-ABCDEFGHIJ").operation, "status")  # type: ignore[union-attr]
        self.assertEqual(parse_work_command("/work cancel RW-ABCDEFGHIJ").operation, "cancel")  # type: ignore[union-attr]
        self.assertEqual(
            parse_work_command("/work continue RW-ABCDEFGHIJ 补充回归测试").instruction,  # type: ignore[union-attr]
            "补充回归测试",
        )

    def test_invalid_exact_commands_fail_closed(self) -> None:
        cases = {
            "/work": "work_command_invalid",
            "/work unknown 修改": "work_project_unknown",
            "/work deploy production": "production_confirmation_required",
            "/work continue RW-short 补充": "work_command_invalid",
        }
        for text, code in cases.items():
            with self.subTest(text=text), self.assertRaises(WorkCommandError) as context:
                parse_work_command(text)
            self.assertEqual(context.exception.code, code)

    def test_request_and_control_match_mac_agent_contract(self) -> None:
        now = dt.datetime(2026, 8, 5, 10, 0, tzinfo=dt.timezone(dt.timedelta(hours=8)))
        start = parse_work_command("/work renovation-hub 增加字段")
        assert start is not None
        topic, request = build_command_document(
            start,
            message_id="RM-ABCDEFGHIJ",
            task_id="RW-ABCDEFGHIJ",
            principal_hash="1" * 64,
            now=now,
            ttl_seconds=1800,
        )
        self.assertEqual(topic, REQUEST_TOPIC)
        self.assertEqual(validate_outgoing_document(topic, request), request)
        self.assertEqual(set(request), {
            "version", "message_id", "task_id", "created_at", "expires_at", "project_alias",
            "operation", "instruction", "source", "authority",
        })
        self.assertFalse({"path", "shell", "model", "sandbox", "git_ref", "remote", "reply_topic"} & set(request))

        continuation = parse_work_command("/work continue RW-ABCDEFGHIJ 补充测试")
        assert continuation is not None
        topic, control = build_command_document(
            continuation,
            message_id="RM-BCDEFGHIJK",
            task_id="RW-ABCDEFGHIJ",
            principal_hash="1" * 64,
            now=now,
            ttl_seconds=1800,
        )
        self.assertEqual(topic, CONTROL_TOPIC)
        self.assertEqual(control["action"], "continue")
        self.assertIn("instruction", control)
        self.assertEqual(validate_outgoing_document(topic, control), control)

        cancellation = parse_work_command("/work cancel RW-ABCDEFGHIJ")
        assert cancellation is not None
        _topic, cancel = build_command_document(
            cancellation,
            message_id="RM-CDEFGHIJKL",
            task_id="RW-ABCDEFGHIJ",
            principal_hash="1" * 64,
            now=now,
            ttl_seconds=1800,
        )
        self.assertNotIn("instruction", cancel)

    def test_status_result_agent_validation_and_privacy(self) -> None:
        task_id = "RW-ABCDEFGHIJ"
        self.assertEqual(validate_incoming_document(STATUS_TOPIC, status_payload(task_id))["state"], "queued")
        self.assertEqual(validate_incoming_document(RESULT_TOPIC, result_payload(task_id))["state"], "completed")
        agent = {
            "version": 1,
            "online": True,
            "protocol_version": 1,
            "agent_version": "0.1.0",
            "codex_version": "0.146.0",
            "capabilities": ["start", "continue", "cancel"],
            "queue_depth": 0,
            "active_task_id": None,
            "updated_at": "2026-08-05T10:00:00+08:00",
        }
        self.assertTrue(validate_incoming_document(AGENT_TOPIC, agent)["online"])
        leaked = result_payload(task_id)
        leaked["diff"] = "secret source"
        with self.assertRaises(RemoteWorkValidationError) as context:
            validate_incoming_document(RESULT_TOPIC, leaked)
        self.assertEqual(context.exception.code, "privacy_payload_rejected")


class StubController:
    configured = False

    async def start(self) -> None:
        return None

    async def close(self) -> None:
        return None


class StubIlinkClient:
    def __init__(self) -> None:
        self.sent: list[dict] = []
        self.download_calls = 0

    async def send_text(self, to_user_id: str, text: str, context_token: str | None, client_id: str) -> dict:
        self.sent.append({"to": to_user_id, "text": text, "context": context_token, "client_id": client_id})
        return {"ret": 0}

    async def download_media(self, _spec: dict) -> bytes:
        self.download_calls += 1
        return b"fixture"

    async def close(self) -> None:
        return None


class StubRuntime:
    def __init__(self, store: GatewayStore) -> None:
        self.store = store
        self.publish_calls = 0

    def publish_pending(self) -> int:
        self.publish_calls += 1
        pending = self.store.remote_work_pending_outbox()
        for item in pending:
            self.store.mark_remote_work_outbox(item["message_id"], success=True)
        return len(pending)


class GatewayRoutingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        self.identity_store = IdentityStore(root / "data")
        self.identity_store.save_identity(identity())
        self.store = GatewayStore(root / "data" / "gateway.sqlite3", data_dir=root / "data")
        self.service = GatewayService(
            identity_store=self.identity_store,
            store=self.store,
            controller=StubController(),  # type: ignore[arg-type]
            bootstrap_identity={},
            poller_enabled=False,
            owner_pairing_enabled=False,
            activation_confirmation="",
            max_media_bytes=1024,
            remote_work_enabled=True,
            remote_work_ttl_seconds=1800,
        )
        self.client = StubIlinkClient()
        self.runtime = StubRuntime(self.store)
        self.service.client = self.client  # type: ignore[assignment]
        self.service.remote_work_runtime = self.runtime  # type: ignore[assignment]

    def tearDown(self) -> None:
        self.temporary.cleanup()

    @staticmethod
    def run_async(coroutine: object) -> object:
        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(coroutine)  # type: ignore[arg-type]
        finally:
            loop.close()

    def test_exact_owner_work_bypasses_controller_and_is_idempotent(self) -> None:
        raw = raw_message("remote-start-0001", "fixture-owner", "/work renovation-hub 增加合同编号")
        self.run_async(self.service._ingest(raw))
        self.run_async(self.service._ingest(raw))
        task_id = self.store.short_id("RW", "remote-start-0001")
        task = self.store.remote_work_task(task_id)
        self.assertEqual(task["state"], "waiting_mac")
        self.assertFalse(self.store.message_exists("remote-start-0001"))
        self.assertEqual(self.runtime.publish_calls, 1)
        self.assertEqual(len(self.client.sent), 1)
        self.assertIn(task_id, self.client.sent[0]["text"])

    def test_near_match_remains_controller_message(self) -> None:
        raw = raw_message("remote-near-0001", "fixture-owner", "/workx renovation-hub 修改页面")
        self.run_async(self.service._ingest(raw))
        self.assertTrue(self.store.message_exists("remote-near-0001"))
        self.assertEqual(self.runtime.publish_calls, 0)

    def test_attachment_is_rejected_without_download_or_controller_submission(self) -> None:
        raw = raw_message(
            "remote-media-0001",
            "fixture-owner",
            "/work renovation-hub 修改图片页面",
            with_media=True,
        )
        self.run_async(self.service._ingest(raw))
        self.assertEqual(self.client.download_calls, 0)
        self.assertFalse(self.store.message_exists("remote-media-0001"))
        self.assertEqual(self.runtime.publish_calls, 0)
        self.assertIn("不能携带附件", self.client.sent[0]["text"])

    def test_member_work_is_denied_and_role_change_suppresses_result(self) -> None:
        invitation = self.store.create_member_invitation(
            expected_revision=self.store.users_revision(),
            request_id="remote-member-invite-0001",
        )
        member = self.store.claim_member_invitation(user_id="fixture-member", text=invitation["code"])
        assert member is not None
        self.run_async(
            self.service._ingest(
                raw_message("remote-member-0001", "fixture-member", "/work renovation-hub 修改页面")
            )
        )
        self.assertEqual(self.runtime.publish_calls, 0)
        self.assertIn("没有 /work 权限", self.client.sent[-1]["text"])

        self.run_async(
            self.service._ingest(
                raw_message("remote-owner-0002", "fixture-owner", "/work renovation-hub 修改页面")
            )
        )
        task_id = self.store.short_id("RW", "remote-owner-0002")
        self.store.record_remote_work_event(STATUS_TOPIC, status_payload(task_id))
        self.store.record_remote_work_event(RESULT_TOPIC, result_payload(task_id))
        current_revision = self.store.users_revision()
        self.run_async(
            self.service.transfer_owner(
                {
                    "target_wx_short": self.store.short_id("WX", member["user_hash"]),
                    "confirmation": "TRANSFER_OWNER",
                    "revision": current_revision,
                    "request_id": "remote-owner-transfer-0001",
                }
            )
        )
        before = len(self.client.sent)
        self.run_async(self.service._deliver_remote_work_replies())
        self.assertEqual(len(self.client.sent), before)
        with self.store._connect() as connection:
            reply = connection.execute(
                "SELECT reply_state,reply_error FROM remote_work_events WHERE topic=?",
                (RESULT_TOPIC,),
            ).fetchone()
        self.assertEqual(reply["reply_state"], "suppressed")
        self.assertEqual(reply["reply_error"], "reply_suppressed_owner_changed")

    def test_status_and_sequence_conflicts_are_deterministic(self) -> None:
        self.run_async(
            self.service._ingest(raw_message("remote-seq-0001", "fixture-owner", "/work renovation-hub 修改页面"))
        )
        task_id = self.store.short_id("RW", "remote-seq-0001")
        first = status_payload(task_id, sequence=2, state="running")
        first["stage"] = "codex"
        self.assertEqual(self.store.record_remote_work_event(STATUS_TOPIC, first)["outcome"], "recorded")
        self.assertEqual(
            self.store.record_remote_work_event(STATUS_TOPIC, status_payload(task_id, sequence=1))["outcome"],
            "stale",
        )
        conflict = dict(first)
        conflict["stage"] = "verify"
        with self.assertRaises(StoreError) as context:
            self.store.record_remote_work_event(STATUS_TOPIC, conflict)
        self.assertEqual(context.exception.code, "remote_work_event_conflict")


class FakePublishInfo:
    def __init__(self) -> None:
        self.published = True

    def wait_for_publish(self, timeout: float) -> None:
        return None

    def is_published(self) -> bool:
        return self.published


class FakeClient:
    def __init__(self, **kwargs: object) -> None:
        self.kwargs = kwargs
        self.subscriptions: list[tuple[str, int]] = []
        self.published: list[tuple[str, int, bool]] = []
        self.acks: list[tuple[int, int]] = []

    def username_pw_set(self, username: str, password: str) -> None:
        self.credentials = (username, password)

    def reconnect_delay_set(self, min_delay: int, max_delay: int) -> None:
        return None

    def tls_set(self) -> None:
        return None

    def subscribe(self, topics: list[tuple[str, int]]) -> None:
        self.subscriptions.extend(topics)

    def publish(self, topic: str, *, payload: str, qos: int, retain: bool) -> FakePublishInfo:
        self.published.append((topic, qos, retain))
        return FakePublishInfo()

    def ack(self, mid: int, qos: int) -> None:
        self.acks.append((mid, qos))

    def connect(self, *args: object, **kwargs: object) -> None:
        return None

    def loop_forever(self, **kwargs: object) -> None:
        return None

    def disconnect(self) -> None:
        return None


class FakeProperties:
    def __init__(self, _packet_type: object) -> None:
        self.SessionExpiryInterval = 0


class FakeMqtt:
    class PacketTypes:
        CONNECT = object()

    class CallbackAPIVersion:
        VERSION2 = object()

    MQTTv5 = object()
    Properties = FakeProperties
    Client = FakeClient


class RuntimeContractTests(unittest.TestCase):
    def test_runtime_uses_v5_manual_ack_qos1_non_retained_and_exact_acl_topics(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            identity_store = IdentityStore(root / "data")
            identity_store.save_identity(identity())
            store = GatewayStore(root / "data" / "gateway.sqlite3", data_dir=root / "data")
            runtime = GatewayRemoteWorkRuntime(
                RemoteWorkConfig(
                    mqtt_host="broker.example",
                    mqtt_port=1883,
                    mqtt_username="remote-work-gateway",
                    mqtt_password="fixture-password",
                    mqtt_tls=False,
                    ttl_seconds=1800,
                    addon_version="0.2.1",
                ),
                FakeMqtt,
                store=store,
            )
            self.assertTrue(runtime.client.kwargs["manual_ack"])
            runtime._on_connect(runtime.client, None, None, type("Reason", (), {"is_failure": False})(), None)
            self.assertEqual(
                runtime.client.subscriptions,
                [(STATUS_TOPIC, 1), (RESULT_TOPIC, 1), (AGENT_TOPIC, 1)],
            )
            self.assertEqual(runtime.connect_properties.SessionExpiryInterval, 86400)


if __name__ == "__main__":
    unittest.main()
