"""Closed administrator settings, independent of mounted replica guarantees."""
from dataclasses import replace
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from codex_delivery.codex_delivery.fnos_file_settings import FnosFileSettings
from codex_delivery.codex_delivery.fnos_file_replica import FileBinding, POLICY
from codex_delivery.codex_delivery.store import DeliveryError


class FnosSettingsTests(unittest.TestCase):
    def options(self):
        return dict(enabled=True, policy=POLICY, endpoint='https://nas.invalid:5667',
            directory='/vol1/backup/objects', store_ref='nas-store', namespace_ref='delivery',
            qualification_ref='qualification-v1', credential_ref='oauth-runtime', key_ref='backup-key')

    def test_default_disabled_does_not_construct_adapter_or_touch_local_files(self):
        with patch('codex_delivery.codex_delivery.fnos_file_replica.FnosFileReplica',
                   side_effect=AssertionError('must not construct')):
            self.assertIsNone(FnosFileSettings.from_dict(None))
            self.assertIsNone(FnosFileSettings.from_dict({'enabled': False}))
            self.assertIsNone(FnosFileSettings.from_dict({**self.options(), 'enabled': False}))

    def test_explicit_fixed_policy_and_nonsecret_references_only(self):
        settings = FnosFileSettings.from_dict(self.options())
        self.assertEqual(settings.binding, FileBinding('nas-store', 'delivery', 'qualification-v1'))
        self.assertEqual(settings.policy, POLICY)
        self.assertEqual(settings.timeout_seconds, 10)
        self.assertNotIn('nas.invalid', repr(settings))
        self.assertNotIn('backup-key', repr(settings))

    def test_malformed_policy_path_budget_or_secret_bearing_extra_fields_fail(self):
        changes = [dict(policy='stable-write'), dict(endpoint='http://nas.invalid'),
            dict(endpoint='https://user:pass@nas.invalid'), dict(endpoint='https://nas.invalid/'),
            dict(endpoint='https://nas.invalid/?token=x'), dict(endpoint='https://NAS.invalid'),
            dict(endpoint='https://nas.invalid:'), dict(endpoint='https://nas.invalid:0'),
            dict(directory='/vol1/../objects'), dict(directory='/vol1/objects/'),
            dict(directory='/vol1/objects%2Fother'), dict(directory='/vol1/a\\b'),
            dict(directory='/vol1/a\nb'), dict(directory='/tmp/objects'),
            dict(timeout_seconds=True), dict(timeout_seconds=0), dict(timeout_seconds=31),
            dict(maxbytes=True), dict(maxbytes=0), dict(maxbytes=512*1024*1024+1),
            dict(credential_ref='https://bearer.invalid'), dict(key='forbidden-secret'),
            dict(refreshToken='forbidden-secret'), dict(enabled=1)]
        for change in changes:
            with self.subTest(change=change), self.assertRaisesRegex(DeliveryError, '^fnos_file_config_invalid$'):
                FnosFileSettings.from_dict({**self.options(), **change})

    def test_missing_or_ambiguous_disabled_configuration_is_rejected(self):
        for options in ({}, {'enabled': True}, {'enabled': False, 'policy': POLICY},
                        {'enabled': False, 'key': 'secret'}):
            with self.subTest(options=options), self.assertRaises(DeliveryError):
                FnosFileSettings.from_dict(options)
        settings = FnosFileSettings.from_dict(self.options())
        with self.assertRaises(DeliveryError):
            replace(settings, policy='different')

    def test_create_is_explicit_private_candidate_and_never_qualifies_itself(self):
        settings = FnosFileSettings.from_dict(self.options())
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            root.chmod(0o700)
            with patch('codex_delivery.codex_delivery.fnos_file_port.FnosHTTPFilePort._wire',
                       side_effect=AssertionError('no network at construction')):
                replica = settings.create(root, encryption_key=b'k'*32,
                    credential_provider=lambda **_: 'access-token-fixture', failure_domain='nas-live')
            self.assertFalse(replica.status()['qualified'])
            self.assertTrue((root / 'fnos-file-replica-journal.json').is_file())
            self.assertFalse((root / 'replica-journal.json').exists())


if __name__ == '__main__':
    unittest.main()
