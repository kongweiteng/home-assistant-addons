from __future__ import annotations

from datetime import datetime, timedelta
import hashlib
from pathlib import Path
import tempfile
import unittest
from zoneinfo import ZoneInfo

from codex_controller.prepare_car import (
    HomeAssistantPrepareCarError,
    PREPARE_CAR_ENTITY_ID,
    PREPARE_CAR_TOOL_NAMES,
    prepare_car_intent,
)
from codex_controller.service import ControllerService
from codex_controller.store import ControllerStore
from codex_controller.tool_proxy import ToolProxyError, ToolRouter


SHANGHAI = ZoneInfo("Asia/Shanghai")


def conversation_key(label: str) -> str:
    return "sha256:" + hashlib.sha256(label.encode("utf-8")).hexdigest()


def job_payload(message_id: str, text: str, *, profile: str = "owner", conversation: str | None = None) -> dict:
    return {
        "version": 1,
        "message_id": message_id,
        "conversation_key": conversation or conversation_key("weixin:prepare-car-owner"),
        "received_at": "2026-08-24T12:00:00+08:00",
        "text": text,
        "attachments": [],
        "reply_capabilities": ["text"],
        "capability_profile": profile,
    }


class PrepareCarIntentTests(unittest.TestCase):
    def test_only_bounded_attachment_free_commands_are_recognized(self) -> None:
        self.assertEqual(prepare_car_intent(job_payload("m1", "请帮我开始备车")), ("request", True))
        self.assertEqual(prepare_car_intent(job_payload("m2", "确认停止备车。")), ("execute", False))
        self.assertEqual(prepare_car_intent(job_payload("m3", "查看备车状态")), ("status", None))
        self.assertIsNone(prepare_car_intent(job_payload("m4", "把温度设为 24 度再备车")))
        with_attachment = job_payload("m5", "备车")
        with_attachment["attachments"] = [{"attachment_ref": "fixture"}]
        self.assertIsNone(prepare_car_intent(with_attachment))


class PrepareCarStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.store = ControllerStore(Path(self.temporary.name) / "controller.sqlite3")
        self.conversation = conversation_key("weixin:store-owner")
        self.store.create_job(job_payload("bootstrap-job", "普通消息", conversation=self.conversation))

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_request_is_persistent_idempotent_and_next_message_cancels(self) -> None:
        now = datetime(2026, 8, 24, 12, 0, tzinfo=SHANGHAI)
        created = self.store.prepare_car_request(
            self.conversation,
            "request-message-1",
            True,
            ttl_seconds=120,
            now=now,
        )
        replay = self.store.prepare_car_request(
            self.conversation,
            "request-message-1",
            True,
            ttl_seconds=120,
            now=now + timedelta(seconds=1),
        )
        self.assertEqual(created["confirmation_id"], replay["confirmation_id"])
        self.assertTrue(replay["idempotent_replay"])
        self.assertTrue(
            self.store.cancel_prepare_car_pending(
                self.conversation,
                "next-unrelated-message",
                now=now + timedelta(seconds=2),
            )
        )
        with self.assertRaisesRegex(Exception, "没有待确认"):
            self.store.claim_prepare_car_execute(
                self.conversation,
                "execute-after-cancel",
                True,
                now=now + timedelta(seconds=3),
            )

    def test_expiry_mismatch_and_single_consumption_fail_closed(self) -> None:
        now = datetime(2026, 8, 24, 12, 0, tzinfo=SHANGHAI)
        self.store.prepare_car_request(
            self.conversation,
            "request-expiry",
            True,
            ttl_seconds=30,
            now=now,
        )
        with self.assertRaisesRegex(Exception, "已过期"):
            self.store.claim_prepare_car_execute(
                self.conversation,
                "confirm-expired",
                True,
                now=now + timedelta(seconds=31),
            )
        self.store.prepare_car_request(
            self.conversation,
            "request-mismatch",
            True,
            ttl_seconds=120,
            now=now + timedelta(minutes=1),
        )
        with self.assertRaisesRegex(Exception, "动作不一致"):
            self.store.claim_prepare_car_execute(
                self.conversation,
                "confirm-mismatch",
                False,
                now=now + timedelta(minutes=1, seconds=1),
            )
        self.store.prepare_car_request(
            self.conversation,
            "request-success",
            False,
            ttl_seconds=120,
            now=now + timedelta(minutes=2),
        )
        claim = self.store.claim_prepare_car_execute(
            self.conversation,
            "confirm-success",
            False,
            now=now + timedelta(minutes=2, seconds=1),
        )
        result = self.store.finish_prepare_car_execute(
            claim["confirmation_id"],
            {"status": "submitted", "target": False},
            now=now + timedelta(minutes=2, seconds=2),
        )
        replay = self.store.claim_prepare_car_execute(
            self.conversation,
            "confirm-success",
            False,
            now=now + timedelta(minutes=2, seconds=3),
        )
        self.assertEqual(result["status"], "submitted")
        self.assertTrue(replay["idempotent_replay"])


class PrepareCarRouterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.store = ControllerStore(Path(self.temporary.name) / "controller.sqlite3")
        self.conversation = conversation_key("weixin:router-owner")
        self.store.create_job(job_payload("router-bootstrap", "普通消息", conversation=self.conversation))
        self.calls: list[tuple[str, str, dict | None]] = []

        def request(method: str, path: str, _token: str, payload: dict | None):
            self.calls.append((method, path, payload))
            if method == "GET":
                return {
                    "entity_id": PREPARE_CAR_ENTITY_ID,
                    "state": "off",
                    "attributes": {
                        "command_state": "idle",
                        "command_error_code": None,
                        "vin": "must-not-leak",
                    },
                }
            return []

        self.router = ToolRouter(
            home_assistant_token="h" * 32,
            request_home_assistant=request,
            store=self.store,
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def begin(self, job_id: str, message_id: str, profile: str = "owner") -> None:
        self.router.begin_job(
            job_id,
            message_id,
            profile,
            conversation_key=self.conversation,
        )

    def test_tools_are_owner_only_and_use_fixed_ha_routes(self) -> None:
        self.assertTrue(PREPARE_CAR_TOOL_NAMES.issubset(self.router.available_tools("owner")))
        self.assertTrue(PREPARE_CAR_TOOL_NAMES.isdisjoint(self.router.available_tools("owner_legacy")))
        self.assertTrue(PREPARE_CAR_TOOL_NAMES.isdisjoint(self.router.available_tools("member_read_only")))
        self.begin("request-job", "request-message")
        requested = self.router.call("aito_prepare_car_request", {"target": True})
        self.assertEqual(requested["status"], "pending_confirmation")
        self.assertEqual(self.calls, [])
        self.router.clear_job("request-job")

        self.begin("execute-job", "execute-message")
        executed = self.router.call("aito_prepare_car_execute", {"target": True})
        self.assertEqual(executed["status"], "submitted")
        self.assertEqual(
            self.calls,
            [
                ("GET", f"/states/{PREPARE_CAR_ENTITY_ID}", None),
                ("POST", "/services/switch/turn_on", {"entity_id": PREPARE_CAR_ENTITY_ID}),
            ],
        )
        self.router.clear_job("execute-job")
        self.begin("replay-job", "execute-message")
        replay = self.router.call("aito_prepare_car_execute", {"target": True})
        self.assertTrue(replay["idempotent_replay"])
        self.assertEqual(len(self.calls), 2)

    def test_unknown_post_outcome_is_persisted_and_never_retried(self) -> None:
        calls: list[tuple[str, str]] = []

        def request(method: str, path: str, _token: str, _payload: dict | None):
            calls.append((method, path))
            if method == "GET":
                return {
                    "entity_id": PREPARE_CAR_ENTITY_ID,
                    "state": "off",
                    "attributes": {"command_state": "idle"},
                }
            raise HomeAssistantPrepareCarError("HA_API_UNAVAILABLE", outcome_unknown=True)

        router = ToolRouter(
            home_assistant_token="h" * 32,
            request_home_assistant=request,
            store=self.store,
        )
        router.begin_job("request-unknown", "request-unknown", "owner", conversation_key=self.conversation)
        router.call("aito_prepare_car_request", {"target": True})
        router.clear_job("request-unknown")
        router.begin_job("execute-unknown", "execute-unknown", "owner", conversation_key=self.conversation)
        result = router.call("aito_prepare_car_execute", {"target": True})
        self.assertEqual(result["status"], "unknown")
        router.clear_job("execute-unknown")
        router.begin_job("replay-unknown", "execute-unknown", "owner", conversation_key=self.conversation)
        replay = router.call("aito_prepare_car_execute", {"target": True})
        self.assertEqual(replay["status"], "unknown")
        self.assertTrue(replay["idempotent_replay"])
        self.assertEqual(len(calls), 2)

    def test_member_call_is_rejected_before_ha(self) -> None:
        self.begin("member-job", "member-message", "member_read_only")
        with self.assertRaises(ToolProxyError) as context:
            self.router.call("aito_prepare_car_status", {})
        self.assertEqual(context.exception.code, "tool_not_allowed_for_profile")
        self.assertEqual(self.calls, [])


class PrepareCarControllerServiceTests(unittest.TestCase):
    class NoModelApp:
        supports_dynamic_tool_definitions = True

        def __init__(self) -> None:
            self.notification_handler = None
            self.started_turns = 0

        def configure_developer_context(self, *_args, **_kwargs) -> None:
            return None

        def start_thread(self) -> str:
            return "thread-fixture"

        def resume_thread(self, thread_id: str) -> str:
            return thread_id

        def start_turn(self, *_args, **_kwargs) -> str:
            self.started_turns += 1
            return f"turn-{self.started_turns}"

    def test_exact_weixin_request_and_confirmation_bypass_model(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = ControllerStore(Path(temporary) / "controller.sqlite3")
            calls: list[tuple[str, str]] = []

            def request(method: str, path: str, _token: str, _payload: dict | None):
                calls.append((method, path))
                if method == "GET":
                    return {
                        "entity_id": PREPARE_CAR_ENTITY_ID,
                        "state": "off",
                        "attributes": {"command_state": "idle"},
                    }
                return []

            router = ToolRouter(
                home_assistant_token="h" * 32,
                request_home_assistant=request,
                store=store,
            )
            app = self.NoModelApp()
            service = ControllerService(store, app, intake_enabled=True, tool_context=router)
            conversation = conversation_key("weixin:service-owner")
            request_job = store.create_job(job_payload("service-request", "备车", conversation=conversation))
            service._dispatch(store.claim_next())
            requested = store.get_job(request_job["job_id"])
            self.assertEqual(requested["state"], "completed")
            self.assertIn("确认备车", requested["result"])
            confirm_job = store.create_job(job_payload("service-confirm", "确认备车", conversation=conversation))
            service._dispatch(store.claim_next())
            confirmed = store.get_job(confirm_job["job_id"])
            self.assertEqual(confirmed["state"], "completed")
            self.assertIn("不代表车辆已完成", confirmed["result"])
            self.assertEqual(app.started_turns, 0)
            self.assertEqual([method for method, _path in calls], ["GET", "POST"])

    def test_member_is_denied_and_other_next_message_cancels_pending(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = ControllerStore(Path(temporary) / "controller.sqlite3")
            router = ToolRouter(
                home_assistant_token="h" * 32,
                request_home_assistant=lambda *_args: (_ for _ in ()).throw(AssertionError("HA must not be called")),
                store=store,
            )
            app = self.NoModelApp()
            service = ControllerService(store, app, intake_enabled=True, tool_context=router)
            member = job_payload(
                "member-request",
                "备车",
                profile="member_read_only",
                conversation=conversation_key("weixin:member"),
            )
            member_job = store.create_job(member)
            service._dispatch(store.claim_next())
            self.assertIn("没有备车控制权限", store.get_job(member_job["job_id"])["result"])

            owner_conversation = conversation_key("weixin:cancel-owner")
            store.create_job(job_payload("cancel-request", "备车", conversation=owner_conversation))
            service._dispatch(store.claim_next())
            normal_job = store.create_job(job_payload("cancel-next", "今天天气怎么样", conversation=owner_conversation))
            service._dispatch(store.claim_next())
            with self.assertRaisesRegex(Exception, "没有待确认"):
                store.claim_prepare_car_execute(
                    owner_conversation,
                    "late-confirmation",
                    True,
                    now=datetime(2026, 8, 24, 12, 0, 10, tzinfo=SHANGHAI),
                )
            service.handle_notification(
                {"method": "turn/completed", "params": {"turn": {"id": "turn-1", "status": "completed"}}}
            )
            self.assertEqual(store.get_job(normal_job["job_id"])["state"], "completed")


if __name__ == "__main__":
    unittest.main()
