from __future__ import annotations

import base64
import json
from pathlib import Path
import tempfile
import unittest
import zipfile

from renovation_hub.api import dispatch_tool
from renovation_hub.ledger import LedgerError, LedgerStore


FIXTURE_PAYMENT_KEY = "0" * 32


def fixture_idempotency(label: str) -> str:
    return f"fixture-{label}-" + "0" * 24


class RenovationLedgerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        self.store = LedgerStore(
            root / "data" / "ledger.sqlite3",
            data_dir=root / "data",
            share_dir=root / "share",
            portable_history_limit=3,
        )
        self.store.set_writer_mode("read_only", force_initial=True)
        self.store.set_writer_mode("shadow_validated")
        self.store.set_writer_mode("cutover_ready")
        self.store.set_writer_mode("primary_writer")

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def add_payment(self, key: str = FIXTURE_PAYMENT_KEY) -> dict:
        return self.store.add_payment(
            {
                "idempotency_key": key,
                "amount_cents": 100_000,
                "occurred_on": "2026-01-02",
                "main_category": "水电工程",
                "merchant": "示例商家",
                "note": "合成数据",
                "is_deposit": False,
                "tags": ["智能家居", "隐蔽工程"],
            },
            actor_hash="sha256:fixture",
        )

    def test_payment_is_idempotent_and_conflicting_reuse_fails(self) -> None:
        first = self.add_payment()
        second = self.add_payment()
        self.assertFalse(first["idempotent_replay"])
        self.assertTrue(second["idempotent_replay"])
        self.assertEqual(first["transaction"]["id"], second["transaction"]["id"])
        with self.assertRaisesRegex(LedgerError, "不同请求") as context:
            self.store.add_payment(
                {
                    "idempotency_key": FIXTURE_PAYMENT_KEY,
                    "amount_cents": 200_000,
                    "occurred_on": "2026-01-02",
                    "main_category": "水电工程",
                }
            )
        self.assertEqual(context.exception.code, "idempotency_conflict")

    def test_refund_inherits_category_and_tags_and_cannot_exceed_payment(self) -> None:
        payment = self.add_payment()["transaction"]
        refund = self.store.add_refund(
            {
                "idempotency_key": fixture_idempotency("refund-1"),
                "original_payment_id": payment["id"],
                "amount_cents": 10_000,
                "occurred_on": "2026-01-05",
                "note": "合成退款",
            }
        )["transaction"]
        self.assertEqual(refund["main_category"], "水电工程")
        self.assertEqual(refund["tags"], ["智能家居", "隐蔽工程"])
        summary = self.store.summary()
        self.assertEqual(summary["net_amount_cents"], 90_000)
        self.assertTrue(summary["tag_totals_overlap"])
        with self.assertRaises(LedgerError) as context:
            self.store.add_refund(
                {
                    "idempotency_key": fixture_idempotency("refund-2"),
                    "original_payment_id": payment["id"],
                    "amount_cents": 100_000,
                    "occurred_on": "2026-01-06",
                }
            )
        self.assertEqual(context.exception.code, "refund_exceeds_payment")

    def test_correction_and_undo_preserve_refund_invariants(self) -> None:
        payment = self.add_payment()["transaction"]
        refund = self.store.add_refund(
            {
                "idempotency_key": fixture_idempotency("refund-3"),
                "original_payment_id": payment["id"],
                "amount_cents": 40_000,
                "occurred_on": "2026-01-05",
            }
        )["transaction"]
        with self.assertRaises(LedgerError) as context:
            self.store.correct_payment(
                {
                    "idempotency_key": fixture_idempotency("correct"),
                    "payment_id": payment["id"],
                    "changes": {"amount_cents": 30_000},
                    "reason": "错误金额测试",
                }
            )
        self.assertEqual(context.exception.code, "refund_exceeds_payment")
        with self.assertRaises(LedgerError) as context:
            self.store.undo(
                {
                    "idempotency_key": fixture_idempotency("undo-payment-blocked"),
                    "transaction_id": payment["id"],
                    "reason": "测试",
                }
            )
        self.assertEqual(context.exception.code, "payment_has_refunds")
        self.store.undo(
            {
                "idempotency_key": fixture_idempotency("undo-refund"),
                "transaction_id": refund["id"],
                "reason": "撤销退款",
            }
        )
        undone = self.store.undo(
            {
                "idempotency_key": fixture_idempotency("undo-payment"),
                "transaction_id": payment["id"],
                "reason": "撤销付款",
            }
        )
        self.assertEqual(undone["transaction"]["status"], "voided")

    def test_attachment_export_verification_chart_and_shadow_import(self) -> None:
        payment = self.add_payment()["transaction"]
        attached = self.store.attach_content(
            {
                "idempotency_key": fixture_idempotency("attach"),
                "transaction_id": payment["id"],
                "original_filename": "receipt.txt",
                "mime_type": "text/plain",
                "content_base64": base64.b64encode("合成附件".encode()).decode(),
            }
        )
        self.assertEqual(attached["attachment"]["size_bytes"], 12)
        exported = self.store.export_portable()
        verified = self.store.verify_portable(exported["path"])
        self.assertTrue(verified["valid"])
        shadow = self.store.import_shadow(exported["path"])
        self.assertEqual(shadow["state"], "shadow_validated")
        chart = self.store.generate_chart()
        self.assertEqual((chart["width"], chart["height"]), (1280, 960))
        self.assertTrue((self.store.charts_dir / chart["download_ref"]).is_file())

    def test_malicious_zip_path_is_rejected(self) -> None:
        bad = Path(self.temporary.name) / "bad.zip"
        with zipfile.ZipFile(bad, "w") as archive:
            archive.writestr("../escape", "x")
            archive.writestr("manifest.json", json.dumps({"format_id": "kanhuwan-renovation-ledger@1", "schema_version": 1, "files": []}))
        with self.assertRaises(LedgerError) as context:
            self.store.verify_portable(bad)
        self.assertEqual(context.exception.code, "import_invalid")

    def test_tool_dispatch_rejects_unknown_names(self) -> None:
        with self.assertRaises(LedgerError) as context:
            dispatch_tool(self.store, {"name": "ledger_execute_sql", "arguments": {}})
        self.assertEqual(context.exception.code, "unknown_tool")

    def test_read_only_mode_rejects_writes(self) -> None:
        root = Path(self.temporary.name) / "readonly"
        store = LedgerStore(root / "ledger.sqlite3", data_dir=root, share_dir=root / "share")
        store.set_writer_mode("read_only", force_initial=True)
        with self.assertRaises(LedgerError) as context:
            store.add_payment(
                {
                    "idempotency_key": fixture_idempotency("readonly"),
                    "amount_cents": 100,
                    "occurred_on": "2026-01-01",
                    "main_category": "测试",
                }
            )
        self.assertEqual(context.exception.code, "writer_disabled")


if __name__ == "__main__":
    unittest.main()
