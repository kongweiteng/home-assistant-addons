from __future__ import annotations

import datetime as dt
from pathlib import Path
import tempfile
import threading
import time
import unittest

from codex_controller.desktop_api import get_desktop_api
from codex_controller.desktop_protocol import body_digest
from codex_controller.desktop_service import DesktopControllerService
from codex_controller.desktop_store import DesktopStore
from codex_controller.store import StoreError


NOW = dt.datetime(2026, 9, 5, 15, 30, tzinfo=dt.timezone(dt.timedelta(hours=8)))
HOST_A = {
    "runner_id": "RN-" + "A" * 20,
    "host_ref": "HS-" + "A" * 20,
    "project_ref": "PJ-" + "A" * 20,
    "thread_ref": "TH-" + "A" * 20,
    "turn_ref": "TR-" + "A" * 20,
}
HOST_B = {
    "runner_id": "RN-" + "B" * 20,
    "host_ref": "HS-" + "B" * 20,
    "project_ref": "PJ-" + "B" * 20,
    "thread_ref": "TH-" + "B" * 20,
    "turn_ref": "TR-" + "B" * 20,
}


def digest(document: dict) -> dict:
    result = dict(document)
    result["body_digest"] = body_digest(result)
    return result


def snapshot(identity: dict[str, str], *, revision: int = 1) -> dict:
    return digest(
        {
            "version": 1,
            "message_type": "desktop_snapshot",
            "runner_id": identity["runner_id"],
            "created_at": NOW.isoformat(),
            "host_ref": identity["host_ref"],
            "project_ref": identity["project_ref"],
            "thread_ref": identity["thread_ref"],
            "thread_revision": revision,
            "snapshot": {
                "project_alias": f"project-{identity['host_ref'][-1].lower()}",
                "project_ref": identity["project_ref"],
                "thread_ref": identity["thread_ref"],
                "title": f"Host {identity['host_ref'][-1]} task",
                "preview": "公开摘要",
                "status": "active",
                "active_turn_ref": identity["turn_ref"],
                "thread_revision": revision,
                "control_revision": revision,
                "created_at": NOW.isoformat(),
                "updated_at": NOW.isoformat(),
                "history_incomplete": False,
                "turns": [],
                "control_state": "ready",
            },
            "host": {
                "host_ref": identity["host_ref"],
                "state": "normal",
                "app_version": "26.810.52044",
                "app_build": "6662",
                "cli_version": "0.148.0-alpha.9",
                "schema_digest": "a" * 64,
                "socket_mode": "0600",
                "tcp_listener_count": 0,
                "capabilities": [
                    "list_read",
                    "interrupt_expected_turn",
                    "continue_same_thread",
                ],
                "control_enabled": True,
                "models": [],
                "synced_at": NOW.isoformat(),
            },
        }
    )


def desktop_event(identity: dict[str, str], *, sequence: int, revision: int = 1) -> dict:
    return digest(
        {
            "version": 1,
            "message_type": "desktop_event",
            "runner_id": identity["runner_id"],
            "created_at": NOW.isoformat(),
            "host_ref": identity["host_ref"],
            "project_ref": identity["project_ref"],
            "thread_ref": identity["thread_ref"],
            "turn_ref": identity["turn_ref"],
            "thread_revision": revision,
            "event_sequence": sequence,
            "event_kind": "thread.updated",
            "source": "desktop",
            "payload": {"status": "active", "sequence": sequence},
        }
    )


def receipt(identity: dict[str, str], request_id: str, *, revision: int) -> dict:
    return digest(
        {
            "version": 1,
            "message_type": "desktop_receipt",
            "runner_id": identity["runner_id"],
            "created_at": (NOW + dt.timedelta(seconds=1)).isoformat(),
            "request_id": request_id,
            "host_ref": identity["host_ref"],
            "thread_ref": identity["thread_ref"],
            "turn_ref": identity["turn_ref"],
            "action": "interrupt",
            "state": "accepted",
            "thread_revision": revision,
        }
    )


class Publisher:
    def __init__(self) -> None:
        self.commands: list[tuple[str, dict]] = []

    def publish_desktop_command(self, runner_id: str, document: dict) -> None:
        self.commands.append((runner_id, dict(document)))


class DesktopHostEventsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.publisher = Publisher()
        self.store = DesktopStore(Path(self.temporary.name) / "controller.sqlite3")
        self.service = DesktopControllerService(
            self.store,
            publisher=self.publisher,
            now=lambda: NOW,
            runner_authorizer=lambda _runner_id: True,
        )
        self.service.receive("desktop_snapshot", snapshot(HOST_A))
        self.service.receive("desktop_snapshot", snapshot(HOST_B))

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def host_events(
        self,
        host_ref: str,
        *,
        after_cursor: int = 0,
        limit: int = 100,
        wait_seconds: float = 0,
    ) -> dict:
        return get_desktop_api(
            self.service,
            "/api/desktop/v1/events",
            (
                f"host_ref={host_ref}&after_cursor={after_cursor}"
                f"&limit={limit}&wait_seconds={wait_seconds}"
            ),
        )

    def wait_for_host_change(
        self,
        host_ref: str,
        trigger,
        *,
        after_cursor: int = 0,
    ) -> tuple[dict, float]:
        result: list[dict] = []
        started = threading.Event()

        def wait() -> None:
            started.set()
            result.append(
                self.host_events(
                    host_ref,
                    after_cursor=after_cursor,
                    wait_seconds=2,
                )
            )

        worker = threading.Thread(target=wait)
        before = time.monotonic()
        worker.start()
        self.assertTrue(started.wait(timeout=1))
        time.sleep(0.03)
        trigger()
        worker.join(timeout=1)
        elapsed = time.monotonic() - before
        self.assertFalse(worker.is_alive(), "host event long poll did not wake immediately")
        self.assertEqual(len(result), 1)
        return result[0], elapsed

    def test_host_isolation_cursor_and_has_more(self) -> None:
        first_a = self.service.receive("desktop_event", desktop_event(HOST_A, sequence=1))
        self.service.receive("desktop_event", desktop_event(HOST_B, sequence=1))
        second_a = self.service.receive("desktop_event", desktop_event(HOST_A, sequence=2))

        first_page = self.host_events(HOST_A["host_ref"], limit=1)
        self.assertEqual([item["event_sequence"] for item in first_page["events"]], [1])
        self.assertEqual(first_page["next_cursor"], first_a["cursor"])
        self.assertTrue(first_page["has_more"])
        self.assertFalse(first_page["changed"])

        second_page = self.host_events(
            HOST_A["host_ref"],
            after_cursor=first_page["next_cursor"],
            limit=1,
        )
        self.assertEqual([item["event_sequence"] for item in second_page["events"]], [2])
        self.assertEqual(second_page["next_cursor"], second_a["cursor"])
        self.assertFalse(second_page["has_more"])
        self.assertTrue(all(item["host_ref"] == HOST_A["host_ref"] for item in second_page["events"]))
        self.assertNotIn("runner_id", second_page["events"][0])

        host_b_page = self.host_events(HOST_B["host_ref"])
        self.assertEqual([item["event_sequence"] for item in host_b_page["events"]], [1])
        self.assertTrue(all(item["host_ref"] == HOST_B["host_ref"] for item in host_b_page["events"]))

    def test_desktop_event_wakes_waiter_immediately(self) -> None:
        result, elapsed = self.wait_for_host_change(
            HOST_A["host_ref"],
            lambda: self.service.receive(
                "desktop_event",
                desktop_event(HOST_A, sequence=1),
            ),
        )

        self.assertLess(elapsed, 1)
        self.assertEqual([item["event_sequence"] for item in result["events"]], [1])
        self.assertFalse(result["changed"])
        self.assertFalse(result["has_more"])

    def test_empty_timeout_and_invalid_hosts(self) -> None:
        before = time.monotonic()
        result = self.host_events(HOST_A["host_ref"], wait_seconds=0.05)
        elapsed = time.monotonic() - before

        self.assertGreaterEqual(elapsed, 0.025)
        self.assertLess(elapsed, 0.5)
        self.assertEqual(result["events"], [])
        self.assertEqual(result["next_cursor"], 0)
        self.assertFalse(result["has_more"])
        self.assertFalse(result["changed"])

        with self.subTest("well-formed unknown host"):
            with self.assertRaises(StoreError) as context:
                self.host_events("HS-" + "Z" * 20)
            self.assertEqual(context.exception.code, "desktop_host_not_found")
            self.assertEqual(context.exception.status, 404)

        with self.subTest("malformed host ref"):
            with self.assertRaises(StoreError) as context:
                self.host_events("not-a-host")
            self.assertEqual(context.exception.code, "desktop_ref_invalid")
            self.assertEqual(context.exception.status, 400)

    def test_snapshot_and_receipt_wake_as_changed_without_fake_events(self) -> None:
        snapshot_result, _ = self.wait_for_host_change(
            HOST_A["host_ref"],
            lambda: self.service.receive(
                "desktop_snapshot",
                snapshot(HOST_A, revision=2),
            ),
        )
        self.assertEqual(snapshot_result["events"], [])
        self.assertTrue(snapshot_result["changed"])
        self.assertEqual(snapshot_result["next_cursor"], 0)

        request_id = "desktop-host-event-receipt-0001"
        self.service.submit(
            HOST_A["thread_ref"],
            "interrupt",
            {
                "request_id": request_id,
                "expected_turn_ref": HOST_A["turn_ref"],
                "thread_revision": 2,
            },
        )
        receipt_result, _ = self.wait_for_host_change(
            HOST_A["host_ref"],
            lambda: self.service.receive(
                "desktop_receipt",
                receipt(HOST_A, request_id, revision=2),
            ),
        )
        self.assertEqual(receipt_result["events"], [])
        self.assertTrue(receipt_result["changed"])
        self.assertEqual(receipt_result["next_cursor"], 0)

        settled = self.host_events(HOST_A["host_ref"])
        self.assertEqual(settled["events"], [])
        self.assertFalse(settled["changed"])


if __name__ == "__main__":
    unittest.main()
