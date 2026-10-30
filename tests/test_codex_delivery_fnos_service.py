"""Unique Store/Archive composition with real loopback TLS, no live NAS."""
import base64
import json
from pathlib import Path
import tempfile
import unittest

import test_codex_delivery_fnos_file_port as wire
from codex_delivery.codex_delivery.object_archive import ObjectArchive
from codex_delivery.codex_delivery.service import DeliveryService, ServiceConfig
from codex_delivery.codex_delivery.store import DeliveryError


class FnosServiceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        wire.FilePortTests.setUpClass.__func__(cls)

    @classmethod
    def tearDownClass(cls):
        wire.FilePortTests.tearDownClass.__func__(cls)

    def setUp(self):
        wire.FilePortTests.setUp(self)

    def options(self):
        return {'enabled': True, 'policy': 'nas-live-primary-loss-v1',
            'endpoint': self.endpoint, 'directory': wire.DIRECTORY,
            'store_ref': wire.BINDING.store_ref, 'namespace_ref': wire.BINDING.namespace_ref,
            'qualification_ref': wire.BINDING.qualification_ref,
            'credential_ref': 'fixture-oauth', 'key_ref': 'fixture-aes', 'timeout_seconds': 3}

    def config(self, **changes):
        return ServiceConfig.from_dict({'extension_bearer': 's'*32, 'origin_key': 'k'*32,
            'allowed_actors': ['actor:'+'a'*64], 'replica_domain': 'nas-fixture',
            'fnos_file_replica': self.options(), **changes})

    def test_options_cannot_construct_a_credential_provider_or_start_network(self):
        config = self.config()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)/'state'
            with self.assertRaisesRegex(DeliveryError, 'replica_config_invalid'):
                DeliveryService(config, root, address=('127.0.0.1', 0))
            self.assertFalse(root.exists())
        self.assertEqual(self.fixture.calls, [])
        self.assertNotIn(self.endpoint, repr(config))

    def test_disabled_file_backend_is_local_only_and_does_not_use_factory(self):
        config = self.config(fnos_file_replica={'enabled': False})
        with tempfile.TemporaryDirectory() as directory:
            service = DeliveryService(config, Path(directory)/'state', address=('127.0.0.1', 0))
            try:
                self.assertFalse(service.health()['replica_configured'])
            finally:
                service.close()
        self.assertEqual(self.fixture.calls, [])

    def test_mount_file_and_injected_backends_cannot_be_combined(self):
        mounted = {'enabled': True, 'root': '/mnt/fixture/objects', 'fstype': 'nfs4',
            'source': 'fixture.invalid:/export', 'mountpoint': '/mnt/fixture',
            'key_ref': 'mount-key', 'key': base64.b64encode(b'X'*32).decode(), 'timeout_seconds': 10}
        with self.assertRaisesRegex(DeliveryError, 'replica_config_invalid'):
            self.config(replica=mounted)
        with self.assertRaisesRegex(DeliveryError, 'replica_config_invalid'):
            self.config(replica_domain='')
        with tempfile.TemporaryDirectory() as directory, self.assertRaises(DeliveryError):
            DeliveryService(self.config(), Path(directory)/'state', replica=object(),
                fnos_replica_factory=lambda *a, **kw: object())
        self.assertEqual(self.fixture.calls, [])

    def test_one_real_encrypted_replica_is_shared_by_store_and_archive(self):
        created = []
        def factory(settings, root, *, failure_domain):
            replica = settings.create(root, encryption_key=wire.FIXTURE_KEY,
                credential_provider=self.provider, failure_domain=failure_domain,
                ssl_context=self.client_context)
            created.append(replica)
            return replica
        with tempfile.TemporaryDirectory() as directory:
            service = DeliveryService(self.config(), Path(directory).resolve()/'state',
                address=('127.0.0.1', 0), fnos_replica_factory=factory)
            try:
                self.assertEqual(self.fixture.calls, [])
                self.assertEqual(len(created), 1)
                self.assertIs(service.store.replica, created[0])
                archive = ObjectArchive(service.store.replica, local_domain=service.config.local_domain,
                    replica_domain=service.config.replica_domain)
                self.assertIs(archive.replica, created[0])
                result = service.store.create_project('owner-fixture', 'project-fixture',
                    project_alias='fixture', name='Fixture')
                self.assertEqual(result['status'], 'accepted')
                snapshot = service.store.snapshot(result['event_id'])
                receipt = created[0].save(snapshot)
                self.assertEqual(created[0].get(receipt), snapshot)
                health = service.health()
                self.assertEqual(health['replica_policy'], 'nas-live-primary-loss-v1')
                self.assertFalse(health['replica_qualified'])
                self.assertFalse(health['execution_enabled'])
                self.assertNotIn(wire.BEARER, json.dumps(health))
                self.assertTrue(self.fixture.objects)
                self.assertEqual(self.fixture.violations, [])
            finally:
                service.close()


if __name__ == '__main__':
    unittest.main()
