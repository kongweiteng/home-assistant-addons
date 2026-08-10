from __future__ import annotations

import asyncio
from pathlib import Path
import tempfile
import unittest

from weixin_gateway.remote_work import parse_work_command as parse_v1_work_command
from weixin_gateway.remote_work_v2 import (
    ROUTE_ORDINARY,
    ROUTE_REJECT,
    ROUTE_V1,
    ROUTE_V2,
    RUNNER_MANAGER_COMMAND_PATH,
    RunnerManagerContractError,
    RunnerManagerResponseError,
    WorkCommandError,
    build_request_id,
    build_runner_manager_request,
    parse_runner_manager_response,
    parse_work_command,
    select_work_route,
    validate_runner_manager_request,
)
from weixin_gateway.service import GatewayService
from weixin_gateway.store import GatewayStore, IdentityStore, StoreError
from tests.test_weixin_gateway_remote_work import StubIlinkClient, identity, raw_message


class ParserTests(unittest.TestCase):
    def test_exact_start_status_continue_and_cancel(self) -> None:
        start = parse_work_command("/work renovation-hub 增加 Runner 列表测试")
        assert start is not None
        self.assertEqual(start.operation, "start")
        self.assertEqual(start.project_alias, "renovation-hub")
        self.assertEqual(start.instruction, "增加 Runner 列表测试")

        status = parse_work_command("/work status RW-ABCDEFGHIJ")
        continuation = parse_work_command("/work continue RW-ABCDEFGHIJ 补充断线回归")
        cancellation = parse_work_command("/work cancel RW-ABCDEFGHIJ")
        assert status is not None and continuation is not None and cancellation is not None
        self.assertEqual(status.operation, "status")
        self.assertEqual(continuation.instruction, "补充断线回归")
        self.assertEqual(cancellation.operation, "cancel")

    def test_ordinary_and_approximate_messages_are_not_classified(self) -> None:
        messages = (
            "普通聊天消息",
            "请帮我看看 /work 怎么使用",
            "/workx renovation-hub 修改页面",
            "/Work renovation-hub 修改页面",
            "/work: renovation-hub 修改页面",
            "",
        )
        for message in messages:
            with self.subTest(message=message):
                self.assertIsNone(parse_work_command(message))
                self.assertEqual(
                    select_work_route(message, role="owner", v2_enabled=True).route,
                    ROUTE_ORDINARY,
                )

    def test_invalid_exact_commands_fail_closed(self) -> None:
        cases = {
            "/work": "work_command_invalid",
            "/work unknown 修改页面": "work_project_unknown",
            "/work renovation-hub": "work_instruction_required",
            "/work status RW2-short": "work_task_id_invalid",
            "/work continue RW2-short 补充": "work_command_invalid",
            "/work cancel": "work_task_id_invalid",
            "/work deploy production": "production_confirmation_required",
        }
        for message, code in cases.items():
            with self.subTest(message=message), self.assertRaises(WorkCommandError) as context:
                parse_work_command(message)
            self.assertEqual(context.exception.code, code)

    def test_unsafe_execution_selectors_are_rejected(self) -> None:
        cases = {
            "/work renovation-hub --path /srv/repo 修改页面": "work_path_forbidden",
            "/work renovation-hub cwd=/srv/repo 修改页面": "work_path_forbidden",
            "/work renovation-hub --shell bash -c 'id'": "work_shell_forbidden",
            "/work renovation-hub command=make deploy": "work_shell_forbidden",
            "/work renovation-hub --model gpt-next 修改": "work_model_forbidden",
            "/work renovation-hub sandbox=danger-full-access 修改": "work_sandbox_forbidden",
            "/work renovation-hub --git-ref refs/heads/main 修改": "work_git_ref_forbidden",
            "/work renovation-hub branch=main 修改": "work_git_ref_forbidden",
            "/work renovation-hub --remote origin 修改": "work_remote_forbidden",
            "/work renovation-hub repo=https://example.invalid/repo.git 修改": "work_remote_forbidden",
            "/work renovation-hub --deploy production": "production_confirmation_required",
        }
        for message, code in cases.items():
            with self.subTest(message=message), self.assertRaises(WorkCommandError) as context:
                parse_work_command(message)
            self.assertEqual(context.exception.code, code)


class RouteSelectionTests(unittest.TestCase):
    def test_v2_is_default_off_and_exact_work_returns_to_v1_unchanged(self) -> None:
        messages = (
            "/work renovation-hub 修改页面",
            "/work",
            "/work deploy production",
            "/work unknown 修改",
        )
        for message in messages:
            with self.subTest(message=message):
                decision = select_work_route(message, role="member")
                self.assertEqual(decision.route, ROUTE_V1)
                self.assertEqual(decision.dispatch_targets, (ROUTE_V1,))
                self.assertFalse(decision.runtime_fallback_allowed)

        v1_command = parse_v1_work_command("/work renovation-hub 修改页面")
        assert v1_command is not None
        self.assertEqual(v1_command.operation, "start")

    def test_v2_selection_is_exclusive_and_never_falls_back_at_runtime(self) -> None:
        decision = select_work_route(
            "/work renovation-hub 修改页面",
            role="owner",
            v2_enabled=True,
        )
        self.assertEqual(decision.route, ROUTE_V2)
        self.assertEqual(decision.dispatch_targets, (ROUTE_V2,))
        self.assertFalse(decision.runtime_fallback_allowed)
        self.assertNotIn(ROUTE_V1, decision.dispatch_targets)

    def test_member_attachment_and_invalid_v2_commands_are_rejected(self) -> None:
        member = select_work_route(
            "/work renovation-hub 修改页面",
            role="member",
            v2_enabled=True,
        )
        attachment = select_work_route(
            "/work renovation-hub 修改页面",
            role="owner",
            has_attachments=True,
            v2_enabled=True,
        )
        unknown = select_work_route(
            "/work unknown 修改页面",
            role="owner",
            v2_enabled=True,
        )
        for decision in (member, attachment, unknown):
            self.assertEqual(decision.route, ROUTE_REJECT)
            self.assertEqual(decision.dispatch_targets, ())
            self.assertFalse(decision.runtime_fallback_allowed)
        self.assertEqual(member.error_code, "work_owner_required")
        self.assertEqual(attachment.error_code, "work_attachments_unsupported")
        self.assertEqual(unknown.error_code, "work_project_unknown")

    def test_no_available_remote_work_route_does_not_capture_ordinary_chat(self) -> None:
        ordinary = select_work_route(
            "帮我查看装修进度",
            role="owner",
            v2_enabled=False,
            v1_available=False,
        )
        exact = select_work_route(
            "/work renovation-hub 修改页面",
            role="owner",
            v2_enabled=False,
            v1_available=False,
        )
        self.assertEqual(ordinary.route, ROUTE_ORDINARY)
        self.assertEqual(exact.route, ROUTE_REJECT)
        self.assertEqual(exact.error_code, "remote_work_disabled")


class RequestContractTests(unittest.TestCase):
    def test_request_id_is_stable_identity_scoped_and_body_independent(self) -> None:
        first = build_request_id(identity_id="identity-owner", message_id="message-0001")
        repeated = build_request_id(identity_id="identity-owner", message_id="message-0001")
        other_identity = build_request_id(identity_id="identity-other", message_id="message-0001")
        other_message = build_request_id(identity_id="identity-owner", message_id="message-0002")
        self.assertEqual(first, repeated)
        self.assertNotEqual(first, other_identity)
        self.assertNotEqual(first, other_message)

        first_command = parse_work_command("/work renovation-hub 修改页面")
        changed_command = parse_work_command("/work renovation-hub 修改另一页面")
        assert first_command is not None and changed_command is not None
        first_request = build_runner_manager_request(
            first_command,
            identity_id="identity-owner",
            message_id="message-0001",
            principal_hash="1" * 64,
        )
        changed_request = build_runner_manager_request(
            changed_command,
            identity_id="identity-owner",
            message_id="message-0001",
            principal_hash="1" * 64,
        )
        self.assertEqual(first_request.request_id, changed_request.request_id)
        self.assertNotEqual(first_request.body_digest, changed_request.body_digest)

    def test_start_request_is_bounded_and_contains_no_execution_selectors(self) -> None:
        command = parse_work_command("/work renovation-hub 增加状态测试")
        assert command is not None
        request = build_runner_manager_request(
            command,
            identity_id="identity-owner",
            message_id="message-0001",
            principal_hash="2" * 64,
        )
        self.assertEqual(request.method, "POST")
        self.assertEqual(request.path, RUNNER_MANAGER_COMMAND_PATH)
        self.assertEqual(validate_runner_manager_request(request.body), request.body)
        self.assertEqual(request.body["source"]["principal_hash"], "sha256:" + "2" * 64)
        self.assertEqual(
            set(request.body),
            {"version", "request_id", "operation", "source", "project_alias", "instruction"},
        )
        self.assertFalse(
            {
                "path",
                "cwd",
                "shell",
                "command",
                "model",
                "sandbox",
                "git_ref",
                "branch",
                "remote",
                "repo",
                "reply_topic",
            }
            & set(request.body)
        )

    def test_status_continue_and_cancel_have_exact_shapes(self) -> None:
        cases = (
            ("/work status RW-ABCDEFGHIJ", {"version", "request_id", "operation", "source", "task_id"}),
            (
                "/work continue RW-ABCDEFGHIJ 补充测试",
                {"version", "request_id", "operation", "source", "task_id", "instruction"},
            ),
            ("/work cancel RW-ABCDEFGHIJ", {"version", "request_id", "operation", "source", "task_id"}),
        )
        for index, (message, expected_fields) in enumerate(cases):
            with self.subTest(message=message):
                command = parse_work_command(message)
                assert command is not None
                request = build_runner_manager_request(
                    command,
                    identity_id="identity-owner",
                    message_id=f"message-000{index + 2}",
                    principal_hash="3" * 64,
                )
                self.assertEqual(set(request.body), expected_fields)
                self.assertEqual(validate_runner_manager_request(request.body), request.body)

    def test_request_validator_rejects_extra_fields_and_invalid_source(self) -> None:
        command = parse_work_command("/work renovation-hub 增加测试")
        assert command is not None
        request = build_runner_manager_request(
            command,
            identity_id="identity-owner",
            message_id="message-0009",
            principal_hash="4" * 64,
        )
        leaked = dict(request.body)
        leaked["path"] = "/srv/repo"
        with self.assertRaises(RunnerManagerContractError) as context:
            validate_runner_manager_request(leaked)
        self.assertEqual(context.exception.code, "invalid_payload")

        invalid_source = dict(request.body)
        invalid_source["source"] = {"channel": "weixin", "principal_hash": "raw-user", "role": "owner"}
        with self.assertRaises(RunnerManagerContractError) as context:
            validate_runner_manager_request(invalid_source)
        self.assertEqual(context.exception.code, "source_invalid")


class ResponseContractTests(unittest.TestCase):
    @staticmethod
    def request_id() -> str:
        return build_request_id(identity_id="identity-owner", message_id="message-result-0001")

    def valid_result(self) -> dict:
        return {
            "version": 2,
            "request_id": self.request_id(),
            "operation": "status",
            "task_id": "RW-ABCDEFGHIJ",
            "state": "completed",
            "updated_at": "2026-08-10T12:00:00+08:00",
            "stage": "handoff",
            "summary": "本地候选和测试已完成。",
            "candidate_id": "sha256:" + "c" * 64,
            "test_summary": "18 tests passed",
            "changed_path_count": 2,
            "next_actions": ["等待主控完成完整矩阵"],
        }

    def test_safe_result_is_parsed_to_bounded_dto(self) -> None:
        result = parse_runner_manager_response(
            200,
            self.valid_result(),
            expected_request_id=self.request_id(),
            expected_operation="status",
        )
        self.assertEqual(result.state, "completed")
        self.assertEqual(result.changed_path_count, 2)
        self.assertEqual(result.next_actions, ("等待主控完成完整矩阵",))

    def test_result_rejects_private_or_unbounded_details(self) -> None:
        forbidden = self.valid_result()
        forbidden["diff"] = "private source"
        with self.assertRaises(RunnerManagerContractError) as context:
            parse_runner_manager_response(
                200,
                forbidden,
                expected_request_id=self.request_id(),
                expected_operation="status",
            )
        self.assertEqual(context.exception.code, "invalid_payload")

        oversized = self.valid_result()
        oversized["summary"] = "x" * 6001
        with self.assertRaises(RunnerManagerContractError) as context:
            parse_runner_manager_response(
                200,
                oversized,
                expected_request_id=self.request_id(),
                expected_operation="status",
            )
        self.assertEqual(context.exception.code, "invalid_payload")

    def test_response_must_echo_request_and_operation(self) -> None:
        mismatched_request = self.valid_result()
        mismatched_request["request_id"] = build_request_id(
            identity_id="identity-owner",
            message_id="different-message",
        )
        with self.assertRaises(RunnerManagerContractError) as context:
            parse_runner_manager_response(
                200,
                mismatched_request,
                expected_request_id=self.request_id(),
                expected_operation="status",
            )
        self.assertEqual(context.exception.code, "request_id_mismatch")

        mismatched_operation = self.valid_result()
        mismatched_operation["operation"] = "cancel"
        with self.assertRaises(RunnerManagerContractError) as context:
            parse_runner_manager_response(
                200,
                mismatched_operation,
                expected_request_id=self.request_id(),
                expected_operation="status",
            )
        self.assertEqual(context.exception.code, "operation_mismatch")

    def test_bounded_controller_error_is_raised_without_transport_fallback(self) -> None:
        payload = {
            "version": 2,
            "request_id": self.request_id(),
            "error_code": "runner_unavailable",
            "message": "当前没有符合策略的在线 Runner。",
            "retryable": True,
        }
        with self.assertRaises(RunnerManagerResponseError) as context:
            parse_runner_manager_response(
                503,
                payload,
                expected_request_id=self.request_id(),
                expected_operation="start",
            )
        self.assertEqual(context.exception.code, "runner_unavailable")
        self.assertEqual(context.exception.status_code, 503)
        self.assertTrue(context.exception.retryable)


class StubRunnerController:
    configured = True

    def __init__(self, *, fail: bool = False) -> None:
        self.requests = []
        self.fail = fail

    async def start(self) -> None:
        return None

    async def close(self) -> None:
        return None

    async def runner_manager(self, request):
        self.requests.append(request)
        if self.fail:
            raise StoreError("runner_unavailable", "当前没有符合策略的在线 Runner。", status=503)
        return parse_runner_manager_response(
            200,
            {
                "version": 2,
                "request_id": request.request_id,
                "operation": request.body["operation"],
                "task_id": "RW-ABCDEFGHIJ",
                "state": "waiting_runner",
                "updated_at": "2026-08-10T12:00:00+08:00",
                "stage": "queue",
                "next_actions": ["等待符合策略的在线 Runner"],
            },
            expected_request_id=request.request_id,
            expected_operation=request.body["operation"],
        )


class GatewayV2IntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        self.identity_store = IdentityStore(root / "data")
        self.identity_store.save_identity(identity())
        self.store = GatewayStore(root / "data" / "gateway.sqlite3", data_dir=root / "data")
        self.controller = StubRunnerController()
        self.service = GatewayService(
            identity_store=self.identity_store,
            store=self.store,
            controller=self.controller,  # type: ignore[arg-type]
            bootstrap_identity={},
            poller_enabled=False,
            owner_pairing_enabled=False,
            activation_confirmation="",
            max_media_bytes=1024,
            remote_work_enabled=True,
            runner_manager_v2_enabled=True,
            remote_work_ttl_seconds=1800,
        )
        self.client = StubIlinkClient()
        self.service.client = self.client  # type: ignore[assignment]

    def tearDown(self) -> None:
        self.temporary.cleanup()

    @staticmethod
    def run_async(coroutine):
        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(coroutine)
        finally:
            loop.close()

    def test_exact_owner_work_uses_only_v2_and_duplicate_reply_is_idempotent(self) -> None:
        raw = raw_message("v2-route-0001", "fixture-owner", "/work renovation-hub 增加 Runner 页面测试")
        self.run_async(self.service._ingest(raw))
        self.run_async(self.service._ingest(raw))
        self.assertEqual(len(self.controller.requests), 2)
        self.assertEqual(self.controller.requests[0].request_id, self.controller.requests[1].request_id)
        self.assertEqual(len(self.client.sent), 1)
        self.assertIn("RW-ABCDEFGHIJ", self.client.sent[0]["text"])
        self.assertEqual(self.store.remote_work_pending_outbox(), [])
        self.assertFalse(self.store.message_exists("v2-route-0001"))

    def test_near_match_remains_ordinary_controller_message(self) -> None:
        raw = raw_message("v2-near-0001", "fixture-owner", "/workx renovation-hub 修改页面")
        self.run_async(self.service._ingest(raw))
        self.assertTrue(self.store.message_exists("v2-near-0001"))
        self.assertEqual(self.controller.requests, [])

    def test_v2_failure_never_falls_back_to_v1_or_ordinary_chat(self) -> None:
        self.controller.fail = True
        raw = raw_message("v2-failure-0001", "fixture-owner", "/work renovation-hub 修改页面")
        self.run_async(self.service._ingest(raw))
        self.assertEqual(len(self.controller.requests), 1)
        self.assertEqual(self.store.remote_work_pending_outbox(), [])
        self.assertFalse(self.store.message_exists("v2-failure-0001"))
        self.assertIn("没有符合策略", self.client.sent[0]["text"])


if __name__ == "__main__":
    unittest.main()
