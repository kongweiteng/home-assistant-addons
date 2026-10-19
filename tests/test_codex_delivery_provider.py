"""Real HTTP/SQLite failure boundaries; fixtures do not prove NAS durability."""
from contextlib import closing
from datetime import datetime
import http.client
import json
from pathlib import Path
import socket
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from zoneinfo import ZoneInfo

from codex_delivery.codex_delivery.durability import ReplicaReceipt, Snapshot, digest
from codex_delivery.codex_delivery.journal import InputJournal, MetadataWorker
from codex_delivery.codex_delivery.origin import issue_origin
from codex_delivery.codex_delivery.provider import DeliveryProvider, OUTPUT, manifest
from codex_delivery.codex_delivery.server import DeliveryHTTPServer
from codex_delivery.codex_delivery.store import DeliveryError, DeliveryStore, canonical
from codex_controller.codex_controller.extension_schema import validate_schema, validate_value
from codex_controller.codex_controller.extensions import ExtensionRegistry, request_extension


class ReplicaFixture:
    def __init__(self):
        self.objects = {}
        self.lose_receipt = False
        self.entered = None
        self.release = None

    def save(self, snapshot):
        old = self.objects.get(snapshot.event_id)
        if old is not None and old != snapshot:
            raise ValueError('immutable_conflict')
        self.objects[snapshot.event_id] = snapshot
        if self.entered is not None:
            self.entered.set()
            if not self.release.wait(5):
                raise TimeoutError('fixture_timeout')
        if self.lose_receipt:
            raise TimeoutError('fixture response lost after save')
        return ReplicaReceipt(snapshot.event_id, snapshot.sha256, 'replica', snapshot.event_id)


class DeliveryProviderTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / 'delivery.sqlite3'
        self.replica = ReplicaFixture()
        self.store = self.open()
        self.repo = self.store.enroll_repository('owner', 'enroll', repo_alias='sample', remote_identity='github:example/sample')
        self.project = self.store.create_project('owner', 'project', project_alias='alpha', name='Alpha')
        self.project_id = self.project['project_id']
        self.store.bind_repository('owner', 'bind', project_id=self.project_id, repo_id=self.repo['repo_id'], expected_revision=1)
        self.store.project_control('owner', 'activate', project_id=self.project_id, expected_revision=2, action='activate')
        self.journal = InputJournal(self.store)
        self.worker = MetadataWorker(self.journal)
        self.bearer = 'fixture-http-service-authentication-32-bytes'
        self.origin_key = b'fixture-distinct-origin-key-material-32-bytes'
        self.instant = datetime(2026, 10, 19, 12, tzinfo=ZoneInfo('Asia/Shanghai'))
        self.provider = DeliveryProvider(self.journal, bearer=self.bearer, origin_key=self.origin_key,
                                         allowed_actors=frozenset({'owner', 'another-owner'}), clock=lambda: self.instant)
        self.args = {'project_ref': self.project_id, 'expected_project_revision': 3,
                     'requirement': 'Implement a tested report', 'mode': 'develop'}
        self.server = None

    def open(self, **kwargs):
        return DeliveryStore(self.path, local_domain='primary', replica_domain='replica', replica=self.replica, **kwargs)

    def payload(self, name='plan', args=None, *, actor='owner', event='event-1', revision=1, input_digest=None):
        arguments = self.args if args is None else args
        origin = issue_origin(self.origin_key, actor_ref=actor, conversation_ref='chat', source_event_ref=event,
                              input_revision=revision, input_digest=input_digest or 'sha256:' + '1' * 64,
                              name=name, arguments=arguments, issued_at=self.instant)
        return {'version': 1, 'name': name, 'arguments': arguments,
                'context': {'job_hash': 'job', 'turn_hash': 'turn', 'idempotency_key': 'call'}, 'origin': origin}

    def call(self, name='plan', args=None, **kwargs):
        result = self.provider.call(self.payload(name, args, **kwargs))
        validate_value(result, OUTPUT)
        return result

    def serve(self):
        if self.server is None:
            self.server = DeliveryHTTPServer(('127.0.0.1', 0), self.provider)
            thread = threading.Thread(target=self.server.serve_forever, kwargs={'poll_interval': .01}, daemon=True)
            thread.start()
            def cleanup():
                self.server.shutdown(); self.server.server_close(); thread.join(2)
            self.addCleanup(cleanup)
        return self.server.server_address

    def http(self, payload=None, *, authorization=None, raw=None, headers=None):
        connection = http.client.HTTPConnection(*self.serve(), timeout=2)
        values = {'Authorization': 'Bearer ' + self.bearer, 'Content-Type': 'application/json'}
        if authorization is not None:
            values['Authorization'] = authorization
        values.update(headers or {})
        try:
            body = raw if raw is not None else canonical(payload or self.payload()).encode()
            connection.request('POST', '/internal/v1/tools/call', body=body, headers=values)
            response = connection.getresponse()
            return response.status, json.loads(response.read()), response.getheader('Cache-Control')
        finally:
            connection.close()

    def test_admission_is_local_only_then_worker_confirms_exactly_one_task(self):
        status, first, cache = self.http()
        self.assertEqual((status, first['status'], first['durability'], cache), (200, 'durability_pending', 'local_only', 'no-store'))
        self.assertEqual(self.store.catalog('owner', kind='tasks')['total'], 0)
        self.worker.run_once()
        final = self.call('operation_status', {'operation_ref': first['operation_ref']})
        self.assertEqual(final['status'], 'accepted')
        self.assertFalse(final['execution_enabled'])
        self.assertEqual(self.store.catalog('owner', kind='tasks')['total'], 1)
        self.assertEqual(self.http()[1], final)
        self.assertFalse(self.worker.run_once())

    def test_existing_controller_envelope_fails_closed_without_origin(self):
        payload = self.payload(); payload.pop('origin')
        # Real current client sends only version/name/arguments/context.
        result = request_extension('http://%s:%d' % self.serve(), self.bearer, payload)
        self.assertEqual(result['code'], 'request_invalid')
        self.assertEqual(self.store.catalog('owner', kind='tasks')['total'], 0)

    def test_bearer_alone_and_model_supplied_actor_cannot_select_an_owner(self):
        payload = self.payload('projects', {})
        payload['origin']['actor_ref'] = 'another-owner'
        self.assertEqual(self.http(payload)[1]['code'], 'origin_invalid')
        payload = self.payload('projects', {'owner_ref': 'owner'})
        self.assertEqual(self.http(payload)[1]['code'], 'arguments_invalid')
        self.assertEqual(self.http(authorization='Bearer invalid')[0], 401)

    def test_duplicate_context_keys_do_not_bypass_idempotency_or_change_input(self):
        first = self.call()
        payload = self.payload(); payload['context']['idempotency_key'] = 'a-different-key'
        self.assertEqual(self.provider.call(payload), first)
        self.assertEqual(self.call(args={**self.args, 'requirement': 'Changed body'})['code'], 'idempotency_conflict')
        self.assertEqual(self.call(input_digest='sha256:' + '2' * 64)['code'], 'idempotency_conflict')

    def test_abrupt_process_exit_after_metadata_commit_replays_only_local_metadata(self):
        first = self.call()
        source = '''import os,sys
from pathlib import Path
from codex_delivery.codex_delivery.store import DeliveryStore
from codex_delivery.codex_delivery.journal import InputJournal,MetadataWorker
s=DeliveryStore(Path(sys.argv[1]),local_domain='primary',replica_domain='replica')
w=MetadataWorker(InputJournal(s));apply=w._apply
def interrupt(row):
 apply(row)
 os._exit(23)
w._apply=interrupt
w.run_once()
'''
        result = subprocess.run([sys.executable, '-c', source, str(self.path)], capture_output=True, timeout=10)
        self.assertEqual(result.returncode, 23)
        self.assertEqual(self.store.catalog('owner', kind='tasks')['total'], 1)
        worker = MetadataWorker(InputJournal(self.open()))
        worker.run_once()
        self.assertEqual(self.journal.status('owner', first['operation_ref'])['status'], 'accepted')
        self.assertEqual(self.store.catalog('owner', kind='tasks')['total'], 1)

    def test_backup_success_with_lost_receipt_reuses_frozen_bytes_after_restart(self):
        first = self.call(); self.replica.lose_receipt = True
        self.worker.run_once()
        pending = self.journal.status('owner', first['operation_ref'])
        self.assertEqual(pending['status'], 'durability_pending')
        snapshot = self.replica.objects[pending['event_ref']]
        self.replica.lose_receipt = False
        MetadataWorker(InputJournal(self.open())).run_once()
        self.assertEqual(self.replica.objects[pending['event_ref']], snapshot)
        self.assertEqual(self.journal.status('owner', first['operation_ref'])['status'], 'accepted')

    def test_slow_replica_does_not_block_http_admission_or_operation_queries(self):
        first = self.call()
        entered, release = threading.Event(), threading.Event()
        self.replica.entered = entered; self.replica.release = release
        errors = []
        def run():
            try: self.worker.run_once()
            except BaseException as exc: errors.append(type(exc).__name__)
        thread = threading.Thread(target=run)
        thread.start()
        try:
            self.assertTrue(entered.wait(2))
            second = self.http(self.payload(event='event-2'))[1]
            self.assertEqual(second['status'], 'durability_pending')
            self.assertNotEqual(second['operation_ref'], first['operation_ref'])
            query = self.payload('operation_status', {'operation_ref': first['operation_ref']})
            self.assertEqual(self.http(query)[1]['status'], 'durability_pending')
            with self.assertRaisesRegex(DeliveryError, 'worker_busy'):
                MetadataWorker(self.journal).run_once()
        finally:
            release.set(); thread.join(3)
        self.assertFalse(thread.is_alive()); self.assertEqual(errors, [])

    def test_owner_project_and_task_pairs_are_checked_before_acceptance(self):
        op = self.call(actor='another-owner'); self.worker.run_once()
        self.assertEqual(self.journal.status('another-owner', op['operation_ref'])['code'], 'not_found')
        self.assertEqual(self.call('operation_status', {'operation_ref': op['operation_ref']})['code'], 'not_found')
        first = self.call(event='owner-event'); self.worker.run_once()
        task = self.journal.status('owner', first['operation_ref'])['task_ref']
        self.assertEqual(self.call('status', {'project_ref': 'wrong-project', 'task_ref': task})['code'], 'not_found')

    def test_bounded_queue_refuses_without_dropping_first_input(self):
        self.journal.capacity = 1
        first = self.call()
        self.assertEqual(self.call(event='event-2')['code'], 'journal_full')
        self.assertEqual(self.journal.status('owner', first['operation_ref'])['status'], 'durability_pending')

    def test_metadata_controls_and_supplements_never_enable_execution(self):
        first = self.call(); self.worker.run_once()
        task = self.journal.status('owner', first['operation_ref'])['task_ref']
        revised = self.call('revise', {'project_ref': self.project_id, 'task_ref': task, 'expected_revision': 1, 'supplement': 'Add tests'}, event='revise')
        self.worker.run_once()
        self.assertEqual(self.journal.status('owner', revised['operation_ref'])['revision'], 2)
        paused = self.call('control', {'project_ref': self.project_id, 'task_ref': task, 'expected_revision': 2, 'action': 'pause'}, event='pause')
        self.worker.run_once()
        self.assertEqual(self.journal.status('owner', paused['operation_ref'])['status'], 'accepted')
        status = self.call('status', {'project_ref': self.project_id, 'task_ref': task})
        self.assertEqual(status['item']['state'], 'paused')
        self.assertNotIn('requirement', status['item'])
        self.assertFalse(status['execution_enabled'])
        project = self.call('project_control', {'project_ref': self.project_id, 'expected_revision': 3, 'action': 'pause'}, event='project-pause')
        self.worker.run_once()
        self.assertEqual(self.journal.status('owner', project['operation_ref'])['revision'], 4)

    def test_catalog_pages_and_events_use_closed_existing_extension_schemas(self):
        for tool in manifest()['tools']:
            validate_schema(tool['inputSchema']); validate_schema(tool['outputSchema'])
            ExtensionRegistry._tool('delivery', 'http://delivery-provider:8103', 'sha256:' + '1' * 64, tool, True)
        for name in ('projects', 'repositories', 'list'):
            result = self.call(name, {'limit': 1})
            self.assertEqual(result['status'], 'ok')
            self.assertFalse(result['has_more'])
            self.assertNotIn('next_offset', result)
        events = self.call('events', {'project_ref': self.project_id, 'limit': 1})
        self.assertTrue(events['has_more'])
        self.assertEqual(len(events['items']), 1)

    def test_unavailable_action_or_unknown_arguments_are_not_journaled(self):
        for name, args in [('start', {}), ('plan', {**self.args, 'approved': True}),
                           ('plan', {**self.args, 'expected_project_revision': True}),
                           ('control', {'project_ref': self.project_id, 'task_ref': 'task', 'expected_revision': 1, 'action': 'shell'})]:
            self.assertEqual(self.call(name, args)['status'], 'rejected')
        self.assertFalse(self.worker.run_once())

    def test_http_rejects_duplicate_json_nonfinite_oversize_and_wrong_content_type(self):
        for raw in (b'{"version":1,"version":1}', b'{"bad":NaN}', b'\xff'):
            self.assertEqual(self.http(raw=raw)[0], 400)
        self.assertEqual(self.http(raw=b'x' * 65537)[0], 413)
        self.assertEqual(self.http(headers={'Content-Type': 'text/plain'})[0], 415)

    def test_http_rejects_duplicate_lengths_and_chunked_or_duplicate_auth(self):
        for headers in (b'Content-Length: 2\r\nContent-Length: 2\r\n',
                        b'Content-Length: 2\r\nTransfer-Encoding: chunked\r\n',
                        b'Content-Length: 2\r\nAuthorization: Bearer duplicate\r\n'):
            with socket.create_connection(self.serve(), timeout=2) as connection:
                request = (b'POST /internal/v1/tools/call HTTP/1.1\r\nHost: localhost\r\nAuthorization: Bearer '
                           + self.bearer.encode() + b'\r\nContent-Type: application/json\r\n' + headers + b'\r\n{}')
                connection.sendall(request)
                response = b''
                while True:
                    part = connection.recv(4096)
                    if not part: break
                    response += part
            self.assertTrue(response.startswith((b'HTTP/1.1 400', b'HTTP/1.1 401')))
            self.assertNotIn(self.bearer.encode(), response)

    def test_http_incremental_headers_cannot_extend_absolute_read_deadline(self):
        self.serve()
        self.server.read_deadline_seconds = .1
        with socket.create_connection(self.server.server_address, timeout=1) as connection:
            connection.sendall(b'POST /internal/v1/tools/call HTTP/1.1\r\nX-Incomplete: ')
            time.sleep(.03)
            connection.sendall(b'a')
            time.sleep(.03)
            connection.sendall(b'b')
            time.sleep(.12)
            # Per-read idle timers would still be waiting for the rest of this
            # header. The fixed deadline closes the connection instead.
            self.assertEqual(connection.recv(4096), b'')

    def test_restore_freezes_journal_and_worker_even_after_reopening(self):
        first = self.call(); self.worker.run_once()
        result = self.journal.status('owner', first['operation_ref'])
        snapshot = self.replica.objects[result['event_ref']]
        receipt = ReplicaReceipt(snapshot.event_id, snapshot.sha256, 'replica', snapshot.event_id)
        target = Path(self.directory.name) / 'restored.sqlite3'
        DeliveryStore.restore(target, snapshot, receipt, local_domain='new-primary', replica_domain='replica')
        restored = DeliveryStore(target, local_domain='new-primary', replica_domain='replica', replica=self.replica)
        journal = InputJournal(restored)
        with self.assertRaisesRegex(DeliveryError, 'recovery_read_only'): MetadataWorker(journal).run_once()
        self.assertEqual(journal.status('owner', first['operation_ref'])['code'], 'recovery_read_only')
        provider = DeliveryProvider(journal, bearer=self.bearer, origin_key=self.origin_key,
                                    allowed_actors=frozenset({'owner'}), clock=lambda: self.instant)
        self.assertEqual(provider.call(self.payload(event='new-event'))['code'], 'recovery_read_only')

    def test_journal_original_input_cannot_be_rewritten_or_deleted(self):
        self.call()
        with closing(self.store._connect()) as c:
            for sql in ('DELETE FROM input_journal', "UPDATE input_journal SET payload_json='{}'"):
                with self.assertRaises(sqlite3.IntegrityError): c.execute(sql)

    def test_explicit_v1_migration_keeps_private_backup_and_existing_records(self):
        with closing(self.store._connect()) as c:
            c.execute('DROP TRIGGER journal_no_delete'); c.execute('DROP TRIGGER journal_no_rewrite')
            c.execute('DROP TABLE input_journal'); c.execute('PRAGMA user_version=1')
        with self.assertRaisesRegex(DeliveryError, 'schema_upgrade_required'): self.open()
        upgraded = self.open(upgrade_schema=True)
        self.assertEqual(upgraded.catalog('owner', kind='projects')['total'], 1)
        self.assertEqual(upgraded.schema_backup.stat().st_mode & 0o777, 0o600)
        with closing(sqlite3.connect(upgraded.schema_backup)) as c:
            self.assertEqual(c.execute('PRAGMA user_version').fetchone()[0], 1)
            self.assertEqual(c.execute('SELECT count(*) FROM projects').fetchone()[0], 1)
        self.assertFalse(MetadataWorker(InputJournal(upgraded)).run_once())

    def test_migration_failure_rolls_back_schema_and_retains_preupgrade_backup(self):
        with closing(self.store._connect()) as c:
            c.execute('DROP TRIGGER journal_no_delete'); c.execute('DROP TRIGGER journal_no_rewrite')
            c.execute('DROP TABLE input_journal'); c.execute('PRAGMA user_version=1')
        original = Path.read_text
        def broken(path, *args, **kwargs):
            content = original(path, *args, **kwargs)
            return content + '\nINVALID MIGRATION SQL;' if path.name == 'schema.sql' else content
        with patch.object(Path, 'read_text', broken), self.assertRaises(sqlite3.Error):
            self.open(upgrade_schema=True)
        with closing(sqlite3.connect(self.path)) as c:
            self.assertEqual(c.execute('PRAGMA user_version').fetchone()[0], 1)
            self.assertEqual(c.execute('SELECT count(*) FROM projects').fetchone()[0], 1)
        self.assertEqual(len(list(self.path.parent.glob('*.v1-*.sqlite3'))), 1)

    def test_worker_failure_does_not_expose_sensitive_diagnostic_and_is_not_terminal(self):
        first = self.call()
        with patch.object(self.store, 'plan', side_effect=RuntimeError('Bearer fixture-private-debug-text')):
            with self.assertRaisesRegex(DeliveryError, '^worker_result_unknown$'): self.worker.run_once()
        self.assertEqual(self.journal.status('owner', first['operation_ref'])['status'], 'durability_pending')
        self.worker.run_once()
        self.assertEqual(self.journal.status('owner', first['operation_ref'])['status'], 'accepted')

    def test_postcommit_storage_error_is_pending_and_restart_does_not_duplicate(self):
        first = self.call()
        with patch.object(self.store, 'snapshot', side_effect=DeliveryError('snapshot_missing')):
            with self.assertRaisesRegex(DeliveryError, '^worker_result_unknown$'):
                self.worker.run_once()
        self.assertEqual(self.store.catalog('owner', kind='tasks')['total'], 1)
        self.assertEqual(self.journal.status('owner', first['operation_ref'])['status'], 'durability_pending')
        MetadataWorker(InputJournal(self.open())).run_once()
        self.assertEqual(self.journal.status('owner', first['operation_ref'])['status'], 'accepted')
        self.assertEqual(self.store.catalog('owner', kind='tasks')['total'], 1)

    def test_v1_snapshot_restore_requires_explicit_migration_and_remains_frozen(self):
        # Use an actual old-layout SQLite image, with a genuine event chain.
        with closing(sqlite3.connect(':memory:')) as c:
            c.deserialize(self.replica.objects[self.project['event_id']].content)
            c.execute('DROP TRIGGER journal_no_delete'); c.execute('DROP TRIGGER journal_no_rewrite')
            c.execute('DROP TABLE input_journal'); c.execute('PRAGMA user_version=1')
            content = c.serialize()
        snapshot = Snapshot(self.project['event_id'], content, digest(content))
        receipt = ReplicaReceipt(snapshot.event_id, snapshot.sha256, 'replica', snapshot.event_id)
        target = Path(self.directory.name) / 'legacy-restored.sqlite3'
        with self.assertRaisesRegex(DeliveryError, 'schema_upgrade_required'):
            DeliveryStore.restore(target, snapshot, receipt, local_domain='new-primary', replica_domain='replica')
        self.assertFalse(target.exists())
        restored = DeliveryStore.restore(target, snapshot, receipt, local_domain='new-primary',
                                         replica_domain='replica', upgrade_schema=True)
        self.assertEqual(restored.catalog('owner', kind='projects')['total'], 1)
        self.assertTrue(restored.schema_backup.is_file())
        self.assertGreater(restored.verify_event_chain(), 0)
        reopened = DeliveryStore(target, local_domain='new-primary', replica_domain='replica')
        with self.assertRaisesRegex(DeliveryError, 'recovery_read_only'):
            MetadataWorker(InputJournal(reopened)).run_once()
