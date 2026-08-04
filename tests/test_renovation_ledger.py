from __future__ import annotations

import base64
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
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

    def test_v2_grouped_tags_payment_refund_and_summary(self) -> None:
        grouped = {
            "专业": ["隐蔽工程"],
            "生态": ["网络"],
            "主题": ["x" * 37],
        }
        payment = self.store.add_payment(
            {
                "idempotency_key": fixture_idempotency("v2-payment"),
                "ledger_format_version": 2,
                "amount_cents": 88_800,
                "occurred_on": "2026-08-04",
                "merchant": "示例供应方",
                "grouped_tags": grouped,
            }
        )["transaction"]
        self.assertEqual(payment["main_category"], "")
        self.assertEqual(payment["ledger_format_version"], 2)
        self.assertEqual(payment["grouped_tags"]["专业"], ["隐蔽工程"])
        refund = self.store.add_refund(
            {
                "idempotency_key": fixture_idempotency("v2-refund"),
                "original_payment_id": payment["id"],
                "amount_cents": 8_800,
                "occurred_on": "2026-08-05",
            }
        )["transaction"]
        self.assertEqual(refund["ledger_format_version"], 2)
        self.assertEqual(refund["grouped_tags"], payment["grouped_tags"])
        summary = self.store.summary()
        self.assertNotIn("未分类", summary["category_totals"])
        self.assertEqual(summary["dimensions"]["生态"]["网络"], 80_000)

        with self.assertRaises(LedgerError) as too_many:
            self.store.add_payment(
                {
                    "idempotency_key": fixture_idempotency("v2-too-many"),
                    "amount_cents": 100,
                    "occurred_on": "2026-08-04",
                    "grouped_tags": {"主题": [str(index) for index in range(25)]},
                }
            )
        self.assertEqual(too_many.exception.code, "invalid_tags")
        with self.assertRaises(LedgerError) as too_long:
            self.store.add_payment(
                {
                    "idempotency_key": fixture_idempotency("v2-too-long"),
                    "amount_cents": 100,
                    "occurred_on": "2026-08-04",
                    "grouped_tags": {"主题": ["x" * 38]},
                }
            )
        self.assertEqual(too_long.exception.code, "invalid_tags")

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

    def test_attachment_failures_have_no_persistent_file_side_effects(self) -> None:
        payment = self.add_payment("fixture-attachment-target-" + "0" * 24)["transaction"]

        def files(store: LedgerStore) -> set[str]:
            return {path.name for path in store.attachments_dir.iterdir()}

        missing_before = files(self.store)
        with self.assertRaises(LedgerError) as missing:
            self.store.attach_content(
                {
                    "idempotency_key": fixture_idempotency("attach-missing"),
                    "transaction_id": "missing",
                    "original_filename": "missing.txt",
                    "mime_type": "text/plain",
                    "content_base64": base64.b64encode(b"missing").decode(),
                }
            )
        self.assertEqual(missing.exception.code, "transaction_not_found")
        self.assertEqual(files(self.store), missing_before)

        first = self.store.attach_content(
            {
                "idempotency_key": fixture_idempotency("attach-conflict"),
                "transaction_id": payment["id"],
                "original_filename": "first.txt",
                "mime_type": "text/plain",
                "content_base64": base64.b64encode(b"first").decode(),
            }
        )
        self.assertFalse(first["idempotent_replay"])
        conflict_before = files(self.store)
        with self.assertRaises(LedgerError) as conflict:
            self.store.attach_content(
                {
                    "idempotency_key": fixture_idempotency("attach-conflict"),
                    "transaction_id": payment["id"],
                    "original_filename": "second.txt",
                    "mime_type": "text/plain",
                    "content_base64": base64.b64encode(b"second").decode(),
                }
            )
        self.assertEqual(conflict.exception.code, "idempotency_conflict")
        self.assertEqual(files(self.store), conflict_before)

        failed_before = files(self.store)
        with patch("renovation_hub.ledger.os.replace", side_effect=OSError("simulated rename failure")):
            with self.assertRaises(OSError):
                self.store.attach_content(
                    {
                        "idempotency_key": fixture_idempotency("attach-rename-failure"),
                        "transaction_id": payment["id"],
                        "original_filename": "failure.txt",
                        "mime_type": "text/plain",
                        "content_base64": base64.b64encode(b"rename failure").decode(),
                    }
                )
        self.assertEqual(files(self.store), failed_before)

        read_only_root = Path(self.temporary.name) / "attachment-readonly"
        read_only = LedgerStore(
            read_only_root / "ledger.sqlite3",
            data_dir=read_only_root,
            share_dir=read_only_root / "share",
        )
        read_only.set_writer_mode("read_only", force_initial=True)
        read_only_before = files(read_only)
        with self.assertRaises(LedgerError) as disabled:
            read_only.attach_content(
                {
                    "idempotency_key": fixture_idempotency("attach-readonly"),
                    "transaction_id": "missing",
                    "original_filename": "readonly.txt",
                    "mime_type": "text/plain",
                    "content_base64": base64.b64encode(b"readonly").decode(),
                }
            )
        self.assertEqual(disabled.exception.code, "writer_disabled")
        self.assertEqual(files(read_only), read_only_before)

    def test_attachment_crash_marker_recovery_only_cleans_marked_orphans(self) -> None:
        root = Path(self.temporary.name) / "attachment-recovery"
        store = LedgerStore(
            root / "ledger.sqlite3",
            data_dir=root,
            share_dir=root / "share",
        )
        storage_name = "a" * 64 + ".txt"
        temporary_name = ".attach-crash.tmp"
        marker = store.attachments_dir / ".attach-pending-crash.json"
        marker.write_text(
            json.dumps(
                {
                    "storage_name": storage_name,
                    "temporary_name": temporary_name,
                }
            ),
            encoding="utf-8",
        )
        (store.attachments_dir / temporary_name).write_bytes(b"temporary")
        (store.attachments_dir / storage_name).write_bytes(b"orphan")
        unrelated = store.attachments_dir / "manual-review.bin"
        unrelated.write_bytes(b"preserve")

        LedgerStore(
            root / "ledger.sqlite3",
            data_dir=root,
            share_dir=root / "share",
        )
        self.assertFalse(marker.exists())
        self.assertFalse((store.attachments_dir / temporary_name).exists())
        self.assertFalse((store.attachments_dir / storage_name).exists())
        self.assertTrue(unrelated.exists())


if __name__ == "__main__":
    unittest.main()
