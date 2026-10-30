"""Real Store/claim persistence, synthetic execution; no HA/network qualification."""
from contextlib import closing
from datetime import datetime, timedelta
import fcntl
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from codex_delivery.codex_delivery.callback import CallbackUnknown
from codex_delivery.codex_delivery.coordinator import WorkCoordinator
from codex_delivery.codex_delivery.durability import ReplicaReceipt
from codex_delivery.codex_delivery.scheduling import confirmed_turns, ordered_tasks, WAIT_PROMOTION_SECONDS
from codex_delivery.codex_delivery.store import DeliveryError, SHANGHAI
from codex_delivery.codex_delivery.work import WorkStore


class Replica:
    def __init__(self):
        self.online = True

    def save(self, snapshot):
        if not self.online:
            raise TimeoutError('synthetic unavailable replica')
        return ReplicaReceipt(snapshot.event_id, snapshot.sha256, 'independent', snapshot.event_id)


class Adapter:
    def __init__(self):
        self.cancelled = []

    def qualifies(self, profile, runtime_id):
        return profile == 'document' and runtime_id == 'synthetic-python'

    def cancel(self, task, claim):
        self.cancelled.append(task['task_id'])


class RecordingCoordinator(WorkCoordinator):
    """Only execution is synthetic; dispatch, eligibility, claim and finish are real."""
    def __init__(self, *args, selected, **kwargs):
        super().__init__(*args, **kwargs)
        self.selected = selected

    def _execute(self, task, claim, spec, inputs):
        self.selected.append(task['task_id'])
        self.store.finish_work(task['owner_ref'], 'fixture-stop:' + claim['lease_ref'],
            task_ref=task['task_id'], lease_ref=claim['lease_ref'], lease_epoch=claim['lease_epoch'],
            state='failed', evidence={'error_code': 'synthetic_scheduling_fixture'})


class ProjectSchedulingTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / 'delivery.sqlite3'
        self.replica = Replica()
        self.store = self.open_store()
        self.adapter = Adapter()
        self.selected = []
        self.coordinator = self.open_coordinator()
        self.serial = 0

    def open_store(self):
        return WorkStore(self.path, local_domain='primary', replica_domain='independent', replica=self.replica)

    def open_coordinator(self):
        return RecordingCoordinator(self.store, runner_ref='runner', adapter=self.adapter,
                                    callback=None, selected=self.selected)

    def project(self, owner, alias):
        repo = self.store.enroll_repository(owner, 'repo-' + alias, repo_alias=alias,
            remote_identity='fixture:' + owner + '/' + alias)['repo_id']
        project = self.store.create_project(owner, 'project-' + alias,
            project_alias=alias, name=alias)['project_id']
        self.store.bind_repository(owner, 'bind-' + alias, project_id=project,
                                   repo_id=repo, expected_revision=1)
        self.store.project_control(owner, 'activate-' + alias, project_id=project,
                                   expected_revision=2, action='activate')
        return project

    def create(self, owner='owner', project=None, **spec_changes):
        self.serial += 1
        spec = dict(profile='document', runtime_id='synthetic-python', program='synthetic fixture',
                    output_names=['report.txt'], input_refs=[])
        spec.update(spec_changes)
        result = self.store.create_work(owner, 'create-' + str(self.serial), conversation_ref='conversation',
            source_event_ref='source-' + str(self.serial), input_revision=1,
            requirement='Synthetic scheduler fixture', spec=spec, project_id=project,
            expected_project_revision=3 if project else None)
        return result['task_id']

    def turns(self, runner='runner'):
        with closing(self.store._connect()) as connection:
            return confirmed_turns(connection, runner)

    def queue(self):
        with closing(self.store._connect()) as connection:
            rows = connection.execute("SELECT * FROM tasks WHERE state='queued'").fetchall()
            return [task['task_id'] for task in ordered_tasks(rows, confirmed_turns(connection, 'runner'))]

    def claim(self, owner, task, runner='runner'):
        return self.store.claim_work(owner, 'direct-claim:' + task, task_ref=task,
            expected_revision=1, runner_ref=runner, qualified_profiles=frozenset({'document'}))

    def stop(self, owner, task, claim):
        self.store.finish_work(owner, 'direct-stop:' + claim['lease_ref'], task_ref=task,
            lease_ref=claim['lease_ref'], lease_epoch=claim['lease_epoch'], state='failed',
            evidence={'error_code': 'synthetic_scheduling_fixture'})

    def test_two_projects_rotate_despite_earlier_large_backlog(self):
        a, b = self.project('owner', 'a'), self.project('owner', 'b')
        aa = [self.create(project=a) for _ in range(3)]
        bb = [self.create(project=b) for _ in range(2)]
        for _ in range(5):
            self.assertTrue(self.coordinator.run_once())
        self.assertEqual(self.selected, [aa[0], bb[0], aa[1], bb[1], aa[2]])
        self.assertFalse(self.coordinator.run_once())

    def test_restart_recovers_original_confirmed_turn_without_new_scheduler_state(self):
        a, b = self.project('owner', 'a'), self.project('owner', 'b')
        first, second, other = self.create(project=a), self.create(project=a), self.create(project=b)
        self.coordinator.run_once()
        before = self.turns()
        self.store = self.open_store()
        restarted = self.open_coordinator()
        self.assertEqual(self.turns(), before)
        restarted.run_once()
        self.assertEqual(self.selected, [first, other])
        self.assertEqual(self.queue(), [second])
        with closing(self.store._connect()) as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM events WHERE kind LIKE 'scheduling.%'").fetchone()[0], 0)

    def test_fresh_process_recovers_same_queue_from_readonly_database(self):
        a, b = self.project('owner', 'a'), self.project('owner', 'b')
        self.create(project=a)
        tail, other = self.create(project=a), self.create(project=b)
        self.coordinator.run_once()
        code = '''import sqlite3,sys,json
sys.path.insert(0,sys.argv[1])
from codex_delivery.codex_delivery.scheduling import confirmed_turns,ordered_tasks
connection=sqlite3.connect('file:'+sys.argv[2]+'?mode=ro',uri=True)
connection.row_factory=sqlite3.Row
connection.execute('BEGIN')
tasks=connection.execute("SELECT * FROM tasks WHERE state='queued'").fetchall()
print(json.dumps([row['task_id'] for row in ordered_tasks(tasks,confirmed_turns(connection,'runner'))]))
connection.close()
'''
        result = subprocess.run([sys.executable, '-I', '-B', '-c', code,
            str(Path(__file__).resolve().parents[1]), str(self.path)],
            check=True, capture_output=True, text=True, timeout=10)
        import json
        self.assertEqual(json.loads(result.stdout), [other, tail])

    def test_multiple_owners_and_owner_work_have_independent_buckets(self):
        a, b = self.project('alice', 'same'), self.project('bob', 'same')
        personal_a = [self.create('alice') for _ in range(3)]
        project_a = self.create('alice', a)
        personal_b = [self.create('bob') for _ in range(2)]
        project_b = self.create('bob', b)
        for _ in range(4):
            self.coordinator.run_once()
        self.assertEqual(self.selected, [personal_a[0], project_a, personal_b[0], project_b])
        keys = set(self.turns())
        self.assertEqual(keys, {('alice', 'owner_work', ''), ('bob', 'owner_work', ''),
                                ('alice', 'project', a), ('bob', 'project', b)})

    def test_single_project_keeps_old_fifo(self):
        project = self.project('owner', 'only')
        tasks = [self.create(project=project) for _ in range(3)]
        for _ in tasks:
            self.coordinator.run_once()
        self.assertEqual(self.selected, tasks)

    def test_unqualified_head_interleaves_other_bucket_before_eligible_tail(self):
        a, b = self.project('owner', 'a'), self.project('owner', 'b')
        blocked = self.create(project=a, runtime_id='unqualified')
        tail, other = self.create(project=a), self.create(project=b)
        self.coordinator.run_once()
        self.assertEqual(self.selected, [other])
        self.coordinator.run_once()
        self.assertEqual(self.selected, [other, tail])
        self.assertEqual(self.store.work_task('owner', blocked)['state'], 'queued')

    def test_unknown_input_does_not_consume_turn_or_block_other_project(self):
        a, b = self.project('owner', 'a'), self.project('owner', 'b')
        blocked = self.create(project=a, input_refs=['WI-' + 'a' * 32])
        other = self.create(project=b)
        self.coordinator.input_resolver = lambda *args: (_ for _ in ()).throw(CallbackUnknown('callback_outcome_unknown'))
        self.coordinator.run_once()
        self.assertEqual(self.selected, [other])
        self.assertNotIn(('owner', 'project', a), self.turns())
        self.assertEqual(self.store.work_task('owner', blocked)['state'], 'queued')

    def test_failed_claim_cas_does_not_advance_turn_or_hide_other_bucket(self):
        a, b = self.project('owner', 'a'), self.project('owner', 'b')
        first, other = self.create(project=a), self.create(project=b)
        original = self.store.claim_work
        def fail_first(owner, key, **arguments):
            if arguments['task_ref'] == first:
                raise DeliveryError('revision_conflict')
            return original(owner, key, **arguments)
        with patch.object(self.store, 'claim_work', side_effect=fail_first):
            self.coordinator.run_once()
        self.assertEqual(self.selected, [other])
        self.assertNotIn(('owner', 'project', a), self.turns())
        self.assertEqual(self.queue(), [first])

    def test_actual_revision_race_does_not_claim_or_consume_stale_task_turn(self):
        a, b = self.project('owner', 'a'), self.project('owner', 'b')
        first, other = self.create(project=a), self.create(project=b)
        original = self.store.claim_work
        def revise_then_claim(owner, key, **arguments):
            if arguments['task_ref'] == first:
                self.store.revise_work(owner, 'race-revise', task_ref=first,
                    expected_revision=1, input_revision=2, supplement='Synthetic concurrent revision')
            return original(owner, key, **arguments)
        with patch.object(self.store, 'claim_work', side_effect=revise_then_claim):
            self.coordinator.run_once()
        self.assertEqual(self.selected, [other])
        self.assertEqual(self.store.work_task('owner', first)['revision'], 2)
        self.assertNotIn(('owner', 'project', a), self.turns())

    def test_writer_busy_preserves_turn_and_next_attempt(self):
        a, b = self.project('owner', 'a'), self.project('owner', 'b')
        first, other = self.create(project=a), self.create(project=b)
        with patch.object(self.store, 'claim_work', side_effect=DeliveryError('writer_busy')):
            self.assertFalse(self.coordinator.run_once())
        self.assertEqual(self.turns(), {})
        self.assertEqual(self.queue(), [first, other])
        self.coordinator.run_once()
        self.assertEqual(self.selected, [first])

    def test_actual_dispatch_flock_blocks_second_coordinator_without_claim(self):
        first = self.create()
        descriptor = os.open(self.path.with_suffix('.work-coordinator.lock'), os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaisesRegex(DeliveryError, 'worker_busy'):
                self.open_coordinator().run_once()
            self.assertEqual(self.turns(), {})
        finally:
            os.close(descriptor)
        self.coordinator.run_once()
        self.assertEqual(self.selected, [first])

    def test_pending_claim_never_advances_until_original_confirmation(self):
        a, b = self.project('owner', 'a'), self.project('owner', 'b')
        first, other = self.create(project=a), self.create(project=b)
        original = self.store.claim_work
        def lose_ack(*args, **kwargs):
            self.replica.online = False
            return original(*args, **kwargs)
        with patch.object(self.store, 'claim_work', side_effect=lose_ack):
            self.assertTrue(self.coordinator.run_once())
        self.assertEqual(self.selected, [])
        self.assertEqual(self.turns(), {})
        self.assertFalse(self.open_coordinator().run_once())
        with closing(self.store._connect()) as connection:
            event = connection.execute("SELECT event_id FROM events WHERE kind='work.claim'").fetchone()[0]
        self.replica.online = True
        self.assertEqual(self.store.reconcile(event)['status'], 'accepted')
        self.assertIn(('owner', 'project', a), self.turns())
        self.assertTrue(self.open_coordinator().run_once())
        self.assertEqual(self.adapter.cancelled, [first])
        self.assertEqual(self.selected, [])
        self.assertEqual(self.store.work_task('owner', first)['state'], 'recovery_required')
        self.open_coordinator().run_once()
        self.assertEqual(self.selected, [other])

    def test_other_runner_claim_does_not_change_this_runners_turns(self):
        a, b = self.project('owner', 'a'), self.project('owner', 'b')
        first, tail, other = self.create(project=a), self.create(project=a), self.create(project=b)
        claim = self.claim('owner', first, 'other-runner')
        self.stop('owner', first, claim)
        self.assertEqual(self.turns(), {})
        self.coordinator.run_once()
        self.assertEqual(self.selected, [tail])
        self.assertEqual(self.queue(), [other])

    def test_corrupt_original_command_fails_closed(self):
        task = self.create()
        claim = self.claim('owner', task)
        self.stop('owner', task, claim)
        with closing(self.store._connect()) as connection:
            connection.execute("UPDATE commands SET argument_digest='invalid' WHERE idempotency_key=?", ('direct-claim:' + task,))
            connection.commit()
        with self.assertRaisesRegex(DeliveryError, 'scheduling_history_invalid'):
            self.turns()

    def test_missing_original_claim_command_is_not_an_unserved_bucket(self):
        task = self.create()
        claim = self.claim('owner', task)
        self.stop('owner', task, claim)
        with closing(self.store._connect()) as connection:
            connection.execute('DELETE FROM commands WHERE idempotency_key=?', ('direct-claim:' + task,))
            connection.commit()
            self.assertEqual(connection.execute('PRAGMA quick_check').fetchone()[0], 'ok')
            self.assertEqual(connection.execute('PRAGMA foreign_key_check').fetchall(), [])
        with self.assertRaisesRegex(DeliveryError, 'scheduling_history_invalid'):
            self.turns()

    def test_multiple_command_aliases_cannot_duplicate_original_claim(self):
        task = self.create()
        claim = self.claim('owner', task)
        self.stop('owner', task, claim)
        with closing(self.store._connect()) as connection:
            connection.execute("INSERT INTO commands SELECT owner_ref,'fixture-claim-alias',argument_digest,event_id,result_json "
                               'FROM commands WHERE idempotency_key=?', ('direct-claim:' + task,))
            connection.commit()
        with self.assertRaisesRegex(DeliveryError, 'scheduling_history_invalid'):
            self.turns()

    def test_missing_claim_durability_is_not_silently_treated_as_unserved(self):
        task = self.create()
        claim = self.claim('owner', task)
        self.stop('owner', task, claim)
        with closing(self.store._connect()) as connection:
            connection.execute("DELETE FROM durability WHERE event_id IN (SELECT event_id FROM events WHERE kind='work.claim')")
            connection.commit()
        with self.assertRaisesRegex(DeliveryError, 'scheduling_history_invalid'):
            self.turns()

    def test_missing_original_task_link_fails_closed(self):
        task = self.create()
        claim = self.claim('owner', task)
        self.stop('owner', task, claim)
        with closing(self.store._connect()) as connection:
            connection.execute('DELETE FROM tasks WHERE task_id=?', (task,))
            connection.commit()
        with self.assertRaisesRegex(DeliveryError, 'scheduling_history_invalid'):
            self.turns()

    def test_idle_coordinator_does_not_read_historical_turns(self):
        with patch('codex_delivery.codex_delivery.coordinator.confirmed_turns', side_effect=AssertionError('unneeded history read')):
            self.assertFalse(self.coordinator.run_once())

    def test_running_unknown_reconciles_before_corrupt_queued_timestamp(self):
        running, queued = self.create(), self.create()
        self.claim('owner', running)
        with closing(self.store._connect()) as connection:
            connection.execute("UPDATE tasks SET created_at='invalid' WHERE task_id=?", (queued,))
            connection.commit()
        self.assertTrue(self.coordinator.run_once())
        self.assertEqual(self.adapter.cancelled, [running])
        self.assertEqual(self.store.work_task('owner', running)['state'], 'recovery_required')
        self.assertEqual(self.selected, [])
        with self.assertRaisesRegex(DeliveryError, 'scheduling_history_invalid'):
            self.coordinator.run_once()

    def test_running_unknown_reconciles_before_corrupt_fairness_history(self):
        running = self.create()
        self.claim('owner', running)
        self.create()
        with closing(self.store._connect()) as connection:
            connection.execute("UPDATE commands SET argument_digest='invalid' WHERE idempotency_key=?", ('direct-claim:' + running,))
            connection.commit()
        self.assertTrue(self.coordinator.run_once())
        self.assertEqual(self.adapter.cancelled, [running])
        self.assertEqual(self.store.work_task('owner', running)['state'], 'recovery_required')
        self.assertEqual(self.selected, [])
        with self.assertRaisesRegex(DeliveryError, 'scheduling_history_invalid'):
            self.coordinator.run_once()


class PromotionTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 10, 31, 12, tzinfo=SHANGHAI)

    def task(self, task, project, *, owner='owner', seconds=0):
        return dict(task_id=task, owner_ref=owner, project_id=project,
                    task_scope='project' if project else 'owner_work',
                    created_at=(self.now - timedelta(seconds=seconds)).isoformat())

    def test_waiting_promotion_precedes_new_unserved_bucket(self):
        old = self.task('old', 'a', seconds=WAIT_PROMOTION_SECONDS + 1)
        new = self.task('new', 'b')
        turns = {('owner', 'project', 'a'): (10, self.now - timedelta(seconds=WAIT_PROMOTION_SECONDS + 2))}
        self.assertEqual([row['task_id'] for row in ordered_tasks([new, old], turns, checked_at=self.now)], ['old', 'new'])

    def test_all_promoted_buckets_rotate_by_claim_sequence_not_old_backlog(self):
        tasks = [self.task('a1', 'a', seconds=2000), self.task('a2', 'a', seconds=1900),
                 self.task('b1', 'b', seconds=1000)]
        turns = {('owner', 'project', 'a'): (12, self.now - timedelta(seconds=700)),
                 ('owner', 'project', 'b'): (8, self.now - timedelta(seconds=500))}
        self.assertEqual([row['task_id'] for row in ordered_tasks(tasks, turns, checked_at=self.now)], ['b1', 'a1', 'a2'])
        turns[('owner', 'project', 'b')] = (15, self.now - timedelta(seconds=400))
        self.assertEqual(ordered_tasks(tasks, turns, checked_at=self.now)[0]['task_id'], 'a1')

    def test_confirmed_service_resets_old_backlogs_promotion(self):
        old = self.task('old', 'a', seconds=2000)
        new = self.task('new', 'b')
        turns = {('owner', 'project', 'a'): (15, self.now)}
        self.assertEqual(ordered_tasks([old, new], turns, checked_at=self.now)[0]['task_id'], 'new')

    def test_creation_and_id_keep_single_bucket_fifo_with_timezone_offsets(self):
        rows = [self.task('b', 'a', seconds=1), self.task('c', 'a'), self.task('a', 'a', seconds=1)]
        self.assertEqual([row['task_id'] for row in ordered_tasks(rows, {}, checked_at=self.now)], ['a', 'b', 'c'])

    def test_naive_timestamp_is_not_trusted_as_waiting_fact(self):
        task = self.task('a', 'project')
        task['created_at'] = '2026-10-31T12:00:00'
        with self.assertRaisesRegex(DeliveryError, 'scheduling_history_invalid'):
            ordered_tasks([task], {}, checked_at=self.now)


if __name__ == '__main__':
    unittest.main()
