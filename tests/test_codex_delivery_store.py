"""Delivery metadata failure tests; fake replicas do not prove NAS acceptance."""
from contextlib import closing
from dataclasses import replace
from datetime import datetime
import json
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest
from unittest.mock import patch
from zoneinfo import ZoneInfo

from codex_delivery.codex_delivery.durability import ReplicaReceipt
from codex_delivery.codex_delivery.store import DeliveryError, DeliveryStore


class ReplicaFixture:
    def __init__(self):
        self.objects = {}; self.receipts = {}
        self.timeout_after_save = False; self.available = True; self.bad_domain = False

    def save(self, snapshot):
        if not self.available:
            raise TimeoutError('private adapter diagnostic')
        old = self.objects.get(snapshot.event_id)
        if old and old.content != snapshot.content:
            raise ValueError('immutable_object_conflict')
        receipt = ReplicaReceipt(snapshot.event_id, snapshot.sha256,
                                 'primary' if self.bad_domain else 'replica', snapshot.event_id)
        self.objects[snapshot.event_id] = snapshot; self.receipts[snapshot.event_id] = receipt
        if self.timeout_after_save:
            raise TimeoutError('response lost after save')
        return receipt


class DeliveryStoreTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(); self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name); self.path = self.root / 'delivery.sqlite3'
        self.replica = ReplicaFixture(); self.store = self.open(); self.count = 0

    def open(self, **kwargs):
        return DeliveryStore(self.path, local_domain='primary', replica_domain='replica',
                             replica=kwargs.pop('replica', self.replica), **kwargs)

    def key(self):
        self.count += 1; return 'command-' + str(self.count)

    def project(self, name='alpha', owner='owner'):
        return self.store.create_project(owner, self.key(), project_alias=name, name=name)

    def repo(self, name='library'):
        return self.store.enroll_repository('owner', self.key(), repo_alias=name,
                                            remote_identity='github:example/' + name)

    def active(self, name='alpha', repository=None):
        project = self.project(name); repository = repository or self.repo(name)
        self.store.bind_repository('owner', self.key(), project_id=project['project_id'],
                                   repo_id=repository['repo_id'], expected_revision=1)
        return self.store.project_control('owner', self.key(), project_id=project['project_id'],
                                          expected_revision=2, action='activate')

    def plan(self, project, event='source-1'):
        return self.store.plan('owner', self.key(), project_id=project['project_id'],
                               expected_project_revision=project['revision'], conversation_ref='chat-1',
                               source_event_ref=event, requirement='Implement a tested report', mode='develop')

    def error(self, code, call):
        with self.assertRaises(DeliveryError) as caught: call()
        self.assertEqual(caught.exception.code, code)

    def test_multi_project_shared_and_independent_repository_bindings(self):
        shared = self.repo(); alpha = self.active('alpha', shared); beta = self.active('beta', shared)
        extra = self.repo('beta-only')
        beta = self.store.bind_repository('owner', self.key(), project_id=beta['project_id'],
                                          repo_id=extra['repo_id'], expected_revision=3, access='write')
        one = self.plan(alpha); two = self.plan(beta, 'source-2')
        self.assertEqual(len(self.store.task('owner', alpha['project_id'], one['task_id'])['repositories']), 1)
        self.assertEqual(len(self.store.task('owner', beta['project_id'], two['task_id'])['repositories']), 2)
        self.error('not_found', lambda: self.store.task('owner', beta['project_id'], one['task_id']))
        self.assertEqual(self.store.catalog('owner', kind='repositories')['total'], 2)

    def test_idempotency_once_and_argument_conflict(self):
        args = dict(project_alias='alpha', name='Alpha')
        first = self.store.create_project('owner', 'stable-key', **args)
        self.assertEqual(self.store.create_project('owner', 'stable-key', **args), first)
        self.error('idempotency_conflict', lambda: self.store.create_project('owner', 'stable-key', project_alias='alpha', name='Beta'))
        self.assertEqual(self.store.catalog('owner', kind='projects')['total'], 1)

    def test_duplicate_source_cannot_create_second_task(self):
        project = self.active(); self.plan(project)
        self.error('catalog_conflict', lambda: self.plan(project))
        self.assertEqual(self.store.catalog('owner', kind='tasks')['total'], 1)

    def test_no_replica_no_acceptance_or_notification_or_following_write(self):
        self.store = self.open(replica=None); first = self.project()
        self.assertEqual(first['status'], 'durability_pending')
        self.assertIsNone(self.store.claim_notification())
        self.error('durability_pending', lambda: self.project('beta'))
        self.assertEqual(self.store.catalog('owner', kind='projects')['total'], 1)

    def test_restart_after_upload_timeout_reconciles_identical_snapshot(self):
        self.replica.timeout_after_save = True; first = self.project()
        frozen = self.replica.objects[first['event_id']]
        self.assertEqual(first['status'], 'durability_pending')
        self.store = self.open(); self.replica.timeout_after_save = False
        self.assertEqual(self.store.reconcile(first['event_id'])['status'], 'accepted')
        self.assertEqual(self.replica.objects[first['event_id']].content, frozen.content)
        self.assertEqual(self.project('beta')['status'], 'accepted')

    def test_same_failure_domain_is_never_accepted(self):
        self.replica.bad_domain = True
        self.assertEqual(self.project()['status'], 'durability_pending')
        self.assertIsNone(self.store.claim_notification())

    def test_tampered_receipt_does_not_drop_input(self):
        original = self.replica.save
        self.replica.save = lambda s: replace(original(s), sha256='sha256:' + '0' * 64)
        self.assertEqual(self.project()['status'], 'durability_pending')
        self.assertEqual(self.store.verify_event_chain(), 1)

    def test_cross_owner_catalog_and_binding_denied(self):
        project = self.project(owner='other-owner'); repository = self.repo()
        self.error('not_found', lambda: self.store.bind_repository('owner', self.key(), project_id=project['project_id'], repo_id=repository['repo_id'], expected_revision=1))
        self.assertEqual(self.store.catalog('owner', kind='projects')['total'], 0)
        self.assertEqual(self.store.catalog('other-owner', kind='repositories')['total'], 0)

    def test_compare_swap_preserves_configuration_history(self):
        project = self.project(); repository = self.repo()
        self.store.bind_repository('owner', self.key(), project_id=project['project_id'], repo_id=repository['repo_id'], expected_revision=1)
        self.error('revision_conflict', lambda: self.store.project_control('owner', self.key(), project_id=project['project_id'], expected_revision=1, action='activate'))
        with closing(sqlite3.connect(self.path)) as c:
            self.assertEqual(c.execute('SELECT count(*) FROM project_versions').fetchone()[0], 2)
            self.assertEqual(json.loads(c.execute('SELECT config_json FROM project_versions WHERE revision=1').fetchone()[0])['repositories'], [])

    def test_task_repository_set_is_frozen(self):
        project = self.active(); task = self.plan(project); second = self.repo('second')
        self.store.bind_repository('owner', self.key(), project_id=project['project_id'], repo_id=second['repo_id'], expected_revision=3)
        old = self.store.task('owner', project['project_id'], task['task_id'])
        self.assertEqual(old['project_revision'], 3); self.assertEqual(len(old['repositories']), 1)
        self.assertFalse(task['execution_enabled'])

    def test_pause_and_archive_keep_source_and_existing_tasks(self):
        project = self.active(); task = self.plan(project)
        paused = self.store.project_control('owner', self.key(), project_id=project['project_id'], expected_revision=3, action='pause')
        self.error('project_inactive', lambda: self.plan(paused, 'new-event'))
        self.store.project_control('owner', self.key(), project_id=project['project_id'], expected_revision=4, action='archive')
        self.assertEqual(self.store.task('owner', project['project_id'], task['task_id'])['state'], 'queued')
        self.assertEqual(self.store.catalog('owner', kind='repositories')['total'], 1)

    def test_task_revision_controls_cannot_cross_owner_or_revision(self):
        project = self.active(); task = self.plan(project)
        args = dict(project_id=project['project_id'], task_id=task['task_id'], expected_revision=1)
        self.store.revise('owner', self.key(), **args, supplement='Keep all source files')
        self.error('revision_conflict', lambda: self.store.control('owner', self.key(), **args, action='cancel'))
        self.error('not_found', lambda: self.store.revise('other-owner', self.key(), **args, supplement='Other'))
        args['expected_revision'] = 2; self.store.control('owner', self.key(), **args, action='cancel')
        args['expected_revision'] = 3
        self.error('task_terminal', lambda: self.store.revise('owner', self.key(), **args, supplement='More'))

    def test_path_isolation_rejects_fallback_to_whole_repository(self):
        project = self.project(); repository = self.repo()
        self.error('path_isolation_unavailable', lambda: self.store.bind_repository('owner', self.key(), project_id=project['project_id'], repo_id=repository['repo_id'], expected_revision=1, paths=('allowed',)))

    def test_pagination_is_complete_and_owner_scoped(self):
        for name in ('alpha', 'beta', 'gamma'): self.project(name)
        first = self.store.catalog('owner', kind='projects', limit=2)
        last = self.store.catalog('owner', kind='projects', offset=first['next_offset'], limit=2)
        self.assertEqual(first['total'], 3); self.assertTrue(first['has_more']); self.assertFalse(last['has_more'])
        self.assertEqual(len(first['items']) + len(last['items']), 3)
        self.assertEqual(self.store.catalog('other-owner', kind='projects')['total'], 0)
        self.error('pagination_invalid', lambda: self.store.catalog('owner', kind='projects', limit=True))

    def test_events_reject_wrong_project_task_pair(self):
        one = self.active(); task = self.plan(one); two = self.project('beta')
        self.error('not_found', lambda: self.store.events('owner', project_id=two['project_id'], task_id=task['task_id']))
        result = self.store.events('owner', project_id=one['project_id'], task_id=task['task_id'])
        self.assertEqual(result['total'], 1); self.assertEqual(result['items'][0]['kind'], 'task.plan')

    def test_history_append_only(self):
        self.project()
        with closing(sqlite3.connect(self.path)) as c:
            with self.assertRaises(sqlite3.IntegrityError): c.execute("UPDATE events SET kind='forged'")
            with self.assertRaises(sqlite3.IntegrityError): c.execute('DELETE FROM project_versions')
        self.assertEqual(self.store.verify_event_chain(), 1)

    def test_unknown_notification_not_replayed_after_restart(self):
        self.project(); notification = self.store.claim_notification(); self.store = self.open()
        self.assertIsNone(self.store.claim_notification())
        self.store.finish_notification(notification['event_id'], evidence='gateway_sent')
        self.error('channel_evidence_invalid', lambda: self.store.finish_notification(notification['event_id'], evidence='phone_read'))

    def test_restore_two_projects_tasks_and_source_bindings_in_read_only_mode(self):
        shared = self.repo(); alpha = self.active('alpha', shared); beta = self.active('beta', shared)
        one = self.plan(alpha, 'one'); two = self.plan(beta, 'two'); event = two['event_id']
        folder = self.root / 'recovery'; folder.mkdir(mode=0o700)
        restored = DeliveryStore.restore(folder / 'state.sqlite3', self.replica.objects[event], self.replica.receipts[event], local_domain='recovery', replica_domain='replica')
        self.assertEqual(restored.catalog('owner', kind='projects')['total'], 2)
        self.assertEqual(restored.task('owner', alpha['project_id'], one['task_id'])['requirement'], 'Implement a tested report')
        self.assertEqual(restored.verify_event_chain(), self.store.verify_event_chain())
        self.error('recovery_read_only', restored.claim_notification)
        self.error('recovery_read_only', lambda: restored.create_project('owner', 'new', project_alias='third', name='Third'))
        self.assertEqual(restored.path.stat().st_mode & 0o777, 0o600)
        self.error('recovery_read_only', lambda: restored.snapshot(event))
        reopened = DeliveryStore(restored.path, local_domain='recovery')
        self.error('recovery_read_only', reopened.claim_notification)
        self.error('recovery_read_only', lambda: reopened.create_project('owner', 'after-restart', project_alias='new', name='New'))

    def test_restore_rejects_tampering_and_existing_destination(self):
        project = self.project(); event = project['event_id']; snapshot = self.replica.objects[event]
        receipt = self.replica.receipts[event]
        self.error('snapshot_digest_invalid', lambda: DeliveryStore.restore(self.root / 'bad.sqlite3', replace(snapshot, content=snapshot.content + b'corrupt'), receipt, local_domain='recovery', replica_domain='replica'))
        self.assertFalse((self.root / 'bad.sqlite3').exists())
        self.error('restore_destination_invalid', lambda: DeliveryStore.restore(self.path, snapshot, receipt, local_domain='recovery', replica_domain='replica'))

    def test_snapshot_local_corruption_blocks_reconfirmation(self):
        self.replica.timeout_after_save = True; project = self.project()
        (self.root / 'delivery.sqlite3.snapshots' / (project['event_id'] + '.sqlite3')).write_bytes(b'corrupt')
        self.replica.timeout_after_save = False
        self.error('snapshot_digest_invalid', lambda: self.store.reconcile(project['event_id']))

    def test_unsupported_schema_never_downgraded(self):
        with closing(sqlite3.connect(self.path)) as c: c.execute('PRAGMA user_version=99')
        self.error('schema_version_unsupported', self.open)
        with closing(sqlite3.connect(self.path)) as c: self.assertEqual(c.execute('PRAGMA user_version').fetchone()[0], 99)

    def test_remote_identity_rejects_url_credentials_shell_and_dot_segments(self):
        for remote in ('https://user:secret@example.test/repo', 'github:../repo', 'github:example/repo;command', 'github:example/repo\n'):
            self.error('remote_identity_invalid', lambda: self.store.enroll_repository('owner', self.key(), repo_alias='demo', remote_identity=remote))

    def test_another_writer_blocked_during_replica_handshake(self):
        entered = threading.Event(); release = threading.Event(); original = self.replica.save
        def save(snapshot):
            entered.set(); release.wait(5); return original(snapshot)
        self.replica.save = save; thread = threading.Thread(target=self.project); thread.start()
        try:
            self.assertTrue(entered.wait(2)); other = self.open()
            self.error('writer_busy', lambda: other.create_project('owner', 'second', project_alias='beta', name='Beta'))
        finally:
            release.set(); thread.join(5)
        self.assertFalse(thread.is_alive()); self.assertEqual(self.store.catalog('owner', kind='projects')['total'], 1)

    def test_storage_requires_private_directory_and_rejects_symlink(self):
        folder = self.root / 'public'; folder.mkdir(mode=0o755)
        self.error('storage_permissions_invalid', lambda: DeliveryStore(folder / 'state.sqlite3', local_domain='primary'))
        link = self.root / 'link.sqlite3'; link.symlink_to(self.path)
        self.error('storage_path_invalid', lambda: DeliveryStore(link, local_domain='primary'))

    def test_shanghai_timestamps_cross_day_month_year_and_accept_utc_clock(self):
        values = [('2026-10-19T15:59:59+00:00', '2026-10-19'), ('2026-10-19T16:00:00+00:00', '2026-10-20'),
                  ('2026-10-31T16:00:00+00:00', '2026-11-01'), ('2026-12-31T16:00:00+00:00', '2027-01-01')]
        for i, (instant, date) in enumerate(values):
            with patch('codex_delivery.codex_delivery.store.datetime') as clock:
                clock.now.return_value = datetime.fromisoformat(instant).astimezone(ZoneInfo('Asia/Shanghai'))
                result = self.project('project-' + str(i)); clock.now.assert_called_once_with(ZoneInfo('Asia/Shanghai'))
            row = next(r for r in self.store.catalog('owner', kind='projects')['items'] if r['project_id'] == result['project_id'])
            self.assertTrue(row['created_at'].startswith(date)); self.assertTrue(row['created_at'].endswith('+08:00'))


if __name__ == '__main__': unittest.main()
