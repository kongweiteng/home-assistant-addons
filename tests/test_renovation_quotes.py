from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from renovation_hub.hub import RenovationHubStore
from renovation_hub.ledger import LedgerError


def key(label: str) -> str:
    return f"quote-{label}-" + "0" * 32


class RenovationQuoteTests(unittest.TestCase):
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
        self.project = self.store.create_project(
            {"idempotency_key": key("project"), "name": "报价测试项目"}
        )["project"]

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def create_quote(self) -> dict:
        return self.store.create_quote(
            {
                "idempotency_key": key("request"),
                "project_id": self.project["id"],
                "title": "厨房墙砖",
                "category": "主材",
                "description": "哑光暖灰色",
                "specification": {"尺寸": "600×1200mm", "表面": "柔光"},
                "quantity_milli": 42_000,
                "unit": "片",
                "follow_up_at": "2026-08-24T09:30:00+08:00",
            }
        )["quote"]

    def test_quote_offer_compare_select_and_ledger_boundary(self) -> None:
        quote = self.create_quote()
        self.assertEqual(quote["follow_up_at"], "2026-08-24T01:30:00Z")
        replay = self.store.create_quote(
            {
                "idempotency_key": key("request"),
                "project_id": self.project["id"],
                "title": "厨房墙砖",
                "category": "主材",
                "description": "哑光暖灰色",
                "specification": {"尺寸": "600×1200mm", "表面": "柔光"},
                "quantity_milli": 42_000,
                "unit": "片",
                "follow_up_at": "2026-08-24T09:30:00+08:00",
            }
        )
        self.assertTrue(replay["idempotent_replay"])
        self.assertEqual(replay["quote"]["id"], quote["id"])

        first = self.store.add_quote_offer(
            {
                "idempotency_key": key("offer-a"),
                "request_id": quote["id"],
                "supplier_name": "示例建材一店",
                "contact_name": "王经理",
                "contact_phone": "13800000000",
                "supplier_address": "示例路 88 号",
                "subtotal_cents": 12_000,
                "shipping_cents": 1_000,
                "discount_cents": 500,
                "quantity_milli": 42_000,
                "unit": "片",
                "brand": "示例陶瓷",
                "model": "WG-612",
                "specification": {"尺寸": "600×1200mm", "等级": "优等品"},
                "lead_time_days": 3,
                "status": "review_required",
                "extraction_confidence": 82,
            }
        )["offer"]
        self.assertEqual(first["total_cents"], 12_500)
        self.assertEqual(first["unit_price_cents"], 298)

        second = self.store.add_quote_offer(
            {
                "idempotency_key": key("offer-b"),
                "request_id": quote["id"],
                "supplier_name": "示例建材二店",
                "total_cents": 11_800,
                "quantity_milli": 42_000,
                "unit": "片",
                "valid_until": "2099-12-31",
            }
        )["offer"]
        compared = self.store.compare_quote(quote["id"])
        self.assertEqual(compared["best_offer_id"], second["id"])
        self.assertTrue(compared["unit_prices_comparable"])
        self.assertEqual(len(compared["offers"]), 2)
        self.assertEqual(compared["quote"]["supplier_names"], ["示例建材一店", "示例建材二店"])

        updated = self.store.update_quote_offer(
            {
                "idempotency_key": key("offer-a-update"),
                "offer_id": first["id"],
                "version": first["version"],
                "changes": {
                    "shipping_cents": 2_000,
                    "quantity_milli": 40_000,
                    "total_cents": first["total_cents"],
                },
            }
        )["offer"]
        self.assertEqual(updated["total_cents"], 13_500)
        self.assertEqual(updated["unit_price_cents"], 338)
        with self.assertRaises(LedgerError) as stale:
            self.store.update_quote_offer(
                {
                    "idempotency_key": key("offer-a-stale"),
                    "offer_id": first["id"],
                    "version": first["version"],
                    "changes": {"note": "过期修改"},
                }
            )
        self.assertEqual(stale.exception.code, "version_conflict")

        current = self.store.show_quote(quote["id"])["quote"]
        selected = self.store.select_quote_offer(
            {
                "idempotency_key": key("select"),
                "request_id": quote["id"],
                "offer_id": second["id"],
                "version": current["version"],
            }
        )
        self.assertEqual(selected["quote"]["selected_offer_id"], second["id"])
        self.assertEqual(selected["offer"]["status"], "selected")
        self.assertEqual(self.store.query({"limit": 100}), [])
        self.assertEqual(self.store.summary({})["transaction_count"], 0)

    def test_expired_offer_media_deduplication_search_and_validation(self) -> None:
        quote = self.create_quote()
        expired = self.store.add_quote_offer(
            {
                "idempotency_key": key("expired"),
                "request_id": quote["id"],
                "supplier_name": "过期供应商",
                "total_cents": 10_000,
                "valid_until": "2020-01-01",
            }
        )["offer"]
        current = self.store.show_quote(quote["id"])["quote"]
        with self.assertRaises(LedgerError) as context:
            self.store.select_quote_offer(
                {
                    "idempotency_key": key("select-expired"),
                    "request_id": quote["id"],
                    "offer_id": expired["id"],
                    "version": current["version"],
                }
            )
        self.assertEqual(context.exception.code, "quote_offer_invalid")

        now = "2026-08-23T08:00:00Z"
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
                    "quote-media-fixture", self.project["id"], "image", "image/jpeg", "墙砖报价单.jpg",
                    "aa/original.jpg", "preview.webp", 1024, "a" * 64, 1200, 900, None, now,
                    now, "fixture", "private-source", "ready", None, now, now,
                ),
            )
        first_link = self.store.attach_quote_media(
            {
                "idempotency_key": key("media-link-a"),
                "request_id": quote["id"],
                "media_id": "quote-media-fixture",
                "role": "quote_sheet",
            }
        )
        second_link = self.store.attach_quote_media(
            {
                "idempotency_key": key("media-link-b"),
                "request_id": quote["id"],
                "media_id": "quote-media-fixture",
                "role": "quote_sheet",
            }
        )
        self.assertFalse(first_link["idempotent_replay"])
        self.assertFalse(second_link["idempotent_replay"])
        detail = self.store.show_quote(quote["id"])
        self.assertEqual(len(detail["media"]), 1)
        self.assertEqual(detail["media"][0]["role"], "quote_sheet")
        self.assertEqual(detail["media"][0]["content_url"], "/api/v1/media/quote-media-fixture/content")
        self.assertNotIn("storage_name", detail["media"][0])
        self.assertEqual(self.store.list_quotes({"project_id": self.project["id"], "keyword": "过期供应商"})[0]["id"], quote["id"])
        self.assertEqual(self.store.status()["counts"]["quote_media"], 1)

        with self.assertRaises(LedgerError) as specification:
            self.store.create_quote(
                {
                    "idempotency_key": key("too-many-specs"),
                    "project_id": self.project["id"],
                    "title": "规格过多",
                    "specification": {f"字段{index}": str(index) for index in range(33)},
                }
            )
        self.assertEqual(specification.exception.code, "invalid_input")


if __name__ == "__main__":
    unittest.main()
