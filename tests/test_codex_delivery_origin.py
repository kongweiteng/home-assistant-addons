from datetime import datetime, timedelta
import unittest
from zoneinfo import ZoneInfo

from codex_delivery.codex_delivery.origin import OriginError, issue_origin, verify_origin


class OriginTests(unittest.TestCase):
    def setUp(self):
        self.key = b'fixture-origin-signing-key-32-bytes'
        self.instant = datetime(2026, 10, 19, 11, 0, tzinfo=ZoneInfo('Asia/Shanghai'))
        self.args = {'project_ref': 'alpha', 'requirement': 'Make a report'}
        self.origin = issue_origin(self.key, actor_ref='owner', conversation_ref='chat', source_event_ref='event',
                                   input_revision=1, input_digest='sha256:' + '1' * 64,
                                   name='plan', arguments=self.args, issued_at=self.instant)

    def verify(self, origin=None, **kwargs):
        arguments = dict(name='plan', arguments=self.args, checked_at=self.instant, allowed_actors=frozenset({'owner'}))
        arguments.update(kwargs)
        return verify_origin(self.key, self.origin if origin is None else origin, **arguments)

    def test_signed_origin_does_not_contain_or_grant_actions(self):
        result = self.verify()
        self.assertEqual(result['actor_ref'], 'owner')
        self.assertNotIn('grant', result)
        self.assertNotIn('approved', result)

    def test_argument_action_and_audience_substitution_rejected(self):
        for values in ({'name': 'start'}, {'arguments': {**self.args, 'requirement': 'Deploy'}}, {'audience': 'another-provider'}):
            with self.assertRaises(OriginError): self.verify(**values)

    def test_unsigned_modified_or_unknown_fields_rejected(self):
        for origin in ({}, {**self.origin, 'actor_ref': 'another-owner'}, {**self.origin, 'approved': True}, {**self.origin, 'signature': 123}):
            with self.assertRaises(OriginError): self.verify(origin)

    def test_member_or_old_owner_not_authorized(self):
        with self.assertRaises(OriginError): self.verify(allowed_actors=frozenset({'member'}))

    def test_expired_and_future_assertions_rejected(self):
        for delta in (-6, 60, 61):
            with self.assertRaises(OriginError): self.verify(checked_at=self.instant + timedelta(seconds=delta))
        self.verify(checked_at=self.instant + timedelta(seconds=59))

    def test_utc_boundary_is_converted_to_shanghai(self):
        result = self.verify(checked_at=self.instant.astimezone(ZoneInfo('UTC')))
        self.assertTrue(result['issued_at'].endswith('+08:00'))
        with self.assertRaises(OriginError): self.verify(checked_at=datetime(2026, 10, 19, 11))

    def test_naive_issuance_or_excessive_lifetime_rejected(self):
        args = dict(actor_ref='owner', conversation_ref='chat', source_event_ref='event', input_revision=1,
                    input_digest='sha256:' + '1' * 64, name='plan', arguments=self.args)
        with self.assertRaises(OriginError): issue_origin(self.key, **args, issued_at=datetime(2026, 10, 19))
        with self.assertRaises(OriginError): issue_origin(self.key, **args, issued_at=self.instant, ttl_seconds=61)

    def test_wrong_or_short_key_rejected(self):
        with self.assertRaises(OriginError): verify_origin(b'wrong-key-material-32-bytes-long!', self.origin, name='plan', arguments=self.args, checked_at=self.instant, allowed_actors=frozenset({'owner'}))
        with self.assertRaises(OriginError): verify_origin(b'short', self.origin, name='plan', arguments=self.args, checked_at=self.instant, allowed_actors=frozenset({'owner'}))
