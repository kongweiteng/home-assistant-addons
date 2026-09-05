from __future__ import annotations

import datetime as dt
from http.client import HTTPConnection, HTTPResponse
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from codex_controller.api import create_server
from codex_controller.desktop_protocol import body_digest
from codex_controller.desktop_service import DesktopControllerService
from codex_controller.desktop_store import DesktopStore


NOW = dt.datetime(2026, 9, 5, 18, 0, tzinfo=dt.timezone(dt.timedelta(hours=8)))
RUNNER_REF = "RN-" + "A" * 20
HOST_REF = "HS-" + "B" * 20
PROJECT_REF = "PJ-" + "C" * 20
THREAD_REF = "TH-" + "D" * 20
TURN_REF = "TR-" + "E" * 20


def digest(document: dict) -> dict:
    result = dict(document)
    result["body_digest"] = body_digest(result)
    return result


def snapshot(
    *,
    thread_ref: str = THREAD_REF,
    turn_ref: str = TURN_REF,
    title: str = "SSE task",
    updated_at: dt.datetime = NOW,
    revision: int = 1,
) -> dict:
    return digest(
        {
            "version": 1,
            "message_type": "desktop_snapshot",
            "runner_id": RUNNER_REF,
            "created_at": NOW.isoformat(),
            "host_ref": HOST_REF,
            "project_ref": PROJECT_REF,
            "thread_ref": thread_ref,
            "thread_revision": revision,
            "snapshot": {
                "project_alias": "sse-fixture",
                "project_ref": PROJECT_REF,
                "thread_ref": thread_ref,
                "title": title,
                "preview": "bounded public preview",
                "status": "active",
                "active_turn_ref": turn_ref,
                "thread_revision": revision,
                "control_revision": revision,
                "created_at": NOW.isoformat(),
                "updated_at": updated_at.isoformat(),
                "history_incomplete": False,
                "turns": [],
                "control_state": "ready",
            },
            "host": {
                "host_ref": HOST_REF,
                "state": "normal",
                "app_version": "26.905.1000",
                "app_build": "7000",
                "cli_version": "0.150.0",
                "schema_digest": "a" * 64,
                "socket_mode": "0600",
                "tcp_listener_count": 0,
                "capabilities": ["list_read", "desktop_takeover_v1"],
                "control_enabled": True,
                "models": [],
                "synced_at": NOW.isoformat(),
            },
        }
    )


def desktop_event(sequence: int, *, thread_ref: str = THREAD_REF) -> dict:
    return digest(
        {
            "version": 1,
            "message_type": "desktop_event",
            "runner_id": RUNNER_REF,
            "created_at": NOW.isoformat(),
            "host_ref": HOST_REF,
            "project_ref": PROJECT_REF,
            "thread_ref": thread_ref,
            "turn_ref": TURN_REF,
            "thread_revision": 1,
            "event_sequence": sequence,
            "event_kind": "thread.updated",
            "source": "desktop",
            "payload": {"sequence": sequence, "status": "active"},
        }
    )


class Controller:
    runner_manager = None

    def __init__(self, desktop: DesktopControllerService) -> None:
        self.desktop_controller = desktop

    def status(self) -> dict:
        return {
            "version": "0.5.35-test",
            "ready": True,
            "queue": {"jobs": {"queued": 0}},
        }


def read_sse_event(response: HTTPResponse) -> dict:
    fields: dict[str, str] = {}
    while True:
        line = response.fp.readline().decode("utf-8")  # type: ignore[union-attr]
        if line == "":
            raise AssertionError("SSE stream closed before a complete event")
        if line in {"\n", "\r\n"}:
            break
        name, separator, value = line.rstrip("\r\n").partition(":")
        if not separator:
            raise AssertionError(f"invalid SSE line: {line!r}")
        fields[name] = value[1:] if value.startswith(" ") else value
    return {
        "id": int(fields["id"]),
        "event": fields["event"],
        "data": json.loads(fields["data"]),
    }


class DesktopSseTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.store = DesktopStore(Path(self.temporary.name) / "controller.sqlite3")
        self.desktop = DesktopControllerService(
            self.store,
            publisher=None,
            now=lambda: NOW,
            runner_status_provider=lambda _runner_id: {
                "connectivity_state": "online",
                "last_heartbeat_at": NOW.isoformat(),
            },
        )
        self.desktop.receive("desktop_snapshot", snapshot())
        self.server = create_server(
            "127.0.0.1",
            0,
            service=Controller(self.desktop),  # type: ignore[arg-type]
            api_token="a" * 32,
            max_request_bytes=1024 * 1024,
        )
        self.server_thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.server_thread.start()

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.server_thread.join(timeout=3)
        self.temporary.cleanup()

    def open_stream(
        self,
        path: str,
        *,
        headers: dict[str, str] | None = None,
    ) -> tuple[HTTPConnection, HTTPResponse]:
        connection = HTTPConnection("127.0.0.1", self.server.server_port, timeout=2)
        connection.request("GET", path, headers=headers or {})
        return connection, connection.getresponse()

    def request_json(
        self,
        path: str,
        *,
        headers: dict[str, str] | None = None,
    ) -> tuple[int, dict]:
        connection, response = self.open_stream(path, headers=headers)
        try:
            return response.status, json.loads(response.read())
        finally:
            connection.close()

    def test_host_stream_starts_at_tail_and_then_pushes_only_new_delta(self) -> None:
        old = self.desktop.receive("desktop_event", desktop_event(1))
        connection, response = self.open_stream(
            f"/api/desktop/v1/stream?host_ref={HOST_REF}"
        )
        try:
            self.assertEqual(response.status, 200)
            self.assertEqual(response.getheader("Content-Type"), "text/event-stream; charset=utf-8")
            self.assertIn("no-cache", response.getheader("Cache-Control", ""))
            self.assertEqual(response.getheader("X-Accel-Buffering"), "no")
            ready = read_sse_event(response)
            self.assertEqual(ready["event"], "ready")
            self.assertEqual(ready["id"], old["cursor"])
            self.assertEqual(ready["data"]["events"], [])
            self.assertEqual(ready["data"]["host"]["host_ref"], HOST_REF)
            self.assertTrue(ready["data"]["host"]["online"])
            self.assertIn("connection_observed_at", ready["data"]["host"])
            self.assertIn("data_synced_at", ready["data"]["host"])
            self.assertEqual(ready["data"]["host"]["data_age_seconds"], 0)
            self.assertEqual(ready["data"]["host"]["data_freshness_state"], "fresh")

            new = self.desktop.receive("desktop_event", desktop_event(2))
            pushed = read_sse_event(response)
            self.assertEqual(pushed["event"], "desktop")
            self.assertEqual(pushed["id"], new["cursor"])
            self.assertEqual([item["event_sequence"] for item in pushed["data"]["events"]], [2])
            self.assertNotIn("snapshot", pushed["data"])
        finally:
            connection.close()

    def test_overview_status_stream_pushes_named_status_frame(self) -> None:
        connection, response = self.open_stream("/api/stream")
        try:
            self.assertEqual(response.status, 200)
            self.assertEqual(response.getheader("Content-Type"), "text/event-stream; charset=utf-8")
            self.assertEqual(response.getheader("X-Accel-Buffering"), "no")
            pushed = read_sse_event(response)
            self.assertEqual(pushed["event"], "status")
            self.assertEqual(pushed["id"], 1)
            self.assertEqual(pushed["data"]["cursor"], 1)
            self.assertTrue(pushed["data"]["status"]["ready"])
        finally:
            connection.close()

    def test_overview_status_stream_rejects_query_parameters(self) -> None:
        status, result = self.request_json("/api/stream?cursor=1")
        self.assertEqual(status, 400)
        self.assertEqual(result["error"]["code"], "status_stream_query_invalid")

    def test_thread_stream_resumes_from_last_event_id(self) -> None:
        first = self.desktop.receive("desktop_event", desktop_event(1))
        second = self.desktop.receive("desktop_event", desktop_event(2))
        connection, response = self.open_stream(
            f"/api/desktop/v1/threads/{THREAD_REF}/stream",
            headers={"Last-Event-ID": str(first["cursor"])},
        )
        try:
            ready = read_sse_event(response)
            pushed = read_sse_event(response)
            self.assertEqual(ready["id"], first["cursor"])
            self.assertEqual(pushed["id"], second["cursor"])
            self.assertEqual([item["event_sequence"] for item in pushed["data"]["events"]], [2])
            self.assertNotIn("host", pushed["data"])
        finally:
            connection.close()

    def test_named_heartbeat_is_bounded_and_contains_no_history(self) -> None:
        thread_stream = self.desktop.thread_stream(
            THREAD_REF,
            after_cursor=None,
            heartbeat_seconds=0.02,
        )
        ready = next(thread_stream)
        heartbeat = next(thread_stream)
        thread_stream.close()

        self.assertEqual(ready["event"], "ready")
        self.assertEqual(heartbeat["event"], "heartbeat")
        self.assertEqual(heartbeat["data"]["events"], [])
        self.assertEqual(heartbeat["data"]["cursor"], heartbeat["cursor"])

        host_stream = self.desktop.host_stream(
            HOST_REF,
            after_cursor=None,
            heartbeat_seconds=0.02,
        )
        next(host_stream)
        host_heartbeat = next(host_stream)
        host_stream.close()
        self.assertEqual(host_heartbeat["event"], "heartbeat")
        self.assertEqual(host_heartbeat["data"]["host"]["host_ref"], HOST_REF)
        self.assertIn("models", host_heartbeat["data"]["host"])
        self.assertIn("capabilities", host_heartbeat["data"]["host"])

    def test_non_event_change_pushes_reconcile_frame(self) -> None:
        stream = self.desktop.thread_stream(
            THREAD_REF,
            after_cursor=None,
            heartbeat_seconds=1,
        )
        ready = next(stream)
        self.desktop._notify_change(host_ref=HOST_REF, thread_ref=THREAD_REF)
        changed = next(stream)
        stream.close()

        self.assertEqual(ready["data"]["scope"], {"type": "thread", "ref": THREAD_REF})
        self.assertEqual(changed["event"], "desktop")
        self.assertTrue(changed["data"]["changed"])
        self.assertEqual(changed["data"]["events"], [])

    def test_pruned_cursor_requires_bounded_snapshot_resync(self) -> None:
        first = self.desktop.receive("desktop_event", desktop_event(1))["cursor"]
        second = self.desktop.receive("desktop_event", desktop_event(2))["cursor"]
        with self.store._connect() as connection:
            connection.execute(
                "INSERT INTO desktop_thread_event_watermarks(thread_ref,pruned_through) VALUES(?,?)",
                (THREAD_REF, first),
            )

        stream = self.desktop.thread_stream(THREAD_REF, after_cursor=first)
        ready = next(stream)
        stream.close()

        self.assertEqual(ready["event"], "ready")
        self.assertEqual(ready["cursor"], second)
        self.assertTrue(ready["data"]["resync_required"])
        self.assertEqual(ready["data"]["events"], [])

    def test_zero_and_future_cursors_resync_after_prune_or_database_rollback(self) -> None:
        first = self.desktop.receive("desktop_event", desktop_event(1))["cursor"]
        tail = self.desktop.receive("desktop_event", desktop_event(2))["cursor"]
        with self.store._connect() as connection:
            connection.execute(
                "INSERT INTO desktop_thread_event_watermarks(thread_ref,pruned_through) VALUES(?,?)",
                (THREAD_REF, first),
            )

        for cursor in (0, tail + 20):
            with self.subTest(cursor=cursor):
                stream = self.desktop.thread_stream(THREAD_REF, after_cursor=cursor)
                ready = next(stream)
                stream.close()
                self.assertEqual(ready["cursor"], tail)
                self.assertTrue(ready["data"]["resync_required"])
                self.assertEqual(ready["data"]["events"], [])

    def test_resume_survives_controller_service_restart(self) -> None:
        first = self.desktop.receive("desktop_event", desktop_event(1))["cursor"]
        second = self.desktop.receive("desktop_event", desktop_event(2))["cursor"]
        restarted = DesktopControllerService(
            self.store,
            publisher=None,
            now=lambda: NOW,
            runner_status_provider=lambda _runner_id: {
                "connectivity_state": "online",
                "last_heartbeat_at": NOW.isoformat(),
            },
        )

        stream = restarted.thread_stream(THREAD_REF, after_cursor=first)
        ready = next(stream)
        pushed = next(stream)
        stream.close()

        self.assertFalse(ready["data"]["resync_required"])
        self.assertEqual(ready["cursor"], first)
        self.assertEqual(pushed["cursor"], second)
        self.assertEqual([item["event_sequence"] for item in pushed["data"]["events"]], [2])

    def test_legacy_host_watermark_is_conservatively_backfilled_per_thread(self) -> None:
        first = self.desktop.receive("desktop_event", desktop_event(1))["cursor"]
        with self.store._connect() as connection:
            connection.execute(
                "INSERT INTO desktop_event_watermarks(host_ref,pruned_through) VALUES(?,?)",
                (HOST_REF, first),
            )

        reopened = DesktopStore(self.store.database_path)

        self.assertEqual(
            reopened.event_pruned_through(scope_kind="thread", scope_ref=THREAD_REF),
            first,
        )

    def test_prune_watermarks_are_scoped_to_host_and_thread(self) -> None:
        second_thread = "TH-" + "F" * 20
        self.desktop.receive(
            "desktop_snapshot",
            snapshot(thread_ref=second_thread, turn_ref="TR-" + "G" * 20),
        )
        first = self.desktop.receive("desktop_event", desktop_event(1))["cursor"]
        second = self.desktop.receive(
            "desktop_event", desktop_event(2, thread_ref=second_thread)
        )["cursor"]
        self.desktop.receive("desktop_event", desktop_event(3))

        with patch("codex_controller.desktop_store.EVENTS_PER_HOST", 1):
            with self.store._connect() as connection:
                self.store._prune_events(connection, HOST_REF)

        self.assertEqual(
            self.store.event_pruned_through(scope_kind="host", scope_ref=HOST_REF),
            second,
        )
        self.assertEqual(
            self.store.event_pruned_through(scope_kind="thread", scope_ref=THREAD_REF),
            first,
        )
        self.assertEqual(
            self.store.event_pruned_through(scope_kind="thread", scope_ref=second_thread),
            second,
        )

    def test_snapshot_commit_between_tail_and_sequence_is_not_missed(self) -> None:
        tail_read = threading.Event()
        snapshot_committed = threading.Event()
        original_tail = self.desktop._event_tail
        original_notify = self.desktop._notify_change

        def paused_tail(*, scope_kind: str, scope_ref: str) -> int:
            result = original_tail(scope_kind=scope_kind, scope_ref=scope_ref)
            tail_read.set()
            self.assertTrue(snapshot_committed.wait(timeout=2))
            return result

        def observed_notify(*, host_ref: str | None = None, thread_ref: str | None = None) -> None:
            snapshot_committed.set()
            original_notify(host_ref=host_ref, thread_ref=thread_ref)

        self.desktop._event_tail = paused_tail  # type: ignore[method-assign]
        self.desktop._notify_change = observed_notify  # type: ignore[method-assign]

        def update_snapshot() -> None:
            self.assertTrue(tail_read.wait(timeout=2))
            self.desktop.receive(
                "desktop_snapshot",
                snapshot(title="committed while stream opens", revision=2),
            )

        worker = threading.Thread(target=update_snapshot)
        worker.start()
        stream = self.desktop.thread_stream(THREAD_REF, after_cursor=None, heartbeat_seconds=1)
        ready = next(stream)
        changed = next(stream)
        stream.close()
        worker.join(timeout=2)

        self.assertFalse(worker.is_alive())
        self.assertEqual(ready["event"], "ready")
        self.assertEqual(changed["event"], "desktop")
        self.assertTrue(changed["data"]["changed"])
        self.assertEqual(changed["data"]["events"], [])

    def test_resume_cursor_validation_fails_closed(self) -> None:
        cursor = self.desktop.receive("desktop_event", desktop_event(1))["cursor"]
        cases = [
            (
                f"/api/desktop/v1/stream?host_ref={HOST_REF}&after_cursor=01",
                None,
                400,
                "desktop_cursor_invalid",
            ),
            (
                f"/api/desktop/v1/stream?host_ref={HOST_REF}&after_cursor=0",
                {"Last-Event-ID": str(cursor)},
                409,
                "desktop_cursor_conflict",
            ),
            (
                f"/api/desktop/v1/threads/{THREAD_REF}/stream?after_cursor={cursor + 1}",
                None,
                200,
                None,
            ),
        ]
        for path, headers, expected_status, expected_code in cases:
            with self.subTest(path=path):
                if expected_code is None:
                    connection, response = self.open_stream(path, headers=headers)
                    try:
                        self.assertEqual(response.status, expected_status)
                        ready = read_sse_event(response)
                        self.assertTrue(ready["data"]["resync_required"])
                    finally:
                        connection.close()
                else:
                    status, result = self.request_json(path, headers=headers)
                    self.assertEqual(status, expected_status)
                    self.assertEqual(result["error"]["code"], expected_code)

    def test_explicit_old_cursor_is_sent_in_bounded_batches(self) -> None:
        for sequence in range(1, 106):
            self.desktop.receive("desktop_event", desktop_event(sequence))
        stream = self.desktop.thread_stream(THREAD_REF, after_cursor=0)
        ready = next(stream)
        first_page = next(stream)
        second_page = next(stream)
        stream.close()

        self.assertEqual(ready["cursor"], 0)
        self.assertEqual(len(first_page["data"]["events"]), 100)
        self.assertTrue(first_page["data"]["has_more"])
        self.assertEqual(len(second_page["data"]["events"]), 5)
        self.assertFalse(second_page["data"]["has_more"])

    def test_host_data_freshness_is_independent_from_runner_connectivity(self) -> None:
        delayed_now = NOW + dt.timedelta(seconds=20)
        stale_now = NOW + dt.timedelta(seconds=31)
        delayed = DesktopControllerService(
            self.store,
            publisher=None,
            now=lambda: delayed_now,
            runner_status_provider=lambda _runner_id: {
                "connectivity_state": "online",
                "last_heartbeat_at": delayed_now.isoformat(),
            },
        ).hosts()["hosts"][0]
        stale = DesktopControllerService(
            self.store,
            publisher=None,
            now=lambda: stale_now,
            runner_status_provider=lambda _runner_id: {
                "connectivity_state": "online",
                "last_heartbeat_at": stale_now.isoformat(),
            },
        ).hosts()["hosts"][0]

        self.assertTrue(delayed["online"])
        self.assertEqual(delayed["data_age_seconds"], 20)
        self.assertEqual(delayed["data_freshness_state"], "delayed")
        self.assertTrue(stale["online"])
        self.assertEqual(stale["data_age_seconds"], 31)
        self.assertEqual(stale["data_freshness_state"], "stale")

    def test_recent_thread_list_uses_offset_cursor_and_keeps_old_default(self) -> None:
        newer_thread = "TH-" + "F" * 20
        newest_thread = "TH-" + "G" * 20
        self.desktop.receive(
            "desktop_snapshot",
            snapshot(
                thread_ref=newer_thread,
                turn_ref="TR-" + "H" * 20,
                title="newer",
                updated_at=NOW + dt.timedelta(minutes=1),
            ),
        )
        self.desktop.receive(
            "desktop_snapshot",
            snapshot(
                thread_ref=newest_thread,
                turn_ref="TR-" + "J" * 20,
                title="newest",
                updated_at=NOW + dt.timedelta(minutes=2),
            ),
        )

        status, first = self.request_json("/api/desktop/v1/threads?order=recent&limit=2")
        self.assertEqual(status, 200)
        self.assertEqual(
            [item["thread_ref"] for item in first["result"]["threads"]],
            [newest_thread, newer_thread],
        )
        self.assertEqual(first["result"]["next_cursor"], 2)
        self.assertTrue(first["result"]["has_more"])

        status, second = self.request_json(
            "/api/desktop/v1/threads?order=recent&limit=2&cursor=2"
        )
        self.assertEqual(status, 200)
        self.assertEqual(
            [item["thread_ref"] for item in second["result"]["threads"]],
            [THREAD_REF],
        )
        self.assertEqual(second["result"]["next_cursor"], 3)
        self.assertFalse(second["result"]["has_more"])

        status, legacy = self.request_json("/api/desktop/v1/threads?limit=2")
        self.assertEqual(status, 200)
        self.assertEqual(legacy["result"]["threads"][0]["thread_ref"], THREAD_REF)


if __name__ == "__main__":
    unittest.main()
