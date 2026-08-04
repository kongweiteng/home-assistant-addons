from __future__ import annotations

import csv
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import stat
import tempfile
import unittest
from unittest.mock import patch
import warnings
import zipfile

from renovation_hub import portable
from renovation_hub.ledger import LedgerError, LedgerStore, canonical_json
from renovation_hub.hub import RenovationHubStore
from renovation_hub.media import MediaService
from renovation_hub.web import create_app


class CanonicalPortableFixture:
    def __init__(self, root: Path, *, format_version: int = portable.FORMAT_VERSION) -> None:
        self.root = root
        self.format_version = format_version
        self.root.mkdir(parents=True, mode=0o700)
        self.package = root / "package"
        self.package.mkdir(mode=0o700)
        (self.package / "attachments" / "1").mkdir(parents=True, mode=0o700)
        self.archive = root / "canonical-ledger.zip"

    @staticmethod
    def _write_csv(path: Path, rows: list[dict[str, str]]) -> None:
        with path.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
            writer.writeheader()
            writer.writerows(rows)

    def build(self) -> Path:
        attachment = self.package / "attachments" / "1" / "receipt.png"
        attachment.write_bytes(b"synthetic-receipt-image")
        attachment_sha = hashlib.sha256(attachment.read_bytes()).hexdigest()
        database = self.package / "bookkeeping.sqlite3"
        connection = sqlite3.connect(database)
        category_column = "category TEXT," if self.format_version == portable.FORMAT_VERSION else ""
        connection.executescript(
            f"""
            PRAGMA foreign_keys=ON;
            CREATE TABLE metadata(key TEXT PRIMARY KEY,value TEXT NOT NULL);
            CREATE TABLE transactions(
              id INTEGER PRIMARY KEY,
              kind TEXT NOT NULL,
              payment_id INTEGER REFERENCES transactions(id),
              amount_cents INTEGER NOT NULL,
              txn_date TEXT NOT NULL,
              {category_column}
              vendor TEXT NOT NULL,
              description TEXT NOT NULL,
              is_deposit INTEGER NOT NULL,
              status TEXT NOT NULL,
              void_reason TEXT NOT NULL,
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL
            );
            CREATE TABLE transaction_tags(
              id INTEGER PRIMARY KEY,
              transaction_id INTEGER NOT NULL REFERENCES transactions(id),
              tag TEXT NOT NULL,
              tag_key TEXT NOT NULL,
              position INTEGER NOT NULL,
              created_at TEXT NOT NULL
            );
            CREATE TABLE attachments(
              id INTEGER PRIMARY KEY,
              transaction_id INTEGER NOT NULL REFERENCES transactions(id),
              original_filename TEXT NOT NULL,
              relative_path TEXT NOT NULL,
              sha256 TEXT NOT NULL,
              size_bytes INTEGER NOT NULL,
              media_type TEXT NOT NULL,
              created_at TEXT NOT NULL
            );
            CREATE TABLE audit_log(
              id INTEGER PRIMARY KEY,
              transaction_id INTEGER NOT NULL REFERENCES transactions(id),
              action TEXT NOT NULL,
              actor TEXT NOT NULL,
              before_json TEXT,
              after_json TEXT NOT NULL,
              reason TEXT NOT NULL,
              created_at TEXT NOT NULL
            );
            """
        )
        timestamp = "2026-08-03T00:00:00Z"
        sqlite_schema_version = "4" if self.format_version == portable.FORMAT_VERSION else "3"
        connection.execute("INSERT INTO metadata VALUES ('schema_version',?)", (sqlite_schema_version,))
        if self.format_version == portable.FORMAT_VERSION:
            connection.executemany(
                "INSERT INTO transactions VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                [
                    (1, "payment", None, 123_400, "2026-07-15", "示例工程", "示例供应方", "合成付款", 0, "active", "", timestamp, timestamp),
                    (2, "refund", 1, 3_400, "2026-08-01", None, "", "合成退款", 0, "active", "", timestamp, timestamp),
                    (3, "payment", None, 50_000, "2026-08-02", "示例设备", "示例设备方", "撤销样例", 1, "void", "合成重复记录", timestamp, timestamp),
                ],
            )
        else:
            connection.executemany(
                "INSERT INTO transactions VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                [
                    (1, "payment", None, 123_400, "2026-07-15", "示例供应方", "合成付款", 0, "active", "", timestamp, timestamp),
                    (2, "refund", 1, 3_400, "2026-08-01", "", "合成退款", 0, "active", "", timestamp, timestamp),
                    (3, "payment", None, 50_000, "2026-08-02", "示例设备方", "撤销样例", 1, "void", "合成重复记录", timestamp, timestamp),
                ],
            )
        tags = (
            [(1, 1, "隐蔽", "隐蔽", 0, timestamp), (2, 1, "网络", "网络", 1, timestamp)]
            if self.format_version == portable.FORMAT_VERSION
            else [
                (1, 1, "专业:隐蔽工程", "专业:隐蔽工程".casefold(), 0, timestamp),
                (2, 1, "生态:网络", "生态:网络".casefold(), 1, timestamp),
            ]
        )
        connection.executemany(
            "INSERT INTO transaction_tags VALUES (?,?,?,?,?,?)",
            tags,
        )
        connection.execute(
            "INSERT INTO attachments VALUES (?,?,?,?,?,?,?,?)",
            (
                1,
                1,
                "receipt.png",
                "1/receipt.png",
                attachment_sha,
                attachment.stat().st_size,
                "image/png",
                timestamp,
            ),
        )
        audit_after = [
            {"id": 1, "kind": "payment", "status": "active"},
            {"id": 2, "kind": "refund", "status": "active"},
            {"id": 3, "kind": "payment", "status": "void"},
        ]
        connection.executemany(
            "INSERT INTO audit_log VALUES (?,?,?,?,?,?,?,?)",
            [
                (1, 1, "add_payment", "fixture", None, canonical_json(audit_after[0]), "", timestamp),
                (2, 2, "add_refund", "fixture", None, canonical_json(audit_after[1]), "", timestamp),
                (
                    3,
                    3,
                    "void",
                    "fixture",
                    canonical_json({"id": 3, "status": "active"}),
                    canonical_json(audit_after[2]),
                    "合成重复记录",
                    timestamp,
                ),
            ],
        )
        connection.commit()
        connection.close()

        if self.format_version == portable.FORMAT_VERSION:
            expected_summary = {
                "categories": [
                    {"category": "示例工程"},
                    {"category": "示例设备"},
                ]
            }
            state = portable._snapshot_state(database, expected_summary)
        else:
            state = portable._snapshot_state_v2(database)
        ledger = {
            "format_id": portable.FORMAT_ID,
            "format_version": self.format_version,
            "generated_at": timestamp,
            "currency": "CNY",
            "amount_unit": "integer_cents",
            **{key: state[key] for key in (
                "metadata",
                "transactions",
                "transaction_tags",
                "attachments",
                "audit_log",
                "invariants",
                "summary",
            )},
        }
        (self.package / "ledger.json").write_text(
            json.dumps(ledger, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        (self.package / "schema.json").write_text(
            json.dumps(
                {"$schema": "https://json-schema.org/draft/2020-12/schema"},
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        (self.package / "FORMAT.md").write_text("# 合成便携账本\n", encoding="utf-8")
        (self.package / "verify.py").write_text("raise SystemExit('fixture only')\n", encoding="utf-8")
        self._write_csv(
            self.package / "transactions.csv",
            portable._expected_transaction_csv(state["transactions"])
            if self.format_version == portable.FORMAT_VERSION
            else portable._expected_transaction_csv_v2(state["transactions"]),
        )
        self._write_csv(
            self.package / "transaction_tags.csv",
            portable._expected_tag_csv(state["transaction_tags"]),
        )
        self._write_csv(
            self.package / "attachments.csv",
            portable._expected_attachment_csv(state["attachments"], True),
        )
        with (self.package / "audit_log.jsonl").open("w", encoding="utf-8") as handle:
            for item in state["audit_log"]:
                handle.write(canonical_json(item) + "\n")
        self._write_manifest(state)
        self._zip(self.archive)
        return self.archive

    def _write_manifest(self, state: dict[str, object] | None = None) -> None:
        ledger = json.loads((self.package / "ledger.json").read_text(encoding="utf-8"))
        invariants = state["invariants"] if state is not None else ledger["invariants"]
        files = []
        for path in sorted(self.package.rglob("*")):
            if not path.is_file() or path.name == "manifest.json":
                continue
            data = path.read_bytes()
            files.append(
                {
                    "path": path.relative_to(self.package).as_posix(),
                    "sha256": hashlib.sha256(data).hexdigest(),
                    "size_bytes": len(data),
                }
            )
        manifest = {
            "format_id": portable.FORMAT_ID,
            "format_version": self.format_version,
            "generated_at": "2026-08-03T00:00:00Z",
            "currency": "CNY",
            "amount_unit": "integer_cents",
            "hermes_required": False,
            "source": {
                "application": "synthetic-fixture",
                "sqlite_schema_version": "4"
                if self.format_version == portable.FORMAT_VERSION
                else "3",
            },
            "semantics": {
                **(
                    {
                        "primary_category_single": True,
                        "refunds_link_to_payments": True,
                        "refunds_inherit_category_and_tags": True,
                        "tag_totals_overlap": True,
                        "tag_totals_must_not_be_summed": True,
                        "void_records_are_retained": True,
                    }
                    if self.format_version == portable.FORMAT_VERSION
                    else {
                        "primary_category_single": False,
                        "grouped_multi_tags": True,
                        "tag_totals_overlap": True,
                        "total_ledger_deduplicates_transactions": True,
                    }
                )
            },
            "export": {
                "attachments_included": True,
                "manifest_self_excluded_from_hashes": True,
                "sqlite_snapshot_consistent": True,
            },
            "invariants": invariants,
            "files": files,
        }
        (self.package / "manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )

    def _zip(self, target: Path) -> None:
        with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as archive:
            directory = zipfile.ZipInfo("attachments/")
            directory.create_system = 3
            directory.external_attr = (stat.S_IFDIR | 0o700) << 16
            archive.writestr(directory, b"")
            for path in sorted(self.package.rglob("*")):
                if not path.is_file():
                    continue
                info = zipfile.ZipInfo(path.relative_to(self.package).as_posix())
                info.create_system = 3
                info.external_attr = (stat.S_IFREG | 0o600) << 16
                info.compress_type = zipfile.ZIP_DEFLATED
                archive.writestr(info, path.read_bytes())

    def mutate_and_repack(self, name: str, mutation) -> Path:
        mutation(self.package)
        self._write_manifest()
        target = self.root / name
        self._zip(target)
        return target


class RenovationPortableTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.fixture = CanonicalPortableFixture(self.root / "fixture")
        self.archive = self.fixture.build()
        self.v2_fixture = CanonicalPortableFixture(
            self.root / "fixture-v2",
            format_version=portable.FORMAT_VERSION_V2,
        )
        self.v2_archive = self.v2_fixture.build()
        self.store_root = self.root / "store"
        self.store = LedgerStore(
            self.store_root / "ledger.sqlite3",
            data_dir=self.store_root,
            share_dir=self.store_root / "share",
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_full_verification_shadow_import_idempotency_and_restart(self) -> None:
        verified = self.store.verify_portable(self.archive)
        self.assertTrue(verified["valid"])
        self.assertEqual(verified["counts"]["transactions"], 3)
        first = self.store.import_shadow(self.archive)
        second = self.store.import_shadow(self.archive)
        reopened = LedgerStore(
            self.store_root / "ledger.sqlite3",
            data_dir=self.store_root,
            share_dir=self.store_root / "share",
        )
        third = reopened.import_shadow(self.archive)
        self.assertEqual(first["state"], "shadow_validated")
        self.assertTrue(all(first["checks"].values()))
        self.assertFalse(first["idempotent_replay"])
        self.assertTrue(second["idempotent_replay"])
        self.assertTrue(third["idempotent_replay"])
        self.assertEqual(first["verification_digest"], third["verification_digest"])
        report_text = canonical_json(first)
        for private_field in ("amount_cents", "net_cents", "payments_cents", "vendor", "description"):
            self.assertNotIn(private_field, report_text)
        self.assertNotIn(str(self.root), report_text)

    def test_void_reason_attachment_audit_and_source_ref_are_restored(self) -> None:
        report = self.store.import_shadow(self.archive)
        destination = self.store.shadow_dir / report["source_sha256"]
        connection = sqlite3.connect(destination / "ledger.sqlite3")
        connection.row_factory = sqlite3.Row
        voided = connection.execute("SELECT * FROM transactions WHERE legacy_id=3").fetchone()
        self.assertEqual(voided["status"], "voided")
        self.assertEqual(voided["void_reason"], "合成重复记录")
        self.assertTrue(voided["source_ref"].endswith(":3"))
        audit_ids = [row[0] for row in connection.execute("SELECT id FROM audit_log ORDER BY id")]
        self.assertEqual(audit_ids, [1, 2, 3])
        attachment = connection.execute("SELECT storage_name,sha256,size_bytes FROM attachments").fetchone()
        connection.close()
        path = destination / "attachments" / attachment["storage_name"]
        self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), attachment["sha256"])
        self.assertEqual(path.stat().st_size, attachment["size_bytes"])

    def test_v2_grouped_tags_are_verified_and_restored_shadow_only(self) -> None:
        verified = self.store.verify_portable(self.v2_archive)
        self.assertEqual(verified["format_version"], portable.FORMAT_VERSION_V2)
        self.assertEqual(verified["counts"]["tag_links"], 2)
        first = self.store.import_shadow(self.v2_archive)
        second = self.store.import_shadow(self.v2_archive)
        self.assertEqual(first["state"], "shadow_validated")
        self.assertFalse(first["idempotent_replay"])
        self.assertTrue(second["idempotent_replay"])
        destination = self.store.shadow_dir / first["source_sha256"]
        connection = sqlite3.connect(destination / "ledger.sqlite3")
        payment = connection.execute(
            "SELECT id,main_category FROM transactions WHERE legacy_id=1"
        ).fetchone()
        tags = [
            row[0]
            for row in connection.execute(
                """
                SELECT tags.display_name
                FROM transaction_tags
                JOIN tags ON tags.normalized=transaction_tags.tag_normalized
                WHERE transaction_id=?
                ORDER BY transaction_tags.position
                """,
                (payment[0],),
            )
        ]
        connection.close()
        self.assertEqual(payment[1], "")
        self.assertEqual(tags, ["专业:隐蔽工程", "生态:网络"])
        self.assertEqual(
            json.loads((destination / "report.json").read_text())["format_version"],
            portable.FORMAT_VERSION_V2,
        )

    def _migration_evidence(self, store: LedgerStore) -> dict[str, object]:
        extracted = self.root / f"verified-{len(list(self.root.glob('verified-*')))}"
        verified = portable.verify_and_extract(self.v2_archive, extracted)
        report = store._canonical_shadow_report(verified)
        return {
            "source_sha256": verified["archive_sha256"],
            "verification_digest": report["verification_digest"],
            "backup_manifest": {
                "haos": {"verified": True},
                "hermes": {"verified": True},
                "media": {"verified": True},
                "off_device": {"verified": True},
            },
            "authorization_ref": "fixture-authorization",
        }

    def test_v2_primary_migration_manifest_lease_restart_and_emergency_stop(self) -> None:
        root = self.root / "primary"
        store = RenovationHubStore(
            root / "ledger.sqlite3",
            data_dir=root,
            share_dir=root / "share",
            enforce_cutover_manifest=True,
        )
        evidence = self._migration_evidence(store)
        prepared = store.prepare_primary_migration(self.v2_archive, evidence)
        replay = store.prepare_primary_migration(self.v2_archive, evidence)
        self.assertEqual(prepared["manifest"]["state"], "migration_prepared")
        self.assertTrue(replay["idempotent_replay"])
        manifest_id = prepared["manifest"]["id"]
        store.mark_source_frozen(
            manifest_id,
            {
                "hermes_writer_stopped": True,
                "running_transactions": 0,
                "queue_depth": 0,
            },
        )
        seeded = store.seed_primary(manifest_id)
        self.assertEqual(seeded["state"], "primary_seeded")
        self.assertEqual(len(store.query({"limit": 1000})), 3)
        with store._connect() as connection:
            self.assertEqual(connection.execute("SELECT count(*) FROM projects").fetchone()[0], 1)
            self.assertEqual(
                connection.execute("SELECT count(*) FROM transaction_context").fetchone()[0],
                3,
            )
        ready = store.mark_cutover_ready(
            manifest_id,
            {
                "owner_bound": True,
                "poller_state": "polling",
                "queue_depth": 0,
                "recovery_depth": 0,
                "single_writer": True,
                "rollback_ready": True,
            },
        )
        generation = ready["writer_generation"]
        with self.assertRaises(LedgerError) as bad_confirmation:
            store.activate_primary_writer(manifest_id, "ACTIVATE_PRIMARY_WRITER")
        self.assertEqual(bad_confirmation.exception.code, "confirmation_required")
        store.activate_primary_writer(
            manifest_id,
            f"ACTIVATE_PRIMARY_WRITER:{manifest_id}:{generation}",
        )
        reopened = RenovationHubStore(
            root / "ledger.sqlite3",
            data_dir=root,
            share_dir=root / "share",
            enforce_cutover_manifest=True,
        )
        reopened.coordinate_configured_writer_mode("primary_writer")
        with reopened._connect() as connection:
            connection.execute(
                "UPDATE writer_leases SET generation=generation+1 WHERE active=1"
            )
        degraded = reopened.coordinate_configured_writer_mode("primary_writer")
        self.assertTrue(degraded["recovery_required"])
        self.assertEqual(reopened.status()["health"], "degraded")
        with reopened._connect() as connection:
            connection.execute(
                "UPDATE writer_leases SET generation=? WHERE active=1", (generation,)
            )
        written = reopened.add_payment(
            {
                "idempotency_key": "fixture-native-v2-" + "0" * 24,
                "amount_cents": 100,
                "occurred_on": "2026-08-04",
                "grouped_tags": {"专业": ["测试"]},
            }
        )
        self.assertEqual(written["portable_export"], "current")
        first_export = reopened.export_portable()
        first_verified = reopened.verify_portable(first_export["path"])
        second_export = reopened.export_portable()
        second_verified = reopened.verify_portable(second_export["path"])
        self.assertEqual(first_verified["format_version"], portable.FORMAT_VERSION_V2)
        self.assertEqual(first_verified["digests"], second_verified["digests"])
        stopped = reopened.coordinate_configured_writer_mode("read_only")
        self.assertEqual(stopped["current"], "suspended")
        next_generation = stopped["writer_generation"]
        self.assertEqual(next_generation, generation + 1)
        self.assertTrue(reopened.status()["recovery_required"])
        with self.assertRaises(LedgerError) as disabled:
            reopened.add_payment(
                {
                    "idempotency_key": "fixture-after-stop-" + "0" * 24,
                    "amount_cents": 100,
                    "occurred_on": "2026-08-04",
                    "grouped_tags": {},
                }
            )
        self.assertEqual(disabled.exception.code, "writer_disabled")
        suspended_manifest = reopened.active_manifest()
        assert suspended_manifest is not None
        self.assertEqual(suspended_manifest["state"], "primary_writer")
        with self.assertRaises(LedgerError) as stale_confirmation:
            reopened.activate_primary_writer(
                manifest_id,
                f"ACTIVATE_PRIMARY_WRITER:{manifest_id}:{generation}",
            )
        self.assertEqual(stale_confirmation.exception.code, "confirmation_required")
        restart_state = reopened.coordinate_configured_writer_mode("primary_writer")
        self.assertTrue(restart_state["recovery_required"])
        recovery_app = create_app(
            store=reopened,
            media=MediaService(
                reopened,
                media_root=root / "media",
                preview_root=root / "preview",
                staging_root=root / "media-staging",
                max_media_bytes=1024 * 1024,
            ),
            api_token="t" * 32,
            cutover_token="c" * 32,
            max_request_bytes=1024 * 1024,
        )
        self.assertIn(
            "/internal/v1/admin/cutover/activate",
            {route.resource.canonical for route in recovery_app.router.routes()},
        )
        reopened.activate_primary_writer(
            manifest_id,
            f"ACTIVATE_PRIMARY_WRITER:{manifest_id}:{next_generation}",
        )
        reopened.coordinate_configured_writer_mode("primary_writer")
        resumed = reopened.add_payment(
            {
                "idempotency_key": "fixture-after-resume-" + "0" * 24,
                "amount_cents": 100,
                "occurred_on": "2026-08-04",
                "grouped_tags": {},
            }
        )
        with reopened._connect() as connection:
            self.assertEqual(
                connection.execute(
                    "SELECT ledger_format_version FROM transactions WHERE id=?",
                    (resumed["transaction"]["id"],),
                ).fetchone()[0],
                2,
            )

    def test_each_suspend_rotates_writer_generation_and_rejects_prior_confirmation(self) -> None:
        root = self.root / "generation-rotation"
        store = RenovationHubStore(
            root / "ledger.sqlite3",
            data_dir=root,
            share_dir=root / "share",
            enforce_cutover_manifest=True,
        )
        prepared = store.prepare_primary_migration(
            self.v2_archive,
            self._migration_evidence(store),
        )
        manifest_id = prepared["manifest"]["id"]
        store.mark_source_frozen(
            manifest_id,
            {
                "hermes_writer_stopped": True,
                "running_transactions": 0,
                "queue_depth": 0,
            },
        )
        store.seed_primary(manifest_id)
        ready = store.mark_cutover_ready(
            manifest_id,
            {
                "owner_bound": True,
                "poller_state": "polling",
                "queue_depth": 0,
                "recovery_depth": 0,
                "single_writer": True,
                "rollback_ready": True,
            },
        )
        first_generation = ready["writer_generation"]
        store.activate_primary_writer(
            manifest_id,
            f"ACTIVATE_PRIMARY_WRITER:{manifest_id}:{first_generation}",
        )
        second_generation = store.suspend_writer("first_stop")["writer_generation"]
        third_generation = store.suspend_writer("second_stop")["writer_generation"]
        self.assertEqual(second_generation, first_generation + 1)
        self.assertEqual(third_generation, second_generation + 1)
        reopened = RenovationHubStore(
            root / "ledger.sqlite3",
            data_dir=root,
            share_dir=root / "share",
            enforce_cutover_manifest=True,
        )
        for stale in (first_generation, second_generation):
            with self.assertRaises(LedgerError) as stale_confirmation:
                reopened.activate_primary_writer(
                    manifest_id,
                    f"ACTIVATE_PRIMARY_WRITER:{manifest_id}:{stale}",
                )
            self.assertEqual(stale_confirmation.exception.code, "confirmation_required")
        reopened.activate_primary_writer(
            manifest_id,
            f"ACTIVATE_PRIMARY_WRITER:{manifest_id}:{third_generation}",
        )
        self.assertFalse(reopened.status()["recovery_required"])

    def test_interrupted_seed_resumes_without_overwriting_the_rollback_baseline(self) -> None:
        root = self.root / "interrupted-seed"
        store = RenovationHubStore(
            root / "ledger.sqlite3",
            data_dir=root,
            share_dir=root / "share",
            enforce_cutover_manifest=True,
        )
        evidence = self._migration_evidence(store)
        prepared = store.prepare_primary_migration(self.v2_archive, evidence)
        manifest_id = prepared["manifest"]["id"]
        store.mark_source_frozen(
            manifest_id,
            {
                "hermes_writer_stopped": True,
                "running_transactions": 0,
                "queue_depth": 0,
            },
        )

        with patch("renovation_hub.ledger.os.replace", side_effect=SystemExit("simulated crash")):
            with self.assertRaises(SystemExit):
                store.seed_primary(manifest_id)

        destination = root / prepared["manifest"]["staging_relative_path"]
        baseline = destination / "rollback" / "pre-primary.sqlite3"
        baseline_digest = hashlib.sha256(baseline.read_bytes()).hexdigest()
        reopened = RenovationHubStore(
            root / "ledger.sqlite3",
            data_dir=root,
            share_dir=root / "share",
            enforce_cutover_manifest=True,
        )
        recovered = reopened.seed_primary(manifest_id)
        self.assertEqual(recovered["state"], "primary_seeded")
        self.assertEqual(hashlib.sha256(baseline.read_bytes()).hexdigest(), baseline_digest)
        self.assertEqual(reopened.status()["counts"]["payments"], 1)

    def test_interrupted_seed_recovers_after_each_attachment_switch_boundary(self) -> None:
        real_replace = os.replace
        for crash_after in (1, 2):
            with self.subTest(crash_after=crash_after):
                root = self.root / f"interrupted-switch-{crash_after}"
                store = RenovationHubStore(
                    root / "ledger.sqlite3",
                    data_dir=root,
                    share_dir=root / "share",
                    enforce_cutover_manifest=True,
                )
                prepared = store.prepare_primary_migration(
                    self.v2_archive,
                    self._migration_evidence(store),
                )
                manifest_id = prepared["manifest"]["id"]
                store.mark_source_frozen(
                    manifest_id,
                    {
                        "hermes_writer_stopped": True,
                        "running_transactions": 0,
                        "queue_depth": 0,
                    },
                )
                calls = 0

                def replace_then_crash(source: object, target: object) -> None:
                    nonlocal calls
                    real_replace(source, target)
                    calls += 1
                    if calls == crash_after:
                        raise SystemExit(f"simulated crash after replace {crash_after}")

                with patch("renovation_hub.ledger.os.replace", side_effect=replace_then_crash):
                    with self.assertRaises(SystemExit):
                        store.seed_primary(manifest_id)

                destination = root / prepared["manifest"]["staging_relative_path"]
                baseline = destination / "rollback" / "pre-primary.sqlite3"
                baseline_digest = hashlib.sha256(baseline.read_bytes()).hexdigest()
                reopened = RenovationHubStore(
                    root / "ledger.sqlite3",
                    data_dir=root,
                    share_dir=root / "share",
                    enforce_cutover_manifest=True,
                )
                recovered = reopened.seed_primary(manifest_id)
                self.assertEqual(recovered["state"], "primary_seeded")
                self.assertEqual(hashlib.sha256(baseline.read_bytes()).hexdigest(), baseline_digest)
                self.assertEqual(reopened.status()["counts"]["payments"], 1)

    def test_interrupted_seed_recovers_after_validation_before_manifest_mark(self) -> None:
        root = self.root / "interrupted-before-mark"
        store = RenovationHubStore(
            root / "ledger.sqlite3",
            data_dir=root,
            share_dir=root / "share",
            enforce_cutover_manifest=True,
        )
        prepared = store.prepare_primary_migration(
            self.v2_archive,
            self._migration_evidence(store),
        )
        manifest_id = prepared["manifest"]["id"]
        store.mark_source_frozen(
            manifest_id,
            {
                "hermes_writer_stopped": True,
                "running_transactions": 0,
                "queue_depth": 0,
            },
        )
        with patch.object(store, "_mark_primary_seeded", side_effect=SystemExit("before mark")):
            with self.assertRaises(SystemExit):
                store.seed_primary(manifest_id)
        destination = root / prepared["manifest"]["staging_relative_path"]
        baseline = destination / "rollback" / "pre-primary.sqlite3"
        baseline_digest = hashlib.sha256(baseline.read_bytes()).hexdigest()
        reopened = RenovationHubStore(
            root / "ledger.sqlite3",
            data_dir=root,
            share_dir=root / "share",
            enforce_cutover_manifest=True,
        )
        recovered = reopened.seed_primary(manifest_id)
        self.assertEqual(recovered["state"], "primary_seeded")
        self.assertEqual(hashlib.sha256(baseline.read_bytes()).hexdigest(), baseline_digest)
        self.assertEqual(reopened.status()["counts"]["payments"], 1)

    def test_primary_migration_rejects_bad_evidence_nonempty_and_tampered_staging(self) -> None:
        root = self.root / "rejections"
        store = RenovationHubStore(
            root / "ledger.sqlite3",
            data_dir=root,
            share_dir=root / "share",
            enforce_cutover_manifest=True,
        )
        evidence = self._migration_evidence(store)
        wrong = dict(evidence)
        wrong["source_sha256"] = "0" * 64
        with self.assertRaises(LedgerError) as digest_error:
            store.prepare_primary_migration(self.v2_archive, wrong)
        self.assertEqual(digest_error.exception.code, "source_digest_mismatch")
        wrong_verification = dict(evidence)
        wrong_verification["verification_digest"] = "0" * 64
        with self.assertRaises(LedgerError) as verification_error:
            store.prepare_primary_migration(self.v2_archive, wrong_verification)
        self.assertEqual(
            verification_error.exception.code, "verification_digest_mismatch"
        )
        wrong_backup = dict(evidence)
        wrong_backup["backup_manifest"] = {"haos": {"verified": True}}
        with self.assertRaises(LedgerError) as backup_error:
            store.prepare_primary_migration(self.v2_archive, wrong_backup)
        self.assertEqual(backup_error.exception.code, "backup_manifest_required")
        prepared = store.prepare_primary_migration(self.v2_archive, evidence)
        manifest_id = prepared["manifest"]["id"]
        with self.assertRaises(LedgerError) as jump_error:
            store.mark_cutover_ready(
                manifest_id,
                {
                    "owner_bound": True,
                    "poller_state": "polling",
                    "queue_depth": 0,
                    "recovery_depth": 0,
                    "single_writer": True,
                    "rollback_ready": True,
                },
            )
        self.assertEqual(jump_error.exception.code, "invalid_writer_transition")
        store.mark_source_frozen(
            manifest_id,
            {
                "hermes_writer_stopped": True,
                "running_transactions": 0,
                "queue_depth": 0,
            },
        )
        staging = root / prepared["manifest"]["staging_relative_path"] / "ledger.sqlite3"
        connection = sqlite3.connect(staging)
        connection.execute("UPDATE transactions SET amount_cents=1 WHERE legacy_id=1")
        connection.commit()
        connection.close()
        with self.assertRaises(LedgerError) as tampered:
            store.seed_primary(manifest_id)
        self.assertEqual(tampered.exception.code, "invariant_mismatch")
        self.assertEqual(store.status()["counts"]["payments"], 0)

        nonempty_root = self.root / "nonempty"
        nonempty = RenovationHubStore(
            nonempty_root / "ledger.sqlite3",
            data_dir=nonempty_root,
            share_dir=nonempty_root / "share",
            enforce_cutover_manifest=True,
        )
        nonempty_evidence = self._migration_evidence(nonempty)
        nonempty_prepared = nonempty.prepare_primary_migration(
            self.v2_archive, nonempty_evidence
        )
        nonempty.mark_source_frozen(
            nonempty_prepared["manifest"]["id"],
            {
                "hermes_writer_stopped": True,
                "running_transactions": 0,
                "queue_depth": 0,
            },
        )
        with nonempty._connect() as connection:
            connection.execute(
                "INSERT INTO projects(id,name,timezone,budget_cents,status,version,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?)",
                ("existing", "已有项目", "Asia/Shanghai", 0, "active", 1, "2026-08-04T00:00:00Z", "2026-08-04T00:00:00Z"),
            )
        with self.assertRaises(LedgerError) as not_empty:
            nonempty.seed_primary(nonempty_prepared["manifest"]["id"])
        self.assertEqual(not_empty.exception.code, "primary_not_empty")

    def test_primary_seed_rejects_tampered_or_extra_hub_derived_state(self) -> None:
        timestamp = "2026-08-04T00:00:00Z"
        for case in ("project", "stage", "area", "context", "missing_context"):
            with self.subTest(case=case):
                root = self.root / f"derived-tamper-{case}"
                store = RenovationHubStore(
                    root / "ledger.sqlite3",
                    data_dir=root,
                    share_dir=root / "share",
                    enforce_cutover_manifest=True,
                )
                prepared = store.prepare_primary_migration(
                    self.v2_archive,
                    self._migration_evidence(store),
                )
                manifest_id = prepared["manifest"]["id"]
                store.mark_source_frozen(
                    manifest_id,
                    {
                        "hermes_writer_stopped": True,
                        "running_transactions": 0,
                        "queue_depth": 0,
                    },
                )
                staging = root / prepared["manifest"]["staging_relative_path"] / "ledger.sqlite3"
                connection = sqlite3.connect(staging)
                project_id = connection.execute("SELECT id FROM projects").fetchone()[0]
                if case == "project":
                    connection.execute("UPDATE projects SET name='篡改项目'")
                elif case == "stage":
                    connection.execute(
                        "INSERT INTO stages(id,project_id,name,position,status,color,planned_start,planned_end,actual_start,actual_end,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                        ("stage-extra", project_id, "额外阶段", 0, "planned", "#8B5CF6", None, None, None, None, timestamp, timestamp),
                    )
                elif case == "area":
                    connection.execute(
                        "INSERT INTO areas(id,project_id,name,position,status,version,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?)",
                        ("area-extra", project_id, "额外空间", 0, "active", 1, timestamp, timestamp),
                    )
                elif case == "context":
                    connection.execute("UPDATE transaction_context SET version=2 WHERE transaction_id='1'")
                else:
                    connection.execute("DELETE FROM transaction_context WHERE transaction_id='1'")
                connection.commit()
                connection.close()
                with self.assertRaises(LedgerError) as tampered:
                    store.seed_primary(manifest_id)
                self.assertEqual(tampered.exception.code, "invariant_mismatch")
                self.assertEqual(store.status()["counts"]["payments"], 0)

    def test_v2_invalid_grouped_tag_is_rejected(self) -> None:
        def mutate(package: Path) -> None:
            database = package / "bookkeeping.sqlite3"
            connection = sqlite3.connect(database)
            connection.execute(
                "UPDATE transaction_tags SET tag='无维度标签',tag_key='无维度标签' WHERE id=1"
            )
            connection.commit()
            connection.close()

        candidate = self.v2_fixture.mutate_and_repack("v2-invalid-tag.zip", mutate)
        with self.assertRaisesRegex(LedgerError, "分组标签") as context:
            self.store.verify_portable(candidate)
        self.assertEqual(context.exception.code, "import_invalid")

    def test_ledger_field_drift_is_rejected_even_with_updated_manifest(self) -> None:
        def mutate(package: Path) -> None:
            ledger_path = package / "ledger.json"
            ledger = json.loads(ledger_path.read_text(encoding="utf-8"))
            ledger["transactions"][0]["vendor"] = "漂移值"
            ledger_path.write_text(json.dumps(ledger, ensure_ascii=False, sort_keys=True), encoding="utf-8")

        candidate = self.fixture.mutate_and_repack("field-drift.zip", mutate)
        with self.assertRaisesRegex(LedgerError, "transactions") as context:
            self.store.verify_portable(candidate)
        self.assertEqual(context.exception.code, "import_invalid")

    def test_attachment_tamper_is_rejected_even_with_updated_manifest(self) -> None:
        candidate = self.fixture.mutate_and_repack(
            "attachment-tamper.zip",
            lambda package: (package / "attachments" / "1" / "receipt.png").write_bytes(b"tampered"),
        )
        with self.assertRaisesRegex(LedgerError, "附件") as context:
            self.store.verify_portable(candidate)
        self.assertEqual(context.exception.code, "import_invalid")

    def test_audit_reordering_is_rejected_even_with_updated_manifest(self) -> None:
        def mutate(package: Path) -> None:
            audit = package / "audit_log.jsonl"
            lines = audit.read_text(encoding="utf-8").splitlines()
            audit.write_text("\n".join(reversed(lines)) + "\n", encoding="utf-8")

        candidate = self.fixture.mutate_and_repack("audit-reordered.zip", mutate)
        with self.assertRaisesRegex(LedgerError, "audit_log") as context:
            self.store.verify_portable(candidate)
        self.assertEqual(context.exception.code, "import_invalid")

    def test_unsafe_symlink_and_duplicate_members_are_rejected(self) -> None:
        symlink = self.root / "symlink.zip"
        with zipfile.ZipFile(self.archive) as source, zipfile.ZipFile(symlink, "w") as target:
            for info in source.infolist():
                target.writestr(info, source.read(info.filename))
            link = zipfile.ZipInfo("attachments/link")
            link.create_system = 3
            link.external_attr = (stat.S_IFLNK | 0o777) << 16
            target.writestr(link, "1/receipt.png")
        with self.assertRaises(LedgerError) as symlink_context:
            self.store.verify_portable(symlink)
        self.assertEqual(symlink_context.exception.code, "import_invalid")

        duplicate = self.root / "duplicate.zip"
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", UserWarning)
            with zipfile.ZipFile(self.archive) as source, zipfile.ZipFile(duplicate, "w") as target:
                for info in source.infolist():
                    target.writestr(info, source.read(info.filename))
                target.writestr("ledger.json", source.read("ledger.json"))
        with self.assertRaises(LedgerError) as duplicate_context:
            self.store.verify_portable(duplicate)
        self.assertEqual(duplicate_context.exception.code, "import_invalid")

    def test_existing_shadow_is_revalidated_instead_of_trusting_cached_report(self) -> None:
        report = self.store.import_shadow(self.archive)
        database = self.store.shadow_dir / report["source_sha256"] / "ledger.sqlite3"
        connection = sqlite3.connect(database)
        connection.execute("UPDATE transactions SET source_ref='tampered' WHERE legacy_id=1")
        connection.commit()
        connection.close()
        with self.assertRaises(LedgerError) as context:
            self.store.import_shadow(self.archive)
        self.assertEqual(context.exception.code, "invariant_mismatch")


if __name__ == "__main__":
    unittest.main()
