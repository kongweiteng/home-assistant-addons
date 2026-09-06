from __future__ import annotations

import datetime as dt
from pathlib import Path
import sqlite3
import tempfile
import unittest

from codex_controller.desktop_dashboard import DESKTOP_DASHBOARD_HTML, DESKTOP_DASHBOARD_JS
from codex_controller.desktop_api import post_desktop_api
from codex_controller.desktop_protocol import (
    DesktopProtocolError,
    body_digest,
    build_desktop_command,
    validate_desktop_command,
    validate_desktop_document,
)
from codex_controller.desktop_service import DesktopControllerService
from codex_controller.desktop_store import DesktopStore
from codex_controller.store import StoreError


NOW = dt.datetime(2026, 9, 6, 12, 0, tzinfo=dt.timezone(dt.timedelta(hours=8)))
RUNNER_ID = "RN-" + "E" * 20
HOST_REF = "HS-" + "A" * 20
PROJECT_REF = "PJ-" + "B" * 20
THREAD_REF = "TH-" + "C" * 20
TURN_REF = "TR-" + "D" * 20
REQUEST_REF = "RQ-" + "F" * 20
QUESTION_REF = "QU-" + "G" * 20


class Publisher:
    def __init__(self) -> None:
        self.commands: list[dict] = []
        self.error: Exception | None = None

    def publish_desktop_command(self, _runner_id: str, document: dict) -> None:
        self.commands.append(dict(document))
        if self.error is not None:
            raise self.error


def snapshot(
    *,
    thread_ref: str = THREAD_REF,
    title: str = "任务名称",
    preview: str = "同步摘要",
    pinned: bool = False,
    sequence: int = 1,
    capabilities: list[str] | None = None,
    catalogs: bool = False,
    pending_requests: list[dict] | None = None,
) -> dict:
    host = {
        "host_ref": HOST_REF,
        "state": "normal",
        "app_version": "26.900.1",
        "app_build": "7000",
        "cli_version": "0.153.0",
        "schema_digest": "a" * 64,
        "socket_mode": "0600",
        "tcp_listener_count": 0,
        "capabilities": capabilities or ["list_read", "thread_rename_v1", "thread_pin_v1"],
        "control_enabled": True,
        "models": [{"id": "gpt-5.6-sol", "display_name": "GPT-5.6 Sol", "is_default": True}],
        "synced_at": NOW.isoformat(),
    }
    if catalogs:
        host["capabilities"] += [
            "settings_catalog_v1",
            "owner_request_response_v1",
            "permission_profile_selection_v1",
        ]
        host["settings_catalog"] = {
            "model": "gpt-5.6-sol",
            "reasoning_effort": "high",
            "personality": "default",
            "service_tier": "priority",
            "web_search": "enabled",
        }
        host["permission_profiles"] = [
            {"id": ":read-only", "label": "只读"},
            {"id": ":workspace", "label": "工作区访问"},
        ]
    thread = {
        "project_alias": "demo-project",
        "project_ref": PROJECT_REF,
        "thread_ref": thread_ref,
        "title": title,
        "preview": preview,
        "pinned": pinned,
        "status": "active" if pending_requests is not None else "idle",
        "active_turn_ref": "TR-" + "D" * 20 if pending_requests is not None else None,
        "thread_revision": 7,
        "control_revision": 7,
        "created_at": NOW.isoformat(),
        "updated_at": NOW.isoformat(),
        "history_incomplete": False,
        "turns": [],
        "control_state": "ready",
    }
    if pending_requests is not None:
        thread["pending_requests"] = pending_requests
    if catalogs:
        thread.setdefault("pending_requests", [])
        thread["permission_profile"] = {
            "id": ":workspace",
            "label": "工作区访问",
            "editable": True,
            "options": [
                {"id": ":read-only", "label": "只读"},
                {"id": ":workspace", "label": "工作区访问"},
            ],
        }
    document = {
        "version": 1,
        "message_type": "desktop_snapshot",
        "runner_id": RUNNER_ID,
        "created_at": NOW.isoformat(),
        "host_ref": HOST_REF,
        "project_ref": PROJECT_REF,
        "thread_ref": thread_ref,
        "thread_revision": 7,
        "snapshot_sequence": sequence,
        "snapshot": thread,
        "host": host,
    }
    document["body_digest"] = body_digest(document)
    return document


def user_input_request(*, secret: bool = False) -> dict:
    return {
        "request_ref": REQUEST_REF,
        "turn_ref": TURN_REF,
        "blocking": True,
        "kind": "user_input",
        "title": "需要你的选择",
        "decisions": ["cancel"] if secret else ["submit", "cancel"],
        "questions": [
            {
                "question_ref": QUESTION_REF,
                "header": "发布方式",
                "question": "请选择发布方式",
                "options": [
                    {"label": "安全发布", "description": "先完成全部检查"},
                    {"label": "暂不发布", "description": "保留当前状态"},
                ],
                "allows_other": False,
                "secret": secret,
                "value_type": "string",
                "required": True,
            }
        ],
    }


class DesktopManagementTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.store = DesktopStore(Path(self.temporary.name) / "controller.sqlite3")
        self.publisher = Publisher()
        self.service = DesktopControllerService(
            self.store,
            publisher=self.publisher,
            now=lambda: NOW,
            runner_authorizer=lambda _runner_id: True,
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_rename_and_pin_are_real_capability_gated_commands(self) -> None:
        self.service.receive("desktop_snapshot", snapshot())
        result = self.service.submit(
            THREAD_REF,
            "rename",
            {"request_id": "rename-1", "thread_revision": 7, "title": "新的任务名称"},
        )
        self.assertEqual(result["state"], "submitted")
        command = self.publisher.commands[-1]
        self.assertEqual(command["title"], "新的任务名称")
        self.assertNotIn("thread_id", command)
        self.assertNotEqual(result["state"], "confirmed")

        other = DesktopStore(Path(self.temporary.name) / "without-capability.sqlite3")
        publisher = Publisher()
        service = DesktopControllerService(
            other,
            publisher=publisher,
            now=lambda: NOW,
            runner_authorizer=lambda _runner_id: True,
        )
        service.receive("desktop_snapshot", snapshot(capabilities=["list_read"]))
        with self.assertRaises(StoreError) as context:
            service.submit(
                THREAD_REF,
                "pin",
                {"request_id": "pin-1", "thread_revision": 7, "pinned": True},
            )
        self.assertEqual(context.exception.code, "desktop_capability_unavailable")
        self.assertEqual(publisher.commands, [])

    def test_fork_and_review_use_native_capabilities_and_strict_targets(self) -> None:
        self.service.receive(
            "desktop_snapshot",
            snapshot(
                capabilities=[
                    "list_read",
                    "thread_fork_v1",
                    "review_inline_v1",
                ]
            ),
        )
        forked = post_desktop_api(
            self.service,
            f"/api/desktop/v1/threads/{THREAD_REF}/fork",
            {"request_id": "fork-controller-1", "thread_revision": 7},
        )
        self.assertEqual(forked["state"], "submitted")
        self.assertEqual(self.publisher.commands[-1]["action"], "fork")

        created_ref = "TH-" + "H" * 20
        receipt = {
            "version": 1,
            "message_type": "desktop_receipt",
            "runner_id": RUNNER_ID,
            "created_at": NOW.isoformat(),
            "request_id": "fork-controller-1",
            "host_ref": HOST_REF,
            "thread_ref": THREAD_REF,
            "turn_ref": None,
            "action": "fork",
            "state": "confirmed",
            "thread_revision": 7,
            "created_thread_ref": created_ref,
        }
        receipt["body_digest"] = body_digest(receipt)
        self.service.receive("desktop_receipt", receipt)
        self.assertEqual(
            self.store.command("fork-controller-1")["receipt"]["created_thread_ref"],
            created_ref,
        )

        # A separate task avoids the intentional single-inflight control gate.
        other = DesktopStore(Path(self.temporary.name) / "review.sqlite3")
        review_publisher = Publisher()
        review_service = DesktopControllerService(
            other,
            publisher=review_publisher,
            now=lambda: NOW,
            runner_authorizer=lambda _runner_id: True,
        )
        review_service.receive(
            "desktop_snapshot",
            snapshot(capabilities=["list_read", "review_inline_v1"]),
        )
        reviewed = post_desktop_api(
            review_service,
            f"/api/desktop/v1/threads/{THREAD_REF}/review",
            {
                "request_id": "review-controller-1",
                "thread_revision": 7,
                "review_target": {"type": "baseBranch", "branch": "origin/main"},
            },
        )
        self.assertEqual(reviewed["state"], "submitted")
        self.assertEqual(
            review_publisher.commands[-1]["review_target"],
            {"type": "baseBranch", "branch": "origin/main"},
        )
        with self.assertRaises(StoreError) as context:
            review_service.submit(
                THREAD_REF,
                "review",
                {
                    "request_id": "review-controller-invalid",
                    "thread_revision": 7,
                    "review_target": {"type": "baseBranch", "branch": "../main"},
                },
            )
        self.assertEqual(context.exception.code, "desktop_review_target_invalid")

    def test_rename_limit_is_eighty_visible_characters(self) -> None:
        self.service.receive("desktop_snapshot", snapshot())
        accepted = self.service.submit(
            THREAD_REF,
            "rename",
            {"request_id": "rename-80", "thread_revision": 7, "title": "x" * 80},
        )
        self.assertEqual(accepted["state"], "submitted")
        for value in ("x" * 81, "bad\u200btitle"):
            store = DesktopStore(Path(self.temporary.name) / f"rename-{len(value)}.sqlite3")
            service = DesktopControllerService(
                store,
                publisher=Publisher(),
                now=lambda: NOW,
                runner_authorizer=lambda _runner_id: True,
            )
            service.receive("desktop_snapshot", snapshot())
            with self.assertRaises(StoreError) as context:
                service.submit(
                    THREAD_REF,
                    "rename",
                    {"request_id": f"rename-invalid-{len(value)}", "thread_revision": 7, "title": value},
                )
            self.assertEqual(context.exception.code, "desktop_title_invalid")

    def test_management_command_fields_are_action_bound_and_strict(self) -> None:
        command = build_desktop_command(
            runner_id=RUNNER_ID,
            request_id="pin-command-1",
            host_ref=HOST_REF,
            thread_ref=THREAD_REF,
            expected_thread_revision=7,
            expected_control_revision=7,
            action="pin",
            pinned=True,
            now=NOW,
        )
        self.assertIs(command["pinned"], True)
        invalid = dict(command)
        invalid["pinned"] = "true"
        invalid["body_digest"] = body_digest(invalid)
        with self.assertRaises(DesktopProtocolError) as context:
            validate_desktop_command(invalid, now=NOW)
        self.assertEqual(context.exception.code, "desktop_pinned_invalid")

    def test_snapshot_metadata_refresh_and_global_search_are_paged(self) -> None:
        self.service.receive("desktop_snapshot", snapshot(title="最初名称"))
        self.service.receive(
            "desktop_snapshot",
            snapshot(title="已改名任务", pinned=True, sequence=2),
        )
        second_ref = "TH-" + "D" * 20
        self.service.receive(
            "desktop_snapshot",
            snapshot(thread_ref=second_ref, title="百分号 % 测试", preview="另一条摘要"),
        )
        current = self.service.thread(THREAD_REF)
        self.assertEqual(current["title"], "已改名任务")
        self.assertTrue(current["pinned"])
        result = self.service.search(
            query="已改名",
            host_ref=HOST_REF,
            project_ref=None,
            status=None,
            cursor=0,
            limit=1,
        )
        self.assertEqual(result["coverage"], "synced_metadata")
        self.assertEqual([item["thread_ref"] for item in result["threads"]], [THREAD_REF])
        literal = self.service.search(
            query="%",
            host_ref=HOST_REF,
            project_ref=None,
            status=None,
            cursor=0,
            limit=20,
        )
        self.assertEqual([item["thread_ref"] for item in literal["threads"]], [second_ref])

    def test_management_catalog_is_strict_and_explicitly_unavailable(self) -> None:
        self.service.receive("desktop_snapshot", snapshot(catalogs=True))
        management = self.service.management(HOST_REF)
        self.assertTrue(management["features"]["settings"]["available"])
        self.assertEqual(management["permission_profiles"][0]["id"], ":read-only")

        invalid = snapshot(capabilities=["list_read", "settings_catalog_v1"])
        with self.assertRaises(DesktopProtocolError):
            validate_desktop_document("desktop_snapshot", invalid)

    def test_permission_profile_command_is_strictly_bound_to_safe_turn_starts(self) -> None:
        command = build_desktop_command(
            runner_id=RUNNER_ID,
            request_id="permission-command-1",
            host_ref=HOST_REF,
            thread_ref=THREAD_REF,
            expected_thread_revision=7,
            expected_control_revision=7,
            action="continue",
            input_text="继续",
            permission_profile_id=":workspace",
            now=NOW,
        )
        self.assertEqual(command["permission_profile_id"], ":workspace")
        for action, profile, mode in (
            ("continue", ":unknown", None),
            ("interrupt", ":workspace", None),
            ("steer", ":workspace", "native"),
        ):
            with self.subTest(action=action, profile=profile):
                with self.assertRaises(DesktopProtocolError):
                    build_desktop_command(
                        runner_id=RUNNER_ID,
                        request_id=f"permission-invalid-{action}",
                        host_ref=HOST_REF,
                        thread_ref=THREAD_REF,
                        expected_thread_revision=7,
                        expected_control_revision=7,
                        action=action,
                        expected_turn_ref=TURN_REF if action in {"interrupt", "steer"} else None,
                        input_text="调整" if action in {"continue", "steer"} else None,
                        mode=mode,
                        permission_profile_id=profile,
                        now=NOW,
                    )

    def test_permission_profile_selection_revalidates_capability_and_task_options(self) -> None:
        self.service.receive(
            "desktop_snapshot",
            snapshot(
                capabilities=["list_read", "continue_same_thread"],
                catalogs=True,
            ),
        )
        result = self.service.submit(
            THREAD_REF,
            "continue",
            {
                "request_id": "permission-continue-1",
                "thread_revision": 7,
                "input": "以只读权限继续",
                "permission_profile_id": ":read-only",
            },
        )
        self.assertEqual(result["state"], "submitted")
        self.assertEqual(self.publisher.commands[-1]["permission_profile_id"], ":read-only")

        cases = (
            (
                "missing-capability",
                snapshot(capabilities=["list_read", "continue_same_thread"]),
                ":read-only",
                "desktop_permission_profile_unavailable",
            ),
            (
                "not-allowed",
                snapshot(
                    capabilities=["list_read", "continue_same_thread"],
                    catalogs=True,
                ),
                ":workspace",
                "desktop_permission_profile_not_allowed",
            ),
            (
                "not-editable",
                snapshot(
                    capabilities=["list_read", "continue_same_thread"],
                    catalogs=True,
                ),
                ":read-only",
                "desktop_permission_profile_not_editable",
            ),
            (
                "custom-unavailable",
                snapshot(
                    capabilities=["list_read", "continue_same_thread"],
                    catalogs=True,
                ),
                ":read-only",
                "desktop_permission_profile_not_editable",
            ),
        )
        for index, (name, document, requested, code) in enumerate(cases):
            if name == "not-allowed":
                document["snapshot"]["permission_profile"]["options"] = [
                    {"id": ":read-only", "label": "只读"}
                ]
            if name == "not-editable":
                document["snapshot"]["permission_profile"] = {
                    "id": ":workspace",
                    "label": "工作区访问",
                    "editable": False,
                    "options": [],
                    "reason": "permission_profile_allowlist_missing",
                }
            if name == "custom-unavailable":
                document["snapshot"]["permission_profile"] = {
                    "id": None,
                    "label": "自定义权限",
                    "editable": False,
                    "options": [],
                    "reason": "permission_profile_unsupported",
                }
            document["body_digest"] = body_digest(document)
            store = DesktopStore(Path(self.temporary.name) / f"permission-{index}.sqlite3")
            publisher = Publisher()
            service = DesktopControllerService(
                store,
                publisher=publisher,
                now=lambda: NOW,
                runner_authorizer=lambda _runner_id: True,
            )
            service.receive("desktop_snapshot", document)
            with self.subTest(name=name), self.assertRaises(StoreError) as context:
                service.submit(
                    THREAD_REF,
                    "continue",
                    {
                        "request_id": f"permission-reject-{index}",
                        "thread_revision": 7,
                        "input": "继续",
                        "permission_profile_id": requested,
                    },
                )
            self.assertEqual(context.exception.code, code)
            self.assertEqual(publisher.commands, [])

    def test_permission_profile_is_forwarded_for_create_and_safe_steer_only(self) -> None:
        create_store = DesktopStore(Path(self.temporary.name) / "permission-create.sqlite3")
        create_publisher = Publisher()
        create_service = DesktopControllerService(
            create_store,
            publisher=create_publisher,
            now=lambda: NOW,
            runner_authorizer=lambda _runner_id: True,
        )
        create_service.receive(
            "desktop_snapshot",
            snapshot(capabilities=["list_read", "create_thread_v1"], catalogs=True),
        )
        created = create_service.create(
            {
                "request_id": "permission-create-1",
                "host_ref": HOST_REF,
                "project_ref": PROJECT_REF,
                "input": "创建任务",
                "permission_profile_id": ":workspace",
            }
        )
        self.assertEqual(created["state"], "submitted")
        self.assertEqual(create_publisher.commands[-1]["permission_profile_id"], ":workspace")

        steer_store = DesktopStore(Path(self.temporary.name) / "permission-steer.sqlite3")
        steer_publisher = Publisher()
        steer_service = DesktopControllerService(
            steer_store,
            publisher=steer_publisher,
            now=lambda: NOW,
            runner_authorizer=lambda _runner_id: True,
        )
        steer_service.receive(
            "desktop_snapshot",
            snapshot(
                capabilities=["list_read", "interrupt_expected_turn", "continue_same_thread"],
                catalogs=True,
                pending_requests=[],
            ),
        )
        steered = steer_service.submit(
            THREAD_REF,
            "steer",
            {
                "request_id": "permission-steer-1",
                "thread_revision": 7,
                "expected_turn_ref": TURN_REF,
                "input": "安全调整",
                "mode": "safe",
                "permission_profile_id": ":read-only",
            },
        )
        self.assertEqual(steered["state"], "submitted")
        self.assertEqual(steer_publisher.commands[-1]["permission_profile_id"], ":read-only")

    def test_same_revision_permission_profile_refreshes_by_snapshot_sequence(self) -> None:
        first = snapshot(catalogs=True, sequence=1)
        self.service.receive("desktop_snapshot", first)
        second = snapshot(catalogs=True, sequence=2)
        second["snapshot"]["permission_profile"] = {
            "id": ":read-only",
            "label": "只读",
            "editable": True,
            "options": [{"id": ":read-only", "label": "只读"}],
        }
        second["body_digest"] = body_digest(second)
        result = self.service.receive("desktop_snapshot", second)
        self.assertEqual(result["status"], "refreshed")
        self.assertEqual(
            self.service.thread(THREAD_REF)["snapshot"]["permission_profile"]["id"],
            ":read-only",
        )

    def test_same_revision_identical_snapshot_refreshes_only_host_capabilities(self) -> None:
        first = snapshot(capabilities=["list_read"], sequence=1)
        self.service.receive("desktop_snapshot", first)
        with sqlite3.connect(self.store.database_path) as connection:
            before_snapshot_json = connection.execute(
                "SELECT snapshot_json FROM desktop_threads WHERE thread_ref=?",
                (THREAD_REF,),
            ).fetchone()[0]

        management_capabilities = [
            "list_read",
            "thread_rename_v1",
            "thread_pin_v1",
            "thread_fork_v1",
            "review_inline_v1",
        ]
        second = snapshot(capabilities=management_capabilities, sequence=2)
        self.assertEqual(first["thread_revision"], second["thread_revision"])
        self.assertEqual(first["snapshot"], second["snapshot"])

        result = self.service.receive("desktop_snapshot", second)

        self.assertEqual(result["status"], "refreshed")
        self.assertEqual(
            set(self.store.list_hosts()[0]["capabilities"]),
            set(management_capabilities),
        )
        with sqlite3.connect(self.store.database_path) as connection:
            after_snapshot_json = connection.execute(
                "SELECT snapshot_json FROM desktop_threads WHERE thread_ref=?",
                (THREAD_REF,),
            ).fetchone()[0]
        self.assertEqual(after_snapshot_json, before_snapshot_json)
        self.assertEqual(self.store.thread(THREAD_REF)["snapshot"], first["snapshot"])

    def test_mobile_ui_uses_server_search_and_capability_gates(self) -> None:
        combined = DESKTOP_DASHBOARD_HTML + DESKTOP_DASHBOARD_JS
        for value in (
            "搜索全部已同步任务",
            "/search?host_ref=",
            "thread_rename_v1",
            "thread_pin_v1",
            "thread_fork_v1",
            "review_inline_v1",
            'id="reviewSheet"',
            'maxlength="80"',
            "created_thread_ref",
            "managementSheet",
            "Runner 更新后可用",
            "permissionSelect",
            "permission_profile_selection_v1",
            "只能保持或收紧当前权限",
        ):
            self.assertIn(value, combined)
        self.assertIn("window.setTimeout(() => void loadThreadPage({reset: true}), 280)", DESKTOP_DASHBOARD_JS)
        self.assertNotIn("innerHTML", DESKTOP_DASHBOARD_JS)
        management_open = DESKTOP_DASHBOARD_JS[
            DESKTOP_DASHBOARD_JS.index("async function setManagementOpen") :
            DESKTOP_DASHBOARD_JS.index("function renderManagement")
        ]
        self.assertNotIn("q('managementSheet').classList.add('hidden')", management_open)

    def test_pending_request_response_uses_turn_cas_and_never_fakes_confirmation(self) -> None:
        capabilities = ["list_read", "owner_request_response_v1"]
        self.service.receive(
            "desktop_snapshot",
            snapshot(capabilities=capabilities, pending_requests=[user_input_request()]),
        )
        result = self.service.submit(
            THREAD_REF,
            "respond_request",
            {
                "request_id": "respond-request-1",
                "thread_revision": 7,
                "expected_turn_ref": TURN_REF,
                "request_ref": REQUEST_REF,
                "decision": "submit",
                "answers": [{"question_ref": QUESTION_REF, "answers": ["安全发布"]}],
            },
        )
        self.assertEqual(result["state"], "submitted")
        self.assertNotEqual(result["state"], "confirmed")
        command = self.publisher.commands[-1]
        self.assertEqual(command["action"], "respond_request")
        self.assertEqual(command["expected_control_revision"], 7)
        self.assertEqual(command["request_ref"], REQUEST_REF)
        self.assertNotIn("question_id", str(command))
        wrong_receipt = {
            "version": 1,
            "message_type": "desktop_receipt",
            "runner_id": RUNNER_ID,
            "created_at": NOW.isoformat(),
            "request_id": "respond-request-1",
            "host_ref": HOST_REF,
            "thread_ref": THREAD_REF,
            "turn_ref": TURN_REF,
            "action": "respond_request",
            "state": "confirmed",
            "thread_revision": 7,
            "request_ref": "RQ-" + "H" * 20,
        }
        wrong_receipt["body_digest"] = body_digest(wrong_receipt)
        with self.assertRaises(StoreError) as context:
            self.service.receive("desktop_receipt", wrong_receipt)
        self.assertEqual(context.exception.code, "desktop_receipt_binding_conflict")

    def test_request_response_api_and_sse_use_the_same_durable_projection(self) -> None:
        capabilities = ["list_read", "owner_request_response_v1"]
        self.service.receive(
            "desktop_snapshot",
            snapshot(capabilities=capabilities, pending_requests=[user_input_request()], sequence=1),
        )
        stream = self.service.thread_stream(THREAD_REF, after_cursor=None, heartbeat_seconds=0.05)
        self.assertEqual(next(stream)["event"], "ready")
        result = post_desktop_api(
            self.service,
            f"/api/desktop/v1/threads/{THREAD_REF}/requests/{REQUEST_REF}/respond",
            {
                "request_id": "respond-api-1",
                "thread_revision": 7,
                "expected_turn_ref": TURN_REF,
                "decision": "cancel",
            },
        )
        self.assertEqual(result["state"], "submitted")
        self.service.receive(
            "desktop_snapshot",
            snapshot(capabilities=capabilities, pending_requests=[], sequence=2),
        )
        frame = next(stream)
        self.assertEqual(frame["event"], "desktop")
        self.assertTrue(frame["data"]["changed"])

    def test_pending_request_response_rejects_stale_decision_and_secret_answer(self) -> None:
        capabilities = ["list_read", "owner_request_response_v1"]
        self.service.receive(
            "desktop_snapshot",
            snapshot(capabilities=capabilities, pending_requests=[user_input_request(secret=True)]),
        )
        for decision, answers in (
            ("accept", None),
            ("submit", [{"question_ref": QUESTION_REF, "answers": ["不得发送"]}]),
        ):
            with self.subTest(decision=decision):
                with self.assertRaises(StoreError):
                    self.service.submit(
                        THREAD_REF,
                        "respond_request",
                        {
                            "request_id": f"secret-{decision}",
                            "thread_revision": 7,
                            "expected_turn_ref": TURN_REF,
                            "request_ref": REQUEST_REF,
                            "decision": decision,
                            **({"answers": answers} if answers is not None else {}),
                        },
                    )
        self.assertEqual(self.publisher.commands, [])

    def test_unknown_response_is_idempotently_observed_and_not_republished(self) -> None:
        capabilities = ["list_read", "owner_request_response_v1"]
        self.service.receive(
            "desktop_snapshot",
            snapshot(capabilities=capabilities, pending_requests=[user_input_request()]),
        )
        self.publisher.error = RuntimeError("indeterminate")
        body = {
            "request_id": "respond-unknown-1",
            "thread_revision": 7,
            "expected_turn_ref": TURN_REF,
            "request_ref": REQUEST_REF,
            "decision": "cancel",
        }
        first = self.service.submit(THREAD_REF, "respond_request", body)
        second = self.service.submit(THREAD_REF, "respond_request", body)
        self.assertEqual(first["state"], "unknown")
        self.assertEqual(second["state"], "unknown")
        self.assertEqual(len(self.publisher.commands), 1)

    def test_request_disappearance_refreshes_same_revision_by_snapshot_sequence(self) -> None:
        capabilities = ["list_read", "owner_request_response_v1"]
        self.service.receive(
            "desktop_snapshot",
            snapshot(capabilities=capabilities, pending_requests=[user_input_request()], sequence=1),
        )
        self.service.receive(
            "desktop_snapshot",
            snapshot(capabilities=capabilities, pending_requests=[], sequence=2),
        )
        self.assertEqual(self.service.thread(THREAD_REF)["snapshot"]["pending_requests"], [])

    def test_request_ui_is_mobile_safe_and_hides_raw_command_details(self) -> None:
        combined = DESKTOP_DASHBOARD_HTML + DESKTOP_DASHBOARD_JS
        for value in (
            "requestTray",
            "requestSheet",
            "owner_request_response_v1",
            "/requests/${encodeURIComponent(body.request_ref)}/respond",
            "系统不会自动或手动重放",
            "手机端不展示原始命令",
            "min-height:44px",
        ):
            self.assertIn(value, combined)


if __name__ == "__main__":
    unittest.main()
