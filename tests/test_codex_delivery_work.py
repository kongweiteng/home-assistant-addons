"""Task ownership, safe-point control, schema upgrades and durable fencing."""
from contextlib import closing
from datetime import datetime, timedelta
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from codex_delivery.codex_delivery.durability import ReplicaReceipt
from codex_delivery.codex_delivery.journal import InputJournal, MetadataWorker
from codex_delivery.codex_delivery.origin import issue_origin
from codex_delivery.codex_delivery.provider import DeliveryProvider
from codex_delivery.codex_delivery.store import DeliveryError, DeliveryStore, SHANGHAI
from codex_delivery.codex_delivery.work import WorkStore


class Replica:
    def __init__(self):
        self.snapshots = {}
        self.online = True

    def save(self, snapshot):
        self.snapshots[snapshot.event_id] = snapshot
        if not self.online:
            raise TimeoutError('fixture acknowledgement lost')
        return ReplicaReceipt(snapshot.event_id, snapshot.sha256, 'independent', snapshot.event_id)


class WorkTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / 'delivery.sqlite3'
        self.replica = Replica()
        self.store = self.open()
        self.spec = {'profile': 'document', 'runtime_id': 'python-v1',
                     'program': "print('report')", 'output_names': ['报告.txt'], 'input_refs': []}

    def open(self, **kwargs):
        return WorkStore(self.path, local_domain='primary', replica_domain='independent', replica=self.replica, **kwargs)

    def create(self, event='event-1'):
        return self.store.create_work('owner', 'create-' + event, conversation_ref='conversation',
            source_event_ref=event, input_revision=1, requirement='Generate a report', spec=self.spec)

    def claim(self, task):
        return self.store.claim_work('owner', 'claim-' + task['task_id'], task_ref=task['task_id'],
            expected_revision=task['revision'], runner_ref='qualified-runner',
            qualified_profiles=frozenset({'document'}))

    def finish(self, task, claim, **changes):
        args = dict(task_ref=task['task_id'], lease_ref=claim['lease_ref'],
            lease_epoch=claim['lease_epoch'], state='completed',
            evidence={'exit_code': 0, 'validation': 'passed', 'delivery': 'gateway_sent'})
        args.update(changes)
        return self.store.finish_work('owner', 'finish-' + task['task_id'], **args)

    def test_owner_work_does_not_create_a_fake_project_or_repo(self):
        task = self.create()
        self.assertEqual(task['status'], 'accepted')
        self.assertEqual(self.store.catalog('owner', kind='projects')['total'], 0)
        row = self.store.work_task('owner', task['task_id'])
        self.assertIsNone(row['project_id'])
        self.assertEqual(row['task_scope'], 'owner_work')
        self.assertTrue(row['workspace_ref'].startswith('WS-'))
        with self.assertRaisesRegex(DeliveryError, 'not_found'):
            self.store.work_task('other-owner', task['task_id'])

    def test_same_source_has_one_task_and_duplicate_command_is_idempotent(self):
        first = self.create()
        self.assertEqual(first, self.create())
        with self.assertRaisesRegex(DeliveryError, 'catalog_conflict'):
            self.store.create_work('owner', 'different-key', conversation_ref='conversation',  # gitleaks:allow -- synthetic idempotency key
                source_event_ref='event-1', input_revision=1, requirement='Another report', spec=self.spec)
        self.assertEqual(self.store.catalog('owner', kind='tasks')['total'], 1)

    def test_missing_replica_never_grants_execution(self):
        self.replica.online = False
        task = self.create()
        self.assertEqual(task['status'], 'durability_pending')
        with self.assertRaisesRegex(DeliveryError, 'durability_pending'):
            self.claim(task)
        self.assertEqual(self.store.work_task('owner', task['task_id'])['state'], 'queued')

    def test_claim_requires_qualified_profile_and_fences_old_epoch(self):
        task = self.create()
        with self.assertRaisesRegex(DeliveryError, 'capability_unavailable'):
            self.store.claim_work('owner', 'bad-claim', task_ref=task['task_id'], expected_revision=1,
                runner_ref='unqualified', qualified_profiles=frozenset({'code'}))
        claim = self.claim(task)
        self.assertTrue(self.store.lease_current('owner', task['task_id'], claim['lease_ref'], 1))
        with self.assertRaisesRegex(DeliveryError, 'lease_fenced'):
            self.finish(task, claim, lease_epoch=0)
        self.assertEqual(self.finish(task, claim)['state'], 'completed')
        self.assertFalse(self.store.lease_current('owner', task['task_id'], claim['lease_ref'], 1))
        self.assertFalse(self.claim(task)['execution_enabled'])

    def test_lost_claim_replica_ack_fences_execution_until_receipt_is_reconciled(self):
        task = self.create()
        self.replica.online = False
        claim = self.claim(task)
        self.assertEqual(claim['status'], 'durability_pending')
        self.assertFalse(claim['execution_enabled'])
        self.assertFalse(self.store.lease_current('owner', task['task_id'], claim['lease_ref'], 1))
        self.replica.online = True
        self.store.reconcile(claim['event_id'])
        self.assertTrue(self.store.lease_current('owner', task['task_id'], claim['lease_ref'], 1))

    def test_cancel_running_is_pending_until_runner_confirms_stop(self):
        task = self.create(); claim = self.claim(task)
        result = self.store.control_work('owner', 'cancel', task_ref=task['task_id'], expected_revision=1, action='cancel')
        self.assertEqual((result['state'], result['control_pending']), ('running', 'cancel'))
        self.assertFalse(self.store.lease_current('owner', task['task_id'], claim['lease_ref'], 1))
        with self.assertRaisesRegex(DeliveryError, 'control_pending'):
            self.finish(task, claim)
        self.assertEqual(self.finish(task, claim, state='cancelled', evidence={'validation': 'process_stopped'})['state'], 'cancelled')

    def test_completion_requires_validation_and_delivery_receipt(self):
        task = self.create(); claim = self.claim(task)
        for evidence in ({'exit_code': 0}, {'exit_code': 0, 'validation': 'passed', 'delivery': 'queued'},
                         {'exit_code': 0, 'validation': 'passed', 'delivery': 'accepted'}):
            with self.assertRaisesRegex(DeliveryError, 'completion_unverified'):
                self.finish(task, claim, evidence=evidence)
        self.assertEqual(self.store.work_task('owner', task['task_id'])['state'], 'running')

    def test_late_result_requires_reconciliation_instead_of_replay(self):
        task = self.create(); claim = self.claim(task)
        later = datetime.fromisoformat(claim['lease_expires_at']) + timedelta(seconds=1)
        with patch('codex_delivery.codex_delivery.store.now', return_value=later.isoformat()):
            with self.assertRaisesRegex(DeliveryError, 'lease_expired'):
                self.finish(task, claim)
            result = self.finish(task, claim, state='recovery_required', evidence={'error_code': 'outcome_unknown'})
        self.assertEqual(result['state'], 'recovery_required')
        with self.assertRaisesRegex(DeliveryError, 'transition_invalid'):
            self.store.control_work('owner', 'resume', task_ref=task['task_id'], expected_revision=1, action='resume')

    def test_supplement_is_saved_but_does_not_steer_running_program(self):
        task = self.create(); claim = self.claim(task)
        result = self.store.revise_work('owner', 'revise', task_ref=task['task_id'],
            expected_revision=1, input_revision=2, supplement='Use portrait layout')
        self.assertEqual(result['revision'], 2)
        row = self.store.work_task('owner', task['task_id'])
        self.assertEqual((row['state'], row['control_pending']), ('running', 'revise'))
        self.assertFalse(self.store.lease_current('owner', task['task_id'], claim['lease_ref'], 1))

    def test_cancel_is_sticky_across_supplements_and_pause_requests(self):
        task = self.create(); claim = self.claim(task)
        self.store.control_work('owner', 'cancel', task_ref=task['task_id'], expected_revision=1, action='cancel')
        self.store.revise_work('owner', 'supplement', task_ref=task['task_id'], expected_revision=2,
            input_revision=2, supplement='Save an additional requirement')
        row = self.store.work_task('owner', task['task_id'])
        self.assertEqual(row['control_pending'], 'cancel')
        with self.assertRaisesRegex(DeliveryError, 'cancel_pending'):
            self.store.control_work('owner', 'pause', task_ref=task['task_id'], expected_revision=3, action='pause')
        self.finish(task, claim, state='cancelled', evidence={'validation': 'process_stopped'})

    def test_supplement_cannot_clear_unknown_execution_recovery(self):
        task = self.create(); claim = self.claim(task)
        self.finish(task, claim, state='recovery_required', evidence={'error_code': 'outcome_unknown'})
        result = self.store.revise_work('owner', 'supplement', task_ref=task['task_id'], expected_revision=1,
            input_revision=2, supplement='Keep this requirement for reconciliation')
        self.assertEqual(result['state'], 'recovery_required')
        with self.assertRaisesRegex(DeliveryError, 'transition_invalid'):
            self.store.prepare_work('owner', 'prepare', task_ref=task['task_id'], expected_revision=2,
                program='print(1)', output_names=['result.txt'], input_refs=[])

    def test_saved_supplement_can_be_replanned_after_safe_stop(self):
        task = self.create(); claim = self.claim(task)
        self.store.revise_work('owner', 'supplement', task_ref=task['task_id'], expected_revision=1,
            input_revision=1, source_event_ref='new-job-source', supplement='Use a new layout')
        self.finish(task, claim, state='waiting_input', evidence={'validation': 'process_stopped'})
        prepared = self.store.prepare_work('owner', 'prepare', task_ref=task['task_id'], expected_revision=2,
            program="print('new layout')", output_names=['new-report.txt'], input_refs=[])
        self.assertEqual((prepared['state'], prepared['revision']), ('queued', 3))
        row = self.store.work_task('owner', task['task_id'])
        self.assertEqual(row['current_source_event_ref'], 'new-job-source')
        self.assertEqual(row['input_revision'], 1)
        self.assertEqual(json.loads(row['work_json'])['profile'], 'document')

    def test_browser_does_not_implicitly_gain_private_inputs(self):
        self.spec.update(profile='browser', input_refs=['WI-' + 'a' * 32])
        with self.assertRaisesRegex(DeliveryError, 'outbound_grant_required'):
            self.create()
        self.assertEqual(self.store.catalog('owner', kind='tasks')['total'], 0)

    def test_output_path_traversal_is_rejected_before_saving(self):
        for name in ('../secrets', '/etc/passwd', 'a\\b', '..', 'a\x00b'):
            self.spec['output_names'] = [name]
            with self.assertRaisesRegex(DeliveryError, 'output_names_invalid'):
                self.create()

    def test_provider_journal_round_trip_preserves_trusted_owner_and_hides_code(self):
        key = b'fixture-independent-origin-key-32-bytes'
        instant = datetime.now(SHANGHAI)
        journal = InputJournal(self.store)
        provider = DeliveryProvider(journal, bearer='fixture-service-bearer-at-least-32-bytes',
            origin_key=key, allowed_actors=frozenset({'owner'}))
        args = {'requirement': 'Report', **self.spec}
        def payload(name, arguments):
            return {'version': 1, 'name': name, 'arguments': arguments,
                'context': {'job_hash': 'job', 'turn_hash': 'turn', 'idempotency_key': 'call'},
                'origin': issue_origin(key, actor_ref='owner', conversation_ref='conversation',
                    source_event_ref='event-1', input_revision=1, input_digest='sha256:' + 'a' * 64,
                    name=name, arguments=arguments, issued_at=instant)}
        local = provider.call(payload('work_submit', args))
        self.assertEqual(local['status'], 'durability_pending')
        MetadataWorker(journal).run_once()
        final = journal.status('owner', local['operation_ref'])
        status = provider.call(payload('work_status', {'task_ref': final['task_ref']}))
        self.assertEqual(status['status'], 'ok')
        self.assertNotIn('program', json.dumps(status))
        self.assertEqual(status['item']['task_scope'], 'owner_work')

    def test_restore_freezes_execution_and_keeps_general_tasks(self):
        task = self.create()
        snapshot = self.replica.snapshots[task['event_id']]
        receipt = ReplicaReceipt(snapshot.event_id, snapshot.sha256, 'independent', snapshot.event_id)
        destination = self.path.parent / 'recovered.sqlite3'
        restored = WorkStore.restore(destination, snapshot, receipt,
            local_domain='replacement', replica_domain='independent')
        self.assertEqual(restored.work_task('owner', task['task_id'])['state'], 'queued')
        with self.assertRaisesRegex(DeliveryError, 'recovery_read_only'):
            restored.claim_work('owner', 'claim', task_ref=task['task_id'], expected_revision=1,
                runner_ref='node', qualified_profiles=frozenset({'document'}))

    def test_real_v2_task_table_upgrade_preserves_children_and_rejects_implicit_upgrade(self):
        repo = self.store.enroll_repository('owner', 'repo', repo_alias='sample', remote_identity='github:example/sample')
        project = self.store.create_project('owner', 'project', project_alias='sample', name='Sample')
        self.store.bind_repository('owner', 'bind', project_id=project['project_id'], repo_id=repo['repo_id'], expected_revision=1)
        task = self.store.plan('owner', 'plan', project_id=project['project_id'], expected_project_revision=2,
            conversation_ref='chat', source_event_ref='legacy-event', requirement='Legacy design')
        # Build the old 13-column task table with real child bindings and events.
        old = '''CREATE TABLE tasks_v2(task_id TEXT PRIMARY KEY,owner_ref TEXT NOT NULL,project_id TEXT NOT NULL,
            conversation_ref TEXT NOT NULL,source_event_ref TEXT NOT NULL,mode TEXT NOT NULL,
            requirement TEXT NOT NULL,project_revision INTEGER NOT NULL,revision INTEGER NOT NULL,
            phase TEXT NOT NULL,state TEXT NOT NULL,created_at TEXT NOT NULL,updated_at TEXT NOT NULL,
            UNIQUE(owner_ref,project_id,task_id));'''
        columns = 'task_id,owner_ref,project_id,conversation_ref,source_event_ref,mode,requirement,project_revision,revision,phase,state,created_at,updated_at'
        with closing(sqlite3.connect(self.path, isolation_level=None)) as c:
            c.executescript('PRAGMA foreign_keys=OFF; BEGIN IMMEDIATE;'
                + ''.join('DROP TABLE ' + name + ';' for name in ('repository_publications', 'repository_candidates', 'repository_artifacts', 'repository_mirrors')) + old
                + f'INSERT INTO tasks_v2 SELECT {columns} FROM tasks;'
                + 'DROP TABLE tasks; ALTER TABLE tasks_v2 RENAME TO tasks; PRAGMA user_version=2; COMMIT;')
        with self.assertRaisesRegex(DeliveryError, 'schema_upgrade_required'):
            self.open()
        upgraded = self.open(upgrade_schema=True)
        self.assertEqual(upgraded.task('owner', project['project_id'], task['task_id'])['repositories'][0]['repo_id'], repo['repo_id'])
        self.assertTrue(upgraded.schema_backup.exists())
        upgraded.verify_event_chain()
        with closing(upgraded._connect()) as c:
            self.assertIsNone(c.execute('PRAGMA foreign_key_check').fetchone())
            self.assertEqual(c.execute('PRAGMA user_version').fetchone()[0], 5)
        self.store = upgraded
        self.assertEqual(self.create()['status'], 'accepted')


if __name__ == '__main__':
    unittest.main()
