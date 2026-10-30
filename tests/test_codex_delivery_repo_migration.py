"""Real SQLite/Git fixtures; no production storage, credentials or acceptance."""
from contextlib import closing
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from codex_delivery.codex_delivery.durability import ReplicaReceipt, Snapshot, digest
from codex_delivery.codex_delivery.journal import InputJournal
from codex_delivery.codex_delivery.repo_manager import install_repository_schema
from codex_delivery.codex_delivery.store import DeliveryError, DeliveryStore, LEGACY_TASK_COLUMNS, SCHEMA_VERSION
from test_codex_delivery_store import ReplicaFixture


REPO_TABLES = ('repository_publications', 'repository_candidates',
               'repository_artifacts', 'repository_mirrors')


class RepositoryMigrationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.path = self.root / 'delivery.sqlite3'
        self.replica = ReplicaFixture()
        self.store = self.open()
        repo = self.store.enroll_repository('owner', 'repo', repo_alias='sample',
                                           remote_identity='github:example/sample')
        project = self.store.create_project('owner', 'project', project_alias='sample', name='Sample')
        self.project = project['project_id']
        self.store.bind_repository('owner', 'bind', project_id=self.project,
                                  repo_id=repo['repo_id'], expected_revision=1)
        task = self.store.plan('owner', 'plan', project_id=self.project,
            expected_project_revision=2, conversation_ref='chat', source_event_ref='source',
            requirement='Keep all evidence', mode='design')
        self.task = task['task_id']
        self.operation = InputJournal(self.store).submit('revise',
            {'task_ref': self.task, 'project_ref': self.project, 'supplement': 'Keep input', 'expected_revision': 1},
            {'actor_ref': 'owner', 'conversation_ref': 'chat', 'source_event_ref': 'input',
             'input_revision': 1, 'call_digest': 'fixture', 'input_digest': 'fixture'})['operation_ref']

    def open(self, **kwargs):
        return DeliveryStore(self.path, local_domain='primary', replica_domain='replica',
                             replica=self.replica, **kwargs)

    def legacy(self, version, *, drop_repository=True):
        with closing(sqlite3.connect(self.path, isolation_level=None)) as c:
            c.execute('PRAGMA foreign_keys=OFF')
            c.execute('BEGIN IMMEDIATE')
            if drop_repository:
                for table in REPO_TABLES:
                    c.execute('DROP TABLE ' + table)
            if version < 3:
                c.execute('''CREATE TABLE tasks_legacy (
                    task_id TEXT PRIMARY KEY,owner_ref TEXT NOT NULL,project_id TEXT NOT NULL,
                    conversation_ref TEXT NOT NULL,source_event_ref TEXT NOT NULL,mode TEXT NOT NULL,
                    requirement TEXT NOT NULL,project_revision INTEGER NOT NULL,revision INTEGER NOT NULL,
                    phase TEXT NOT NULL,state TEXT NOT NULL,created_at TEXT NOT NULL,updated_at TEXT NOT NULL,
                    UNIQUE(owner_ref,project_id,task_id))''')
                c.execute('INSERT INTO tasks_legacy SELECT ' + ','.join(LEGACY_TASK_COLUMNS) + ' FROM tasks')
                c.execute('DROP TABLE tasks')
                c.execute('ALTER TABLE tasks_legacy RENAME TO tasks')
            if version == 1:
                c.execute('DROP TABLE input_journal')
            c.execute('PRAGMA user_version=' + str(version))
            c.execute('COMMIT')
            self.assertIsNone(c.execute('PRAGMA foreign_key_check').fetchone())

    def image(self, event='fixture-snapshot'):
        target = self.root / 'image.sqlite3'
        with closing(sqlite3.connect(self.path)) as source, closing(sqlite3.connect(target)) as backup:
            source.backup(backup)
            backup.execute('PRAGMA journal_mode=DELETE')
        content = target.read_bytes()
        target.unlink()
        snapshot = Snapshot(event, content, digest(content))
        return snapshot, ReplicaReceipt(event, snapshot.sha256, 'replica', event)

    def evidence(self, path=None):
        with closing(sqlite3.connect(path or self.path)) as c:
            return {name: c.execute('SELECT * FROM ' + name).fetchall()
                    for name in ('tasks', 'task_repositories', 'events', 'commands', 'durability',
                                 'notification_outbox', 'runtime_guard', 'input_journal')
                    if c.execute('SELECT 1 FROM sqlite_master WHERE name=?', (name,)).fetchone()}

    def test_new_schema_v5_installs_repo_objects_and_installer_requires_transaction(self):
        with closing(self.store._connect()) as c:
            self.assertEqual(c.execute('PRAGMA user_version').fetchone()[0], SCHEMA_VERSION)
            for table in REPO_TABLES:
                self.assertIsNotNone(c.execute('SELECT sql FROM sqlite_master WHERE name=?', (table,)).fetchone())
            with self.assertRaisesRegex(DeliveryError, 'repository_migration_transaction_required'):
                install_repository_schema(c)

    def test_v1_v2_v3_implicit_open_rejected_without_backup_or_schema_change(self):
        for version in (3, 2, 1):
            with self.subTest(version=version):
                # Each iteration starts from the current v5 database.
                self.legacy(version)
                before = self.evidence()
                with self.assertRaisesRegex(DeliveryError, 'schema_upgrade_required'):
                    self.open()
                self.assertEqual(self.evidence(), before)
                self.assertEqual(list(self.root.glob('*.v*-*.sqlite3')), [])
                self.store = self.open(upgrade_schema=True)
                self.store.schema_backup.unlink()

    def test_explicit_legacy_upgrades_preserve_tasks_children_journal_and_event_chain(self):
        for version in (3, 2, 1):
            with self.subTest(version=version):
                self.legacy(version)
                before = self.evidence()
                with patch('codex_delivery.codex_delivery.store.os.fsync', wraps=os.fsync) as fsync:
                    upgraded = self.open(upgrade_schema=True)
                self.assertGreaterEqual(fsync.call_count, 2)
                self.assertEqual(upgraded.schema_backup.stat().st_mode & 0o777, 0o600)
                with closing(sqlite3.connect(upgraded.schema_backup)) as c:
                    self.assertEqual(c.execute('PRAGMA user_version').fetchone()[0], version)
                    self.assertEqual(c.execute('PRAGMA quick_check').fetchone()[0], 'ok')
                    self.assertIsNone(c.execute('PRAGMA foreign_key_check').fetchone())
                after = self.evidence()
                for table, rows in before.items():
                    if table != 'tasks':
                        self.assertEqual(after[table], rows)
                self.assertEqual(tuple(after['tasks'][0][:13]), tuple(before['tasks'][0][:13]))
                self.assertEqual(upgraded.verify_event_chain(), len(before['events']))
                self.assertEqual(upgraded.task('owner', self.project, self.task)['repositories'][0]['repo_id'],
                                 before['task_repositories'][0][3])
                self.store = upgraded

    def test_v3_work_runtime_fields_and_recovery_guard_survive_upgrade(self):
        with closing(self.store._connect()) as c:
            c.execute("UPDATE tasks SET lease_epoch=7,lease_ref='lease',assigned_runner_ref='runner',work_json='{}',runtime_result_json='{}'")
            c.execute('UPDATE runtime_guard SET recovery_required=1')
        self.legacy(3)
        before = self.evidence()
        upgraded = self.open(upgrade_schema=True)
        self.assertTrue(upgraded.recovery_mode)
        self.assertEqual(self.evidence(), before)

    def test_installer_failure_after_ddl_rolls_back_upgrade_and_preserves_backup(self):
        self.legacy(3)
        before = self.evidence()
        def broken(c):
            install_repository_schema(c)
            raise RuntimeError('fixture injected failure')
        with patch('codex_delivery.codex_delivery.store._install_repository_schema', broken):
            with self.assertRaisesRegex(RuntimeError, 'fixture injected failure'):
                self.open(upgrade_schema=True)
        self.assertEqual(self.evidence(), before)
        with closing(self.store._connect()) as c:
            self.assertEqual(c.execute('PRAGMA user_version').fetchone()[0], 3)
            self.assertIsNone(c.execute("SELECT 1 FROM sqlite_master WHERE name='repository_mirrors'").fetchone())
        self.assertEqual(len(list(self.root.glob('*.v3-*.sqlite3'))), 1)
        self.assertIsNotNone(self.open(upgrade_schema=True))

    def test_fresh_installer_failure_leaves_no_partial_tables_or_v5(self):
        path = self.root / 'fresh.sqlite3'
        def broken(c):
            install_repository_schema(c)
            raise RuntimeError('fixture injected failure')
        with patch('codex_delivery.codex_delivery.store._install_repository_schema', broken):
            with self.assertRaises(RuntimeError):
                DeliveryStore(path, local_domain='primary')
        with closing(sqlite3.connect(path)) as c:
            self.assertEqual(c.execute('PRAGMA user_version').fetchone()[0], 0)
            self.assertEqual(c.execute('SELECT count(*) FROM sqlite_master').fetchone()[0], 0)

    def test_malformed_v3_repository_definition_is_not_blessed_or_repaired(self):
        self.legacy(3)
        with closing(self.store._connect()) as c:
            c.execute('CREATE TABLE repository_mirrors(repo_id TEXT)')
        with self.assertRaises((DeliveryError, sqlite3.Error)):
            self.open(upgrade_schema=True)
        with closing(self.store._connect()) as c:
            self.assertEqual(c.execute('PRAGMA user_version').fetchone()[0], 3)
            self.assertEqual(len(c.execute('PRAGMA table_info(repository_mirrors)').fetchall()), 1)
            self.assertIsNone(c.execute("SELECT 1 FROM sqlite_master WHERE name='repository_artifacts'").fetchone())
        self.assertEqual(len(list(self.root.glob('*.v3-*.sqlite3'))), 1)

    def test_current_v5_missing_repo_trigger_fails_even_with_upgrade_opt_in(self):
        with closing(self.store._connect()) as c:
            trigger = c.execute("SELECT name FROM sqlite_master WHERE type='trigger' AND name LIKE 'repository_%' LIMIT 1").fetchone()[0]
            c.execute('DROP TRIGGER ' + trigger)
        for option in (False, True):
            with self.assertRaisesRegex(DeliveryError, 'schema_repository_invalid'):
                self.open(upgrade_schema=option)
        self.assertEqual(list(self.root.glob('*.v5-*.sqlite3')), [])

    def test_current_v5_missing_repo_table_is_not_implicitly_reinstalled(self):
        with closing(self.store._connect()) as c:
            c.execute('DROP TABLE repository_publications')
        with self.assertRaisesRegex(DeliveryError, 'schema_repository_invalid'):
            self.open(upgrade_schema=True)
        with closing(self.store._connect()) as c:
            self.assertIsNone(c.execute("SELECT 1 FROM sqlite_master WHERE name='repository_publications'").fetchone())

    def test_corrupt_sqlite_header_is_rejected_without_source_rewrite_or_backup(self):
        path = self.root / 'corrupt.sqlite3'
        content = b'fixture invalid SQLite header' + b'\x00' * 512
        path.write_bytes(content)
        path.chmod(0o600)
        with self.assertRaises(sqlite3.DatabaseError):
            DeliveryStore(path, local_domain='primary', upgrade_schema=True)
        self.assertEqual(path.read_bytes(), content)
        self.assertEqual(list(self.root.glob('corrupt.sqlite3.v*-*.sqlite3')), [])

    def test_foreign_key_corruption_is_rejected_before_upgrade_and_backup(self):
        self.legacy(3)
        with closing(sqlite3.connect(self.path, isolation_level=None)) as c:
            c.execute('PRAGMA foreign_keys=OFF')
            c.execute("UPDATE task_repositories SET task_id='missing'")
        before = self.evidence()
        with self.assertRaisesRegex(DeliveryError, 'database_corrupt'):
            self.open(upgrade_schema=True)
        self.assertEqual(self.evidence(), before)
        self.assertEqual(list(self.root.glob('*.v3-*.sqlite3')), [])

    def test_v3_snapshot_requires_explicit_upgrade_and_restore_stays_frozen(self):
        self.legacy(3)
        snapshot, receipt = self.image()
        target = self.root / 'restored.sqlite3'
        with self.assertRaisesRegex(DeliveryError, 'schema_upgrade_required'):
            DeliveryStore.restore(target, snapshot, receipt, local_domain='replacement', replica_domain='replica')
        self.assertFalse(target.exists())
        restored = DeliveryStore.restore(target, snapshot, receipt, local_domain='replacement',
                                        replica_domain='replica', upgrade_schema=True)
        self.assertTrue(restored.recovery_mode)
        with self.assertRaisesRegex(DeliveryError, 'recovery_read_only'):
            restored.claim_notification()
        with self.assertRaisesRegex(DeliveryError, 'recovery_read_only'):
            restored.create_project('owner', 'new', project_alias='new', name='New')
        with closing(sqlite3.connect(restored.schema_backup)) as c:
            self.assertEqual(c.execute('PRAGMA user_version').fetchone()[0], 3)
        self.assertEqual(restored.verify_event_chain(), self.store.verify_event_chain())

    def test_v3_preview_repo_rows_survive_upgrade_and_v5_replica_restore(self):
        import test_codex_delivery_repo_manager as repo_tests
        scenario = repo_tests.RepoManagerTests('test_real_bare_sync_bundle_candidate_publish_and_events_single_task_source')
        scenario.setUp()
        self.addCleanup(scenario.doCleanups)
        publication = scenario.prepared()
        scenario.runtime.failure = 'before'
        result = scenario.manager.publish('owner', 'publish', publication_ref=publication)
        with closing(scenario.store._connect()) as c:
            before = {table: [tuple(r) for r in c.execute('SELECT * FROM ' + table)] for table in REPO_TABLES}
            self.assertTrue(before['repository_candidates'])
            self.assertEqual(c.execute('SELECT state FROM repository_publications').fetchone()[0], 'unknown')
            c.execute('PRAGMA user_version=3')
        upgraded = DeliveryStore(scenario.store.path, local_domain='primary', replica_domain='replica',
                                 replica=scenario.replica, upgrade_schema=True)
        with closing(upgraded._connect()) as c:
            self.assertEqual({table: [tuple(r) for r in c.execute('SELECT * FROM ' + table)] for table in REPO_TABLES}, before)
            self.assertIsNone(c.execute('PRAGMA foreign_key_check').fetchone())
        # The original immutable replica image is v5 with real Repo evidence.
        snapshot = scenario.replica.objects[result['event_id']]
        receipt = scenario.replica.receipts[result['event_id']]
        restored = DeliveryStore.restore(self.root / 'repo-restored.sqlite3', snapshot, receipt,
                                         local_domain='replacement', replica_domain='replica')
        with closing(restored._connect()) as c:
            self.assertEqual({table: [tuple(r) for r in c.execute('SELECT * FROM ' + table)] for table in REPO_TABLES}, before)
            self.assertIsNone(c.execute('PRAGMA foreign_key_check').fetchone())
        self.assertTrue(restored.recovery_mode)
        with self.assertRaisesRegex(DeliveryError, 'recovery_read_only'):
            restored.claim_notification()


class SharedReplicaAdmissionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / 'delivery.sqlite3'
        self.replica = ReplicaFixture()
        self.state = {'state': 'ready'}
        self.replica.status = lambda: self.state
        self.store = DeliveryStore(self.path, local_domain='primary', replica_domain='replica', replica=self.replica)

    def create(self, key='new'):
        return self.store.create_project('owner', key, project_alias=key, name=key)

    def test_byte_object_unknown_blocks_new_db_event_then_recovery_allows_same_command(self):
        for state in ('fused', 'inflight', 'busy', 'unknown', None):
            self.state = {'state': state, 'object_ref': 'opaque-byte-object'}
            with self.assertRaisesRegex(DeliveryError, '^durability_pending$'):
                self.create()
            with closing(self.store._connect()) as c:
                for table in ('projects', 'events', 'commands', 'durability', 'notification_outbox'):
                    self.assertEqual(c.execute('SELECT count(*) FROM ' + table).fetchone()[0], 0)
        self.state = {'state': 'ready'}
        self.assertEqual(self.create()['status'], 'accepted')
        self.assertEqual(self.store.verify_event_chain(), 1)

    def test_status_error_blocks_new_mutation_but_confirmed_idempotent_retry_remains_readable(self):
        accepted = self.create()
        def broken():
            raise RuntimeError('private fixture diagnostic')
        self.replica.status = broken
        self.assertEqual(self.create(), accepted)
        with self.assertRaisesRegex(DeliveryError, '^durability_pending$'):
            self.create('next')
        self.assertEqual(self.store.verify_event_chain(), 1)

    def test_pending_db_event_uses_its_original_frozen_snapshot_for_fused_get_only(self):
        self.replica.timeout_after_save = True
        pending = self.create()
        snapshot = self.replica.objects[pending['event_id']]
        self.state = {'state': 'fused'}
        reads = []
        def reconcile(value):
            reads.append(value)
            return self.replica.receipts[value.event_id]
        self.replica.reconcile = reconcile
        with patch.object(self.replica, 'save', side_effect=AssertionError('no replay')):
            self.assertEqual(self.create()['status'], 'accepted')
        self.assertEqual(reads, [snapshot])

    def test_input_journal_stays_local_only_during_byte_fuse(self):
        self.state = {'state': 'fused'}
        response = InputJournal(self.store).submit('project_control',
            {'project_ref': 'project', 'expected_revision': 1, 'action': 'pause'},
            {'actor_ref': 'owner', 'conversation_ref': 'chat', 'source_event_ref': 'source',
             'input_revision': 1, 'call_digest': 'fixture', 'input_digest': 'fixture'})
        self.assertEqual(response['durability'], 'local_only')
        self.assertEqual(self.store.verify_event_chain(), 0)


if __name__ == '__main__':
    unittest.main()
