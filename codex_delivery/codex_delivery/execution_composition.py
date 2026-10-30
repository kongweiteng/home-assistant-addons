"""Trusted assembly of the fixed runtime, canonical leases and original-chat I/O.

This module exposes no tool or listener and enrolls/enables no node. The service
entry point must supply an image-owned ExecutionPolicy and private settings.
"""
from __future__ import annotations

from contextlib import closing
from dataclasses import dataclass, field
from datetime import datetime, timedelta
import hmac
import fcntl
import os
from pathlib import Path
import secrets
import stat
from typing import Any

if __package__ == 'codex_delivery':
    from codex_work_runtime.qemu_work import QemuWorkBackend
    from codex_work_runtime.work_adapter import WorkExecutorAdapter
    from codex_work_runtime.work_execution import WorkExecutor, WorkRequest
    from codex_work_runtime.qemu_browser import QemuPublicBrowser
    from codex_work_runtime.browser_work import BrowserWorkBackend
    from codex_work_runtime.backend_router import WorkBackendRouter
    from codex_work_runtime.qemu_preview import QemuPreviewBackend
    from codex_work_runtime.preview_work import PreviewWorkBackend
else:
    from ..codex_work_runtime.qemu_work import QemuWorkBackend
    from ..codex_work_runtime.work_adapter import WorkExecutorAdapter
    from ..codex_work_runtime.work_execution import WorkExecutor, WorkRequest
    from ..codex_work_runtime.qemu_browser import QemuPublicBrowser
    from ..codex_work_runtime.browser_work import BrowserWorkBackend
    from ..codex_work_runtime.backend_router import WorkBackendRouter
    from ..codex_work_runtime.qemu_preview import QemuPreviewBackend
    from ..codex_work_runtime.preview_work import PreviewWorkBackend

from .callback import ControllerCallback
from .coordinator import WorkCoordinator
from .execution_policy import ASSET_ROOT, DIGEST, ExecutionPolicy, digest
from .object_archive import ObjectArchive
from .source_archive import WorkSourceArchive
from .repository_sources import RepositorySources
from .preview_authority import CanonicalPreviewAuthority
from .store import DeliveryError, SHANGHAI
from .work_runner_identity import CachedWorkRunnerIdentity, RUNNER, WorkRunnerClient


@dataclass(frozen=True)
class ExecutionSettings:
    controller_url: str
    runner_ref: str
    runner_bearer: str = field(repr=False)
    callback_bearer: str = field(repr=False)
    policy_revision: int
    policy_digest: str

    @classmethod
    def from_dict(cls, value: Any, *, reserved_secrets: tuple[str, ...]) -> 'ExecutionSettings':
        try:
            if (not isinstance(value, dict) or set(value) != {'controller_url', 'runner_ref',
                    'runner_bearer', 'callback_bearer', 'policy_revision', 'policy_digest'}
                    or not isinstance(value['runner_ref'], str) or not RUNNER.fullmatch(value['runner_ref'])
                    or type(value['policy_revision']) is not int or not 1 <= value['policy_revision'] <= 2**53-1
                    or not isinstance(value['policy_digest'], str) or not DIGEST.fullmatch(value['policy_digest'])):
                raise ValueError()
            credentials = (value['runner_bearer'], value['callback_bearer'])
            if any(not isinstance(secret, str) or not 32 <= len(secret) <= 512
                   or any(not 33 <= ord(c) <= 126 for c in secret) for secret in credentials):
                raise ValueError()
            if any(hmac.compare_digest(a.encode(), b.encode()) for i, a in enumerate(credentials)
                   for b in (*credentials[i+1:], *reserved_secrets)):
                raise ValueError()
            # Both transports validate the same fixed origin; this constructs
            # no connections and cannot make a credential request.
            WorkRunnerClient(value['controller_url'], value['runner_ref'], value['runner_bearer'], timeout_seconds=.75)
            ControllerCallback(value['controller_url'], value['callback_bearer'])
            return cls(**value)
        except Exception:
            raise DeliveryError('execution_settings_invalid') from None


class CanonicalBrowserTaskReader:
    """Resolve the original task from Delivery, never from browser intent JSON."""
    def __init__(self, store, runner_ref):
        self.store, self.runner_ref = store, runner_ref

    def __call__(self, request):
        task = self.store.work_task(request.owner_ref, request.task_ref)
        if (task['assigned_runner_ref'] != self.runner_ref or task['revision'] != request.input_revision
                or task['workspace_ref'] != request.workspace_ref
                or not self.store.lease_current(request.owner_ref, request.task_ref,
                    request.lease_id, request.assignment_epoch, runner_ref=self.runner_ref)):
            raise DeliveryError('execution_not_qualified')
        return task


class CanonicalAssignmentReader:
    """Read Delivery only. A Controller busy pointer is never a lease issuer."""
    def __init__(self, store, runner_ref):
        self.store, self.runner_ref = store, runner_ref

    def __call__(self):
        if self.store.recovery_mode:
            raise DeliveryError('execution_not_qualified')
        with closing(self.store._connect()) as connection:
            connection.execute('PRAGMA busy_timeout=50')
            connection.execute('BEGIN')
            if connection.execute("SELECT 1 FROM durability WHERE state='pending' LIMIT 1").fetchone():
                raise DeliveryError('durability_pending')
            rows = connection.execute("SELECT * FROM tasks WHERE mode='work' AND state='running' AND phase='execution' AND assigned_runner_ref=? LIMIT 2",
                                      (self.runner_ref,)).fetchall()
            if not rows:
                return None
            if len(rows) != 1:
                raise DeliveryError('execution_not_qualified')
            row = dict(rows[0])
            project_ok = (not row['project_id'] or connection.execute(
                "SELECT 1 FROM projects WHERE project_id=? AND owner_ref=? AND state='active'",
                (row['project_id'], row['owner_ref'])).fetchone() is not None)
        live = (project_ok and row['control_pending'] is None
                and datetime.fromisoformat(row['lease_expires_at']) > datetime.now(SHANGHAI))
        return {'runner_ref': self.runner_ref, 'task_ref': row['task_id'],
                'lease_ref': row['lease_ref'], 'lease_epoch': row['lease_epoch'], 'live': live}


class PresenceBoundAdapter:
    """Presence failure stops admission without discarding proven checkpoint bytes."""
    def __init__(self, adapter, presence):
        self.adapter, self.presence = adapter, presence

    def qualifies(self, profile, runtime_id):
        return self.presence.qualified and self.adapter.qualifies(profile, runtime_id)

    def execute(self, task, claim, spec, inputs):
        self.presence.begin(task['task_id'])
        try:
            receipt = self.adapter.execute(task, claim, spec, inputs)
        except Exception:
            self.presence.uncertain(task['task_id'])
            raise
        try:
            self.presence.settled(task['task_id'], receipt)
        except Exception:
            # The executor has stopped and fsynced a receipt. Preserve that
            # evidence even when the independent Registry report is unknown.
            self.presence.uncertain(task['task_id'])
        return receipt

    def cancel(self, task, claim):
        return self.adapter.cancel(task, claim)

    def checkpoint(self, task, receipt):
        return self.adapter.checkpoint(task, receipt)


class QualifiedExecution:
    """Callable coordinator factory and bounded capabilities for DeliveryService."""
    def __init__(self, policy: ExecutionPolicy, settings: ExecutionSettings, state_root: Path, *,
                 repository_factory=None):
        if digest(policy.registry_policy()) != settings.policy_digest:
            raise DeliveryError('execution_policy_mismatch')
        if repository_factory is not None and not callable(repository_factory):
            raise DeliveryError('repository_sources_invalid')
        self.policy, self.settings = policy, settings
        self.repository_factory=repository_factory
        self.repository_sources=None
        self.source_archive=None
        self.state_root = state_root.absolute()
        self.store = self.adapter = self.executor = self.backend = self.presence = self.identity = None
        self.preview = None
        self._qualified = False
        self._service_lock = None

    def __call__(self, store):
        if self.store is not None or store.path.parent != self.state_root or store.replica is None:
            raise DeliveryError('execution_not_qualified')
        self.store = store
        try:
            fd = os.open(self.state_root / 'execution-service.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
            try:
                info = os.fstat(fd)
                if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                        or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o600):
                    raise DeliveryError('execution_not_qualified')
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except Exception:
                os.close(fd)
                raise DeliveryError('execution_not_qualified') from None
            self._service_lock = fd
            callback = ControllerCallback(self.settings.controller_url, self.settings.callback_bearer)
            offline = {r.runtime_ref: r for r in self.policy.runtimes if r.kind == 'work-qemu-v1'}
            browsing = {r.runtime_ref: r for r in self.policy.runtimes if r.kind == 'browser-qemu-v1'}
            previews = {r.runtime_ref: r for r in self.policy.runtimes if r.kind == 'preview-qemu-v1'}
            if set(offline) | set(browsing) | set(previews) != {r.runtime_ref for r in self.policy.runtimes}:
                raise DeliveryError('execution_not_qualified')
            backends = []
            if offline:
                backends.append(QemuWorkBackend(offline, {key: ASSET_ROOT / key for key in offline},
                    self.state_root / 'qemu', policy=self.policy.host))
            if browsing:
                browsers = {key: QemuPublicBrowser(ASSET_ROOT / key, runtime.image,
                    self.state_root / ('browser-' + key), policy=self.policy.host)
                    for key, runtime in browsing.items()}
                backends.append(BrowserWorkBackend(browsing, browsers,
                    CanonicalBrowserTaskReader(store, self.settings.runner_ref), callback.resolve_browser_grant))
            if previews:
                preview_vms = {key: QemuPreviewBackend(ASSET_ROOT / key, runtime.image,
                    self.state_root / ('preview-' + key), policy=self.policy.host)
                    for key, runtime in previews.items()}
                authority = CanonicalPreviewAuthority(store, self.settings.runner_ref,
                    self._identity, self._retiring_identity)
                self.preview = PreviewWorkBackend(previews, preview_vms, authority)
                backends.append(self.preview)
            self.backend = backends[0] if len(backends) == 1 else WorkBackendRouter(backends)
            self.policy.verify_backend(self.backend)
            # No model program is run for qualification. Native tests and the
            # actual host budget gate must pass before self_check_ok is sent.
            for runtime in self.policy.runtimes:
                for profile in runtime.profiles:
                    request = WorkRequest('qualification', 'qualification', 'qualification', 1,
                        profile, runtime.runtime_ref, 'qualification', 1,
                        datetime.now(SHANGHAI) + timedelta(seconds=60), dict(self.policy.limits)[profile])
                    self.backend.validate_request(request)
                self.backend._qualify(self.backend.assets[runtime.runtime_ref])
            self.executor = WorkExecutor(self.state_root / 'execution', self.backend)
            unsettled = self.executor.recover()
            with closing(store._connect()) as connection:
                unknown = connection.execute("SELECT 1 FROM tasks WHERE assigned_runner_ref=? AND (state='recovery_required' OR (state='running' AND phase='execution')) LIMIT 1",
                                             (self.settings.runner_ref,)).fetchone() is not None
            client = WorkRunnerClient(self.settings.controller_url, self.settings.runner_ref,
                                      self.settings.runner_bearer, timeout_seconds=.75)
            # Imported only by the trusted assembly, never by options/model code.
            from .work_runner_presence import PresenceMonitor
            self.presence = PresenceMonitor(client, self.state_root / 'runner-presence.json',
                runner_ref=self.settings.runner_ref, boot_ref='BOOT-' + secrets.token_hex(16),
                policy_revision=self.settings.policy_revision, policy_digest=self.settings.policy_digest,
                qualification_digest=self.policy.registry_policy()['qualification_digest'],
                receipt_gate=self._stopped_receipt)
            epoch = self.presence.start(recovery_mode=bool(store.recovery_mode or unsettled or unknown
                                                          or self.presence.frozen))
            self.identity = CachedWorkRunnerIdentity(self.presence.identity,
                runner_ref=self.settings.runner_ref, session_epoch=epoch,
                policy_digest=self.settings.policy_digest,
                assignment_gate=CanonicalAssignmentReader(store, self.settings.runner_ref),
                refresh_seconds=.5, max_age_seconds=2.5)
            self.identity.start()
            raw_adapter = WorkExecutorAdapter(self.executor, runner_ref=self.settings.runner_ref,
                runtimes={r.runtime_ref: r for r in self.policy.runtimes}, limits=dict(self.policy.limits),
                identity_gate=self._identity,
                lease_gate=lambda owner, task, lease, epoch: store.lease_current(owner, task, lease, epoch,
                                                                               runner_ref=self.settings.runner_ref))
            self.adapter = PresenceBoundAdapter(raw_adapter, self.presence)
            archive = ObjectArchive(store.replica, local_domain=store.local_domain, replica_domain=store.replica_domain)
            if self.repository_factory is not None:
                self.repository_sources=self.repository_factory(store,archive,self.state_root)
                if (not isinstance(self.repository_sources,RepositorySources)
                        or self.repository_sources.store is not store
                        or self.repository_sources.archive is not archive):
                    raise DeliveryError('repository_sources_invalid')
            self.source_archive=WorkSourceArchive(store, archive,repository_sources=self.repository_sources)
            coordinator = WorkCoordinator(store, runner_ref=self.settings.runner_ref,
                adapter=self.adapter, callback=callback, input_resolver=callback.resolve_inputs,
                checkpoint_archive=archive, source_archive=self.source_archive,
                preview_transport=self.preview)
            self._qualified = True
            return coordinator
        except Exception:
            self.close()
            raise

    def _identity(self, runner_ref):
        if self.presence is None or self.presence.frozen or self.identity is None:
            raise DeliveryError('work_runner_identity_unavailable')
        return self.identity(runner_ref)

    def _retiring_identity(self, runner_ref, task_ref):
        if self.presence is None or self.presence.frozen or self.identity is None:
            raise DeliveryError('work_runner_identity_unavailable')
        return self.identity.retiring(runner_ref, task_ref)

    def _stopped_receipt(self, task_ref, receipt):
        with closing(self.store._connect()) as connection:
            row = connection.execute("SELECT * FROM tasks WHERE task_id=? AND mode='work' AND assigned_runner_ref=?",
                                     (task_ref, self.settings.runner_ref)).fetchone()
        if (row is None or not isinstance(receipt, dict) or receipt.get('assignment_epoch') != row['lease_epoch']
                or receipt.get('evidence') not in {'exited', 'terminated'}):
            raise DeliveryError('execution_receipt_invalid')
        self.adapter.checkpoint(dict(row), receipt)
        return {'stop_ref': receipt['execution_ref'], 'checkpoint_ref': receipt['checkpoint_ref']}

    def capabilities(self):
        available = False
        if self._qualified and not self.store.recovery_mode:
            try:
                status = self.store.replica.status()
                available = (status['state'] == 'ready' and status.get('qualified') is True
                             and status.get('worker_active') is False)
            except Exception:
                pass
        return [{'profile': profile, 'runtime_id': runtime.runtime_ref,
                 'available': bool(available and self.adapter.qualifies(profile, runtime.runtime_ref))}
                for runtime in self.policy.runtimes for profile in runtime.profiles]

    def close(self):
        self._qualified = False
        try:
            if self.presence is not None:
                self.presence.close()
        finally:
            if self.identity is not None:
                self.identity.close()
            active = (self.adapter is not None and bool(self.adapter.adapter._active))
            if not active and self._service_lock is not None:
                os.close(self._service_lock)
                self._service_lock = None
