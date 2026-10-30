from __future__ import annotations

import json
from pathlib import Path
import tempfile
import threading
import unittest
from urllib.error import HTTPError
from urllib.request import urlopen

from codex_delivery.codex_delivery.service import DeliveryService, ServiceConfig
from codex_delivery.codex_delivery.store import DeliveryError
from test_codex_delivery_work import Replica


class ServiceTests(unittest.TestCase):
    def config(self, **changes):
        value = {'extension_bearer': 's' * 32, 'origin_key': 'k' * 32,
                 'allowed_actors': ['actor:' + 'a' * 64]}
        value.update(changes)
        return ServiceConfig.from_dict(value)

    def test_flags_cannot_enable_unqualified_execution(self):
        for value in (True, 1, 'false', None):
            with self.subTest(value=value), self.assertRaises(DeliveryError):
                self.config(execution_enabled=value)

    def test_config_has_no_secret_repr_and_requires_real_actor_shape(self):
        config = self.config()
        self.assertNotIn('s' * 32, repr(config))
        self.assertNotIn('k' * 32, repr(config))
        for changes in ({'allowed_actors': []}, {'allowed_actors': ['owner:1']},
                        {'allowed_actors': ['actor:' + 'a' * 64] * 2},
                        {'worker_interval_seconds': True}, {'upgrade_schema': 1},
                        {'origin_key': 's' * 32},
                        {'replica_domain': 'ha-delivery'}, {'unknown': True}):
            with self.subTest(changes=changes), self.assertRaises(DeliveryError):
                self.config(**changes)

    def test_duplicate_keys_and_nonfinite_config_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / 'options.json'
            for content in ('{"extension_bearer":"x","extension_bearer":"y"}',
                            '{"worker_interval_seconds":NaN}'):
                path.write_text(content)
                with self.assertRaises(DeliveryError):
                    ServiceConfig.load(path)

    def test_runner_settings_are_private_data_and_cannot_supply_an_execution_backend(self):
        runner = {'enabled': True, 'controller_url': 'http://fixture-controller:8130',
            'runner_ref': 'RN-' + 'A'*20, 'runner_bearer': 'r'*32,
            'callback_bearer': 'c'*32, 'policy_revision': 1, 'policy_digest': 'sha256:'+'f'*64}
        config = self.config(runner=runner)
        self.assertIsNotNone(config.execution_settings)
        self.assertNotIn('r'*32, repr(config))
        self.assertIsNone(self.config(runner={**runner, 'enabled': False}).execution_settings)
        for patch in ({'command': 'sh'}, {'enabled': 1}, {'runner_bearer': 's'*32},
                      {'callback_bearer': 'k'*32}, {'runner_bearer': 'c'*32}):
            with self.subTest(patch=patch), self.assertRaises(DeliveryError):
                self.config(runner={**runner, **patch})

    def test_disabled_worker_hides_qualified_profiles_from_provider_and_health(self):
        class Coordinator:
            def __init__(self, store): self.store = store
            def request_stop(self): pass
        with tempfile.TemporaryDirectory() as directory:
            service = DeliveryService(self.config(replica_domain='independent', worker_enabled=False),
                Path(directory) / 'state', address=('127.0.0.1', 0), replica=Replica(),
                coordinator_factory=Coordinator,
                capabilities=lambda: [{'profile': 'data', 'runtime_id': 'fixture-python', 'available': True}])
            try:
                self.assertEqual([], service.provider.capabilities())
                self.assertFalse(service.health()['execution_enabled'])
            finally:
                service.close()

    def test_health_and_shutdown_without_execution_or_replica(self):
        with tempfile.TemporaryDirectory() as d:
            service = DeliveryService(self.config(), Path(d) / 'state', address=('127.0.0.1', 0))
            service.start()
            try:
                host, port = service.server.server_address
                with urlopen(f'http://{host}:{port}/healthz', timeout=2) as response:
                    health = json.load(response)
                self.assertFalse(health['execution_enabled'])
                self.assertEqual(health['qualified_profiles'], [])
                self.assertFalse(health['replica_configured'])
                self.assertEqual(health['service_mode'], 'metadata_only')
                with self.assertRaises(HTTPError) as caught:
                    urlopen(f'http://{host}:{port}/not-a-health-route', timeout=2)
                self.assertEqual(caught.exception.code, 404)
                with self.assertRaises(DeliveryError):
                    service.start()
            finally:
                service.close()

    def test_worker_exception_is_redacted_and_does_not_kill_listener(self):
        with tempfile.TemporaryDirectory() as d:
            service = DeliveryService(self.config(), Path(d) / 'state', address=('127.0.0.1', 0))
            attempted = threading.Event()
            def fail():
                attempted.set()
                raise RuntimeError('sensitive-exception-never-published')
            service.worker.run_once = fail
            service.start()
            try:
                self.assertTrue(attempted.wait(2))
                with service._guard:
                    pass
                # Readiness exposes only a fixed code, never the raw exception.
                encoded = json.dumps(service.health())
                self.assertNotIn('sensitive-exception', encoded)
                self.assertTrue(service._http.is_alive())
            finally:
                service.close()
            self.assertFalse(service._worker.is_alive())
            self.assertFalse(service._http.is_alive())
            service.close()

    def test_recovery_service_does_not_start_metadata_writes(self):
        with tempfile.TemporaryDirectory() as d:
            service = DeliveryService(self.config(worker_enabled=False), Path(d) / 'state', address=('127.0.0.1', 0))
            service.store.recovery_mode = True
            service.start()
            try:
                self.assertTrue(service.health()['recovery_read_only'])
                self.assertFalse(service._worker.is_alive())
            finally:
                service.close()

    def test_qualified_launcher_has_separate_worker_and_bounded_shutdown(self):
        started, stopped = threading.Event(), threading.Event()
        class Coordinator:
            def __init__(self, store): self.store = store
            def run_once(self):
                started.set()
                stopped.wait(2)
            def request_stop(self): stopped.set()
        with tempfile.TemporaryDirectory() as d:
            service = DeliveryService(self.config(replica_domain='independent'), Path(d) / 'state',
                address=('127.0.0.1', 0), replica=Replica(), coordinator_factory=Coordinator,
                capabilities=lambda: [{'profile': 'document', 'runtime_id': 'fixture-python', 'available': True}])
            service.start()
            try:
                self.assertTrue(started.wait(2))
                self.assertEqual(service.health()['service_mode'], 'qualified_work')
                self.assertTrue(service.health()['execution_enabled'])
                self.assertTrue(service._http.is_alive())
            finally:
                service.close()
            self.assertTrue(stopped.is_set())
            self.assertFalse(service._executor.is_alive())
            self.assertFalse(service.health()['execution_enabled'])

    def test_scheduler_snapshot_receipt_is_reconciled_without_input_journal_row(self):
        with tempfile.TemporaryDirectory() as d:
            replica = Replica()
            service = DeliveryService(self.config(replica_domain='independent'), Path(d) / 'state',
                address=('127.0.0.1', 0), replica=replica)
            replica.online = False
            result = service.store.create_work('owner', 'fixture-create', conversation_ref='conversation',
                source_event_ref='fixture-source', input_revision=1, requirement='Report',
                spec={'profile': 'document', 'runtime_id': 'fixture-python', 'program': 'fixture',
                      'output_names': ['report.txt'], 'input_refs': []})
            self.assertEqual(result['status'], 'durability_pending')
            replica.online = True
            reconciled = threading.Event()
            original = service.store.reconcile
            def reconcile(event):
                value = original(event)
                if value['status'] == 'accepted': reconciled.set()
                return value
            service.store.reconcile = reconcile
            service.start()
            try:
                self.assertTrue(reconciled.wait(2))
                self.assertEqual(original(result['event_id'])['status'], 'accepted')
            finally:
                service.close()


if __name__ == '__main__':
    unittest.main()
