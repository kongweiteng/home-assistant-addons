from __future__ import annotations

import ast
from dataclasses import replace
import hashlib
from http import HTTPStatus
from pathlib import Path
import json
import tempfile
import threading
import unittest
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from renovation_hub.api import create_server, dispatch_tool
from renovation_hub.business_tools import (
    BUSINESS_ACTION_EXCLUSIONS,
    BUSINESS_TOOL_REGISTRY,
    business_action_coverage,
    business_manifest,
    validate_business_tool_registry,
    validate_public_business_actions,
)
from renovation_hub.hub import RenovationHubStore
from renovation_hub.ledger import LedgerError
from renovation_hub.media import MediaService
from renovation_hub.portable import MAX_GROUPED_TAG_LENGTH, MAX_GROUPED_TAGS, TAG_DIMENSIONS


ROOT = Path(__file__).resolve().parents[1]


def key(label: str) -> str:
    return f"dynamic-mcp-{label}-" + "0" * 24


class BusinessManifestTests(unittest.TestCase):
    def test_manifest_is_complete_deterministic_and_self_verifying(self) -> None:
        manifest = business_manifest()
        self.assertEqual(
            set(manifest),
            {
                "version",
                "service",
                "scope",
                "catalog_revision",
                "catalog_digest",
                "tools",
            },
        )
        self.assertEqual(manifest["version"], 1)
        self.assertEqual(manifest["service"], "renovation_hub")
        self.assertEqual(manifest["scope"], "business")
        self.assertGreater(manifest["catalog_revision"], 0)
        self.assertEqual(len(manifest["tools"]), 31)
        names = [tool["name"] for tool in manifest["tools"]]
        self.assertEqual(names, sorted(names))
        self.assertEqual(
            {"renovation_search", "renovation_media_list", "renovation_media_show", "renovation_mutate"} - set(names),
            set(),
        )

        unsigned = {key: value for key, value in manifest.items() if key != "catalog_digest"}
        canonical = json.dumps(
            unsigned,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        self.assertEqual(
            manifest["catalog_digest"],
            "sha256:" + hashlib.sha256(canonical).hexdigest(),
        )
        self.assertEqual(manifest, business_manifest())

        expected_fields = {
            "name",
            "display_name",
            "description",
            "risk_type",
            "transport",
            "exposure",
            "requires_job_context",
            "idempotent_write",
            "inputSchema",
            "annotations",
        }
        for tool in manifest["tools"]:
            self.assertEqual(set(tool), expected_fields)
            self.assertIn(tool["risk_type"], {"read", "write"})
            self.assertIn(
                tool["transport"],
                {"json", "gateway_attachment", "gateway_media_stream"},
            )
            self.assertEqual(tool["exposure"], "mcp")
            self.assertEqual(tool["inputSchema"]["type"], "object")
            self.assertFalse(tool["inputSchema"]["additionalProperties"])
            self.assertEqual(
                tool["annotations"]["readOnlyHint"],
                tool["risk_type"] == "read",
            )
            self.assertFalse(tool["annotations"]["openWorldHint"])

        transports = {tool["name"]: tool["transport"] for tool in manifest["tools"]}
        self.assertEqual(transports["ledger_attach"], "gateway_attachment")
        self.assertEqual(transports["renovation_media_ingest"], "gateway_media_stream")
        self.assertTrue(
            all(
                transport == "json"
                for name, transport in transports.items()
                if name not in {"ledger_attach", "renovation_media_ingest"}
            )
        )

        payment_schema = next(
            tool["inputSchema"]
            for tool in manifest["tools"]
            if tool["name"] == "ledger_add_payment"
        )
        self.assertEqual(
            set(payment_schema["required"]),
            {"amount_cents", "occurred_on", "grouped_tags"},
        )
        self.assertEqual(
            set(payment_schema["properties"]),
            {
                "amount_cents",
                "occurred_on",
                "grouped_tags",
                "merchant",
                "note",
                "is_deposit",
                "source_ref",
                "project_id",
                "stage_id",
                "area_id",
            },
        )
        for legacy in (
            "amount",
            "date",
            "category",
            "description",
            "main_category",
            "tags",
            "ledger_format_version",
        ):
            self.assertNotIn(legacy, payment_schema["properties"])
        grouped_tags = payment_schema["properties"]["grouped_tags"]
        self.assertEqual(grouped_tags["type"], "object")
        self.assertFalse(grouped_tags["additionalProperties"])
        self.assertEqual(set(grouped_tags["properties"]), set(TAG_DIMENSIONS))
        for dimension in TAG_DIMENSIONS:
            values = grouped_tags["properties"][dimension]
            self.assertEqual(values["type"], "array")
            self.assertEqual(values["maxItems"], MAX_GROUPED_TAGS)
            self.assertTrue(values["uniqueItems"])
            self.assertEqual(values["items"]["maxLength"], MAX_GROUPED_TAG_LENGTH)

    def test_registry_rejects_namespace_schema_and_duplicate_drift(self) -> None:
        first = BUSINESS_TOOL_REGISTRY[0]
        with self.assertRaisesRegex(ValueError, "invalid business namespace"):
            validate_business_tool_registry((replace(first, name="ha_operations_attack"),))
        with self.assertRaisesRegex(ValueError, "input schema must reject"):
            validate_business_tool_registry(
                (replace(first, input_schema={"type": "object"}),)
            )
        with self.assertRaisesRegex(ValueError, "duplicate business tool"):
            validate_business_tool_registry((first, first))
        with self.assertRaisesRegex(ValueError, "write context and idempotency"):
            validate_business_tool_registry(
                (replace(first, requires_job_context=False),)
            )
        media_stream = next(
            item for item in BUSINESS_TOOL_REGISTRY if item.name == "renovation_media_ingest"
        )
        with self.assertRaisesRegex(ValueError, "unsupported media stream tool"):
            validate_business_tool_registry(
                (replace(media_stream, name="renovation_future_stream"),)
            )

    def test_public_business_routes_are_mapped_or_excluded_with_reason(self) -> None:
        source_path = ROOT / "renovation_hub" / "renovation_hub" / "web.py"
        tree = ast.parse(source_path.read_text(encoding="utf-8"))
        action_ids: list[str] = []
        direct_business_routes: list[str] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign) and any(
                isinstance(target, ast.Name) and target.id == "business_routes"
                for target in node.targets
            ):
                if isinstance(node.value, (ast.Tuple, ast.List)):
                    for item in node.value.elts:
                        if isinstance(item, (ast.Tuple, ast.List)) and len(item.elts) == 4:
                            action = item.elts[3]
                            if isinstance(action, ast.Constant) and isinstance(action.value, str):
                                action_ids.append(action.value)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                if node.func.attr not in {"add_get", "add_post", "add_patch", "add_put", "add_route"}:
                    continue
                if not node.args or not isinstance(node.args[0], ast.Constant):
                    continue
                path = node.args[0].value
                if isinstance(path, str) and path.startswith("/api/v1/"):
                    direct_business_routes.append(path)

        self.assertEqual(direct_business_routes, [])
        self.assertGreater(len(action_ids), 20)
        validate_public_business_actions(action_ids)
        coverage = business_action_coverage()
        self.assertEqual(set(action_ids) - set(coverage), set())
        for exclusion in BUSINESS_ACTION_EXCLUSIONS:
            self.assertGreater(len(exclusion.reason.strip()), 10)
            self.assertEqual(coverage[exclusion.action_id]["exposure"], "excluded")

        names = {definition.name for definition in BUSINESS_TOOL_REGISTRY}
        for forbidden in ("sql", "shell", "cutover", "writer", "restore", "purge"):
            self.assertFalse(any(forbidden in name for name in names))


class BusinessDispatchTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        self.store = RenovationHubStore(
            root / "data" / "hub.sqlite3",
            data_dir=root / "data",
            share_dir=root / "share",
        )
        self.store.set_writer_mode("read_only", force_initial=True)
        self.store.set_writer_mode("shadow_validated")
        self.store.set_writer_mode("cutover_ready")
        self.store.set_writer_mode("primary_writer")
        self.media = MediaService(
            self.store,
            media_root=root / "media",
            preview_root=root / "previews",
            staging_root=root / "staging",
            max_media_bytes=50 * 1024 * 1024,
        )
        self.project = self.store.create_project(
            {"idempotency_key": key("project"), "name": "厨房改造"}
        )["project"]
        self.payment = self.store.add_payment(
            {
                "idempotency_key": key("payment"),
                "amount_cents": 12_500,
                "occurred_on": "2026-08-05",
                "main_category": "主材",
                "merchant": "示例商家",
                "note": "厨房瓷砖",
                "project_id": self.project["id"],
            }
        )["transaction"]
        self.event = self.store.create_event(
            {
                "idempotency_key": key("event"),
                "project_id": self.project["id"],
                "title": "厨房防水验收",
                "occurred_at": "2026-08-05T09:30:00+08:00",
            }
        )["event"]
        now = "2026-08-05T01:30:00Z"
        with self.store._connect() as connection:
            connection.execute(
                """
                INSERT INTO media_assets(
                    id,project_id,media_type,mime_type,original_filename,storage_name,
                    preview_name,size_bytes,sha256,width,height,duration_ms,captured_at,
                    uploaded_at,source,source_ref_hash,processing_status,error_code,
                    created_at,updated_at
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    "media-fixture",
                    self.project["id"],
                    "image",
                    "image/jpeg",
                    "厨房防水.jpg",
                    "aa/internal-original.jpg",
                    "internal-preview.webp",
                    1024,
                    "a" * 64,
                    640,
                    360,
                    None,
                    now,
                    now,
                    "fixture",
                    "private-source-reference",
                    "ready",
                    None,
                    now,
                    now,
                ),
            )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_search_media_list_and_media_show_have_stable_public_results(self) -> None:
        arguments = {"project_id": self.project["id"], "keyword": "厨房"}
        search = dispatch_tool(
            self.store,
            {"name": "renovation_search", "arguments": arguments},
            media=self.media,
        )
        self.assertEqual([item["id"] for item in search["ledger"]], [self.payment["id"]])
        self.assertEqual([item["id"] for item in search["timeline"]], [self.event["id"]])
        self.assertEqual([item["id"] for item in search["media"]], ["media-fixture"])

        listed = dispatch_tool(
            self.store,
            {"name": "renovation_media_list", "arguments": arguments},
            media=self.media,
        )
        shown = dispatch_tool(
            self.store,
            {"name": "renovation_media_show", "arguments": {"media_id": "media-fixture"}},
            media=self.media,
        )
        self.assertEqual(listed["items"], [shown])
        self.assertEqual(shown["content_url"], "/api/v1/media/media-fixture/content")
        self.assertNotIn("storage_name", shown)
        self.assertNotIn("preview_name", shown)
        self.assertNotIn("source_ref_hash", shown)

        with self.store._connect() as connection:
            connection.execute(
                """
                INSERT INTO media_assets(
                    id,project_id,media_type,mime_type,original_filename,storage_name,
                    preview_name,size_bytes,sha256,width,height,duration_ms,captured_at,
                    uploaded_at,source,source_ref_hash,processing_status,error_code,
                    created_at,updated_at
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    "media-newer-nonmatch",
                    self.project["id"],
                    "image",
                    "image/jpeg",
                    "客厅吊顶.jpg",
                    "bb/internal-original.jpg",
                    None,
                    512,
                    "b" * 64,
                    320,
                    180,
                    None,
                    "2026-08-05T02:00:00Z",
                    "2026-08-05T02:00:00Z",
                    "fixture",
                    "other-private-source",
                    "ready",
                    None,
                    "2026-08-05T02:00:00Z",
                    "2026-08-05T02:00:00Z",
                ),
            )
        limited = dispatch_tool(
            self.store,
            {
                "name": "renovation_media_list",
                "arguments": {"project_id": self.project["id"], "keyword": "厨房", "limit": 1},
            },
            media=self.media,
        )
        self.assertEqual([item["id"] for item in limited["items"]], ["media-fixture"])

        empty = dispatch_tool(
            self.store,
            {
                "name": "renovation_media_list",
                "arguments": {"project_id": self.project["id"], "keyword": "卫生间"},
            },
            media=self.media,
        )
        self.assertEqual(empty, {"items": []})

    def test_dispatch_errors_are_deterministic_and_fail_closed(self) -> None:
        with self.assertRaises(LedgerError) as context:
            dispatch_tool(self.store, {"name": "ledger_query", "arguments": []})
        self.assertEqual(context.exception.code, "invalid_input")
        self.assertEqual(context.exception.status, 400)

        with self.assertRaises(LedgerError) as context:
            dispatch_tool(self.store, {"name": "renovation_missing", "arguments": {}})
        self.assertEqual(context.exception.code, "unknown_tool")
        self.assertEqual(context.exception.status, 404)

        with self.assertRaises(LedgerError) as context:
            dispatch_tool(
                self.store,
                {
                    "name": "renovation_media_show",
                    "arguments": {"media_id": "media-fixture"},
                },
            )
        self.assertEqual(context.exception.code, "media_service_unavailable")
        self.assertEqual(context.exception.status, 503)

        with self.assertRaises(LedgerError) as context:
            dispatch_tool(
                self.store,
                {
                    "name": "renovation_media_ingest",
                    "arguments": {
                        "attachment_ref": "fixture-ref",
                        "project_id": self.project["id"],
                    },
                },
                media=self.media,
            )
        self.assertEqual(context.exception.code, "transport_required")
        self.assertEqual(context.exception.status, 409)

        with self.assertRaises(LedgerError) as context:
            dispatch_tool(
                self.store,
                {
                    "name": "renovation_media_show",
                    "arguments": {"media_id": "missing"},
                },
                media=self.media,
            )
        self.assertEqual(context.exception.code, "media_not_found")
        self.assertEqual(context.exception.status, 404)

    def test_payment_dispatch_is_v2_only_and_rejects_legacy_shapes_before_write(self) -> None:
        created = dispatch_tool(
            self.store,
            {
                "name": "ledger_add_payment",
                "actor_hash": "sha256:fixture",
                "arguments": {
                    "idempotency_key": key("payment-v2"),
                    "amount_cents": 23_800,
                    "occurred_on": "2026-08-05",
                    "grouped_tags": {
                        "主题": ["主材"],
                        "空间": ["厨房"],
                        "专业": ["泥瓦"],
                    },
                    "merchant": "示例供应商",
                    "note": "厨房墙砖",
                    "project_id": self.project["id"],
                },
            },
        )["transaction"]
        self.assertEqual(created["ledger_format_version"], 2)
        self.assertEqual(created["main_category"], "")
        self.assertEqual(created["grouped_tags"]["主题"], ["主材"])

        with self.store._connect() as connection:
            baseline = connection.execute(
                "SELECT (SELECT COUNT(*) FROM transactions), (SELECT COUNT(*) FROM audit_log)"
            ).fetchone()

        invalid_cases = (
            (
                {
                    "idempotency_key": key("legacy-shape"),
                    "amount": "238.00",
                    "date": "2026-08-05",
                    "category": "主材",
                    "description": "厨房墙砖",
                },
                "invalid_input",
            ),
            (
                {
                    "idempotency_key": key("missing-grouped-tags"),
                    "amount_cents": 23_800,
                    "occurred_on": "2026-08-05",
                },
                "invalid_input",
            ),
            (
                {
                    "idempotency_key": key("unknown-dimension"),
                    "amount_cents": 23_800,
                    "occurred_on": "2026-08-05",
                    "grouped_tags": {"自定义维度": ["主材"]},
                },
                "invalid_tags",
            ),
        )
        for arguments, expected_code in invalid_cases:
            with self.subTest(expected_code=expected_code), self.assertRaises(LedgerError) as context:
                dispatch_tool(
                    self.store,
                    {
                        "name": "ledger_add_payment",
                        "actor_hash": "sha256:fixture",
                        "arguments": arguments,
                    },
                )
            self.assertEqual(context.exception.code, expected_code)
            with self.store._connect() as connection:
                current = connection.execute(
                    "SELECT (SELECT COUNT(*) FROM transactions), (SELECT COUNT(*) FROM audit_log)"
                ).fetchone()
            self.assertEqual(tuple(current), tuple(baseline))

    def test_mutation_dispatch_supports_preview_and_apply(self) -> None:
        payment = self.store.add_payment(
            {
                "idempotency_key": key("dispatch-unscoped"),
                "amount_cents": 7600,
                "occurred_on": "2026-08-05",
                "main_category": "主材",
            }
        )["transaction"]
        arguments = {
            "mode": "preview",
            "target_type": "transaction",
            "target_ids": [payment["id"]],
            "patch": {"project_id": self.project["id"]},
            "reason": "动态工具补充项目归属",
        }
        preview = dispatch_tool(
            self.store,
            {"name": "renovation_mutate", "actor_hash": "sha256:fixture", "arguments": arguments},
        )
        applied = dispatch_tool(
            self.store,
            {
                "name": "renovation_mutate",
                "actor_hash": "sha256:fixture",
                "arguments": {
                    **arguments,
                    "mode": "apply",
                    "preview_digest": preview["preview_digest"],
                    "confirmed": True,
                    "idempotency_key": key("dispatch-mutation-apply"),
                },
            },
        )
        self.assertEqual(applied["items"][0]["context"]["project_id"], self.project["id"])


class ManifestEndpointTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        self.store = RenovationHubStore(
            root / "data" / "hub.sqlite3",
            data_dir=root / "data",
            share_dir=root / "share",
        )
        self.token = "m" * 32
        self.server = create_server(
            "127.0.0.1",
            0,
            store=self.store,
            api_token=self.token,
            max_request_bytes=1024 * 1024,
        )
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.temporary.cleanup()

    def test_manifest_requires_private_bearer_and_returns_direct_document(self) -> None:
        url = f"http://127.0.0.1:{self.server.server_port}/internal/v1/mcp/manifest"
        with self.assertRaises(HTTPError) as context:
            urlopen(url, timeout=2)
        self.assertEqual(context.exception.code, HTTPStatus.UNAUTHORIZED)

        request = Request(url, headers={"Authorization": f"Bearer {self.token}"})
        with urlopen(request, timeout=2) as response:
            self.assertEqual(response.status, HTTPStatus.OK)
            payload = json.loads(response.read())
        self.assertEqual(payload, business_manifest())
        self.assertNotIn("result", payload)


if __name__ == "__main__":
    unittest.main()
