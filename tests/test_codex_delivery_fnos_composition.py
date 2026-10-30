"""Fixed NAS boot policy and real TLS/AES calibration; no live credentials."""
from dataclasses import asdict, replace
from datetime import datetime, timedelta
import base64
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import test_codex_delivery_fnos_file_port as wire
from codex_delivery.codex_delivery import fnos_composition as boot
from codex_delivery.codex_delivery.durability import Snapshot, digest
from codex_delivery.codex_delivery.fnos_file_settings import FnosFileSettings
from codex_delivery.codex_delivery.store import DeliveryError, SHANGHAI


class FnosBootTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        wire.FilePortTests.setUpClass.__func__(cls)

    @classmethod
    def tearDownClass(cls):
        wire.FilePortTests.tearDownClass.__func__(cls)

    def setUp(self):
        wire.FilePortTests.setUp(self)
        self.settings = FnosFileSettings(boot.POLICY, self.endpoint, wire.DIRECTORY,
            wire.BINDING.store_ref, wire.BINDING.namespace_ref, wire.BINDING.qualification_ref,
            'oauth-fixture', 'aes-fixture', timeout_seconds=3)
        self.binding = boot.OAuthBinding('oauth-fixture', 'fixture', self.endpoint,
            1000, 'fixture-device', ('trim.file.read', 'trim.file.write'))
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name).resolve()
        (self.root/'state').mkdir(mode=0o700)
        self.core = self.settings.create(self.root/'state', encryption_key=wire.FIXTURE_KEY,
            credential_provider=self.provider, failure_domain='nas-fixture',
            ssl_context=self.client_context)
        content = b'fixed synthetic calibration'
        self.receipt = self.core.save(Snapshot('QF-fixture', content, digest(content)))
        now = datetime.now(SHANGHAI)
        self.policy = dict(version=1, policy=boot.POLICY, settings=asdict(self.settings),
            service_uid=65534, oauth_binding=asdict(self.binding), cli_digest='sha256:'+'a'*64,
            allow_file_backend=True, file_backend_authorization_ref='test-ci-fixture',
            qualification=dict(receipt=asdict(self.receipt),
                qualified_at=(now-timedelta(minutes=1)).isoformat(),
                expires_at=(now+timedelta(hours=1)).isoformat(),
                recovery_evidence_digest='sha256:'+'b'*64))
        self.secret = dict(version=1, key_ref='aes-fixture', failure_domain='nas-fixture',
            key=base64.b64encode(wire.FIXTURE_KEY).decode())
        self.fixture.calls.clear()

    def parse(self, policy=None, secret=None, settings=None, reserved=()):
        return boot.FnosBootPlan.parse(json.dumps(policy or self.policy).encode(),
            json.dumps(secret or self.secret).encode(), settings=settings or self.settings,
            failure_domain='nas-fixture', reserved_secrets=reserved)

    def test_exact_calibration_is_get_only_and_ciphertext_is_unchanged(self):
        plan = self.parse()
        original_objects = dict(self.fixture.objects)
        with patch.object(boot, 'FileSessionLoader') as loader, \
                patch.object(boot, 'FixedCLIRefreshPort') as refresh, \
                patch.object(boot, 'FnosOAuthProvider', return_value=self.provider), \
                patch.object(FnosFileSettings, 'create', return_value=self.core) as factory:
            replica = plan.create(self.settings, self.root/'state', failure_domain='nas-fixture')
        self.assertIs(replica._core, self.core)
        self.assertEqual(factory.call_count, 1)
        self.assertEqual(refresh.call_args.args[2], 'a'*64)
        self.assertTrue(loader.call_args.kwargs['allow_file_backend'])
        self.assertEqual(self.fixture.objects, original_objects)
        self.assertEqual([(c[0], c[1].split('?')[0]) for c in self.fixture.calls],
            [('POST', wire.PROXY), ('POST', wire.DOWNLOAD), ('GET', wire.DOWNLOAD)])
        self.assertTrue(replica.status()['qualified'])
        self.assertNotIn(self.secret['key'], repr(plan))

    def test_tampered_calibration_fails_without_upload_or_repair(self):
        plan = self.parse()
        self.fixture.objects[self.receipt.object_ref] = b'corrupt-envelope'
        with patch.object(boot, 'FileSessionLoader'), patch.object(boot, 'FixedCLIRefreshPort'), \
                patch.object(boot, 'FnosOAuthProvider', return_value=self.provider), \
                patch.object(FnosFileSettings, 'create', return_value=self.core):
            with self.assertRaises(DeliveryError):
                plan.create(self.settings, self.root/'state', failure_domain='nas-fixture')
        self.assertFalse(any(c[1] == wire.UPLOAD for c in self.fixture.calls))
        self.assertEqual(self.fixture.objects[self.receipt.object_ref], b'corrupt-envelope')

    def test_policy_mismatches_fail_before_provider_or_remote_access(self):
        cases = []
        for key, value in [('service_uid', 0), ('version', True), ('allow_file_backend', False),
                ('cli_digest', 'a'*64), ('file_backend_authorization_ref', '../bad')]:
            candidate = copy.deepcopy(self.policy)
            candidate[key] = value
            cases.append(candidate)
        for section, key, value in [('settings', 'namespace_ref', 'other-domain'),
                ('oauth_binding', 'credential_ref', 'other-oauth'),
                ('qualification', 'expires_at', datetime.now(SHANGHAI).isoformat()),
                ('qualification', 'recovery_evidence_digest', 'true')]:
            candidate = copy.deepcopy(self.policy)
            candidate[section][key] = value
            cases.append(candidate)
        for candidate in cases:
            with self.subTest(candidate=candidate), self.assertRaisesRegex(DeliveryError,
                    'fnos_composition_invalid'):
                self.parse(policy=candidate)
        with self.assertRaises(DeliveryError):
            self.parse(reserved=(self.secret['key'],))
        with self.assertRaises(DeliveryError):
            self.parse(secret={**self.secret, 'key_ref': 'another-key'})
        self.assertEqual(self.fixture.calls, [])

    def test_duplicate_fields_and_nonfinite_values_are_rejected(self):
        raw = json.dumps(self.policy).encode()
        for content in (b'{"version":2,'+raw[1:], raw.replace(b'65534', b'NaN')):
            with self.assertRaises(DeliveryError):
                boot.FnosBootPlan.parse(content, json.dumps(self.secret).encode(),
                    settings=self.settings, failure_domain='nas-fixture', reserved_secrets=())
        self.assertEqual(self.fixture.calls, [])

    def test_non_ascii_independent_service_secret_is_not_a_backend_failure(self):
        plan = self.parse(reserved=('独立来源签名材料'*8,))
        self.assertEqual(plan.settings, self.settings)
        self.assertEqual(self.fixture.calls, [])

    def test_expired_runtime_rejects_every_io_and_does_not_claim_qualified(self):
        plan = replace(self.parse(), expires_at=datetime.now(SHANGHAI)-timedelta(seconds=1))
        replica = boot.QualifiedFileReplica(self.core, plan)
        for method, value in [('save', object()), ('get', self.receipt),
                             ('reconcile', object()), ('ciphertext_digest', self.receipt)]:
            with self.subTest(method=method), self.assertRaisesRegex(DeliveryError,
                    'fnos_qualification_expired'):
                getattr(replica, method)(value)
        self.assertFalse(replica.status()['qualified'])
        self.assertEqual(self.fixture.calls, [])

    def test_fused_writer_cannot_be_qualified_by_a_static_policy(self):
        replica = boot.QualifiedFileReplica(Mock(status=lambda: {'state': 'fused', 'policy': boot.POLICY}),
                                           self.parse())
        self.assertFalse(replica.status()['qualified'])

    def test_main_config_check_cannot_initialize_state_or_issue_calibration_reads(self):
        from codex_delivery.codex_delivery import main
        from codex_delivery.codex_delivery.service import ServiceConfig
        config = ServiceConfig.from_dict(dict(extension_bearer='b'*32, origin_key='o'*32,
            allowed_actors=['actor:'+'a'*64], replica_domain='nas-fixture',
            fnos_file_replica={'enabled': True, **asdict(self.settings)}))
        with patch.object(main.sys, 'argv', ['delivery', '--check-config']), \
                patch.object(main.ServiceConfig, 'load', return_value=config), \
                patch.object(boot.FnosBootPlan, 'load', return_value=self.parse()), \
                patch('codex_delivery.codex_delivery.bootstrap.prepare_and_drop') as drop, \
                patch.object(main, 'DeliveryService') as service, \
                patch.dict(main.os.environ, {'CODEX_DELIVERY_STATE_ROOT': '/data/state'}):
            self.assertEqual(main.main(), 0)
        drop.assert_not_called()
        service.assert_not_called()
        self.assertEqual(self.fixture.calls, [])


if __name__ == '__main__':
    unittest.main()
