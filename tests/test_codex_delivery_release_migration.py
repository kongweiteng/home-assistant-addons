"""Release v5 adoption/recovery tests using real private SQLite databases."""
from contextlib import closing
from pathlib import Path
import os
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from codex_delivery.codex_delivery.durability import Snapshot, ReplicaReceipt, digest
from codex_delivery.codex_delivery.ha_release import HAReleaseCoordinator
from codex_delivery.codex_delivery.store import (
    DeliveryError, DeliveryStore, SCHEMA_VERSION, _install_release_schema,
)

# Frozen v4 candidate schema, originally owned by Coordinator construction.
LEGACY_RELEASE_DDL = """
CREATE TABLE IF NOT EXISTS ha_release_operations(
 deployment_ref TEXT PRIMARY KEY,owner_ref TEXT NOT NULL,task_ref TEXT NOT NULL,
 project_ref TEXT NOT NULL,target_ref TEXT NOT NULL,request_digest TEXT NOT NULL,
 request_json TEXT NOT NULL,state TEXT NOT NULL,evidence_json TEXT NOT NULL,
 created_at TEXT NOT NULL,updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS ha_release_locks(
 resource_ref TEXT PRIMARY KEY,deployment_ref TEXT NOT NULL UNIQUE
 REFERENCES ha_release_operations(deployment_ref));
CREATE TABLE IF NOT EXISTS ha_release_steps(
 deployment_ref TEXT NOT NULL REFERENCES ha_release_operations(deployment_ref),
 step TEXT NOT NULL,operation_ref TEXT NOT NULL UNIQUE,state TEXT NOT NULL,
 result_json TEXT NOT NULL,created_at TEXT NOT NULL,updated_at TEXT NOT NULL,
 PRIMARY KEY(deployment_ref,step));
"""
TABLES = ('ha_release_operations', 'ha_release_locks', 'ha_release_steps')


class ReleaseMigrationTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.path = self.root / 'delivery.sqlite3'
        self.store = self.open()

    def open(self, **kwargs):
        return DeliveryStore(self.path, local_domain='primary', **kwargs)

    def legacy(self, version=4, *, candidate=False, malformed=None):
        with closing(self.store._connect()) as c:
            for table in reversed(TABLES):
                c.execute('DROP TABLE ' + table)
            if candidate:
                ddl = LEGACY_RELEASE_DDL
                if malformed == 'missing_unique':
                    ddl = ddl.replace('deployment_ref TEXT NOT NULL UNIQUE', 'deployment_ref TEXT NOT NULL')
                if malformed == 'missing_fk':
                    ddl = ddl.replace('REFERENCES ha_release_operations(deployment_ref)', '')
                c.executescript(ddl)
                self.add_release_rows(c)
            c.execute('PRAGMA user_version=' + str(version))

    def add_release_rows(self, c):
        c.execute("INSERT INTO ha_release_operations VALUES('deploy','owner','task','project','target','digest','{}','recovery_required','{}','created','updated')")
        c.execute("INSERT INTO ha_release_locks VALUES('instance:canary','deploy')")
        c.execute("INSERT INTO ha_release_steps VALUES('deploy','update','op','unknown','{}','created','updated')")

    def rows(self, path=None):
        with closing(sqlite3.connect(path or self.path)) as c:
            return {table: c.execute('SELECT * FROM ' + table).fetchall() for table in TABLES
                    if c.execute('SELECT 1 FROM sqlite_master WHERE name=?', (table,)).fetchone()}

    def image(self):
        path = self.root / 'snapshot.sqlite3'
        with closing(sqlite3.connect(self.path)) as source, closing(sqlite3.connect(path)) as target:
            source.backup(target)
            target.execute('PRAGMA journal_mode=DELETE')
        content = path.read_bytes()
        path.unlink()
        snapshot = Snapshot('release-snapshot', content, digest(content))
        return snapshot, ReplicaReceipt(snapshot.event_id, snapshot.sha256, 'replica', 'object')

    def coordinator(self):
        return HAReleaseCoordinator(self.store, targets={}, releases={}, manager=None, authorize=lambda *_: False)

    def test_fresh_v5_has_every_release_table_and_installer_requires_transaction(self):
        with closing(self.store._connect()) as c:
            self.assertEqual(c.execute('PRAGMA user_version').fetchone()[0], SCHEMA_VERSION)
            self.assertEqual(set(self.rows()), set(TABLES))
            with self.assertRaisesRegex(DeliveryError, 'release_migration_transaction_required'):
                _install_release_schema(c)

    def test_v1_through_v4_require_explicit_upgrade_and_preserve_private_backup(self):
        for version in (1, 2, 3, 4):
            with self.subTest(version=version):
                self.legacy(version)
                with self.assertRaisesRegex(DeliveryError, 'schema_upgrade_required'):
                    self.open()
                with patch('codex_delivery.codex_delivery.store.os.fsync', wraps=os.fsync) as fsync:
                    self.store = self.open(upgrade_schema=True)
                self.assertGreaterEqual(fsync.call_count, 2)
                self.assertEqual(self.store.schema_backup.stat().st_mode & 0o777, 0o600)
                with closing(sqlite3.connect(self.store.schema_backup)) as c:
                    self.assertEqual(c.execute('PRAGMA user_version').fetchone()[0], version)
                    self.assertEqual(c.execute('PRAGMA quick_check').fetchone()[0], 'ok')
                    self.assertIsNone(c.execute('PRAGMA foreign_key_check').fetchone())
                self.assertEqual(set(self.rows()), set(TABLES))

    def test_compatible_v4_candidate_retains_all_rows_locks_and_unknown_steps(self):
        self.legacy(candidate=True)
        before = self.rows()
        upgraded = self.open(upgrade_schema=True)
        self.assertEqual(self.rows(), before)
        self.assertEqual(self.rows(upgraded.schema_backup), before)
        self.assertEqual(upgraded.schema_backup.stat().st_mode & 0o777, 0o600)
        with closing(upgraded._connect()) as c:
            self.assertEqual(c.execute('PRAGMA user_version').fetchone()[0], SCHEMA_VERSION)
            self.assertIsNone(c.execute('PRAGMA foreign_key_check').fetchone())

    def test_partial_compatible_v4_schema_is_adopted_without_rewriting_existing_rows(self):
        self.legacy(candidate=True)
        with closing(self.store._connect()) as c:
            c.execute('DROP TABLE ha_release_steps')
        before = self.rows()
        self.open(upgrade_schema=True)
        after = self.rows()
        self.assertEqual(after['ha_release_operations'], before['ha_release_operations'])
        self.assertEqual(after['ha_release_locks'], before['ha_release_locks'])
        self.assertEqual(after['ha_release_steps'], [])

    def test_v4_constraint_drift_fails_transaction_and_keeps_candidate_and_backup(self):
        for defect in ('missing_unique', 'missing_fk'):
            with self.subTest(defect=defect):
                self.legacy(candidate=True, malformed=defect)
                before = self.rows()
                with self.assertRaisesRegex(DeliveryError, 'schema_release_invalid'):
                    self.open(upgrade_schema=True)
                self.assertEqual(self.rows(), before)
                with closing(self.store._connect()) as c:
                    self.assertEqual(c.execute('PRAGMA user_version').fetchone()[0], 4)
                backups = list(self.root.glob('*.v4-*.sqlite3'))
                self.assertEqual(len(backups), 1)
                self.assertEqual(self.rows(backups[0]), before)
                backups[0].unlink()
                # Reset the malformed fixture without accepting it as v5.
                self.legacy(candidate=False)
                self.store = self.open(upgrade_schema=True)
                self.store.schema_backup.unlink()

    def test_v4_install_failure_rolls_back_all_release_ddl_and_preserves_backup(self):
        self.legacy()
        def broken(c):
            _install_release_schema(c)
            raise RuntimeError('injected release migration failure')
        with patch('codex_delivery.codex_delivery.store._install_release_schema', broken):
            with self.assertRaisesRegex(RuntimeError, 'injected release migration failure'):
                self.open(upgrade_schema=True)
        self.assertEqual(self.rows(), {})
        with closing(self.store._connect()) as c:
            self.assertEqual(c.execute('PRAGMA user_version').fetchone()[0], 4)
        self.assertEqual(len(list(self.root.glob('*.v4-*.sqlite3'))), 1)

    def test_v5_missing_release_table_is_never_reinstalled_by_open_or_coordinator(self):
        with closing(self.store._connect()) as c:
            c.execute('DROP TABLE ha_release_steps')
        for option in (False, True):
            with self.assertRaisesRegex(DeliveryError, 'schema_release_invalid'):
                self.open(upgrade_schema=option)
        with self.assertRaisesRegex(DeliveryError, 'schema_release_invalid'):
            self.coordinator()
        self.assertNotIn('ha_release_steps', self.rows())
        self.assertEqual(list(self.root.glob('*.v*-*.sqlite3')), [])

    def test_v5_extra_release_trigger_is_not_silently_trusted(self):
        with closing(self.store._connect()) as c:
            c.execute("CREATE TRIGGER release_surprise AFTER INSERT ON ha_release_operations BEGIN DELETE FROM ha_release_steps; END")
        with self.assertRaisesRegex(DeliveryError, 'schema_release_invalid'):
            self.open()

    def test_coordinator_initialization_only_reads_even_in_recovery(self):
        original = self.store._connect
        statements = []
        def connect():
            c = original()
            c.set_trace_callback(statements.append)
            return c
        self.store.recovery_mode = True
        with patch.object(self.store, '_connect', connect):
            self.coordinator()
        self.assertTrue(statements)
        self.assertTrue(all(s.lstrip().upper().startswith('SELECT') for s in statements))

    def test_v4_replica_requires_opt_in_then_keeps_release_rows_and_recovery_frozen(self):
        self.legacy(candidate=True)
        before = self.rows()
        snapshot, receipt = self.image()
        path = self.root / 'restored.sqlite3'
        with self.assertRaisesRegex(DeliveryError, 'schema_upgrade_required'):
            DeliveryStore.restore(path, snapshot, receipt, local_domain='replacement', replica_domain='replica')
        self.assertFalse(path.exists())
        restored = DeliveryStore.restore(path, snapshot, receipt, local_domain='replacement',
                                         replica_domain='replica', upgrade_schema=True)
        self.assertTrue(restored.recovery_mode)
        self.assertEqual(self.rows(path), before)
        self.assertEqual(self.rows(restored.schema_backup), before)
        with self.assertRaisesRegex(DeliveryError, 'recovery_read_only'):
            restored.create_project('owner', 'new', project_alias='new', name='New')
        self.assertEqual(restored.schema_backup.stat().st_mode & 0o777, 0o600)
