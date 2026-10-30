"""Bounded service lifecycle; task code only runs through a qualified adapter.

Execution qualification is a separate trust boundary. An administrator cannot
turn this metadata package into an executor by setting an options flag.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from contextlib import closing
import base64
import json
import hmac
from pathlib import Path
import re
import threading
import time
from typing import Any, Callable

from . import __version__
from .coordinator import WorkCoordinator
from .durability import Replica
from .journal import InputJournal, MetadataWorker
from .provider import DeliveryProvider
from .replica_config import ReplicaSettings
from .fnos_file_settings import FnosFileSettings
from .server import DeliveryHandler, DeliveryHTTPServer
from .store import DeliveryError, reference
from .work import WorkStore


ACTOR = re.compile(r"actor:[a-f0-9]{64}\Z")


def _pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in items:
        if key in value:
            raise DeliveryError('service_config_invalid')
        value[key] = item
    return value


@dataclass(frozen=True)
class ServiceConfig:
    extension_bearer: str = field(repr=False)
    origin_key: bytes = field(repr=False)
    allowed_actors: frozenset[str]
    local_domain: str = 'ha-delivery'
    replica_domain: str = ''
    upgrade_schema: bool = False
    worker_enabled: bool = True
    worker_interval_seconds: int = 1
    journal_capacity: int = 128
    replica_settings: ReplicaSettings | None = field(default=None, repr=False)
    execution_settings: Any | None = field(default=None, repr=False)
    file_replica_settings: FnosFileSettings | None = field(default=None, repr=False)

    @classmethod
    def load(cls, path: Path) -> 'ServiceConfig':
        try:
            raw = path.read_bytes()
            if len(raw) > 65536:
                raise DeliveryError('service_config_invalid')
            value = json.loads(raw, object_pairs_hook=_pairs,
                               parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
            return cls.from_dict(value)
        except (OSError, ValueError, TypeError, KeyError, RecursionError):
            raise DeliveryError('service_config_invalid') from None

    @classmethod
    def from_dict(cls, value: Any) -> 'ServiceConfig':
        required = {'extension_bearer', 'origin_key', 'allowed_actors'}
        optional = {'local_domain', 'replica_domain', 'upgrade_schema', 'worker_enabled',
                    'worker_interval_seconds', 'journal_capacity', 'execution_enabled', 'replica', 'runner',
                    'fnos_file_replica'}
        if not isinstance(value, dict) or not required <= value.keys() or value.keys() - required - optional:
            raise DeliveryError('service_config_invalid')
        if value.get('execution_enabled', False) is not False:
            raise DeliveryError('execution_not_qualified')
        bearer, origin = value['extension_bearer'], value['origin_key']
        if (not isinstance(bearer, str) or not 32 <= len(bearer) <= 4096
                or any(not 33 <= ord(c) <= 126 for c in bearer)
                or not isinstance(origin, str) or not 32 <= len(origin.encode()) <= 4096
                or any(ord(c) < 32 or ord(c) == 127 for c in origin)):
            raise DeliveryError('service_config_invalid')
        if hmac.compare_digest(bearer.encode(), origin.encode()):
            raise DeliveryError('service_config_invalid')
        actors = value['allowed_actors']
        if (not isinstance(actors, list) or not 1 <= len(actors) <= 64
                or any(not isinstance(x, str) or not ACTOR.fullmatch(x) for x in actors)
                or len(set(actors)) != len(actors)):
            raise DeliveryError('service_config_invalid')
        for key in ('upgrade_schema', 'worker_enabled'):
            if key in value and type(value[key]) is not bool:
                raise DeliveryError('service_config_invalid')
        for key, low, high in (('worker_interval_seconds', 1, 30), ('journal_capacity', 1, 1024)):
            if key in value and (type(value[key]) is not int or not low <= value[key] <= high):
                raise DeliveryError('service_config_invalid')
        local, replica = value.get('local_domain', 'ha-delivery'), value.get('replica_domain', '')
        reference(local)
        if replica:
            reference(replica)
            if replica == local:
                raise DeliveryError('service_config_invalid')
        elif not isinstance(replica, str):
            raise DeliveryError('service_config_invalid')
        settings = ReplicaSettings.from_dict(value.get('replica'), domain=replica,
                                              reserved_secrets=(bearer, origin))
        file_settings = FnosFileSettings.from_dict(value.get('fnos_file_replica'))
        if file_settings is not None and (settings is not None or not replica):
            raise DeliveryError('replica_config_invalid')
        execution = None
        runner = value.get('runner')
        if runner is not None:
            fields = {'enabled', 'controller_url', 'runner_ref', 'runner_bearer', 'callback_bearer',
                      'policy_revision', 'policy_digest'}
            if (not isinstance(runner, dict) or set(runner) != fields
                    or type(runner['enabled']) is not bool
                    or any(not isinstance(runner[k], str) for k in fields - {'enabled', 'policy_revision'})
                    or type(runner['policy_revision']) is not int or not 1 <= runner['policy_revision'] <= 2**53-1):
                raise DeliveryError('execution_settings_invalid')
            if runner['enabled']:
                from .execution_composition import ExecutionSettings
                reserved = (bearer, origin)
                if settings is not None:
                    reserved += (base64.b64encode(settings.key).decode(),)
                    try:
                        reserved += (settings.key.decode(),)
                    except UnicodeError:
                        pass
                execution = ExecutionSettings.from_dict({k: v for k, v in runner.items() if k != 'enabled'},
                                                        reserved_secrets=reserved)
        return cls(bearer, origin.encode(), frozenset(actors), local, replica,
                   value.get('upgrade_schema', False), value.get('worker_enabled', True),
                   value.get('worker_interval_seconds', 1), value.get('journal_capacity', 128), settings, execution,
                   file_settings)


class ServiceHandler(DeliveryHandler):
    def do_GET(self) -> None:
        if self._build_evidence_request():
            return
        if self._authority_request():
            return
        if self.path != '/healthz':
            self._reject(404, 'not_found')
            return
        self._reply(200, self.server.service.health())


class DeliveryService:
    def __init__(self, config: ServiceConfig, state_root: Path, *,
                 address: tuple[str, int] = ('0.0.0.0', 8131), replica: Replica | None = None,
                 coordinator_factory: Callable[[WorkStore], WorkCoordinator] | None = None,
                 capabilities: Callable[[], list[dict[str, Any]]] | None = None,
                 fnos_replica_factory: Callable[..., Replica] | None = None,
                 release_authority_factory: Callable[[WorkStore], Any] | None = None):
        self.config = config
        if replica is not None and (config.replica_settings is not None or config.file_replica_settings is not None):
            raise DeliveryError('replica_config_invalid')
        # Only trusted boot composition supplies credentials and qualification.
        # Options alone cannot install a provider, choose code, or enable the
        # weaker NAS-survives policy through the existing mounted backend.
        if (config.file_replica_settings is None) != (fnos_replica_factory is None):
            raise DeliveryError('replica_config_invalid')
        if replica is not None and not config.replica_domain:
            raise DeliveryError('service_config_invalid')
        self.store = WorkStore(state_root / 'delivery.sqlite3', local_domain=config.local_domain,
                               replica_domain=config.replica_domain, replica=replica,
                               upgrade_schema=config.upgrade_schema)
        if config.replica_settings is not None:
            self.store.replica = config.replica_settings.create(state_root, failure_domain=config.replica_domain)
            replica = self.store.replica
        elif config.file_replica_settings is not None:
            self.store.replica = fnos_replica_factory(config.file_replica_settings, state_root,
                failure_domain=config.replica_domain)
            replica = self.store.replica
            if (not callable(getattr(replica, 'save', None)) or not callable(getattr(replica, 'get', None))
                    or not callable(getattr(replica, 'status', None))
                    or replica.status().get('policy') != config.file_replica_settings.policy):
                raise DeliveryError('replica_config_invalid')
        self.journal = InputJournal(self.store, capacity=config.journal_capacity)
        self.worker = MetadataWorker(self.journal)
        # Only a trusted launcher can inject qualified execution. options.json
        # cannot instantiate a backend or enable one by setting a flag.
        if (coordinator_factory is None) != (capabilities is None):
            raise DeliveryError('execution_not_qualified')
        if coordinator_factory is not None and replica is None:
            raise DeliveryError('execution_not_qualified')
        self.coordinator = coordinator_factory(self.store) if coordinator_factory else None
        if self.coordinator is not None and self.coordinator.store is not self.store:
            raise DeliveryError('execution_not_qualified')
        configured_capabilities = capabilities or (lambda: [])
        self.capabilities = lambda: (configured_capabilities() if self.config.worker_enabled
            and not self.store.recovery_mode and not self._stop.is_set() else [])
        self.provider = DeliveryProvider(self.journal, bearer=config.extension_bearer,
                                        origin_key=config.origin_key, allowed_actors=config.allowed_actors,
                                        capabilities=self.capabilities)
        self.server = DeliveryHTTPServer(address, self.provider)
        if release_authority_factory is not None:
            authority = release_authority_factory(self.store)
            from .release_authority import CanonicalReleaseAuthority
            reserved = [config.extension_bearer.encode(), config.origin_key]
            if config.execution_settings is not None:
                reserved.extend((config.execution_settings.runner_bearer.encode(),
                                 config.execution_settings.callback_bearer.encode()))
            if (type(authority) is not CanonicalReleaseAuthority or authority.store is not self.store
                    or any(hmac.compare_digest(principal.token.encode(), secret)
                           for principal in authority.principals for secret in reserved)):
                self.server.server_close()
                raise DeliveryError('release_authority_unavailable')
            self.server.release_authority = authority
        preview_transport = getattr(self.coordinator, 'preview_transport', None)
        if preview_transport is not None and config.worker_enabled:
            from .preview_rpc import PreviewRPC
            self.server.preview_rpc = PreviewRPC(preview_transport, config.allowed_actors)
        self.server.RequestHandlerClass = ServiceHandler
        self.server.service = self
        self._stop = threading.Event()
        self._guard = threading.Lock()
        self._error: str | None = None
        self._execution_error: str | None = None
        self._started = False
        self._http = threading.Thread(target=self.server.serve_forever,
                                      kwargs={'poll_interval': .1}, daemon=True, name='delivery-http')
        self._worker = threading.Thread(target=self._work, daemon=True, name='delivery-metadata')
        self._executor = threading.Thread(target=self._execute, daemon=True, name='delivery-execution')

    def health(self) -> dict[str, Any]:
        with self._guard:
            error = self._error or self._execution_error
        try:
            profiles = self.capabilities() if (self.coordinator and self.config.worker_enabled
                                                and not self.store.recovery_mode and not self._stop.is_set()) else []
            enabled = bool(profiles) and any(item.get('available') is True for item in profiles)
        except Exception:
            profiles, enabled, error = [], False, 'qualification_unavailable'
        result: dict[str, Any] = {
            'status': 'degraded' if error else 'ok', 'version': __version__,
            'service_mode': 'qualified_work' if self.coordinator else 'metadata_only', 'execution_enabled': enabled,
            'qualified_profiles': sorted({item['profile'] for item in profiles if item.get('available') is True}),
            'recovery_read_only': self.store.recovery_mode,
            'replica_configured': self.store.replica is not None,
            'worker_enabled': self.config.worker_enabled,
        }
        if self.config.replica_settings is not None or self.config.file_replica_settings is not None:
            try:
                replica_health = self.store.replica.status()
                result['replica_state'] = replica_health['state']
                result['replica_qualified'] = replica_health.get('qualified', False)
                if self.config.file_replica_settings is not None:
                    result['replica_policy'] = self.config.file_replica_settings.policy
                if replica_health['state'] not in {'ready', 'unqualified'}:
                    result['status'] = 'degraded'
            except Exception:
                result.update(status='degraded', replica_state='unavailable', replica_qualified=False)
        if error:
            result['error_code'] = error
        return result

    def start(self) -> None:
        if self._started or self._stop.is_set():
            raise DeliveryError('service_already_started')
        self._started = True
        self._http.start()
        if self.config.worker_enabled:
            self._worker.start()
            if self.coordinator:
                self._executor.start()

    def _work(self) -> None:
        while not self._stop.is_set():
            error = None
            try:
                if not self.store.recovery_mode:
                    # A lost receipt for a scheduler mutation is not tied to an
                    # input-journal row. Reconcile its exact frozen snapshot
                    # before accepting another metadata or execution action.
                    with closing(self.store._connect()) as connection:
                        pending = connection.execute("SELECT event_id FROM durability WHERE state='pending' ORDER BY rowid LIMIT 1").fetchone()
                    if pending:
                        self.store.reconcile(pending['event_id'])
                    self.worker.run_once()
            except DeliveryError as exc:
                error = exc.code if exc.code in {'worker_busy', 'journal_unavailable',
                                                 'worker_result_unknown', 'recovery_read_only'} else 'worker_unavailable'
            except Exception:
                error = 'worker_unavailable'
            with self._guard:
                self._error = error
            # A missing replica never causes a busy retry loop or acceptance.
            self._stop.wait(self.config.worker_interval_seconds)

    def _execute(self) -> None:
        while not self._stop.is_set():
            error = None
            try:
                if not self.store.recovery_mode and self.coordinator:
                    self.coordinator.run_once()
            except Exception:
                # Do not put runtime paths, program text or adapter exceptions
                # into health, model tools or normal logs.
                error = 'execution_reconciliation_required'
            with self._guard:
                self._execution_error = error
            self._stop.wait(self.config.worker_interval_seconds)

    def close(self, timeout: float = 5) -> None:
        self._stop.set()
        if self.coordinator:
            self.coordinator.request_stop()
        deadline = time.monotonic() + timeout
        if self._http.is_alive():
            self.server.shutdown()
            self._http.join(max(0, deadline - time.monotonic()))
        self.server.server_close()
        if self._worker.is_alive():
            self._worker.join(max(0, deadline - time.monotonic()))
        if self._executor.is_alive():
            self._executor.join(max(0, deadline - time.monotonic()))
        if self._http.is_alive() or self._worker.is_alive() or self._executor.is_alive():
            raise DeliveryError('service_shutdown_incomplete')
