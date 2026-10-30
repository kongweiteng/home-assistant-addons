"""Trusted boot ordering and real lock retention; no production runtime/HA.

The scheduler Ports below are explicit fixtures. The blocked worker test uses
the actual SoftwareDeliveryWorker and OS flock, not a simulated close result.
"""
import fcntl
import os
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from codex_delivery.codex_delivery.production_lifecycle import ProductionLifecycle
from codex_delivery.codex_delivery.repository_composition import SoftwareDeliveryWorker
from codex_delivery.codex_delivery.repository_execution import SoftwareDeliveryPipeline
from codex_delivery.codex_delivery.store import DeliveryError


class LifecycleTests(unittest.TestCase):
    def prepared(self):
        order = []
        life = ProductionLifecycle()
        store = object()
        source = SimpleNamespace(manager=object(), archive=object())
        original = SimpleNamespace(repository_sources=source)
        worker = Mock()
        worker.start.side_effect = lambda: order.append('worker.start')
        worker.close.side_effect = lambda **kw: (order.append('worker.close'), True)[1]
        factory = Mock(sources=source)
        factory.attach.side_effect = lambda archive: (order.append('worker.attach'), worker)[1]
        ports = Mock(evidence_routes=object())
        ports.attach.side_effect = lambda *args: order.append('registry.attach')
        repo = Mock()
        repo.create.side_effect = lambda **kw: (order.append('repository.create'), factory)[1]
        repo.attach_validation.side_effect = lambda *args: order.append('validation.attach')
        registry = Mock()
        registry.create.side_effect = lambda: (order.append('registry.create'), ports)[1]
        life.prepare_repository(repo, registry)
        life.execution = Mock(repository_factory=factory, store=store,
            repository_sources=source, source_archive=original)
        life.execution.close.side_effect = lambda: order.append('execution.close')
        life.service = Mock(_started=False, store=store,
            coordinator=SimpleNamespace(store=store), config=SimpleNamespace(worker_enabled=True))
        life.service.start.side_effect = lambda: order.append('service.start')
        life.service.close.side_effect = lambda **kw: order.append('service.close')
        return life, repo, ports, factory, worker, order

    def test_original_factory_registry_and_source_attach_before_either_scheduler(self):
        life, repo, ports, factory, worker, order = self.prepared()
        with self.assertRaises(DeliveryError): life.start()
        life.attach_repository(Path('/fixture'))
        repo.attach_validation.assert_called_once_with(factory, life.execution, life.service.coordinator)
        ports.attach.assert_called_once_with(factory.sources.manager, factory.sources.archive, Path('/fixture'))
        factory.attach.assert_called_once_with(life.execution.source_archive)
        self.assertIs(life.service.server.build_evidence, ports.evidence_routes)
        life.start(); life.close(timeout=1); life.close(timeout=1)
        self.assertEqual(order, ['registry.create', 'repository.create', 'validation.attach',
            'registry.attach', 'worker.attach', 'service.start', 'worker.start',
            'worker.close', 'service.close', 'execution.close'])

    def test_failed_validation_does_not_attach_registry_or_start_and_cannot_retry(self):
        life, repo, ports, factory, worker, order = self.prepared()
        repo.attach_validation.side_effect = DeliveryError('repository_validation_unavailable')
        with self.assertRaises(DeliveryError): life.attach_repository(Path('/fixture'))
        repo.attach_validation.side_effect = None
        with self.assertRaises(DeliveryError): life.attach_repository(Path('/fixture'))
        with self.assertRaises(DeliveryError): life.start()
        ports.attach.assert_not_called(); worker.start.assert_not_called()
        life.close(timeout=1)
        life.execution.close.assert_called_once()

    def test_registry_failure_does_not_create_worker_or_expose_route(self):
        life, repo, ports, factory, worker, order = self.prepared()
        ports.attach.side_effect = DeliveryError('registry_build_unavailable')
        with self.assertRaises(DeliveryError): life.attach_repository(Path('/fixture'))
        factory.attach.assert_not_called()
        with self.assertRaises(DeliveryError): life.start()
        life.close(timeout=1)

    def test_wrong_store_or_prestarted_service_fails_before_any_attach(self):
        for change in ('store', 'started', 'factory'):
            with self.subTest(change=change):
                life, repo, ports, factory, worker, order = self.prepared()
                if change == 'store': life.execution.store = object()
                elif change == 'started': life.service._started = True
                else: life.execution.repository_factory = object()
                with self.assertRaises(DeliveryError): life.attach_repository(Path('/fixture'))
                repo.attach_validation.assert_not_called(); ports.attach.assert_not_called()
                life.close(timeout=1)

    def test_prepare_and_start_are_single_attempt_even_after_failure(self):
        life = ProductionLifecycle()
        registry, repo = Mock(), Mock()
        registry.create.side_effect = RuntimeError('fixture')
        with self.assertRaises(DeliveryError): life.prepare_repository(repo, registry)
        registry.create.side_effect = None
        with self.assertRaises(DeliveryError): life.prepare_repository(repo, registry)
        life.close(timeout=1)
        life = ProductionLifecycle(); life.service = Mock()
        life.service.start.side_effect = RuntimeError('fixture')
        with self.assertRaises(RuntimeError): life.start()
        with self.assertRaises(DeliveryError): life.start()
        life.close(timeout=1)

    def test_disabled_workers_never_start_software_scheduler(self):
        life, repo, ports, factory, worker, order = self.prepared()
        life.service.config.worker_enabled = False
        life.attach_repository(Path('/fixture')); life.start()
        worker.start.assert_not_called()
        life.close(timeout=1)

    def test_unsettled_service_or_worker_retains_execution_and_never_retries_cleanup(self):
        for cause in ('worker_false', 'worker_error', 'service_error'):
            with self.subTest(cause=cause):
                life, repo, ports, factory, worker, order = self.prepared()
                life.attach_repository(Path('/fixture'))
                if cause == 'worker_false': worker.close.side_effect = lambda **kw: False
                elif cause == 'worker_error': worker.close.side_effect = RuntimeError('fixture')
                else: life.service.close.side_effect = RuntimeError('fixture')
                with self.assertRaises(DeliveryError) as caught: life.close(timeout=.01)
                self.assertEqual(caught.exception.code, 'service_shutdown_incomplete')
                with self.assertRaises(DeliveryError): life.close(timeout=.01)
                worker.close.assert_called_once(); life.service.close.assert_called_once()
                life.execution.close.assert_not_called()
                with self.assertRaises(DeliveryError): life.start()

    def test_actual_blocked_worker_retains_both_os_singleton_locks(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            pipeline = SoftwareDeliveryPipeline.__new__(SoftwareDeliveryPipeline)
            pipeline.store = SimpleNamespace(path=root/'delivery.sqlite3')
            pipeline.build_executor = pipeline.build_observer = pipeline.target_observer = lambda: None
            worker = SoftwareDeliveryWorker(pipeline, poll_seconds=.05)
            entered, release = threading.Event(), threading.Event()
            def blocked(): entered.set(); release.wait(5)
            worker.run_once = blocked
            execution_fd = os.open(root/'execution-service.lock', os.O_CREAT|os.O_RDWR, 0o600)
            fcntl.flock(execution_fd, fcntl.LOCK_EX|fcntl.LOCK_NB)
            execution_close = Mock(side_effect=lambda: os.close(execution_fd))
            life = ProductionLifecycle()
            life.software_worker = worker
            life.execution = SimpleNamespace(close=execution_close)
            life.service = Mock()
            worker.start()
            self.assertTrue(entered.wait(1))
            try:
                with self.assertRaises(DeliveryError): life.close(timeout=.01)
                self.assertIsNotNone(worker._fd)
                execution_close.assert_not_called()
                for filename in ('execution-service.lock', 'software-worker.lock'):
                    fd = os.open(root/filename, os.O_RDWR)
                    try:
                        with self.assertRaises(BlockingIOError): fcntl.flock(fd, fcntl.LOCK_EX|fcntl.LOCK_NB)
                    finally: os.close(fd)
                replacement = SoftwareDeliveryWorker(pipeline)
                with self.assertRaises(DeliveryError): replacement.start()
                life.service.close.assert_called_once()
            finally:
                release.set()
                self.assertTrue(worker.close(timeout_seconds=2))
                os.close(execution_fd)


if __name__ == '__main__': unittest.main()
