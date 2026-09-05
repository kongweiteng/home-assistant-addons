"""Bounded directory responses, validation, status invalidation and legacy compatibility."""

import copy
from http.client import HTTPConnection
import json
import threading
from types import SimpleNamespace
import unittest
from urllib.parse import urlencode

from codex_controller.api import create_server
from codex_controller.service import ControllerService
from codex_controller.store import StoreError
from codex_controller.tool_catalog import TOOL_DEFINITIONS
from codex_controller.tool_catalog_page import SERVICES, parse_page_query, tool_catalog_page, with_catalog_revision


def fixture():
    return {
        "revision": 17, "policy_error": None,
        "mcp": {"observed_at": "2026-09-06T12:00:00+08:00", "current": True},
        "summary": {"known": 53, "configured": 53, "enabled": 50, "published": 50, "callable": 50},
        "tools": [{"name": f"tool_{index:02}", "display_name": f"工具 {index:02}",
                   "service": "renovation_hub" if index < 30 else "ha_operations_broker",
                   "risk_type": "write" if index % 2 else "read_only", "callable": index < 50,
                   "enabled": index < 50, "intent_examples": [f"查询 {index:02}", "Alpha"],
                   "last_invocation": None} for index in range(53)],
    }


class ToolCatalogPagingTests(unittest.TestCase):
    def test_bounded_pages_cover_directory_without_duplicates_and_keep_cas_revision(self):
        source = fixture()
        names = []
        offset = 0
        while True:
            result = tool_catalog_page(source, f"limit=12&offset={offset}")
            self.assertLessEqual(len(result["tools"]), 12)
            self.assertEqual(result["revision"], 17)
            self.assertEqual(result["page"]["total"], 53)
            self.assertEqual(result["page"]["callable"], 50)
            names.extend(tool["name"] for tool in result["tools"])
            offset = result["page"]["next_offset"]
            if offset is None:
                break
        self.assertEqual(len(names), 53)
        self.assertEqual(len(set(names)), 53)
        self.assertEqual(len(source["tools"]), 53)

    def test_server_side_search_service_and_risk_filters(self):
        for search in ("查询 52", "TOOL_52", "工具 52"):
            page = tool_catalog_page(fixture(), urlencode({"search": search, "limit": 12}))
            self.assertEqual([tool["name"] for tool in page["tools"]], ["tool_52"])
        page = tool_catalog_page(fixture(), "service=renovation_hub&risk=write&search=ALPHA")
        self.assertEqual(page["page"]["total"], 15)
        self.assertTrue(all(tool["service"] == "renovation_hub" and tool["risk_type"] == "write"
                            for tool in page["tools"]))
        self.assertEqual(tool_catalog_page(fixture(), "search=Operations")["page"]["total"], 23)
        self.assertTrue({tool.service for tool in TOOL_DEFINITIONS}.issubset(SERVICES))
        for service in ("family_memo", "home_assistant_prepare_car"):
            source = fixture()
            source["tools"][0]["service"] = service
            result = tool_catalog_page(source, "service=" + service)
            self.assertEqual([tool["name"] for tool in result["tools"]], ["tool_00"])

    def test_empty_and_deleted_last_page_normalize_offsets(self):
        empty = tool_catalog_page(fixture(), "search=missing&offset=48")
        self.assertEqual(empty["tools"], [])
        self.assertEqual(empty["page"]["offset"], 0)
        self.assertIsNone(empty["page"]["next_offset"])
        result = tool_catalog_page(fixture(), "offset=999&limit=12")
        self.assertEqual(result["page"]["offset"], 48)
        self.assertEqual(len(result["tools"]), 5)

    def test_query_validation_rejects_unknown_empty_duplicate_and_unbounded_fields(self):
        for query in ("limit=0", "limit=49", "limit=-1", "limit=1.5", "limit=01", "offset=-1",
                      "offset=1000001", "limit=12&limit=12", "service=unknown", "risk=admin",
                      "search=", "wat=1", "offset", "search=" + "x" * 201,
                      "search=abc%00", "catalog_revision=17", "service=", "search=" + "x" * 4096):
            with self.subTest(query=query[:70]), self.assertRaises(StoreError) as caught:
                parse_page_query(query)
            self.assertEqual(caught.exception.status, 400)

    def test_catalog_revision_ignores_heartbeats_but_tracks_visible_changes(self):
        source = fixture()
        original = with_catalog_revision(source)["catalog_revision"]
        heartbeat = copy.deepcopy(source)
        heartbeat["mcp"]["observed_at"] = "2026-09-06T12:00:05+08:00"
        heartbeat["tools"].reverse()
        self.assertEqual(with_catalog_revision(heartbeat)["catalog_revision"], original)
        for key, value in (("enabled", False), ("callable", False), ("display_name", "Updated"),
                           ("last_invocation", {"outcome": "failed", "created_at": "now"})):
            changed = copy.deepcopy(source)
            changed["tools"][0][key] = value
            self.assertNotEqual(with_catalog_revision(changed)["catalog_revision"], original)
        changed = copy.deepcopy(source)
        changed["revision"] = 18
        with self.assertRaises(StoreError) as caught:
            tool_catalog_page(changed, "catalog_revision=" + original)
        self.assertEqual(caught.exception.code, "tool_catalog_changed")
        self.assertEqual(caught.exception.status, 409)

    def test_service_exposes_revision_in_status_summary_without_changing_policy_revision(self):
        source = fixture()
        service = object.__new__(ControllerService)
        service.tool_context = SimpleNamespace(store=object(), tool_status=lambda: source)
        result = service.tool_status()
        self.assertEqual(result["summary"]["catalog_revision"], result["catalog_revision"])
        self.assertEqual(result["revision"], 17)
        self.assertNotIn("catalog_revision", source["summary"])
        service.tool_context = None
        service.store = SimpleNamespace(tool_control_document=lambda *_: source)
        self.assertEqual(service.tool_status()["catalog_revision"], result["catalog_revision"])

    def test_http_legacy_compatibility_and_paged_errors(self):
        source = fixture()
        calls = []
        service = SimpleNamespace(tool_status=lambda: calls.append(True) or source)
        server = create_server("127.0.0.1", 0, service=service, api_token="fixture-only" * 4,
                               max_request_bytes=1024)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        connection = HTTPConnection("127.0.0.1", server.server_port, timeout=3)
        try:
            for path, expected_status, expected_count in (
                ("/api/tools", 200, 53), ("/api/tools?limit=12", 200, 12),
                ("/api/tools?limit=48", 200, 48), ("/api/tools?offset=48&limit=12", 200, 5),
                ("/api/tools?service=unknown", 400, None),
                ("/api/tools?catalog_revision=" + "0" * 64, 409, None),
            ):
                connection.request("GET", path)
                response = connection.getresponse()
                document = json.loads(response.read())
                self.assertEqual(response.status, expected_status)
                if expected_count is not None:
                    self.assertEqual(len(document["result"]["tools"]), expected_count)
                    self.assertEqual(document["result"]["revision"], 17)
                if path == "/api/tools":
                    self.assertEqual(document, {"version": 1, "result": source})
            self.assertEqual(len(calls), 5)  # invalid filters fail before reading the directory
        finally:
            connection.close()
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)


if __name__ == "__main__":
    unittest.main()
