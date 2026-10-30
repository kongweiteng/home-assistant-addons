"""Replica composition without network mounts or real credentials."""
import base64
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from codex_delivery.codex_delivery.durability import ReplicaReceipt
from codex_delivery.codex_delivery.replica_config import ReplicaSettings
from codex_delivery.codex_delivery.service import DeliveryService, ServiceConfig
from codex_delivery.codex_delivery.store import DeliveryError
from codex_delivery.codex_delivery.work import WorkStore


class ReplicaConfigTests(unittest.TestCase):
    def options(self):
        return {'enabled': True, 'root': '/mnt/fixture-share/private-objects', 'fstype': 'nfs4',
            'source': 'fixture.invalid:/export', 'mountpoint': '/mnt/fixture-share',
            'key_ref': 'fixture-backup-key', 'key': base64.b64encode(b'fixture-'*4).decode(),
            'timeout_seconds': 10}

    def config(self, options=None, **changes):
        value = {'extension_bearer': 's'*32, 'origin_key': 'k'*32,
            'allowed_actors': ['actor:' + 'a'*64], 'replica_domain': 'independent',
            'replica': self.options() if options is None else options}
        value.update(changes)
        return ServiceConfig.from_dict(value)

    def test_configuration_validation_does_not_access_mount_or_create_adapter(self):
        with patch('codex_delivery.codex_delivery.bounded_replica.DeadlineReplica', side_effect=AssertionError('no instance')):
            value = self.config()
        self.assertIsInstance(value.replica_settings, ReplicaSettings)
        self.assertNotIn(self.options()['key'], repr(value))
        self.assertNotIn('fixture-key', repr(value.replica_settings))
        self.assertNotIn('fixture.invalid', repr(value))

    def test_enabled_configuration_requires_exact_identity_and_independent_key(self):
        cases = [{'key': 'not-base64'}, {'key': base64.b64encode(b'k'*32).decode()},
            {'key': base64.b64encode(b'x'*31).decode()}, {'source': 'https://fixture.invalid/export'},
            {'source': '//user:pass@fixture.invalid/export'}, {'root': '/tmp/local'},
            {'mountpoint': '/'}, {'timeout_seconds': True}, {'extra': 1}, {'fstype': 'ext4'}]
        for changes in cases:
            with self.subTest(changes=changes), self.assertRaisesRegex(DeliveryError, 'replica_config_invalid'):
                self.config({**self.options(), **changes})
        with self.assertRaises(DeliveryError):
            self.config(replica_domain='')

    def test_disabled_settings_do_not_construct_replica(self):
        config = self.config({**self.options(), 'enabled': False})
        self.assertIsNone(config.replica_settings)
        with tempfile.TemporaryDirectory() as directory:
            service = DeliveryService(config, Path(directory) / 'state', address=('127.0.0.1', 0))
            try:
                self.assertFalse(service.health()['replica_configured'])
            finally:
                service.close()

    def test_composition_only_initializes_private_local_journal_until_first_operation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve() / 'state'
            with patch('codex_delivery.codex_delivery.bounded_replica.DeadlineReplica._launch',
                       side_effect=AssertionError('no network worker at startup')):
                service = DeliveryService(self.config(), root, address=('127.0.0.1', 0))
                try:
                    health = service.health()
                    self.assertTrue(health['replica_configured'])
                    self.assertEqual(health['replica_state'], 'unqualified')
                    self.assertFalse(health['replica_qualified'])
                    self.assertFalse(health['execution_enabled'])
                    self.assertTrue((root / 'replica-journal.json').is_file())
                    self.assertEqual((root / 'replica-journal.json').stat().st_mode & 0o777, 0o600)
                    self.assertNotIn('fixture.invalid', json.dumps(health))
                finally:
                    service.close()

    def test_fused_replica_reconciliation_never_saves_again_or_regenerates_snapshot(self):
        class LostReceipt:
            def __init__(self): self.fused, self.saves, self.reads, self.snapshot = False, 0, 0, None
            def status(self): return {'state': 'fused' if self.fused else 'unqualified'}
            def save(self, snapshot):
                self.saves += 1
                self.snapshot, self.fused = snapshot, True
                raise TimeoutError('fixture acknowledgement lost')
            def reconcile(self, snapshot):
                self.reads += 1
                if self.snapshot != snapshot: raise AssertionError('snapshot changed')
                return ReplicaReceipt(snapshot.event_id, snapshot.sha256, 'independent', 'fixture-object')
        with tempfile.TemporaryDirectory() as directory:
            replica = LostReceipt()
            store = WorkStore(Path(directory) / 'state' / 'delivery.sqlite3', local_domain='primary',
                replica_domain='independent', replica=replica)
            result = store.create_project('owner', 'create-fixture', project_alias='fixture', name='Fixture')
            self.assertEqual(result['status'], 'durability_pending')
            self.assertEqual(store.reconcile(result['event_id'])['status'], 'accepted')
            self.assertEqual((replica.saves, replica.reads), (1, 1))
            self.assertEqual(store.reconcile(result['event_id'])['status'], 'accepted')
            self.assertEqual((replica.saves, replica.reads), (1, 1))


if __name__ == '__main__':
    unittest.main()
