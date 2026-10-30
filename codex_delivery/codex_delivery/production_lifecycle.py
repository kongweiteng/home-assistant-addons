"""Serial trusted boot and shutdown; never used as a task/plugin loader.

All Ports come from fixed image-owned boot plans. Attachment precedes either
scheduler. An unsettled software Port or Coordinator retains execution resources
and locks; cleanup never retries or silently substitutes an execution instance.
"""
import time

from .store import DeliveryError


class ProductionLifecycle:
    def __init__(self):
        self.execution = self.service = self.repository_factory = None
        self.registry_ports = self.software_worker = None
        self._repository_plan = None
        self._registry_plan = self.software_management = None
        self._prepare_attempted = False
        self._attached = self._attach_attempted = self._start_attempted = False
        self._close_attempted = self._closed = False
        self._management_attempted = False

    def prepare_repository(self, repository_plan, registry_plan, git_plan=None):
        if (self._prepare_attempted or self.repository_factory is not None or self._repository_plan is not None
                or self.execution is not None or self.service is not None
                or self._close_attempted):
            raise DeliveryError('repository_boot_unavailable')
        # A failed prepare is also a consumed attempt: a different plan must
        # never repair the identity of a partially created set of Ports.
        self._repository_plan = repository_plan
        self._registry_plan = registry_plan
        self._prepare_attempted = True
        try:
            self.registry_ports = registry_plan.create()
            ports = dict(build_executor=self.registry_ports.execute, build_observer=self.registry_ports.observe)
            if git_plan is not None:
                ports['git_runtime'] = git_plan.create(repository_plan)
            self.repository_factory = repository_plan.create(**ports)
        except Exception:
            raise DeliveryError('repository_boot_unavailable') from None

    def attach_repository(self, state_root):
        if (self._attach_attempted or self._start_attempted or self._close_attempted
                or self.repository_factory is None or self.registry_ports is None
                or self.execution is None or self.service is None):
            raise DeliveryError('repository_boot_unavailable')
        self._attach_attempted = True
        execution, service, factory = self.execution, self.service, self.repository_factory
        if (execution.repository_factory is not factory or service._started
                or service.coordinator is None or service.coordinator.store is not service.store
                or execution.store is not service.store or factory.sources is not execution.repository_sources
                or execution.source_archive.repository_sources is not factory.sources):
            raise DeliveryError('repository_boot_unavailable')
        self._repository_plan.attach_validation(factory, execution, service.coordinator)
        # Original manager/archive only; no second Store, source archive or VM.
        self.registry_ports.attach(factory.sources.manager, factory.sources.archive, state_root)
        routes = self.registry_ports.evidence_routes
        self.software_worker = factory.attach(execution.source_archive)
        service.server.build_evidence = routes
        self._attached = True

    def attach_software_management(self):
        if (self._management_attempted or not self._attached or self._start_attempted or self._close_attempted
                or self.software_management is not None
                or self.service.provider.software_management is not None
                or self.service.worker.software_management is not None
                or self.software_worker.request_handler is not None):
            raise DeliveryError('software_management_unavailable')
        self._management_attempted = True
        from .software_management import SoftwareManagement
        management = SoftwareManagement(self.repository_factory.pipeline, self._repository_plan, self._registry_plan)
        if management.store is not self.service.store:
            raise DeliveryError('software_management_unavailable')
        self.software_management = management
        self.software_worker.request_handler = management.process_next
        self.service.worker.software_management = management
        self.service.provider.software_management = management

    def start(self):
        if (self._start_attempted or self._close_attempted or self.service is None
                or self._repository_plan is not None and not self._attached
                or self._management_attempted and self.software_management is None):
            raise DeliveryError('repository_boot_unavailable')
        self._start_attempted = True
        self.service.start()
        if self.software_worker is not None and self.service.config.worker_enabled:
            self.software_worker.start()

    def close(self, *, timeout):
        if self._close_attempted:
            if not self._closed:
                raise DeliveryError('service_shutdown_incomplete')
            return
        self._close_attempted = True
        deadline = time.monotonic() + timeout
        settled = True
        if self.software_worker is not None:
            try:
                settled = self.software_worker.close(timeout_seconds=max(0, deadline-time.monotonic())) is True
            except Exception:
                settled = False
        # Stop intake and the original Coordinator even when a build Port has
        # not settled. This method never releases QualifiedExecution itself.
        if self.service is not None:
            try:
                self.service.close(timeout=max(0, deadline-time.monotonic()))
            except Exception:
                settled = False
        if not settled:
            raise DeliveryError('service_shutdown_incomplete')
        if self.execution is not None:
            try:
                self.execution.close()
            except Exception:
                raise DeliveryError('service_shutdown_incomplete') from None
        self._closed = True
