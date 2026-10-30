"""Signed owner admission through the real journal into same-Store groups.

The original-shaped Release/publication events are explicit trusted fixtures;
these HTTP/metadata checks do not qualify Git, a deployment or acceptance.
"""
from contextlib import closing
from copy import deepcopy
from datetime import datetime
import http.client
import json
import threading
import unittest

from codex_delivery.codex_delivery.journal import InputJournal, MetadataWorker
from codex_delivery.codex_delivery.origin import issue_origin
from codex_delivery.codex_delivery.provider import DeliveryProvider, OUTPUT, validate
from codex_delivery.codex_delivery.server import DeliveryHTTPServer
from codex_delivery.codex_delivery.store import SHANGHAI, canonical
import test_codex_delivery_groups as fixtures


class GroupToolsTests(unittest.TestCase):
    open = fixtures.GroupTests.open
    mutate = fixtures.GroupTests.mutate
    member = fixtures.GroupTests.member

    def setUp(self):
        fixtures.GroupTests.setUp(self)
        self.journal = InputJournal(self.store)
        self.worker = MetadataWorker(self.journal)
        self.instant = datetime(2026, 10, 31, 4, tzinfo=SHANGHAI)
        self.origin_key = b'fixture-group-tools-origin-distinct-32-bytes'
        self.bearer = 'fixture-group-tools-http-authentication-32-bytes'
        self.provider = DeliveryProvider(self.journal, bearer=self.bearer,
            origin_key=self.origin_key, allowed_actors=frozenset({'owner', 'other'}),
            clock=lambda: self.instant)
        self.arguments = dict(members=deepcopy(self.members),
            edges=[dict(before='alpha', after='beta')], acceptance_ref='acceptance-fixture')

    def payload(self, name, args, *, actor='owner', event='group-source'):
        return dict(version=1, name=name, arguments=args,
            context=dict(job_hash='job', turn_hash='turn', idempotency_key='context-only'),
            origin=issue_origin(self.origin_key, actor_ref=actor, conversation_ref='group-chat',
                source_event_ref=event, input_revision=1, input_digest='sha256:'+'1'*64,
                name=name, arguments=args, issued_at=self.instant))

    def call(self, name, args, **scope):
        result = self.provider.call(self.payload(name, args, **scope))
        validate(result, OUTPUT)
        return result

    def create(self):
        admitted = self.call('group_create', self.arguments)
        self.assertEqual(admitted['status'], 'durability_pending')
        self.assertTrue(self.worker.run_once())
        accepted = self.call('operation_status', dict(operation_ref=admitted['operation_ref']))
        self.assertEqual(accepted['status'], 'accepted')
        return accepted

    def test_origin_admission_worker_and_status_share_one_store(self):
        admitted = self.call('group_create', self.arguments)
        with closing(self.store._connect()) as c:
            self.assertEqual(c.execute('SELECT count(*) FROM delivery_groups').fetchone()[0], 0)
        self.assertFalse(admitted['execution_enabled'])
        self.worker.run_once()
        accepted = self.call('operation_status', dict(operation_ref=admitted['operation_ref']))
        self.assertEqual((accepted['status'], accepted['durability']), ('accepted', 'replicated'))
        self.assertEqual(self.call('group_create', self.arguments), accepted)
        group_ref = accepted['group_ref']
        result = self.call('group_status', dict(group_ref=group_ref))
        self.assertEqual(result['group']['state'], 'awaiting_acceptance')
        self.assertFalse(result['execution_enabled'])
        self.assertFalse(result['group']['combination_accepted'])
        for member in result['group']['members']:
            self.assertEqual(member['authorization_state'], 'not_evaluated')
            self.assertEqual(member['fact_count'], 5)
            self.assertTrue(member['facts_truncated'])
            self.assertEqual(len(member['facts']), 3)
        self.assertEqual(len(self.groups.status('owner', group_ref)['members'][0]['facts']), 5)
        listed = self.call('group_list', {})
        self.assertEqual(listed['total'], 1)
        self.assertNotIn('members', listed['groups'][0])
        events = self.call('group_events', dict(group_ref=group_ref))
        self.assertTrue(events['items'][0]['event_ref'].startswith('EV-'))
        self.assertEqual(events['items'][0]['durability'], 'replicated')
        self.assertNotIn('body_json', canonical(events))

    def test_closed_schemas_reject_model_success_owner_and_observe(self):
        invalid = deepcopy(self.arguments)
        invalid['members'][0]['approved'] = True
        self.assertEqual(self.call('group_create', invalid)['code'], 'arguments_invalid')
        self.assertEqual(self.call('group_create', dict(self.arguments, owner_ref='other'))['code'],
                         'arguments_invalid')
        self.assertEqual(self.call('group_observe', dict(group_ref='unknown'))['code'], 'tool_unavailable')
        payload = self.payload('group_list', {})
        payload.pop('origin')
        self.assertEqual(self.provider.call(payload)['code'], 'request_invalid')
        payload = self.payload('group_list', {})
        payload['origin']['actor_ref'] = 'other'
        self.assertEqual(self.provider.call(payload)['code'], 'origin_invalid')
        with closing(self.store._connect()) as c:
            self.assertEqual(c.execute('SELECT count(*) FROM input_journal').fetchone()[0], 0)

    def test_other_owner_cannot_read_or_control_any_member(self):
        group_ref = self.create()['group_ref']
        self.assertEqual(self.call('group_status', dict(group_ref=group_ref), actor='other')['code'], 'not_found')
        self.assertEqual(self.call('group_events', dict(group_ref=group_ref), actor='other')['code'], 'not_found')
        self.assertEqual(self.call('group_list', {}, actor='other')['groups'], [])
        op = self.call('group_control', dict(group_ref=group_ref, expected_revision=1, action='cancel'), actor='other')
        self.worker.run_once()
        result = self.call('operation_status', dict(operation_ref=op['operation_ref']), actor='other')
        self.assertEqual((result['status'], result['code']), ('rejected', 'not_found'))
        self.assertEqual(self.groups.status('owner', group_ref)['control'], 'active')

    def test_controls_use_group_cas_and_never_change_member_tasks(self):
        group_ref = self.create()['group_ref']
        with closing(self.store._connect()) as c:
            before = [tuple(row) for row in c.execute('SELECT * FROM tasks ORDER BY task_id')]
        op = self.call('group_control', dict(group_ref=group_ref, expected_revision=1, action='pause'))
        self.worker.run_once()
        self.assertEqual(self.call('operation_status', dict(operation_ref=op['operation_ref']))['revision'], 2)
        stale = self.call('group_control', dict(group_ref=group_ref, expected_revision=1, action='cancel'), event='later-input')
        self.worker.run_once()
        self.assertEqual(self.call('operation_status', dict(operation_ref=stale['operation_ref']))['code'], 'revision_conflict')
        with closing(self.store._connect()) as c:
            self.assertEqual(before, [tuple(row) for row in c.execute('SELECT * FROM tasks ORDER BY task_id')])

    def test_unavailable_deployment_goal_is_terminal_metadata_rejection(self):
        args = deepcopy(self.arguments)
        args['members'][0]['goal'] = 'deployed'
        op = self.call('group_create', args)
        self.worker.run_once()
        result = self.call('operation_status', dict(operation_ref=op['operation_ref']))
        self.assertEqual((result['status'], result['code']), ('rejected', 'group_goal_unavailable'))
        self.assertEqual(self.groups.list('owner')['total'], 0)

    def test_replica_unknown_retains_one_original_group_operation(self):
        self.replica.timeout_after_save = True
        op = self.call('group_create', self.arguments)
        self.worker.run_once()
        result = self.call('operation_status', dict(operation_ref=op['operation_ref']))
        self.assertEqual(result['status'], 'durability_pending')
        group_ref = self.groups.list('owner')['items'][0]['group_ref']
        self.assertEqual(self.groups.status('owner', group_ref)['durability'], 'local_only')
        self.replica.timeout_after_save = False
        self.worker.run_once()
        accepted = self.call('operation_status', dict(operation_ref=op['operation_ref']))
        self.assertEqual((accepted['status'], accepted['group_ref']), ('accepted', group_ref))
        self.assertEqual(self.groups.list('owner')['total'], 1)

    def test_invalid_member_evidence_rejects_without_blocking_later_valid_input(self):
        for field, value in (('release_digest', 'sha256:'+'0'*64), ('release_ref', 'missing-release')):
            with self.subTest(field=field):
                args = deepcopy(self.arguments)
                args['members'][0][field] = value
                op = self.call('group_create', args, event='invalid-'+field)
                self.worker.run_once()
                result = self.call('operation_status', dict(operation_ref=op['operation_ref']))
                self.assertEqual((result['status'], result['code']),
                                 ('rejected', 'group_evidence_unavailable'))
        self.assertEqual(self.create()['status'], 'accepted')

    def test_real_http_authentication_and_signed_group_read(self):
        group_ref = self.create()['group_ref']
        server = DeliveryHTTPServer(('127.0.0.1', 0), self.provider)
        thread = threading.Thread(target=server.serve_forever, kwargs={'poll_interval': .01}, daemon=True)
        thread.start()
        def close():
            server.shutdown(); server.server_close(); thread.join(2)
        self.addCleanup(close)
        for bearer, expected in ((self.bearer, 200), ('invalid', 401)):
            with self.subTest(expected=expected):
                connection = http.client.HTTPConnection(*server.server_address, timeout=2)
                try:
                    connection.request('POST', '/internal/v1/tools/call',
                        body=canonical(self.payload('group_status', dict(group_ref=group_ref))).encode(),
                        headers={'Content-Type': 'application/json', 'Authorization': 'Bearer '+bearer})
                    response = connection.getresponse()
                    self.assertEqual(response.status, expected)
                    body = json.loads(response.read())
                    if expected == 200:
                        self.assertEqual(body['group']['group_ref'], group_ref)
                        self.assertFalse(body['execution_enabled'])
                finally:
                    connection.close()


if __name__ == '__main__':
    unittest.main()
