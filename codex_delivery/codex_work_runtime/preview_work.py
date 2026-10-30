"""Preview sessions inside the unique WorkExecutor claim and worker lifetime.

No listener or task database. HTTP readers can observe an already running
session; only the persistent execution worker creates or stops its VM.
"""
from datetime import timedelta
import json
from pathlib import Path
import threading
import time

from .errors import WorkRuntimeError
from .preview_protocol import PreviewLease, decode
from .qemu_preview import LIMITS, STOP_RESERVE_SECONDS
from .qemu_work import _copy_checkpoint, _host_boundary
from .work_execution import MIB, SHANGHAI, _checkpoint_ref, _checkpoint_tree, _private_dir, _write_new

HOST_REPORT = '预览会话已停止，工作区检查点已验证。\n'.encode()


def fail(code, *, uncertain=False):
    return WorkRuntimeError(code, 'Preview execution rejected.', uncertain=uncertain)


def preview_intent(raw):
    try:
        if not isinstance(raw, str) or not 1 <= len(raw.encode()) <= 24000:
            raise ValueError()
        value = decode(raw)
        if (set(value) != {'version', 'interpreter', 'program'} or type(value['version']) is not int
                or value['version'] != 1 or value['interpreter'] not in {'python3', 'node'}
                or not isinstance(value['program'], str) or not 1 <= len(value['program'].encode()) <= 24000):
            raise ValueError()
        return value
    except (ValueError, TypeError, UnicodeError, WorkRuntimeError):
        raise fail('preview_work_intent_invalid') from None


class PreviewWorkBackend:
    def __init__(self, runtimes, previews, authority):
        if not runtimes or set(runtimes) != set(previews) or not callable(getattr(authority, 'open', None)):
            raise fail('preview_work_configuration_invalid')
        for key, runtime in runtimes.items():
            runtime.validate()
            if runtime.runtime_ref != key or runtime.kind != 'preview-qemu-v1':
                raise fail('preview_work_configuration_invalid')
        self.runtimes, self.previews, self.authority = dict(runtimes), dict(previews), authority
        self.policy = next(iter(previews.values())).policy
        if any(p.policy != self.policy for p in previews.values()):
            raise fail('preview_work_configuration_invalid')
        self.assets = {key:p.asset for key,p in previews.items()}
        self.reserve_bytes = max(p.reserve_bytes for p in previews.values())
        self._lock = threading.Lock()
        self._active = None

    def validate_request(self, request):
        runtime = self.runtimes.get(request.runtime_ref)
        if runtime is None or request.profile != 'preview' or request.profile not in runtime.profiles:
            raise fail('work_runtime_not_registered')
        if (request.limits.memory_bytes != 2048*MIB or request.limits.cpu_millis != 2000
                or request.limits.workspace_bytes != 2*MIB
                or request.limits.max_files != 129 or request.limits.timeout_seconds <= 100):
            raise fail('preview_work_budget_invalid')
        _host_boundary(self.policy)

    def _qualify(self, asset):
        for preview in self.previews.values():
            if preview.asset is asset:
                return preview._qualify(asset)
        raise fail('preview_work_configuration_invalid')

    def prepare_restore(self, request, *, source, destination, prior_execution):
        """Derive a guest-only view from an authenticated settled checkpoint.

        Host delivery output stays in the immutable original checkpoint. Only
        the exact report produced by a prior settled preview may be excluded.
        """
        tree = _checkpoint_tree(source, request.limits)
        report = source/'report.txt'
        if not report.exists():
            _checkpoint_tree(source, LIMITS)
            return source
        try:
            receipt = json.loads(prior_execution['receipt_json'])
            if (prior_execution['container_id'] != 'preview:'+receipt['execution_ref']
                    or receipt['workspace_ref'] != request.workspace_ref
                    or receipt['checkpoint_confirmed'] is not True
                    or receipt['checkpoint_ref'] != _checkpoint_ref(tree)
                    or receipt['files'] != tree['files'] or report.read_bytes() != HOST_REPORT):
                raise ValueError()
        except (KeyError, TypeError, ValueError, OSError):
            raise fail('preview_restore_host_output_invalid') from None
        _private_dir(destination)
        _copy_checkpoint(source, destination, request.limits)
        (destination/'report.txt').unlink()
        _checkpoint_tree(destination, LIMITS)
        return destination

    def run(self, request, *, claim_ref, program:Path, inputs:Path, restore:Path,
            destination:Path, created, cancelled, now):
        self.validate_request(request)
        raw = program.read_text()
        intent = preview_intent(raw)
        assignment = self.authority.open(request)
        if json.loads(assignment.original['work_json'])['program'] != raw:
            raise fail('preview_assignment_invalid')
        _checkpoint_tree(inputs, LIMITS)
        contents = {p.relative_to(inputs).as_posix():p.read_bytes() for p in inputs.rglob('*') if p.is_file()}
        started = now()
        expires = min(request.expires_at, started+timedelta(seconds=request.limits.timeout_seconds))
        lease = PreviewLease(request.owner_ref, request.task_ref, request.input_revision,
            assignment.original['input_revision'], request.lease_id, request.assignment_epoch, started, expires)
        preview = self.previews[request.runtime_ref]
        with preview.worker_lifetime() as lifetime:
            created('preview:'+claim_ref)
            session = preview.start(lease, intent['program'], worker_lifetime=lifetime,
                gate=assignment.admit, stop_gate=assignment.retire, inputs=contents,
                restore=restore, interpreter=intent['interpreter'], cancelled=cancelled, now=now)
            try:
                with self._lock:
                    if self._active is not None:
                        raise fail('preview_busy')
                    self._active = (session, assignment)
                # The creator remains alive throughout the lease. Per-request
                # HTTP threads never create QEMU or enter this execution loop.
                while not session._done.wait(.025):
                    if cancelled() or not assignment.admit(lease):
                        row = assignment.authority.store.work_task(lease.owner_ref, lease.task_ref)
                        reason = row.get('control_pending')
                        session.stop(reason=reason if reason in {'pause','cancel','revise'} else 'stopped')
                        break
                receipt = session.stop()
                checkpoint = session.checkpoint_directory()
                if (receipt.get('stopped') is not True or receipt.get('uncertain') is not False
                        or receipt.get('checkpoint_confirmed') is not True or checkpoint is None):
                    raise fail('preview_work_stop_unconfirmed', uncertain=True)
                def stop_authorized():
                    return (assignment.retire(lease) is True and now()<lease.expires_at
                        and time.monotonic()<session.deadline)
                if not stop_authorized():
                    raise fail('preview_work_stop_authority_lost', uncertain=True)
                _private_dir(destination)
                _copy_checkpoint(checkpoint, destination, LIMITS)
                if _checkpoint_ref(_checkpoint_tree(destination,LIMITS))!=receipt.get('checkpoint_ref'):
                    raise fail('preview_checkpoint_changed', uncertain=True)
                # A reserved host summary cannot be supplied by task code.
                report = destination/'report.txt'
                if report.exists() or report.is_symlink():
                    raise fail('preview_work_reserved_output')
                _write_new(report, HOST_REPORT)
                tree = _checkpoint_tree(destination, request.limits)
                if not stop_authorized():
                    raise fail('preview_work_stop_authority_lost', uncertain=True)
                reason = 'cancelled' if cancelled() or receipt['stop_reason'] in {'pause','cancel','revise'} else 'finished'
                return {'checkpoint_written':True, 'files':tree['files'], 'stop_reason':reason, 'exit_code':0}
            finally:
                with self._lock:
                    if self._active is not None and self._active[0] is session:
                        self._active = None
                if not session._done.is_set():
                    session.stop()

    def canonical_scope(self, owner_ref, task_ref, *, deadline):
        if time.monotonic() >= deadline:
            raise fail('preview_deadline')
        with self._lock:
            active = self._active
        if active is None:
            raise fail('preview_not_ready')
        session, assignment = active
        lease = session.lease
        if (lease.owner_ref != owner_ref or lease.task_ref != task_ref or not session.ready
                or session.closing or not assignment.admit(lease) or time.monotonic() >= deadline):
            raise fail('preview_not_ready')
        return {'version':1, 'state':'ready', 'owner_ref':lease.owner_ref, 'task_ref':lease.task_ref,
            'task_revision':lease.task_revision, 'input_revision':lease.input_revision,
            'lease_ref':lease.lease_ref, 'lease_epoch':lease.lease_epoch, 'preview_nonce':session.nonce,
            'started_at':lease.document()['started_at'],
            'expires_at':(lease.expires_at-timedelta(seconds=STOP_RESERVE_SECONDS)).astimezone(SHANGHAI).isoformat(),
            'lease_expires_at':assignment.original['lease_expires_at']}

    def _operate(self, scope, value, deadline):
        if not isinstance(scope, dict):
            raise fail('preview_scope_invalid')
        if self.canonical_scope(scope.get('owner_ref'), scope.get('task_ref'), deadline=deadline) != scope:
            raise fail('preview_scope_changed')
        with self._lock:
            active = self._active
        if active is None:
            raise fail('preview_not_ready')
        session = active[0]
        result = session.frame(deadline=deadline) if value is None else session.action(value, deadline=deadline)
        if self.canonical_scope(scope['owner_ref'], scope['task_ref'], deadline=deadline) != scope:
            raise fail('preview_scope_changed')
        return result

    def frame(self, scope, *, deadline):
        return self._operate(scope, None, deadline)

    def action(self, scope, event, *, deadline):
        return self._operate(scope, event, deadline)
