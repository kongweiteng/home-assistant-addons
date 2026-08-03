from __future__ import annotations

from pathlib import Path
import tempfile
import threading
import unittest
from urllib.request import urlopen

from renovation_hub.api import create_server, dispatch_tool
from renovation_hub.hub import RenovationHubStore
from renovation_hub.ledger import LedgerError


def key(label: str) -> str:
    return f"fixture-{label}-" + "0" * 24


class RenovationHubDomainTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        self.store = RenovationHubStore(root / "data" / "hub.sqlite3", data_dir=root / "data", share_dir=root / "share")
        self.store.set_writer_mode("read_only", force_initial=True)
        self.store.set_writer_mode("shadow_validated")
        self.store.set_writer_mode("cutover_ready")
        self.store.set_writer_mode("primary_writer")
        self.project = self.store.create_project(
            {
                "idempotency_key": key("project"),
                "name": "示例装修项目",
                "budget_cents": 1_000_000,
            }
        )["project"]

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_project_is_idempotent_and_version_conflict_is_rejected(self) -> None:
        replay = self.store.create_project(
            {
                "idempotency_key": key("project"),
                "name": "示例装修项目",
                "budget_cents": 1_000_000,
            }
        )
        self.assertTrue(replay["idempotent_replay"])
        updated = self.store.update_project(
            {
                "idempotency_key": key("project-update"),
                "project_id": self.project["id"],
                "version": 1,
                "changes": {"budget_cents": 1_200_000},
            }
        )["project"]
        self.assertEqual(updated["version"], 2)
        with self.assertRaises(LedgerError) as context:
            self.store.update_project(
                {
                    "idempotency_key": key("project-stale"),
                    "project_id": self.project["id"],
                    "version": 1,
                    "changes": {"name": "过期覆盖"},
                }
            )
        self.assertEqual(context.exception.code, "version_conflict")

    def test_only_one_active_stage_and_cross_project_refs_are_rejected(self) -> None:
        active = self.store.create_stage(
            {
                "idempotency_key": key("stage-active"),
                "project_id": self.project["id"],
                "name": "水电工程",
                "status": "active",
            }
        )["stage"]
        with self.assertRaises(LedgerError) as context:
            self.store.create_stage(
                {
                    "idempotency_key": key("stage-conflict"),
                    "project_id": self.project["id"],
                    "name": "泥木工程",
                    "status": "active",
                }
            )
        self.assertEqual(context.exception.code, "stage_active_conflict")
        area = self.store.create_area(
            {
                "idempotency_key": key("area"),
                "project_id": self.project["id"],
                "name": "厨房",
            }
        )["area"]
        event = self.store.create_event(
            {
                "idempotency_key": key("event"),
                "project_id": self.project["id"],
                "stage_id": active["id"],
                "area_id": area["id"],
                "title": "水电定位完成",
                "occurred_at": "2026-08-03T10:00:00+08:00",
            }
        )["event"]
        self.assertEqual(event["occurred_at"], "2026-08-03T02:00:00Z")
        self.assertEqual(self.store.timeline({"project_id": self.project["id"]})[0]["area_name"], "厨房")

    def test_dashboard_and_dispatch_share_the_same_store(self) -> None:
        result = dispatch_tool(
            self.store,
            {
                "name": "renovation_area_create",
                "actor_hash": "sha256:fixture",
                "arguments": {
                    "idempotency_key": key("dispatch-area"),
                    "project_id": self.project["id"],
                    "name": "客厅",
                },
            },
        )
        self.assertEqual(result["area"]["name"], "客厅")
        dashboard = self.store.dashboard(self.project["id"])
        self.assertEqual(dashboard["counts"]["areas"], 1)
        self.assertEqual(dashboard["budget_remaining_cents"], 1_000_000)
        status = self.store.status()
        self.assertEqual(status["version"], "0.1.1")
        self.assertEqual(status["hub_schema_version"], 1)
        self.assertGreaterEqual(status["counts"]["audit_events"], 2)

    def test_existing_ledger_database_is_extended_without_losing_transactions(self) -> None:
        payment = self.store.add_payment(
            {
                "idempotency_key": key("payment"),
                "amount_cents": 12345,
                "occurred_on": "2026-08-03",
                "main_category": "水电工程",
            }
        )["transaction"]
        reopened = RenovationHubStore(self.store.database_path, data_dir=self.store.data_dir, share_dir=self.store.share_dir)
        self.assertEqual(reopened.show(payment["id"])["amount_cents"], 12345)
        self.assertEqual(reopened.metadata()["format_id"], "kanhuwan-renovation-ledger@1")

    def test_read_api_exposes_projects_and_dashboard(self) -> None:
        server = create_server("127.0.0.1", 0, store=self.store, api_token="t" * 32, max_request_bytes=1024 * 1024)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with urlopen(f"http://127.0.0.1:{server.server_port}/api/v1/projects", timeout=2) as response:
                self.assertEqual(response.status, 200)
                self.assertIn(self.project["id"].encode(), response.read())
            with urlopen(f"http://127.0.0.1:{server.server_port}/api/v1/dashboard?project_id={self.project['id']}", timeout=2) as response:
                self.assertEqual(response.status, 200)
                self.assertIn(b"budget_remaining_cents", response.read())
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)


if __name__ == "__main__":
    unittest.main()
